"""Codex PreToolUse + PostToolUse + PermissionRequest hook handler."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from core.common import debug_dump, env, error, get_timestamp_ms, log, redact_content
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


def _get_first(payload: dict, *keys):
    for key in keys:
        value = payload.get(key)
        if value is not None and value != "":
            return value
    return None


def _get_path(payload: dict, *path):
    current = payload
    for key in path:
        value = current.get(key)
    if value is None or value == "":
        return None
    return value


def _extract_call_id(payload: dict) -> str:
    return (
        _get_first(
            payload,
            "tool_use_id",
            "toolUseId",
            "tool_call_id",
            "toolCallId",
            "call_id",
            "callId",
            "id",
        )
        or _get_path(payload, "tool_call", "call_id")
        or _get_path(payload, "tool_call", "id")
        or _get_path(payload, "toolCall", "callId")
        or _get_path(payload, "toolCall", "id")
        or ""
    )


def _extract_tool_name(payload: dict) -> str:
    return _get_first(payload, "tool_name", "toolName", "tool", "name") or "unknown_tool"


def _extract_tool_input(payload: dict):
    return _get_first(payload, "tool_input", "toolInput", "input", "arguments", "args", "parameters")


def _extract_tool_output(payload: dict):
    output = _get_first(
        payload,
        "tool_output",
        "toolOutput",
        "tool_response",
        "toolResponse",
        "tool_result",
        "toolResult",
        "output",
        "result",
        "structured_content",
        "structuredContent",
        "content",
        "response",
    )
    if output is not None:
        return output

    command_result = {}
    for key in (
        "stdout",
        "stderr",
        "exit_code",
        "exitCode",
        "status",
        "success",
        "error",
        "duration_ms",
        "durationMs",
    ):
        value = payload.get(key)
        if value is not None and value != "":
            command_result[key] = value
    return command_result or None


def _handle_pre_tool_use(thread_id: str, payload: dict) -> None:
    call_id = _extract_call_id(payload)
    tool_name = _extract_tool_name(payload)
    args_raw = _serialize(_extract_tool_input(payload))
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
    output_raw = _serialize(_extract_tool_output(payload))
    output = redact_content(env.log_tool_content, output_raw)
    span_buffer.append_tool_end(
        thread_id,
        call_id=call_id,
        output=output,
        ts_ms=get_timestamp_ms(),
    )


def _handle_permission_request(thread_id: str, payload: dict) -> None:
    call_id = _extract_call_id(payload)
    decision = _get_first(payload, "permission_decision", "permissionDecision", "decision", "status") or "unknown"
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
        debug_dump(f"codex_tool_hook_{event or 'unknown'}", payload)
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
