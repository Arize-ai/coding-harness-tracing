"""Kiro CLI hook handlers — dispatches by hook_event_name.

Span model:
- agentSpawn:        init session state (no span)
- userPromptSubmit:  start a turn — generate trace_id + span_id, save raw
                     prompt + start_ms (no span emitted yet — deferred root)
- preToolUse:        push tool-state with span_id + start_ms (no span yet)
- postToolUse:       pop matching tool-state, build TOOL span, emit
- stop:              read pending turn state, build LLM span using
                     assistant_response, emit
"""

from __future__ import annotations

import json
import sys
from typing import Any

from core.common import (
    StateManager,
    build_span,
    env,
    generate_span_id,
    generate_trace_id,
    get_timestamp_ms,
    log,
    redact_content,
    send_span,
)
from tracing.kiro.hooks.adapter import (
    SCOPE_NAME,
    SERVICE_NAME,
    check_requirements,
    ensure_session_initialized,
    extract_sidecar_attrs,
    gc_stale_state_files,
    load_session_sidecar,
    resolve_session,
)

# ---------------------------------------------------------------------------
# Event handlers
# ---------------------------------------------------------------------------


def _handle_agent_spawn(input_json: dict, state: StateManager) -> None:
    ensure_session_initialized(state, input_json)


def _handle_user_prompt_submit(input_json: dict, state: StateManager) -> None:
    ensure_session_initialized(state, input_json)
    state.increment("trace_count")

    trace_id = generate_trace_id()
    span_id = generate_span_id()
    state.set("pending_turn_trace_id", trace_id)
    state.set("pending_turn_span_id", span_id)
    state.set("pending_turn_start_ms", str(get_timestamp_ms()))
    # Store RAW prompt; redact at emit (in _handle_stop)
    state.set("pending_turn_prompt", input_json.get("prompt", "") or "")


def _handle_pre_tool_use(input_json: dict, state: StateManager) -> None:
    ensure_session_initialized(state, input_json)
    state.increment("tool_count")
    tool_count = state.get("tool_count") or "?"

    span_id = generate_span_id()
    start_ms = get_timestamp_ms()
    slot = f"tool_pending_{tool_count}"
    state.set(slot + "_span_id", span_id)
    state.set(slot + "_start_ms", str(start_ms))
    state.set(slot + "_tool_name", input_json.get("tool_name", "") or "")
    # Store RAW tool_input as JSON; redact at emit (in postToolUse)
    state.set(slot + "_tool_input", json.dumps(input_json.get("tool_input", {})))


def _handle_post_tool_use(input_json: dict, state: StateManager) -> None:
    ensure_session_initialized(state, input_json)
    tool_count = state.get("tool_count") or "0"
    slot = f"tool_pending_{tool_count}"

    pending_span_id = state.get(slot + "_span_id")
    if not pending_span_id:
        log(f"postToolUse: no pending tool slot at tool_count={tool_count}; " f"emitting orphan span")
        pending_span_id = generate_span_id()
        start_ms = get_timestamp_ms()
        tool_name = input_json.get("tool_name", "") or ""
        tool_input_raw = input_json.get("tool_input", {})
    else:
        start_ms = int(state.get(slot + "_start_ms") or get_timestamp_ms())
        tool_name = state.get(slot + "_tool_name") or input_json.get("tool_name", "") or ""
        tool_input_raw_json = state.get(slot + "_tool_input") or "{}"
        tool_input_raw = json.loads(tool_input_raw_json)

    end_ms = get_timestamp_ms()
    tool_response_raw = input_json.get("tool_response", {})

    # Build attrs — redact at emit
    redacted_input = redact_content(env.log_tool_content, json.dumps(tool_input_raw))
    redacted_output = redact_content(env.log_tool_content, json.dumps(tool_response_raw))
    description = ""
    if isinstance(tool_input_raw, dict):
        description = tool_input_raw.get("__tool_use_purpose", "") or ""

    parent_span_id = state.get("pending_turn_span_id") or ""
    trace_id = state.get("pending_turn_trace_id") or generate_trace_id()
    session_id = state.get("session_id") or ""

    attrs: dict[str, Any] = {
        "session.id": session_id,
        "openinference.span.kind": "TOOL",
        "tool.name": tool_name,
        "input.value": redacted_input,
        "output.value": redacted_output,
    }
    if description:
        attrs["tool.description"] = description
    user_id = state.get("user_id") or ""
    if user_id:
        attrs["user.id"] = user_id
    project_name = state.get("project_name") or ""
    if project_name:
        attrs["project.name"] = project_name

    span = build_span(
        f"Tool: {tool_name}" if tool_name else "Tool",
        "TOOL",
        pending_span_id,
        trace_id,
        parent_span_id,
        start_ms,
        end_ms,
        attrs,
        SERVICE_NAME,
        SCOPE_NAME,
    )
    send_span(span)
    log(f"postToolUse: emitted TOOL span {pending_span_id}")

    # Clear the pending slot
    for suffix in ("_span_id", "_start_ms", "_tool_name", "_tool_input"):
        state.set(slot + suffix, "")


def _handle_stop(input_json: dict, state: StateManager) -> None:
    ensure_session_initialized(state, input_json)

    trace_id = state.get("pending_turn_trace_id") or generate_trace_id()
    span_id = state.get("pending_turn_span_id") or generate_span_id()
    start_ms_str = state.get("pending_turn_start_ms")
    start_ms = int(start_ms_str) if start_ms_str else get_timestamp_ms()
    end_ms = get_timestamp_ms()
    raw_prompt = state.get("pending_turn_prompt") or ""
    raw_response = input_json.get("assistant_response", "") or ""

    redacted_input = redact_content(env.log_prompts, raw_prompt)
    redacted_output = redact_content(env.log_prompts, raw_response)
    output_messages = [
        {
            "message.role": "assistant",
            "message.content": redacted_output,
        }
    ]

    session_id = state.get("session_id") or ""
    project_name = state.get("project_name") or ""
    user_id = state.get("user_id") or ""
    trace_count = state.get("trace_count") or "?"

    attrs: dict[str, Any] = {
        "session.id": session_id,
        "openinference.span.kind": "LLM",
        "trace.number": trace_count,
        "input.value": redacted_input,
        "output.value": redacted_output,
        "llm.output_messages": json.dumps(output_messages),
    }
    if project_name:
        attrs["project.name"] = project_name
    if user_id:
        attrs["user.id"] = user_id

    # Enrich with sidecar data — always use -1 (most recent turn).
    # trace_count can drift from the sidecar turn count, so a fixed index
    # would pick the wrong turn or go out of bounds.
    sidecar = load_session_sidecar(session_id)
    attrs.update(extract_sidecar_attrs(sidecar, turn_index=-1))

    span = build_span(
        f"Turn {trace_count}",
        "LLM",
        span_id,
        trace_id,
        "",  # root
        start_ms,
        end_ms,
        attrs,
        SERVICE_NAME,
        SCOPE_NAME,
    )
    send_span(span)
    log(f"stop: emitted LLM span {span_id}")

    # Clear pending turn keys
    for key in ("pending_turn_trace_id", "pending_turn_span_id", "pending_turn_start_ms", "pending_turn_prompt"):
        state.set(key, "")


_DISPATCH = {
    "agentSpawn": _handle_agent_spawn,
    "userPromptSubmit": _handle_user_prompt_submit,
    "preToolUse": _handle_pre_tool_use,
    "postToolUse": _handle_post_tool_use,
    "stop": _handle_stop,
}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> int:
    """Hook entry point. NEVER raises — always exits 0 even on internal error."""
    try:
        if not check_requirements():
            return 0
        try:
            input_json = json.load(sys.stdin)
        except (json.JSONDecodeError, ValueError) as exc:
            log(f"Could not parse stdin JSON: {exc}")
            return 0
        if not isinstance(input_json, dict):
            log(f"stdin payload not a dict: {type(input_json).__name__}")
            return 0

        event = input_json.get("hook_event_name") or ""
        handler = _DISPATCH.get(event)
        if handler is None:
            log(f"Unknown hook_event_name: {event!r}")
            return 0

        state = resolve_session(input_json)
        handler(input_json, state)

        if event == "agentSpawn":
            try:
                gc_stale_state_files()
            except OSError as exc:
                log(f"GC failed (non-fatal): {exc}")
    except Exception as exc:  # noqa: BLE001 — never block Kiro on a bug
        log(f"hook main() crashed: {exc!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
