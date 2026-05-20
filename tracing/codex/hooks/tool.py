"""Codex PreToolUse + PostToolUse + PermissionRequest hook handler."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from core.common import env, error, get_timestamp_ms, log, redact_content
from tracing.codex.hooks import span_buffer
from tracing.codex.hooks.adapter import check_requirements, load_env_file


def _serialize(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list)):
        try:
            return json.dumps(value, separators=(",", ":"))
        except (TypeError, ValueError):
            return str(value)
    return str(value)


def _extract_call_id(payload: dict) -> str:
    return payload.get("tool_call_id") or payload.get("call_id") or ""


def _handle_pre_tool_use(thread_id: str, payload: dict) -> None:
    call_id = _extract_call_id(payload)
    tool_name = payload.get("tool_name") or "unknown_tool"
    args_raw = _serialize(payload.get("tool_input"))
    args = redact_content(env.log_tool_details, args_raw)
    span_buffer.append_tool_start(
        thread_id,
        call_id=call_id,
        tool=tool_name,
        args=args,
        ts_ms=get_timestamp_ms(),
    )


def _handle_post_tool_use(thread_id: str, payload: dict) -> None:
    call_id = _extract_call_id(payload)
    output_value = payload.get("tool_output")
    if output_value is None:
        output_value = payload.get("tool_result")
    output_raw = _serialize(output_value)
    output = redact_content(env.log_tool_content, output_raw)
    span_buffer.append_tool_end(
        thread_id,
        call_id=call_id,
        output=output,
        ts_ms=get_timestamp_ms(),
    )


def _handle_permission_request(thread_id: str, payload: dict) -> None:
    call_id = _extract_call_id(payload)
    decision = payload.get("permission_decision") or payload.get("decision") or "unknown"
    span_buffer.append_permission(
        thread_id,
        call_id=call_id,
        decision=decision,
        ts_ms=get_timestamp_ms(),
    )


def main() -> None:
    try:
        load_env_file(Path.home() / ".codex" / "arize-env.sh")
        if not check_requirements():
            return
        raw = sys.stdin.read()
        if not raw.strip():
            return
        payload = json.loads(raw)
        event = payload.get("hook_event_name") or payload.get("hook_event") or ""
        thread_id = payload.get("session_id") or payload.get("thread_id") or ""
        if not thread_id:
            log("tool hook: missing thread_id, skipping")
            return
        if event == "PreToolUse":
            _handle_pre_tool_use(thread_id, payload)
        elif event == "PostToolUse":
            _handle_post_tool_use(thread_id, payload)
        elif event == "PermissionRequest":
            _handle_permission_request(thread_id, payload)
        else:
            log(f"tool hook: ignoring event {event!r}")
    except Exception as e:
        error(f"codex tool hook failed: {e}")


if __name__ == "__main__":
    main()
