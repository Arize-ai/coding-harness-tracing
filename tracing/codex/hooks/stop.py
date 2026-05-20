"""Codex Stop hook -- assembles span trees from state + JSONL and ships them."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from core.common import (
    StateManager,
    build_multi_span,
    build_span,
    debug_dump,
    error,
    generate_span_id,
    generate_trace_id,
    get_timestamp_ms,
    log,
)
from core.common import send_span as send_span_to_backend
from tracing.codex.hooks import span_buffer
from tracing.codex.hooks.adapter import (
    SCOPE_NAME,
    SERVICE_NAME,
    check_requirements,
    ensure_session_initialized,
    load_env_file,
    resolve_session,
)

# Maximum time to wait for the notify hook to populate token usage in state.
_TOKEN_POLL_TIMEOUT_MS = 500
_TOKEN_POLL_INTERVAL_MS = 50


def _wait_for_pending_tokens(state: StateManager) -> "dict | None":
    """Poll the state file briefly for token usage data written by the notify hook.

    Each StateManager.get() does a fresh read from disk, so a separate writer
    (the notify hook) lands its update without needing reload semantics here.
    """
    waited = 0
    raw = state.get("pending_token_usage")
    while not raw and waited < _TOKEN_POLL_TIMEOUT_MS:
        time.sleep(_TOKEN_POLL_INTERVAL_MS / 1000.0)
        waited += _TOKEN_POLL_INTERVAL_MS
        raw = state.get("pending_token_usage")
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
    except (TypeError, ValueError):
        pass
    return None


def _parse_int(value) -> "int | None":
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


def finalize_turn(state: StateManager, thread_id: str) -> None:
    """Assemble and send the end-of-turn span tree, then clear turn-scoped state.

    Single source of truth for end-of-turn assembly; also called by the session
    hook when it detects an orphaned turn (e.g. on the next UserPromptSubmit or
    on a SessionStart with source clear/resume).
    """
    session_id = state.get("session_id") or thread_id
    project_name = state.get("project_name") or ""
    user_id = state.get("user_id")
    model = state.get("model")
    permission_mode = state.get("permission_mode")
    sandbox_mode = state.get("sandbox_mode")
    turn_start_ms = state.get("turn_start_ms")
    user_prompt = state.get("user_prompt") or ""
    last_assistant_message = state.get("last_assistant_message") or ""

    rows = span_buffer.read_all(thread_id)
    tool_entries = span_buffer.join_by_call_id(rows)
    existing_tokens = state.get("pending_token_usage")

    if not tool_entries and not user_prompt and not last_assistant_message and not existing_tokens:
        log("stop hook: nothing to finalize")
        return

    pending_token_usage = _wait_for_pending_tokens(state)

    state.increment("trace_count")
    new_trace_count = state.get("trace_count") or "0"

    trace_id = generate_trace_id()
    parent_span_id = generate_span_id()

    start_time = _parse_int(turn_start_ms)
    if start_time is None:
        start_time = get_timestamp_ms()
    end_time = get_timestamp_ms()

    attrs: dict = {
        "session.id": session_id,
        "trace.number": new_trace_count,
        "project.name": project_name,
        "openinference.span.kind": "LLM",
        "input.value": user_prompt,
        "output.value": last_assistant_message or "(No response)",
        "codex.thread_id": thread_id,
    }
    if user_id:
        attrs["user.id"] = user_id
    if model:
        attrs["llm.model_name"] = model
    if permission_mode:
        attrs["codex.approval_mode"] = permission_mode
    if sandbox_mode:
        attrs["codex.sandbox_mode"] = sandbox_mode
    if last_assistant_message:
        attrs["llm.output_messages"] = json.dumps(
            [{"message.role": "assistant", "message.content": last_assistant_message}]
        )

    if pending_token_usage:
        for src, dst in (
            ("prompt_tokens", "llm.token_count.prompt"),
            ("completion_tokens", "llm.token_count.completion"),
            ("total_tokens", "llm.token_count.total"),
        ):
            v = pending_token_usage.get(src)
            if v is not None:
                try:
                    attrs[dst] = int(v)
                except (ValueError, TypeError):
                    pass
        if "llm.model_name" not in attrs and pending_token_usage.get("model"):
            attrs["llm.model_name"] = pending_token_usage["model"]

    child_spans: list = []
    for entry in tool_entries:
        tool_name = entry.get("tool") or "unknown_tool"
        tool_attrs: dict = {
            "openinference.span.kind": "TOOL",
            "tool.name": tool_name,
            "input.value": entry.get("args") or "",
            "output.value": entry.get("output") or "",
            "session.id": session_id,
        }
        if entry.get("decision"):
            tool_attrs["codex.tool.approval_status"] = entry["decision"]
        if entry.get("call_id"):
            tool_attrs["codex.tool.call_id"] = entry["call_id"]

        child_start = entry.get("start_ts_ms") or start_time
        child_end = entry.get("end_ts_ms") or entry.get("start_ts_ms") or end_time
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
        f"Turn {new_trace_count}",
        "LLM",
        parent_span_id,
        trace_id,
        "",
        start_time,
        end_time,
        attrs,
        SERVICE_NAME,
        SCOPE_NAME,
    )

    debug_dump(f"stop_{thread_id}_parent_span", parent_span)

    if child_spans:
        payload = build_multi_span([parent_span] + child_spans, SERVICE_NAME, SCOPE_NAME)
        debug_dump(f"stop_{thread_id}_multi_span", payload)
    else:
        payload = parent_span

    try:
        if not send_span_to_backend(payload):
            error("Failed to send span to backend")
        else:
            log(f"Turn {new_trace_count} sent (thread={thread_id}, children={len(child_spans)})")
    except Exception as e:
        error(f"send_span raised: {e}")

    for key in ("turn_start_ms", "user_prompt", "pending_token_usage", "last_assistant_message"):
        state.delete(key)

    span_buffer.delete(thread_id)


def main() -> None:
    """Entry point for arize-hook-codex-stop. Codex Stop hook.

    Input contract: a single JSON object read from stdin.
    """
    try:
        load_env_file(Path.home() / ".codex" / "arize-env.sh")
        if not check_requirements():
            return
        raw = sys.stdin.read()
        if not raw.strip():
            return
        payload = json.loads(raw)
        thread_id = payload.get("session_id") or payload.get("thread_id") or ""
        if not thread_id:
            log("stop hook: missing thread_id, skipping")
            return
        state = resolve_session(thread_id)
        ensure_session_initialized(state, thread_id, payload.get("cwd") or "")
        finalize_turn(state, thread_id)
    except Exception as e:
        error(f"codex stop hook failed: {e}")


if __name__ == "__main__":
    main()
