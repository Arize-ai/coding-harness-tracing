#!/usr/bin/env python3
"""Claude Code hook handlers. One exported function per hook event.

Replaces 9 bash scripts in tracing/claude_code/hooks/. Each function is a CLI
entry point registered in pyproject.toml [project.scripts].
"""

import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path

from core.common import (
    build_span,
    env,
    error,
    generate_span_id,
    generate_trace_id,
    get_timestamp_ms,
    log,
    redact_content,
    send_span,
)
from core.event_model import AgentEvent, EventStatus, GraphDiagnostic, ModelCallEvent, ToolEvent, TurnEvent

from .adapter import (
    SCOPE_NAME,
    SERVICE_NAME,
    check_requirements,
    ensure_session_initialized,
    gc_stale_state_files,
    resolve_session,
    resolve_transcript_path,
)
from .span_renderer import render_event_graph
from .tool_buffer import ToolBuffer, ToolObservation
from .transcript import parse_claude_transcript

# ---------------------------------------------------------------------------
# Shared helper
# ---------------------------------------------------------------------------


def _read_stdin() -> dict:
    """Read JSON from stdin. Returns {} on empty/invalid input."""
    try:
        raw = sys.stdin.read()
        return json.loads(raw) if raw else {}
    except (json.JSONDecodeError, OSError):
        return {}


# ---------------------------------------------------------------------------
# Internal handler implementations
# ---------------------------------------------------------------------------


def _has_live_transcript(input_json: dict) -> bool:
    """Return whether this hook can participate in transcript correlation."""
    transcript_path = input_json.get("transcript_path")
    if not isinstance(transcript_path, str) or not transcript_path:
        return False
    try:
        return Path(transcript_path).is_file()
    except OSError:
        return False


def _handle_session_start(input_json: dict) -> None:
    """Handle session_start: initialize session."""
    state = resolve_session(input_json)
    ensure_session_initialized(state, input_json)
    log(f"Session started: {state.get('session_id')}")


def _handle_pre_tool_use(input_json: dict) -> None:
    """Handle pre_tool_use: durably record the tool start observation."""
    state = resolve_session(input_json)
    tool_id = input_json.get("tool_use_id") or generate_trace_id()
    started_at_ms = get_timestamp_ms()
    state.set(f"tool_{tool_id}_start", str(started_at_ms))
    if _has_live_transcript(input_json):
        ToolBuffer(state).record_start(
            tool_id,
            tool_name=input_json.get("tool_name"),
            tool_input=input_json.get("tool_input"),
            started_at_ms=started_at_ms,
            hook_event_metadata={"hook_event_name": input_json.get("hook_event_name", "PreToolUse")},
        )


def _handle_post_tool_use(input_json: dict) -> None:
    """Handle post_tool_use: buffer for correlation or use the legacy immediate fallback."""
    state = resolve_session(input_json)
    session_id = state.get("session_id")
    if session_id is None:
        return

    if _has_live_transcript(input_json):
        tool_id = input_json.get("tool_use_id") or generate_trace_id()
        state.increment("tool_count")
        buffer = ToolBuffer(state)
        if buffer.get(tool_id) is None:
            stored_start = state.get(f"tool_{tool_id}_start")
            buffer.record_start(
                tool_id,
                tool_name=input_json.get("tool_name"),
                tool_input=input_json.get("tool_input"),
                started_at_ms=int(stored_start) if stored_start and stored_start.isdigit() else None,
            )
        buffer.record_result(
            tool_id,
            status="success",
            tool_response=input_json.get("tool_response"),
            ended_at_ms=get_timestamp_ms(),
            hook_event_metadata={"hook_event_name": input_json.get("hook_event_name", "PostToolUse")},
        )
        state.delete(f"tool_{tool_id}_start")
        return

    trace_id = state.get("current_trace_id")
    parent_span_id = state.get("current_trace_span_id")
    state.increment("tool_count")

    # Extract tool info
    tool_name = input_json.get("tool_name", "unknown")
    tool_id = input_json.get("tool_use_id", "")
    tool_input = json.dumps(input_json.get("tool_input", {}))
    tool_response = str(input_json.get("tool_response", ""))

    # Tool-specific metadata
    tool_command = ""
    tool_file_path = ""
    tool_url = ""
    tool_query = ""
    tool_description = ""

    if tool_name == "Bash":
        tool_command = input_json.get("tool_input", {}).get("command", "")
        tool_description = tool_command[:200]
    elif tool_name in ("Read", "Write", "Edit", "Glob"):
        tool_file_path = input_json.get("tool_input", {}).get("file_path") or input_json.get("tool_input", {}).get(
            "pattern", ""
        )
        tool_description = tool_file_path[:200]
    elif tool_name == "WebSearch":
        tool_query = input_json.get("tool_input", {}).get("query", "")
        tool_description = tool_query[:200]
    elif tool_name == "WebFetch":
        tool_url = input_json.get("tool_input", {}).get("url", "")
        tool_description = tool_url[:200]
    elif tool_name == "Grep":
        tool_query = input_json.get("tool_input", {}).get("pattern", "")
        tool_file_path = input_json.get("tool_input", {}).get("path", "")
        tool_description = f"grep: {tool_query[:100]}"
    else:
        tool_description = tool_input[:200]

    # Timing
    start_time = state.get(f"tool_{tool_id}_start") or str(get_timestamp_ms())
    end_time = str(get_timestamp_ms())
    state.delete(f"tool_{tool_id}_start")

    # Redaction: tool input/output may contain raw file contents or command output;
    # tool_command/file_path/url/query/description describe what was requested.
    tool_input = redact_content(env.log_tool_content, tool_input)
    tool_response = redact_content(env.log_tool_content, tool_response)
    tool_description = redact_content(env.log_tool_details, tool_description)
    if tool_command:
        tool_command = redact_content(env.log_tool_details, tool_command)
    if tool_file_path:
        tool_file_path = redact_content(env.log_tool_details, tool_file_path)
    if tool_url:
        tool_url = redact_content(env.log_tool_details, tool_url)
    if tool_query:
        tool_query = redact_content(env.log_tool_details, tool_query)

    # Build attributes
    user_id = state.get("user_id") or ""
    attrs = {
        "session.id": session_id,
        "openinference.span.kind": "TOOL",
        "tool.name": tool_name,
        "input.value": tool_input,
        "output.value": tool_response,
        "tool.description": tool_description,
    }
    if user_id:
        attrs["user.id"] = user_id
    if tool_command:
        attrs["tool.command"] = tool_command
    if tool_file_path:
        attrs["tool.file_path"] = tool_file_path
    if tool_url:
        attrs["tool.url"] = tool_url
    if tool_query:
        attrs["tool.query"] = tool_query

    span = build_span(
        tool_name,
        "TOOL",
        generate_span_id(),
        trace_id or "",
        parent_span_id or "",
        start_time,
        end_time,
        attrs,
        SERVICE_NAME,
        SCOPE_NAME,
    )
    send_span(span)


def _handle_post_tool_use_failure(input_json: dict) -> None:
    """Handle failure: buffer for correlation or use the legacy immediate fallback."""
    state = resolve_session(input_json)
    session_id = state.get("session_id")
    if session_id is None:
        return

    if _has_live_transcript(input_json):
        tool_id = input_json.get("tool_use_id") or generate_trace_id()
        state.increment("tool_count")
        buffer = ToolBuffer(state)
        if buffer.get(tool_id) is None:
            stored_start = state.get(f"tool_{tool_id}_start")
            buffer.record_start(
                tool_id,
                tool_name=input_json.get("tool_name"),
                tool_input=input_json.get("tool_input"),
                started_at_ms=int(stored_start) if stored_start and stored_start.isdigit() else None,
            )
        error_text = input_json.get("error", "")
        buffer.record_result(
            tool_id,
            status="error",
            tool_response=input_json.get("tool_response"),
            error=error_text,
            ended_at_ms=get_timestamp_ms(),
            hook_event_metadata={"hook_event_name": input_json.get("hook_event_name", "PostToolUseFailure")},
        )
        state.delete(f"tool_{tool_id}_start")
        return

    trace_id = state.get("current_trace_id")
    parent_span_id = state.get("current_trace_span_id")
    state.increment("tool_count")

    # Extract tool info
    tool_name = input_json.get("tool_name", "unknown")
    tool_id = input_json.get("tool_use_id") or generate_trace_id()
    tool_input = json.dumps(input_json.get("tool_input", {}))
    tool_response = str(input_json.get("tool_response", ""))
    error_text = input_json.get("error", "")

    # Tool-specific metadata
    tool_command = ""
    tool_file_path = ""
    tool_url = ""
    tool_query = ""
    tool_description = ""

    if tool_name == "Bash":
        tool_command = input_json.get("tool_input", {}).get("command", "")
        tool_description = tool_command[:200]
    elif tool_name in ("Read", "Write", "Edit", "Glob"):
        tool_file_path = input_json.get("tool_input", {}).get("file_path") or input_json.get("tool_input", {}).get(
            "pattern", ""
        )
        tool_description = tool_file_path[:200]
    elif tool_name == "WebSearch":
        tool_query = input_json.get("tool_input", {}).get("query", "")
        tool_description = tool_query[:200]
    elif tool_name == "WebFetch":
        tool_url = input_json.get("tool_input", {}).get("url", "")
        tool_description = tool_url[:200]
    elif tool_name == "Grep":
        tool_query = input_json.get("tool_input", {}).get("pattern", "")
        tool_file_path = input_json.get("tool_input", {}).get("path", "")
        tool_description = f"grep: {tool_query[:100]}"
    else:
        tool_description = tool_input[:200]

    # Timing
    start_time = state.get(f"tool_{tool_id}_start") or str(get_timestamp_ms())
    end_time = str(get_timestamp_ms())
    state.delete(f"tool_{tool_id}_start")

    # Use error as output when tool_response is empty
    output_value = tool_response if tool_response else error_text

    # Redaction
    tool_input = redact_content(env.log_tool_content, tool_input)
    output_value = redact_content(env.log_tool_content, output_value)
    redacted_error = redact_content(env.log_tool_content, error_text)
    tool_description = redact_content(env.log_tool_details, tool_description)
    if tool_command:
        tool_command = redact_content(env.log_tool_details, tool_command)
    if tool_file_path:
        tool_file_path = redact_content(env.log_tool_details, tool_file_path)
    if tool_url:
        tool_url = redact_content(env.log_tool_details, tool_url)
    if tool_query:
        tool_query = redact_content(env.log_tool_details, tool_query)

    # Build attributes
    user_id = state.get("user_id") or ""
    attrs = {
        "session.id": session_id,
        "openinference.span.kind": "TOOL",
        "tool.name": tool_name,
        "input.value": tool_input,
        "output.value": output_value,
        "tool.description": tool_description,
        "error.type": "tool_failure",
        "error.message": redacted_error,
    }
    if user_id:
        attrs["user.id"] = user_id
    if tool_command:
        attrs["tool.command"] = tool_command
    if tool_file_path:
        attrs["tool.file_path"] = tool_file_path
    if tool_url:
        attrs["tool.url"] = tool_url
    if tool_query:
        attrs["tool.query"] = tool_query

    span = build_span(
        f"{tool_name} (failed)",
        "TOOL",
        generate_span_id(),
        trace_id or "",
        parent_span_id or "",
        start_time,
        end_time,
        attrs,
        SERVICE_NAME,
        SCOPE_NAME,
    )
    send_span(span)


def _handle_user_prompt_expansion(input_json: dict) -> None:
    """Handle UserPromptExpansion: stash command metadata for the next Turn span to attach."""
    state = resolve_session(input_json)
    expansion_type = input_json.get("expansion_type", "")
    command_name = input_json.get("command_name", "")
    command_args = input_json.get("command_args", "")
    command_source = input_json.get("command_source", "")
    if expansion_type:
        state.set("pending_expansion_type", expansion_type)
    if command_name:
        state.set("pending_command_name", command_name)
    if command_args:
        state.set("pending_command_args", command_args)
    if command_source:
        state.set("pending_command_source", command_source)


def _handle_user_prompt_submit(input_json: dict) -> None:
    """Handle user_prompt_submit: set up a new trace (close orphaned turn first)."""
    state = resolve_session(input_json)
    ensure_session_initialized(state, input_json)

    # Retry a retained turn before starting a new one.  A failed export keeps the
    # old state intact so observations and stable span IDs can be retried later.
    prev_trace_id = state.get("current_trace_id")
    if prev_trace_id:
        retry_input = dict(input_json)
        retry_input.setdefault("last_assistant_message", "(Turn closed by fail-safe: Stop hook did not fire)")
        _handle_stop(retry_input)
        current_trace_id = state.get("current_trace_id")
        if current_trace_id == prev_trace_id:
            log("Fail-safe: retained orphaned turn after failed export")
            return
        if current_trace_id is not None:
            log("Fail-safe: a concurrent newer turn owns the session state")
            return
        log("Fail-safe: exported and closed orphaned turn")

    # Set up new trace
    state.increment("trace_count")
    state.set("current_trace_id", generate_trace_id())
    state.set("current_trace_span_id", generate_span_id())
    state.set("current_trace_start_time", str(get_timestamp_ms()))
    prompt = input_json.get("prompt", "") or ""
    # Store RAW prompt in state; redact only at span build time so the redaction
    # toggle is read once-per-emit instead of being baked into the state file.
    state.set("current_trace_prompt", prompt)

    # Track transcript position
    transcript = input_json.get("transcript_path", "")
    if transcript and Path(transcript).is_file():
        with open(transcript) as f:
            line_count = sum(1 for _ in f)
        state.set("trace_start_line", str(line_count))
    else:
        state.set("trace_start_line", "0")


def _usage_int(usage: dict, key: str) -> int:
    """Read an int token count from a usage dict, treating non-ints as 0."""
    val = usage.get(key, 0)
    return val if isinstance(val, int) else 0


@dataclass
class _TokenUsage:
    """Token counts parsed from a transcript.

    ``prompt`` is the OpenInference prompt total: it includes *all* input
    subtypes (uncached input + cache reads + cache writes), per the Arize/
    Phoenix cost model where the base "input" portion is derived as
    ``prompt - cache_read - cache_write``. ``cache_read`` and ``cache_write``
    are therefore subsets of ``prompt``, surfaced separately so the cost
    engine can price them at their own (much cheaper) rates instead of the
    full input rate. Without the breakdown, prompt-cache tokens are billed as
    full-price input, over-reporting cost ~3-4x for heavily-cached agent runs.
    """

    prompt: int = 0
    completion: int = 0
    cache_read: int = 0
    cache_write: int = 0

    def token_count_attrs(self) -> dict:
        """Return OpenInference token-count attributes for span emission.

        Cache detail attributes are only included when non-zero to avoid
        cluttering spans for uncached calls.
        """
        attrs: dict = {
            "llm.token_count.prompt": self.prompt,
            "llm.token_count.completion": self.completion,
            "llm.token_count.total": self.prompt + self.completion,
        }
        if self.cache_read:
            attrs["llm.token_count.prompt_details.cache_read"] = self.cache_read
        if self.cache_write:
            attrs["llm.token_count.prompt_details.cache_write"] = self.cache_write
        return attrs


def _scan_transcript_for_usage(
    transcript: Path,
    start_line: int,
) -> "tuple[str, _TokenUsage, str]":
    """Walk the transcript JSONL from *start_line* forward and return:
    (combined_text, usage, model_name)
    """
    output = ""
    usage_totals = _TokenUsage()
    model = ""

    # A streamed API response is written to the transcript as one assistant
    # record per content block, and every record of that response repeats the
    # same usage snapshot (same requestId / message.id). Summing each record
    # would bill one API call several times (2-3x on real transcripts), so
    # usage is collected per request and the last record wins — on older
    # harness versions the earlier snapshots are partial and the final one is
    # authoritative. Records with no identity at all are counted individually.
    last_usage: "dict[object, dict]" = {}

    with open(transcript) as f:
        for i, line in enumerate(f):
            if i < start_line:
                continue
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            msg = entry.get("message")
            if not isinstance(msg, dict) or msg.get("role") != "assistant":
                continue

            content = msg.get("content")
            if isinstance(content, list):
                text = "\n".join(
                    item.get("text", "") for item in content if isinstance(item, dict) and item.get("type") == "text"
                )
            elif isinstance(content, str):
                text = content
            else:
                text = ""
            if text:
                output = f"{output}\n{text}" if output else text

            model = msg.get("model", "") or model

            usage = msg.get("usage")
            if isinstance(usage, dict) and usage:
                key = entry.get("requestId") or msg.get("id") or object()
                last_usage[key] = usage

    for usage in last_usage.values():
        # Anthropic reports input_tokens (uncached), cache_read_input_tokens,
        # and cache_creation_input_tokens as disjoint buckets. The prompt
        # total is their sum (the OpenInference total), while the two cache
        # buckets are tracked separately and surfaced as prompt_details so
        # downstream cost pricing can apply the cheaper cache-read /
        # cache-write rates instead of the full input rate.
        uncached = _usage_int(usage, "input_tokens")
        cache_read = _usage_int(usage, "cache_read_input_tokens")
        cache_write = _usage_int(usage, "cache_creation_input_tokens")
        usage_totals.prompt += uncached + cache_read + cache_write
        usage_totals.cache_read += cache_read
        usage_totals.cache_write += cache_write
        usage_totals.completion += _usage_int(usage, "output_tokens")

    return output, usage_totals, model


def _has_stable_model_ids(graph) -> bool:
    """Gate high-fidelity rendering on Claude v2 assistant UUIDs."""
    models = [event for event in graph.events if isinstance(event, ModelCallEvent)]
    return bool(models) and all(not event.event_id.startswith("assistant-line-") for event in models)


def _merge_tool_observations(graph, observations) -> list:
    """Overlay each hook observation onto the first correlated tool event."""
    by_call_id = {observation.tool_use_id: observation for observation in observations}
    matched = []
    seen_call_ids: set[str] = set()
    for event in graph.events:
        if not isinstance(event, ToolEvent) or not event.tool_call_id or event.tool_call_id in seen_call_ids:
            continue
        seen_call_ids.add(event.tool_call_id)
        observation = by_call_id.get(event.tool_call_id)
        if observation is None:
            continue
        matched.append(observation)
        if observation.tool_name:
            event.tool_name = observation.tool_name
        if observation.tool_input is not None:
            event.input = observation.tool_input
        if observation.tool_response is not None:
            if isinstance(event.output, dict) and isinstance(event.output.get("toolUseResult"), dict):
                event.output = {
                    "content": observation.tool_response,
                    "toolUseResult": event.output["toolUseResult"],
                }
            else:
                event.output = observation.tool_response
        _overlay_timestamp(graph, event, "started_at_ms", observation.started_at_ms)
        _overlay_timestamp(graph, event, "ended_at_ms", observation.ended_at_ms)
        if observation.status == "error":
            event.status = EventStatus.FAILED
            event.error = str(observation.error or observation.tool_response or "Tool call failed")
        elif observation.status == "success":
            event.status = EventStatus.COMPLETED
    return matched


def _overlay_timestamp(graph, event, attribute: str, value: object) -> None:
    """Apply one persisted timestamp without letting corrupt state break export."""
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        graph.diagnostics.append(
            GraphDiagnostic(
                code="invalid_timestamp",
                message=f"tool observation {attribute} is invalid",
                event_id=event.event_id,
                severity="warning",
            )
        )
        return
    setattr(event, attribute, int(value))


def _decode_pending_subagents(raw: object) -> dict[str, dict]:
    if not isinstance(raw, str) or not raw:
        return {}
    try:
        decoded = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return {}
    if not isinstance(decoded, dict):
        return {}
    return {str(agent_id): descriptor for agent_id, descriptor in decoded.items() if isinstance(descriptor, dict)}


def _pending_subagents(state) -> dict[str, dict]:
    return _decode_pending_subagents(state.get("pending_subagents") or "")


def _transcript_has_stable_assistant_uuid(path_value: object) -> bool:
    if not isinstance(path_value, str) or not path_value:
        return False
    try:
        path = Path(path_value)
        if not path.is_file():
            return False
        with path.open(encoding="utf-8") as handle:
            for raw_line in handle:
                try:
                    record = json.loads(raw_line)
                except (json.JSONDecodeError, TypeError, ValueError):
                    continue
                if record.get("type") == "assistant" and isinstance(record.get("uuid"), str) and record["uuid"]:
                    return True
    except OSError:
        return False
    return False


def _buffer_subagent(state, input_json: dict, ended_at_ms: int) -> bool:
    agent_id = input_json.get("agent_id")
    transcript_path = input_json.get("agent_transcript_path")
    main_transcript_path = input_json.get("transcript_path")
    if main_transcript_path == transcript_path or not _transcript_has_stable_assistant_uuid(main_transcript_path):
        return False
    if not isinstance(agent_id, str) or not agent_id:
        return False
    if not isinstance(transcript_path, str) or not transcript_path:
        return False
    try:
        if not Path(transcript_path).is_file():
            return False
    except OSError:
        return False

    descriptor = {
        "agent_id": agent_id,
        "agent_type": input_json.get("agent_type") or "unknown",
        "transcript_path": transcript_path,
        "started_at_ms": None,
        "ended_at_ms": ended_at_ms,
        "output": input_json.get("last_assistant_message") or "",
    }
    if state.state_file is None:
        return False
    try:
        with state._lock():
            data = state._read_safe()
            descriptors = _decode_pending_subagents(data.get("pending_subagents", ""))
            stored_start = str(data.get(f"subagent_{agent_id}_start_time", ""))
            descriptor["started_at_ms"] = int(stored_start) if stored_start.isdigit() else None
            descriptors[agent_id] = descriptor
            data["pending_subagents"] = json.dumps(descriptors, sort_keys=True)
            state._write(data)
    except Exception as exc:
        error(f"Failed to buffer subagent {agent_id}: {exc}")
        return False
    return True


def _agent_id_from_tool(event: ToolEvent) -> str:
    if not isinstance(event.output, dict):
        return ""
    result = event.output.get("toolUseResult")
    if not isinstance(result, dict):
        return ""
    agent_id = result.get("agentId")
    return agent_id if isinstance(agent_id, str) else ""


def _merge_pending_subagents(graph, descriptors: dict[str, dict]) -> dict[str, dict]:
    """Insert correlated foreground agent graphs and return exported descriptors."""
    matched: dict[str, dict] = {}
    if not descriptors or not graph.events:
        return matched
    root = graph.events[0]
    for agent_id, descriptor in descriptors.items():
        parent_tool = next(
            (
                event
                for event in graph.events
                if isinstance(event, ToolEvent) and _agent_id_from_tool(event) == agent_id
            ),
            None,
        )
        if parent_tool is None:
            graph.diagnostics.append(
                GraphDiagnostic(
                    code="unmatched_subagent",
                    message="Subagent descriptor had no correlated Agent tool",
                    event_id=f"agent:{agent_id}",
                )
            )
            continue
        parent_event_id = parent_tool.event_id
        agent_type = str(descriptor.get("agent_type") or "unknown")
        agent_input = None
        if parent_tool is not None and isinstance(parent_tool.input, dict):
            agent_input = parent_tool.input.get("prompt")
        agent_event = AgentEvent(
            event_id=f"agent:{agent_id}",
            parent_event_id=parent_event_id,
            session_id=root.session_id,
            turn_id=root.turn_id,
            sequence=(parent_tool.sequence + 1) if parent_tool is not None else len(graph.events),
            started_at_ms=descriptor.get("started_at_ms"),
            ended_at_ms=descriptor.get("ended_at_ms"),
            status=EventStatus.COMPLETED,
            input=agent_input,
            output=descriptor.get("output"),
            agent_id=agent_id,
            source_id=agent_type,
        )
        transcript_path = Path(str(descriptor.get("transcript_path") or ""))
        subgraph = parse_claude_transcript(transcript_path, agent_event)
        insertion_index = graph.events.index(parent_tool) + 1 if parent_tool is not None else len(graph.events)
        graph.events[insertion_index:insertion_index] = subgraph.events
        graph.diagnostics.extend(subgraph.diagnostics)
        matched[agent_id] = descriptor
    graph.validate()
    return matched


def _cleanup_pending_subagents(state, exported: dict[str, dict]) -> set[str]:
    """Acknowledge only descriptors unchanged since the export snapshot."""
    if state.state_file is None:
        return set()
    removed: set[str] = set()
    try:
        with state._lock():
            data = state._read_safe()
            current = _decode_pending_subagents(data.get("pending_subagents", ""))
            for agent_id, descriptor in exported.items():
                if current.get(agent_id) != descriptor:
                    continue
                current.pop(agent_id, None)
                data.pop(f"subagent_{agent_id}_start_time", None)
                data.pop(f"subagent_{agent_id}_prompt", None)
                removed.add(agent_id)
            if current:
                data["pending_subagents"] = json.dumps(current, sort_keys=True)
            else:
                data.pop("pending_subagents", None)
            state._write(data)
    except Exception as exc:
        error(f"Failed to clean up exported subagents: {exc}")
    return removed


def _acknowledge_exported_turn(
    state,
    expected_trace_id: str,
    observations: list[ToolObservation],
    subagents: dict[str, dict],
) -> bool:
    """Atomically acknowledge one exported turn without touching a newer turn."""
    if state.state_file is None:
        return False
    try:
        with state._lock():
            data = state._read_safe()
            if data.get("current_trace_id") != expected_trace_id:
                return False

            current_observations = ToolBuffer._decode(data.get(ToolBuffer.STATE_KEY, ""))
            for observation in observations:
                if current_observations.get(observation.tool_use_id) == observation:
                    current_observations.pop(observation.tool_use_id, None)
            data[ToolBuffer.STATE_KEY] = ToolBuffer._encode(current_observations)

            current_subagents = _decode_pending_subagents(data.get("pending_subagents", ""))
            for agent_id, descriptor in subagents.items():
                if current_subagents.get(agent_id) != descriptor:
                    continue
                current_subagents.pop(agent_id, None)
                data.pop(f"subagent_{agent_id}_start_time", None)
                data.pop(f"subagent_{agent_id}_prompt", None)
            if current_subagents:
                data["pending_subagents"] = json.dumps(current_subagents, sort_keys=True)
            else:
                data.pop("pending_subagents", None)

            for key in (
                "current_trace_id",
                "current_trace_span_id",
                "current_trace_start_time",
                "current_trace_prompt",
                "trace_start_line",
                "pending_expansion_type",
                "pending_command_name",
                "pending_command_args",
                "pending_command_source",
                "high_fidelity_span_ids",
            ):
                data.pop(key, None)
            state._write(data)
        return True
    except Exception as exc:
        error(f"Failed to acknowledge exported turn: {exc}")
        return False


def _periodic_gc(trace_count: str) -> None:
    try:
        count = int(trace_count or "0")
    except (ValueError, TypeError):
        count = 0
    if count % 5 == 0:
        gc_stale_state_files()


def _handle_stop(input_json: dict) -> None:
    """Handle Stop: send the LLM span for the completed turn and clean up trace state."""
    state = resolve_session(input_json)
    session_id = state.get("session_id")
    trace_id = state.get("current_trace_id")
    if session_id is None or trace_id is None:
        return

    trace_span_id = state.get("current_trace_span_id") or generate_span_id()
    trace_start_time = state.get("current_trace_start_time") or str(get_timestamp_ms())
    user_prompt = state.get("current_trace_prompt") or ""
    project_name = state.get("project_name") or ""
    trace_count = state.get("trace_count") or "0"
    user_id = state.get("user_id") or ""

    # Claude Code v2 ships the assistant's final text directly.  Earlier versions
    # didn't, so we still scan the transcript when last_assistant_message is empty.
    last_msg = input_json.get("last_assistant_message", "") or ""
    output = last_msg

    usage = _TokenUsage()
    model = ""

    transcript = resolve_transcript_path(input_json, session_id)
    if transcript is not None:
        start_line = int(state.get("trace_start_line") or "0")
        scanned_output, usage, scanned_model = _scan_transcript_for_usage(transcript, start_line)
        if not output:
            output = scanned_output
        model = scanned_model

    if not output:
        output = "(No response)"

    if transcript is not None:
        root_event = TurnEvent(
            event_id=f"turn:{trace_count}",
            session_id=session_id,
            turn_id=trace_count,
            sequence=0,
            started_at_ms=int(trace_start_time) if trace_start_time.isdigit() else None,
            ended_at_ms=get_timestamp_ms(),
            status=EventStatus.COMPLETED,
            input=user_prompt,
            output=output,
        )
        graph = parse_claude_transcript(
            transcript,
            root_event,
            start_line=int(state.get("trace_start_line") or "0"),
        )
        if _has_stable_model_ids(graph):
            buffer = ToolBuffer(state)
            observations = buffer.all()
            pending_subagents = _pending_subagents(state)
            matched_observations = _merge_tool_observations(graph, observations)
            matched_subagents = _merge_pending_subagents(graph, pending_subagents)
            graph.validate()
            timestamp_candidates = [
                int(event.ended_at_ms)
                for event in graph.events
                if isinstance(event.ended_at_ms, (int, float))
                and not isinstance(event.ended_at_ms, bool)
                and math.isfinite(event.ended_at_ms)
                and event.ended_at_ms >= 0
            ]
            if timestamp_candidates:
                root_event.ended_at_ms = max(root_event.ended_at_ms or 0, *timestamp_candidates)

            root_attrs = {
                "trace.number": trace_count,
                "project.name": project_name,
            }
            if user_id:
                root_attrs["user.id"] = user_id
            expansion_type = state.get("pending_expansion_type") or ""
            command_name = state.get("pending_command_name") or ""
            command_args = state.get("pending_command_args") or ""
            command_source = state.get("pending_command_source") or ""
            if expansion_type:
                root_attrs["command.expansion_type"] = expansion_type
            if command_name:
                root_attrs["command.name"] = command_name
            if command_args:
                root_attrs["command.args"] = redact_content(env.log_prompts, command_args)
            if command_source:
                root_attrs["command.source"] = command_source

            span_id_overrides = {root_event.event_id: trace_span_id}
            stored_span_ids = state.get("high_fidelity_span_ids") or ""
            if stored_span_ids:
                try:
                    decoded_span_ids = json.loads(stored_span_ids)
                    if isinstance(decoded_span_ids, dict):
                        span_id_overrides.update(
                            {
                                str(event_id): str(span_id)
                                for event_id, span_id in decoded_span_ids.items()
                                if isinstance(event_id, str) and isinstance(span_id, str) and span_id
                            }
                        )
                except (json.JSONDecodeError, TypeError, ValueError) as exc:
                    # Persisted span-id state can be malformed; ignore and regenerate IDs below.
                    log(f"invalid high_fidelity_span_ids state; regenerating span IDs: {exc}")
            for event in graph.events:
                span_id_overrides.setdefault(event.event_id, generate_span_id())
            state.set("high_fidelity_span_ids", json.dumps(span_id_overrides, sort_keys=True))

            payload = render_event_graph(
                graph,
                trace_id=trace_id,
                service_name=SERVICE_NAME,
                scope_name=SCOPE_NAME,
                span_id_overrides=span_id_overrides,
                extra_attributes={root_event.event_id: root_attrs},
            )
            if send_span(payload) is False:
                return
            _acknowledge_exported_turn(state, trace_id, matched_observations, matched_subagents)
            _periodic_gc(trace_count)
            return

    # Legacy fallback for old/incomplete transcripts without stable assistant IDs.
    # Build and send LLM span. Redact at emit time, not at state-write time.
    redacted_prompt = redact_content(env.log_prompts, user_prompt)
    redacted_output = redact_content(env.log_prompts, output)
    output_messages = [{"message.role": "assistant", "message.content": redacted_output}]
    attrs = {
        "session.id": session_id,
        "trace.number": trace_count,
        "project.name": project_name,
        "openinference.span.kind": "LLM",
        "llm.model_name": model,
        **usage.token_count_attrs(),
        "input.value": redacted_prompt,
        "output.value": redacted_output,
        "llm.output_messages": json.dumps(output_messages),
    }
    if user_id:
        attrs["user.id"] = user_id

    # Attach command metadata from UserPromptExpansion if present
    expansion_type = state.get("pending_expansion_type") or ""
    command_name = state.get("pending_command_name") or ""
    command_args = state.get("pending_command_args") or ""
    command_source = state.get("pending_command_source") or ""
    if expansion_type:
        attrs["command.expansion_type"] = expansion_type
    if command_name:
        attrs["command.name"] = command_name
    if command_args:
        attrs["command.args"] = redact_content(env.log_prompts, command_args)
    if command_source:
        attrs["command.source"] = command_source

    span = build_span(
        f"Turn {trace_count}",
        "LLM",
        trace_span_id,
        trace_id,
        "",
        trace_start_time,
        str(get_timestamp_ms()),
        attrs,
        SERVICE_NAME,
        SCOPE_NAME,
    )
    if send_span(span) is False:
        return

    # The legacy payload contains only the Turn span. Preserve unmatched durable
    # observations/descriptors rather than acknowledging data that was not sent.
    _acknowledge_exported_turn(state, trace_id, [], {})
    _periodic_gc(trace_count)


def _handle_subagent_start(input_json: dict) -> None:
    """Handle SubagentStart: record start time + prompt keyed by agent_id."""
    state = resolve_session(input_json)
    agent_id = input_json.get("agent_id", "")
    if not agent_id:
        return
    state.set(f"subagent_{agent_id}_start_time", str(get_timestamp_ms()))
    prompt = input_json.get("prompt", "") or ""
    if prompt:
        state.set(f"subagent_{agent_id}_prompt", prompt)


def _handle_subagent_stop(input_json: dict) -> None:
    """Handle subagent_stop: parse subagent transcript and send CHAIN span."""
    state = resolve_session(input_json)
    trace_id = state.get("current_trace_id")
    if trace_id is None:
        return

    session_id = state.get("session_id")
    agent_id = input_json.get("agent_id", "")
    agent_type = input_json.get("agent_type", "")
    end_time = str(get_timestamp_ms())

    # Claude Code 2.1.209 exposes the dedicated transcript only at
    # SubagentStop. Buffer it until main Stop can resolve agent_id to the
    # invoking Agent tool_use_id and export one coherent tree.
    if _buffer_subagent(state, input_json, int(end_time)):
        return

    if not agent_type or agent_type in ("unknown", "null"):
        return

    span_id = generate_span_id()
    parent = state.get("current_trace_span_id")

    # Claude Code v2 ships the assistant's final text directly.
    last_msg = input_json.get("last_assistant_message", "") or ""
    output = last_msg

    model = ""
    usage = _TokenUsage()

    # Prefer state-stored start time set by SubagentStart; fall back to transcript birth time.
    stored_start = state.get(f"subagent_{agent_id}_start_time")
    if stored_start:
        start_time = stored_start
    else:
        start_time = end_time  # default; may be overwritten below

    transcript = resolve_transcript_path(input_json, session_id or "")
    if transcript is not None:
        if not stored_start:
            st = transcript.stat()
            # st_birthtime is macOS/BSD only; fall back to ctime elsewhere.
            birth = getattr(st, "st_birthtime", st.st_ctime)
            start_time = str(int(birth * 1000))

        scanned_output, usage, scanned_model = _scan_transcript_for_usage(transcript, 0)
        if not output:
            output = scanned_output
        model = scanned_model

    if not output:
        output = "(No response)"

    # Subagent output is a tool-like result — redact unless opted in.
    output = redact_content(env.log_tool_content, output)

    # Build attributes
    attrs = {
        "session.id": session_id,
        "openinference.span.kind": "CHAIN",
        "subagent.id": agent_id,
        "subagent.type": agent_type,
        "llm.model_name": model,
        **usage.token_count_attrs(),
        "output.value": output,
    }
    stored_prompt = state.get(f"subagent_{agent_id}_prompt") or ""
    if stored_prompt:
        attrs["input.value"] = redact_content(env.log_prompts, stored_prompt)
    user_id = state.get("user_id") or ""
    if user_id:
        attrs["user.id"] = user_id

    span = build_span(
        f"Subagent: {agent_type}",
        "CHAIN",
        span_id,
        trace_id,
        parent or "",
        start_time,
        end_time,
        attrs,
        SERVICE_NAME,
        SCOPE_NAME,
    )
    send_span(span)

    # Clean up per-agent state keys
    if agent_id:
        state.delete(f"subagent_{agent_id}_start_time")
        state.delete(f"subagent_{agent_id}_prompt")


def _handle_stop_failure(input_json: dict) -> None:
    """Handle StopFailure: emit a span describing the failed turn so it doesn't disappear silently."""
    state = resolve_session(input_json)
    session_id = state.get("session_id")
    trace_id = state.get("current_trace_id")
    if session_id is None or trace_id is None:
        return

    trace_span_id = state.get("current_trace_span_id") or generate_span_id()
    trace_start_time = state.get("current_trace_start_time") or str(get_timestamp_ms())
    user_prompt = state.get("current_trace_prompt") or ""
    project_name = state.get("project_name") or ""
    trace_count = state.get("trace_count") or "0"
    user_id = state.get("user_id") or ""

    error_type = input_json.get("error", "") or ""
    error_details = input_json.get("error_details", "") or ""
    last_msg = input_json.get("last_assistant_message", "") or ""

    output_text = last_msg or f"(Stop failed: {error_type})"
    redacted_prompt = redact_content(env.log_prompts, user_prompt)
    redacted_output = redact_content(env.log_prompts, output_text)
    output_messages = [{"message.role": "assistant", "message.content": redacted_output}]

    attrs = {
        "session.id": session_id,
        "trace.number": trace_count,
        "project.name": project_name,
        "openinference.span.kind": "LLM",
        "input.value": redacted_prompt,
        "output.value": redacted_output,
        "llm.output_messages": json.dumps(output_messages),
        "error.type": error_type,
        "error.message": redact_content(env.log_prompts, error_details),
    }
    if user_id:
        attrs["user.id"] = user_id

    span = build_span(
        f"Turn {trace_count} (failed)",
        "LLM",
        trace_span_id,
        trace_id,
        "",
        trace_start_time,
        str(get_timestamp_ms()),
        attrs,
        SERVICE_NAME,
        SCOPE_NAME,
    )
    if send_span(span) is False:
        return

    # The failure payload contains only the Turn span; buffered child snapshots
    # were not exported and must remain available for diagnosis/retry.
    _acknowledge_exported_turn(state, trace_id, [], {})


def _handle_notification(input_json: dict) -> None:
    """Handle notification: send a CHAIN span for the notification event."""
    state = resolve_session(input_json)
    trace_id = state.get("current_trace_id")
    if trace_id is None:
        return

    session_id = state.get("session_id")
    message = redact_content(env.log_prompts, input_json.get("message", ""))
    title = redact_content(env.log_prompts, input_json.get("title", ""))
    notification_type = input_json.get("type", "info")

    attrs = {
        "session.id": session_id,
        "openinference.span.kind": "CHAIN",
        "notification.message": message,
        "notification.title": title,
        "notification.type": notification_type,
        "input.value": message,
    }
    user_id = state.get("user_id") or ""
    if user_id:
        attrs["user.id"] = user_id

    now = str(get_timestamp_ms())
    span = build_span(
        f"Notification: {notification_type}",
        "CHAIN",
        generate_span_id(),
        trace_id,
        state.get("current_trace_span_id") or "",
        now,
        now,
        attrs,
        SERVICE_NAME,
        SCOPE_NAME,
    )
    send_span(span)


def _handle_permission_request(input_json: dict) -> None:
    """Handle permission_request: send a CHAIN span for the permission event."""
    state = resolve_session(input_json)
    log(f"DEBUG permission_request input: {json.dumps(input_json)}")

    trace_id = state.get("current_trace_id")
    if trace_id is None:
        return

    session_id = state.get("session_id")
    permission = input_json.get("permission", "")
    tool_name = input_json.get("tool_name", "")
    tool_input = redact_content(env.log_tool_details, json.dumps(input_json.get("tool_input", {})))

    attrs = {
        "session.id": session_id,
        "openinference.span.kind": "CHAIN",
        "permission.type": permission,
        "permission.tool": tool_name,
        "input.value": tool_input,
    }
    user_id = state.get("user_id") or ""
    if user_id:
        attrs["user.id"] = user_id

    now = str(get_timestamp_ms())
    span = build_span(
        "Permission Request",
        "CHAIN",
        generate_span_id(),
        trace_id,
        state.get("current_trace_span_id") or "",
        now,
        now,
        attrs,
        SERVICE_NAME,
        SCOPE_NAME,
    )
    send_span(span)


def _handle_permission_denied(input_json: dict) -> None:
    """Handle PermissionDenied: emit a CHAIN span recording an auto-mode tool denial."""
    state = resolve_session(input_json)
    trace_id = state.get("current_trace_id")
    if trace_id is None:
        return

    session_id = state.get("session_id")
    permission = input_json.get("permission", "")
    tool_name = input_json.get("tool_name", "")
    tool_input = redact_content(env.log_tool_details, json.dumps(input_json.get("tool_input", {})))

    attrs = {
        "session.id": session_id,
        "openinference.span.kind": "CHAIN",
        "permission.type": permission,
        "permission.tool": tool_name,
        "permission.denied": "true",
        "input.value": tool_input,
    }
    user_id = state.get("user_id") or ""
    if user_id:
        attrs["user.id"] = user_id

    now = str(get_timestamp_ms())
    span = build_span(
        "Permission Denied",
        "CHAIN",
        generate_span_id(),
        trace_id,
        state.get("current_trace_span_id") or "",
        now,
        now,
        attrs,
        SERVICE_NAME,
        SCOPE_NAME,
    )
    send_span(span)


def _handle_session_end(input_json: dict) -> None:
    """Handle session_end: log summary and clean up state file."""
    state = resolve_session(input_json)
    session_id = state.get("session_id")
    if session_id is None:
        return

    trace_count = state.get("trace_count") or "0"
    tool_count = state.get("tool_count") or "0"

    error(f"Session complete: {trace_count} traces, {tool_count} tools")
    error(f"View in Arize/Phoenix: session.id = {session_id}")

    # Clean up state file and lock
    if state.state_file is not None:
        state.state_file.unlink(missing_ok=True)
    if state._lock_path is not None and state._lock_path.is_dir():
        try:
            state._lock_path.rmdir()
        except OSError:
            pass

    gc_stale_state_files()


def _handle_pre_compact(input_json: dict) -> None:
    """Handle PreCompact: record start time of compaction event."""
    state = resolve_session(input_json)
    state.set("compact_start_time", str(get_timestamp_ms()))
    trigger = input_json.get("trigger", "")
    if trigger:
        state.set("compact_trigger", trigger)


def _handle_post_compact(input_json: dict) -> None:
    """Handle PostCompact: emit a CHAIN span describing the compaction.

    Skip emission when compaction fires between turns (no `current_trace_id`).
    An orphan compact span in its own trace is hard to correlate in Arize;
    matches the permission_denied/notification guard pattern.
    """
    state = resolve_session(input_json)
    session_id = state.get("session_id")
    if session_id is None:
        return

    trace_id = state.get("current_trace_id")
    if trace_id is None:
        # Compaction between turns. Clean up pending state, no span emitted.
        state.delete("compact_start_time")
        state.delete("compact_trigger")
        return

    start_time = state.get("compact_start_time") or str(get_timestamp_ms())
    end_time = str(get_timestamp_ms())
    trigger = input_json.get("trigger") or state.get("compact_trigger") or "unknown"

    parent = state.get("current_trace_span_id") or ""

    attrs = {
        "session.id": session_id,
        "openinference.span.kind": "CHAIN",
        "compact.trigger": trigger,
    }
    user_id = state.get("user_id") or ""
    if user_id:
        attrs["user.id"] = user_id

    span = build_span(
        f"Compact ({trigger})",
        "CHAIN",
        generate_span_id(),
        trace_id,
        parent,
        start_time,
        end_time,
        attrs,
        SERVICE_NAME,
        SCOPE_NAME,
    )
    send_span(span)

    state.delete("compact_start_time")
    state.delete("compact_trigger")


# ---------------------------------------------------------------------------
# CLI entry points
# ---------------------------------------------------------------------------


def session_start():
    """Entry point for arize-hook-session-start."""
    try:
        if not check_requirements():
            return
        input_json = _read_stdin()
        _handle_session_start(input_json)
    except Exception as e:
        error(f"session_start hook failed: {e}")


def pre_tool_use():
    """Entry point for arize-hook-pre-tool-use."""
    try:
        if not check_requirements():
            return
        input_json = _read_stdin()
        _handle_pre_tool_use(input_json)
    except Exception as e:
        error(f"pre_tool_use hook failed: {e}")


def post_tool_use():
    """Entry point for arize-hook-post-tool-use."""
    try:
        if not check_requirements():
            return
        input_json = _read_stdin()
        _handle_post_tool_use(input_json)
    except Exception as e:
        error(f"post_tool_use hook failed: {e}")


def user_prompt_submit():
    """Entry point for arize-hook-user-prompt-submit."""
    try:
        if not check_requirements():
            return
        input_json = _read_stdin()
        _handle_user_prompt_submit(input_json)
    except Exception as e:
        error(f"user_prompt_submit hook failed: {e}")


def stop():
    """Entry point for arize-hook-stop."""
    try:
        if not check_requirements():
            return
        input_json = _read_stdin()
        _handle_stop(input_json)
    except Exception as e:
        error(f"stop hook failed: {e}")


def subagent_stop():
    """Entry point for arize-hook-subagent-stop."""
    try:
        if not check_requirements():
            return
        input_json = _read_stdin()
        _handle_subagent_stop(input_json)
    except Exception as e:
        error(f"subagent_stop hook failed: {e}")


def stop_failure():
    """Entry point for arize-hook-stop-failure."""
    try:
        if not check_requirements():
            return
        input_json = _read_stdin()
        _handle_stop_failure(input_json)
    except Exception as e:
        error(f"stop_failure hook failed: {e}")


def notification():
    """Entry point for arize-hook-notification."""
    try:
        if not check_requirements():
            return
        input_json = _read_stdin()
        _handle_notification(input_json)
    except Exception as e:
        error(f"notification hook failed: {e}")


def permission_request():
    """Entry point for arize-hook-permission-request."""
    try:
        if not check_requirements():
            return
        input_json = _read_stdin()
        _handle_permission_request(input_json)
    except Exception as e:
        error(f"permission_request hook failed: {e}")


def session_end():
    """Entry point for arize-hook-session-end."""
    try:
        if not check_requirements():
            return
        input_json = _read_stdin()
        _handle_session_end(input_json)
    except Exception as e:
        error(f"session_end hook failed: {e}")


def post_tool_use_failure():
    """Entry point for arize-hook-post-tool-use-failure."""
    try:
        if not check_requirements():
            return
        input_json = _read_stdin()
        _handle_post_tool_use_failure(input_json)
    except Exception as e:
        error(f"post_tool_use_failure hook failed: {e}")


def subagent_start():
    """Entry point for arize-hook-subagent-start."""
    try:
        if not check_requirements():
            return
        input_json = _read_stdin()
        _handle_subagent_start(input_json)
    except Exception as e:
        error(f"subagent_start hook failed: {e}")


def user_prompt_expansion():
    """Entry point for arize-hook-user-prompt-expansion."""
    try:
        if not check_requirements():
            return
        input_json = _read_stdin()
        _handle_user_prompt_expansion(input_json)
    except Exception as e:
        error(f"user_prompt_expansion hook failed: {e}")


def pre_compact():
    """Entry point for arize-hook-pre-compact."""
    try:
        if not check_requirements():
            return
        input_json = _read_stdin()
        _handle_pre_compact(input_json)
    except Exception as e:
        error(f"pre_compact hook failed: {e}")


def post_compact():
    """Entry point for arize-hook-post-compact."""
    try:
        if not check_requirements():
            return
        input_json = _read_stdin()
        _handle_post_compact(input_json)
    except Exception as e:
        error(f"post_compact hook failed: {e}")


def permission_denied():
    """Entry point for arize-hook-permission-denied."""
    try:
        if not check_requirements():
            return
        input_json = _read_stdin()
        _handle_permission_denied(input_json)
    except Exception as e:
        error(f"permission_denied hook failed: {e}")
