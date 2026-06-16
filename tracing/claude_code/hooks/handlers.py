#!/usr/bin/env python3
"""Claude Code hook handlers. One exported function per hook event.

Replaces 9 bash scripts in tracing/claude_code/hooks/. Each function is a CLI
entry point registered in pyproject.toml [project.scripts].
"""
import json
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
from tracing.claude_code.hooks.adapter import (
    SCOPE_NAME,
    SERVICE_NAME,
    check_requirements,
    ensure_session_initialized,
    gc_stale_state_files,
    resolve_session,
    resolve_transcript_path,
)

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


def _handle_session_start(input_json: dict) -> None:
    """Handle session_start: initialize session."""
    state = resolve_session(input_json)
    ensure_session_initialized(state, input_json)
    log(f"Session started: {state.get('session_id')}")


def _handle_pre_tool_use(input_json: dict) -> None:
    """Handle pre_tool_use: record tool start time."""
    state = resolve_session(input_json)
    tool_id = input_json.get("tool_use_id") or generate_trace_id()
    state.set(f"tool_{tool_id}_start", str(get_timestamp_ms()))


def _handle_post_tool_use(input_json: dict) -> None:
    """Handle post_tool_use: build and send a TOOL span."""
    state = resolve_session(input_json)
    session_id = state.get("session_id")
    if session_id is None:
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
    """Handle post_tool_use_failure: build and send a TOOL span with error attributes."""
    state = resolve_session(input_json)
    session_id = state.get("session_id")
    if session_id is None:
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
    session_id = state.get("session_id")

    # Fail-safe: close any orphaned Turn span
    prev_trace_id = state.get("current_trace_id")
    prev_span_id = state.get("current_trace_span_id")
    if prev_trace_id and prev_span_id:
        prev_start = state.get("current_trace_start_time") or str(get_timestamp_ms())
        prev_prompt = state.get("current_trace_prompt") or ""
        prev_count = state.get("trace_count") or "?"
        failsafe_attrs = {
            "session.id": session_id,
            "openinference.span.kind": "LLM",
            "input.value": redact_content(env.log_prompts, prev_prompt),
            "output.value": "(Turn closed by fail-safe: Stop hook did not fire)",
        }
        user_id = state.get("user_id") or ""
        if user_id:
            failsafe_attrs["user.id"] = user_id
        failsafe_span = build_span(
            f"Turn {prev_count}",
            "LLM",
            prev_span_id,
            prev_trace_id,
            "",
            prev_start,
            str(get_timestamp_ms()),
            failsafe_attrs,
            SERVICE_NAME,
            SCOPE_NAME,
        )
        send_span(failsafe_span)
        state.delete("current_trace_id")
        state.delete("current_trace_span_id")
        state.delete("current_trace_start_time")
        state.delete("current_trace_prompt")
        log(f"Fail-safe: closed orphaned Turn {prev_count}")

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

            # Anthropic reports input_tokens (uncached), cache_read_input_tokens,
            # and cache_creation_input_tokens as disjoint buckets. The prompt
            # total is their sum (the OpenInference total), while the two cache
            # buckets are tracked separately and surfaced as prompt_details so
            # downstream cost pricing can apply the cheaper cache-read /
            # cache-write rates instead of the full input rate.
            usage = msg.get("usage", {})
            uncached = _usage_int(usage, "input_tokens")
            cache_read = _usage_int(usage, "cache_read_input_tokens")
            cache_write = _usage_int(usage, "cache_creation_input_tokens")
            usage_totals.prompt += uncached + cache_read + cache_write
            usage_totals.cache_read += cache_read
            usage_totals.cache_write += cache_write
            usage_totals.completion += _usage_int(usage, "output_tokens")

    return output, usage_totals, model


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
    send_span(span)

    # Clean up state
    state.delete("current_trace_id")
    state.delete("current_trace_span_id")
    state.delete("current_trace_start_time")
    state.delete("current_trace_prompt")
    state.delete("trace_start_line")
    state.delete("pending_expansion_type")
    state.delete("pending_command_name")
    state.delete("pending_command_args")
    state.delete("pending_command_source")

    # Periodic GC
    try:
        tc = int(trace_count or "0")
    except (ValueError, TypeError):
        tc = 0
    if tc % 5 == 0:
        gc_stale_state_files()


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

    if not agent_type or agent_type in ("unknown", "null"):
        return

    span_id = generate_span_id()
    end_time = str(get_timestamp_ms())
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
        "error.message": error_details,
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
    send_span(span)

    state.delete("current_trace_id")
    state.delete("current_trace_span_id")
    state.delete("current_trace_start_time")
    state.delete("current_trace_prompt")
    state.delete("trace_start_line")
    state.delete("pending_expansion_type")
    state.delete("pending_command_name")
    state.delete("pending_command_args")
    state.delete("pending_command_source")


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
