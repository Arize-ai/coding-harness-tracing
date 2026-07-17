#!/usr/bin/env python3
"""Codex notify handler -- rollout-JSONL-driven span emission.

Codex's `agent-turn-complete` notify callback is the only signal we listen
for. When it fires, we locate the session's rollout JSONL at
``~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<session_id>.jsonl`` and extract
the full turn's data: user prompt, assistant message, token usage, and every
tool call (shell, apply_patch, web_search, open_page) with structured args,
output, and timing. We then ship one OTLP payload with the parent LLM span
plus all TOOL child spans.

This replaces an earlier design that relied on Codex's lifecycle hooks
(SessionStart, UserPromptSubmit, PreToolUse, PostToolUse, Stop). Those hooks
only cover SOME tool types (shell/apply_patch but not web_search), use a
brittle hook-payload schema, and require explicit ``/hooks`` trust approval.
The rollout JSONL covers every tool, has a stable JSON schema, and is the
single source of truth for the session.

Input contract: JSON as ``sys.argv[1]`` (NOT stdin -- Codex passes notify
JSON as a CLI argument). No stdout response expected.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

from core.common import (
    build_multi_span,
    build_span,
    debug_dump,
    env,
    error,
    generate_span_id,
    generate_trace_id,
    get_timestamp_ms,
    log,
    redact_content,
)
from core.common import send_span as send_span_to_backend
from tracing.codex.hooks.adapter import SCOPE_NAME, SERVICE_NAME, check_requirements, load_env_file

# Root of Codex's per-session rollout transcripts.
_CODEX_SESSIONS_ROOT = Path.home() / ".codex" / "sessions"


# ---------------------------------------------------------------------------
# Payload helpers (notify payload from Codex)
# ---------------------------------------------------------------------------


def _flex_get(d: dict, *keys, default: str = "") -> str:
    """Return the first non-empty value among `keys`, else `default`."""
    for key in keys:
        val = d.get(key)
        if val is not None and val != "":
            return val
    return default


# ---------------------------------------------------------------------------
# Rollout location
# ---------------------------------------------------------------------------


def _find_rollout_file(session_id: str, sessions_root: "Path | None" = None) -> "Path | None":
    """Locate the rollout JSONL file for a given session_id.

    File names embed the session_id, so a filename-pattern match is fast even
    on a deep directory tree.
    """
    root = sessions_root or _CODEX_SESSIONS_ROOT
    if not root.is_dir() or not session_id:
        return None
    try:
        for path in root.rglob(f"rollout-*-{session_id}.jsonl"):
            return path
    except OSError:
        return None
    return None


# ---------------------------------------------------------------------------
# Rollout extraction
# ---------------------------------------------------------------------------


def _iso_to_ms(ts: str) -> int:
    """Convert an ISO-8601 timestamp string (e.g. ``2026-05-20T23:42:45.649Z``) to ms.

    Returns 0 on any parse failure -- callers tolerate zero timestamps.
    """
    if not ts:
        return 0
    try:
        # Python's fromisoformat doesn't accept the trailing Z prior to 3.11;
        # normalize by swapping Z for +00:00 for compatibility.
        normalized = ts.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
        return int(dt.timestamp() * 1000)
    except (ValueError, TypeError):
        return 0


def _extract_turn_from_rollout(rollout_path: Path, turn_id: str) -> "dict | None":
    """Walk the rollout JSONL and extract everything for one turn.

    Returns a dict with: ``trace_count``, ``turn_start_ms``, ``turn_end_ms``,
    ``duration_ms``, ``user_prompt``, ``assistant_output``, ``model``, ``cwd``,
    ``permission_mode``, ``sandbox_mode``, ``token_usage`` (or None), and
    ``tool_calls`` (a list of ``{tool, args, output, call_id, start_ts, end_ts}``).

    Returns None if the turn isn't found.
    """
    if not turn_id or not rollout_path.is_file():
        return None

    fields = (
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "cached_input_tokens",
        "non_cached_input_tokens",
        "reasoning_output_tokens",
    )
    token_sums: dict = {k: 0 for k in fields}
    saw_tokens = False

    in_turn = False
    trace_count = 0
    turn_start_ms = 0
    turn_end_ms = 0
    duration_ms: "int | None" = None
    user_prompt = ""
    assistant_output = ""
    model = ""
    cwd = ""
    permission_mode = ""
    sandbox_mode = ""

    tool_calls: list = []
    pending_func: dict = {}  # call_id -> entry being filled
    pending_search_end: "dict | None" = None  # most recent unmatched web_search_end

    try:
        with open(rollout_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue

                outer = obj.get("type")
                payload = obj.get("payload") or {}
                ptype = payload.get("type") if isinstance(payload, dict) else None
                ts_ms = _iso_to_ms(obj.get("timestamp", ""))

                # turn_context records carry per-turn settings (model, cwd, sandbox).
                # They live at the outer level (no payload.type).
                if outer == "turn_context" and isinstance(payload, dict):
                    if payload.get("turn_id") == turn_id:
                        model = payload.get("model") or model
                        cwd = payload.get("cwd") or cwd
                        permission_mode = payload.get("approval_policy") or permission_mode
                        sandbox_mode = (payload.get("sandbox_policy") or {}).get("type") or sandbox_mode
                    continue

                if outer != "event_msg" and outer != "response_item":
                    continue

                # Turn boundaries
                if outer == "event_msg" and ptype == "task_started":
                    if in_turn:
                        # Next turn started after ours -- stop walking.
                        break
                    trace_count += 1
                    if payload.get("turn_id") == turn_id:
                        in_turn = True
                        # started_at is unix seconds; convert to ms.
                        started_at = payload.get("started_at")
                        if isinstance(started_at, int):
                            turn_start_ms = started_at * 1000
                        elif ts_ms:
                            turn_start_ms = ts_ms
                    continue

                if not in_turn:
                    continue

                # task_complete: final assistant message + accurate timing
                if outer == "event_msg" and ptype == "task_complete":
                    msg = payload.get("last_agent_message")
                    if msg:
                        assistant_output = msg
                    # completed_at is unix seconds; convert to ms.
                    completed_at = payload.get("completed_at")
                    if isinstance(completed_at, int):
                        turn_end_ms = completed_at * 1000
                    d = payload.get("duration_ms")
                    if isinstance(d, int):
                        duration_ms = d
                    continue

                # User prompt
                if outer == "event_msg" and ptype == "user_message":
                    msg = payload.get("message")
                    if msg:
                        user_prompt = msg
                    continue

                # Intermediate assistant message (overwritten by task_complete if both present)
                if outer == "event_msg" and ptype == "agent_message":
                    msg = payload.get("message")
                    if msg:
                        assistant_output = msg
                    continue

                # Token counts -- sum per-call deltas
                if outer == "event_msg" and ptype == "token_count":
                    last = (payload.get("info") or {}).get("last_token_usage") or {}
                    for k in fields:
                        v = last.get(k)
                        if isinstance(v, int):
                            token_sums[k] += v
                            saw_tokens = True
                    continue

                # Shell / apply_patch tool call
                if outer == "response_item" and ptype == "function_call":
                    call_id = payload.get("call_id") or ""
                    entry = {
                        "tool": payload.get("name") or "function_call",
                        "args": payload.get("arguments") or "",
                        "output": "",
                        "call_id": call_id,
                        "start_ts": ts_ms,
                        "end_ts": ts_ms,
                        "decision": None,
                    }
                    tool_calls.append(entry)
                    if call_id:
                        pending_func[call_id] = entry
                    continue

                if outer == "response_item" and ptype == "function_call_output":
                    call_id = payload.get("call_id") or ""
                    pending = pending_func.get(call_id) if call_id else None
                    if pending is not None:
                        pending["output"] = payload.get("output") or ""
                        pending["end_ts"] = ts_ms or pending["end_ts"]
                    continue

                # Web search: web_search_end (event_msg, has call_id) is paired with
                # the next web_search_call (response_item) by emission order.
                if outer == "event_msg" and ptype == "web_search_end":
                    pending_search_end = {
                        "call_id": payload.get("call_id") or "",
                        "ts_ms": ts_ms,
                    }
                    continue

                if outer == "response_item" and ptype == "web_search_call":
                    action = payload.get("action") or {}
                    action_type = action.get("type") or "search"
                    if action_type == "open_page":
                        tool_name = "open_page"
                        args = action.get("url") or ""
                    else:
                        tool_name = "web_search"
                        args = action.get("query") or ""
                    start_ts = ts_ms
                    call_id = ""
                    if pending_search_end is not None:
                        call_id = pending_search_end["call_id"]
                        start_ts = pending_search_end["ts_ms"] or start_ts
                        pending_search_end = None
                    tool_calls.append(
                        {
                            "tool": tool_name,
                            "args": args,
                            "output": payload.get("status") or "",
                            "call_id": call_id,
                            "start_ts": start_ts,
                            "end_ts": ts_ms,
                            "decision": None,
                        }
                    )
                    continue
    except OSError:
        return None

    if not in_turn:
        return None

    if not turn_end_ms:
        candidates = [e["end_ts"] for e in tool_calls if e.get("end_ts")]
        turn_end_ms = max(candidates) if candidates else turn_start_ms

    token_usage: "dict | None" = None
    if saw_tokens:
        token_usage = {
            "prompt_tokens": token_sums["input_tokens"] or None,
            "completion_tokens": token_sums["output_tokens"] or None,
            "total_tokens": token_sums["total_tokens"] or None,
            "cached_input_tokens": token_sums["cached_input_tokens"],
            "non_cached_input_tokens": token_sums["non_cached_input_tokens"],
            "reasoning_output_tokens": token_sums["reasoning_output_tokens"],
            "model": model or "",
        }

    return {
        "trace_count": trace_count,
        "turn_start_ms": turn_start_ms or get_timestamp_ms(),
        "turn_end_ms": turn_end_ms or get_timestamp_ms(),
        "duration_ms": duration_ms,
        "user_prompt": user_prompt,
        "assistant_output": assistant_output,
        "model": model,
        "cwd": cwd,
        "permission_mode": permission_mode,
        "sandbox_mode": sandbox_mode,
        "token_usage": token_usage,
        "tool_calls": tool_calls,
    }


# ---------------------------------------------------------------------------
# Span assembly
# ---------------------------------------------------------------------------


def _build_and_send_spans(thread_id: str, turn_id: str, turn: dict) -> None:
    """Assemble the LLM + TOOL spans from an extracted turn and ship them."""
    project_name = env.project_name_for(SERVICE_NAME) or "codex"
    user_id = env.get_user_id(SERVICE_NAME) or ""

    trace_id = generate_trace_id()
    parent_span_id = generate_span_id()

    user_prompt = redact_content(env.log_prompts, turn.get("user_prompt") or "")
    assistant_output = redact_content(env.log_prompts, turn.get("assistant_output") or "")
    final_output = assistant_output or "(No response)"
    cwd = turn.get("cwd") or ""
    workspace = Path(cwd).name if cwd else ""

    attrs: dict = {
        "session.id": thread_id,
        "trace.number": str(turn.get("trace_count") or 1),
        "project.name": project_name,
        "openinference.span.kind": "LLM",
        "input.value": user_prompt,
        "output.value": final_output,
        "codex.thread_id": thread_id,
    }
    if cwd:
        attrs["codex.cwd"] = cwd
    if workspace:
        attrs["codex.workspace"] = workspace
    if turn_id:
        attrs["codex.turn_id"] = turn_id
    if user_id:
        attrs["user.id"] = user_id
    if turn.get("model"):
        attrs["llm.model_name"] = turn["model"]
    if turn.get("permission_mode"):
        attrs["codex.approval_mode"] = turn["permission_mode"]
    if turn.get("sandbox_mode"):
        attrs["codex.sandbox_mode"] = turn["sandbox_mode"]
    if turn.get("duration_ms") is not None:
        attrs["codex.turn.duration_ms"] = turn["duration_ms"]
    if assistant_output:
        attrs["llm.output_messages"] = json.dumps([{"message.role": "assistant", "message.content": assistant_output}])

    usage = turn.get("token_usage") or {}
    for src, dst in (
        ("prompt_tokens", "llm.token_count.prompt"),
        ("completion_tokens", "llm.token_count.completion"),
        ("total_tokens", "llm.token_count.total"),
    ):
        v = usage.get(src)
        if isinstance(v, int):
            attrs[dst] = v
    if usage:
        attrs["codex.token_usage"] = json.dumps(usage)

    child_spans: list = []
    for entry in turn.get("tool_calls") or []:
        tool_name = entry.get("tool") or "unknown_tool"
        args_raw = entry.get("args") or ""
        output_raw = entry.get("output") or ""
        tool_attrs: dict = {
            "openinference.span.kind": "TOOL",
            "tool.name": tool_name,
            "input.value": redact_content(env.log_tool_details, args_raw),
            "output.value": redact_content(env.log_tool_content, output_raw),
            "session.id": thread_id,
        }
        if cwd:
            tool_attrs["codex.cwd"] = cwd
        if workspace:
            tool_attrs["codex.workspace"] = workspace
        if entry.get("call_id"):
            tool_attrs["codex.tool.call_id"] = entry["call_id"]
        child_start = entry.get("start_ts") or turn["turn_start_ms"]
        child_end = entry.get("end_ts") or child_start
        child = build_span(
            tool_name,
            "TOOL",
            generate_span_id(),
            trace_id,
            parent_span_id,
            child_start,
            child_end,
            tool_attrs,
            SERVICE_NAME,
            SCOPE_NAME,
        )
        child_spans.append(child)

    parent_span = build_span(
        f"Turn {turn.get('trace_count') or 1}",
        "LLM",
        parent_span_id,
        trace_id,
        "",
        turn["turn_start_ms"],
        turn["turn_end_ms"],
        attrs,
        SERVICE_NAME,
        SCOPE_NAME,
    )

    debug_dump(f"notify_{thread_id}_parent_span", parent_span)

    if child_spans:
        payload = build_multi_span([parent_span] + child_spans, SERVICE_NAME, SCOPE_NAME)
        debug_dump(f"notify_{thread_id}_multi_span", payload)
    else:
        payload = parent_span

    if not send_span_to_backend(payload):
        error("Failed to send span to backend")
    else:
        log(f"Turn sent (thread={thread_id}, turn={turn_id}, children={len(child_spans)})")


def _send_legacy_single_span(thread_id: str, turn_id: str, input_json: dict) -> None:
    """Fallback when no rollout file is found -- emit a single LLM span from
    the notify payload alone."""
    assistant_msg = (
        input_json.get("last-assistant-message")
        or input_json.get("last_assistant_message")
        or input_json.get("lastAssistantMessage")
    )
    if isinstance(assistant_msg, dict):
        # Sometimes wrapped: try common keys
        for k in ("text", "message", "content"):
            v = assistant_msg.get(k)
            if isinstance(v, str) and v:
                assistant_msg = v
                break

    user_msgs = input_json.get("input-messages") or input_json.get("input_messages") or ""
    user_prompt = ""
    if isinstance(user_msgs, list):
        for m in reversed(user_msgs):
            if isinstance(m, dict) and m.get("role") == "user":
                c = m.get("content")
                if isinstance(c, str) and c:
                    user_prompt = c
                    break
                if isinstance(c, list):
                    for piece in c:
                        if isinstance(piece, dict) and piece.get("text"):
                            user_prompt = piece["text"]
                            break
                    if user_prompt:
                        break
    elif isinstance(user_msgs, str):
        user_prompt = user_msgs

    user_prompt = redact_content(env.log_prompts, user_prompt)
    assistant_output = redact_content(env.log_prompts, assistant_msg if isinstance(assistant_msg, str) else "")
    final_output = assistant_output or "(No response)"

    trace_id = generate_trace_id()
    span_id = generate_span_id()
    now = get_timestamp_ms()

    attrs = {
        "session.id": thread_id,
        "project.name": env.project_name_for(SERVICE_NAME) or "codex",
        "openinference.span.kind": "LLM",
        "input.value": user_prompt,
        "output.value": final_output,
        "codex.thread_id": thread_id,
        "codex.turn_id": turn_id,
        "codex.notify_fallback": "true",
    }
    user_id = env.get_user_id(SERVICE_NAME)
    if user_id:
        attrs["user.id"] = user_id
    if assistant_output:
        attrs["llm.output_messages"] = json.dumps([{"message.role": "assistant", "message.content": assistant_output}])

    parent_span = build_span(
        "Turn",
        "LLM",
        span_id,
        trace_id,
        "",
        now,
        now,
        attrs,
        SERVICE_NAME,
        SCOPE_NAME,
    )
    debug_dump(f"notify_fallback_{thread_id}_{turn_id}_span", parent_span)
    if not send_span_to_backend(parent_span):
        error("Failed to send fallback span to backend")
    else:
        log(f"Turn sent via fallback (thread={thread_id}, turn={turn_id})")


# ---------------------------------------------------------------------------
# Main handler
# ---------------------------------------------------------------------------


def _handle_notify(input_json: dict) -> None:
    """Handle one Codex notify event."""
    if input_json.get("type") != "agent-turn-complete":
        log(f"Ignoring event type: {input_json.get('type')}")
        return

    thread_id = _flex_get(input_json, "thread-id", "thread_id", "threadId")
    turn_id = _flex_get(input_json, "turn-id", "turn_id", "turnId")

    debug_dump(f"notify_{thread_id or 'unknown'}_{turn_id or 'unknown'}_raw", input_json)

    rollout_path = _find_rollout_file(thread_id)
    if rollout_path is None:
        log(f"notify: no rollout file for session {thread_id}, falling back to single span")
        _send_legacy_single_span(thread_id, turn_id, input_json)
        return

    turn = _extract_turn_from_rollout(rollout_path, turn_id)
    if turn is None:
        log(f"notify: turn {turn_id} not found in {rollout_path}; falling back to single span")
        _send_legacy_single_span(thread_id, turn_id, input_json)
        return

    debug_dump(f"notify_{thread_id}_{turn_id}_extracted", turn)
    _build_and_send_spans(thread_id, turn_id, turn)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def notify() -> None:
    """Entry point for ``arize-hook-codex-notify``.

    Codex passes the notify-event JSON on ``sys.argv[1]`` (not stdin) and
    expects no stdout response.
    """
    try:
        load_env_file(Path.home() / ".codex" / "arize-env.sh")
        if not check_requirements():
            return
        raw = sys.argv[1] if len(sys.argv) > 1 else "{}"
        input_json = json.loads(raw)
        _handle_notify(input_json)
    except Exception as e:
        error(f"codex notify hook failed: {e}")


if __name__ == "__main__":
    notify()
