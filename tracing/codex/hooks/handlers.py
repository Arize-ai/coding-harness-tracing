#!/usr/bin/env python3
"""Codex notify handler -- rollout-JSONL-driven span emission.

Codex's `agent-turn-complete` notify callback is the signal used here. When it
fires, we locate the session rollout JSONL and reconstruct each model response,
its token usage, and the tool calls it originated. Complete rollouts produce:

    CHAIN (turn)
    ├── LLM (response cycle)
    │   └── TOOL
    └── LLM (next response cycle)

Older or incomplete rollouts retain the legacy single-LLM/fallback behavior.
The parser is fail-soft: unknown and malformed records do not prevent a basic
turn trace from being exported.

Input contract: JSON as ``sys.argv[1]`` (NOT stdin -- Codex passes notify
JSON as a CLI argument). No stdout response expected.
"""

from __future__ import annotations

import json
import os
import re
import sys
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
from tracing.codex.constants import get_codex_home
from tracing.codex.hooks.adapter import SCOPE_NAME, SERVICE_NAME, check_requirements, load_env_file
from tracing.codex.hooks.transcript import _extract_turn_from_rollout, _open_regular_text

# Root of Codex's per-session rollout transcripts.
_CODEX_SESSIONS_ROOT = Path.home() / ".codex" / "sessions"
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_MAX_CONTENT_CHARS = 64 * 1024
_MAX_METADATA_CHARS = 4096


def _logged_content(enabled: bool, value: object) -> str:
    """Apply the content gate and bound enabled attributes for backend safety."""
    text = value if isinstance(value, str) else ""
    if not enabled:
        return redact_content(False, text)
    if len(text) <= _MAX_CONTENT_CHARS:
        return text
    visible = _MAX_CONTENT_CHARS
    marker = ""
    # The omitted count changes slightly when the marker itself consumes space;
    # iterate to a stable fixed-width result while keeping the whole value bounded.
    for _ in range(3):
        omitted = len(text) - visible
        marker = f"\n<truncated ({omitted} chars)>"
        visible = _MAX_CONTENT_CHARS - len(marker)
    return f"{text[:visible]}{marker}"


def _metadata_text(value: object, limit: int = _MAX_METADATA_CHARS) -> str:
    return value[:limit] if isinstance(value, str) else ""


# ---------------------------------------------------------------------------
# Payload helpers (notify payload from Codex)
# ---------------------------------------------------------------------------


def _flex_get(d: dict, *keys, default: str = "") -> str:
    """Return the first non-empty value among `keys`, else `default`."""
    for key in keys:
        val = d.get(key)
        if isinstance(val, str) and val:
            return val
    return default


# ---------------------------------------------------------------------------
# Rollout location
# ---------------------------------------------------------------------------


def _rollout_session_matches(path: Path, session_id: str) -> bool:
    """Reject a candidate when its durable session metadata names another ID."""
    try:
        with _open_regular_text(path) as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if not isinstance(record, dict) or record.get("type") != "session_meta":
                    continue
                payload = record.get("payload")
                return isinstance(payload, dict) and payload.get("id") == session_id
    except (OSError, UnicodeError):
        return False
    # Older rollouts may not have session_meta; the literal filename still
    # provides backward-compatible correlation.
    return True


def _find_rollout_file(session_id: str, sessions_root: "Path | None" = None) -> "Path | None":
    """Locate the rollout JSONL file for a given session_id.

    File names embed the session_id, so a filename-pattern match is fast even
    on a deep directory tree.
    """
    if sessions_root is not None:
        root = sessions_root
    elif os.environ.get("CODEX_HOME", ""):
        root = get_codex_home() / "sessions"
    else:
        root = _CODEX_SESSIONS_ROOT
    if not root.is_dir() or not _SESSION_ID_RE.fullmatch(session_id):
        return None
    try:
        root_resolved = root.resolve()
        suffix = f"-{session_id}.jsonl"
        for path in root.rglob("rollout-*.jsonl"):
            try:
                resolved = path.resolve()
            except OSError:
                continue
            if root_resolved not in resolved.parents:
                continue
            if path.name.endswith(suffix) and _rollout_session_matches(path, session_id):
                return path
    except OSError:
        return None
    return None


# ---------------------------------------------------------------------------
# Span assembly
# ---------------------------------------------------------------------------


def _build_and_send_spans(thread_id: str, turn_id: str, turn: dict) -> None:
    """Assemble CHAIN/LLM/TOOL spans from an extracted turn and ship them."""
    thread_id = _metadata_text(thread_id, 128)
    turn_id = _metadata_text(turn_id, 128)
    project_name = _metadata_text(env.project_name_for(SERVICE_NAME)) or "codex"
    user_id = _metadata_text(env.get_user_id(SERVICE_NAME))

    trace_id = generate_trace_id()
    parent_span_id = generate_span_id()

    user_prompt = _logged_content(env.log_prompts, turn.get("user_prompt"))
    assistant_output = _logged_content(env.log_prompts, turn.get("assistant_output"))
    final_output = assistant_output or "(No response)"
    cwd = _metadata_text(turn.get("cwd"))
    workspace = Path(cwd).name if cwd else ""
    turn_model = _metadata_text(turn.get("model"))
    permission_mode = _metadata_text(turn.get("permission_mode"))
    sandbox_mode = _metadata_text(turn.get("sandbox_mode"))

    high_fidelity = bool(turn.get("llm_calls"))
    root_kind = "CHAIN" if high_fidelity else "LLM"
    attrs: dict = {
        "session.id": thread_id,
        "trace.number": str(turn.get("trace_count") or 1),
        "project.name": project_name,
        "openinference.span.kind": root_kind,
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
    if turn_model:
        attrs["llm.model_name"] = turn_model
    if permission_mode:
        attrs["codex.approval_mode"] = permission_mode
    if sandbox_mode:
        attrs["codex.sandbox_mode"] = sandbox_mode
    if turn.get("duration_ms") is not None:
        attrs["codex.turn.duration_ms"] = turn["duration_ms"]
    if assistant_output:
        attrs["llm.output_messages"] = _logged_content(
            True,
            json.dumps([{"message.role": "assistant", "message.content": assistant_output}]),
        )

    usage = turn.get("token_usage") or {}
    if not high_fidelity:
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

    def redact_llm_input(llm_call: dict) -> str:
        parts = llm_call.get("input_parts")
        if not isinstance(parts, list):
            return _logged_content(env.log_prompts, llm_call.get("input_value"))
        rendered: list[str] = []
        for part in parts:
            if not isinstance(part, dict) or not isinstance(part.get("value"), str):
                continue
            enabled = env.log_tool_content if part.get("kind") == "tool_output" else env.log_prompts
            rendered.append(_logged_content(enabled, part["value"]))
        return _logged_content(True, "\n".join(rendered))

    def build_tool_child(entry: dict, parent_id: str) -> dict:
        tool_name = _metadata_text(entry.get("tool")) or "unknown_tool"
        args_raw = entry.get("args") or ""
        output_raw = entry.get("output") or ""
        tool_attrs: dict = {
            "openinference.span.kind": "TOOL",
            "tool.name": tool_name,
            "input.value": _logged_content(env.log_tool_details, args_raw),
            "output.value": _logged_content(env.log_tool_content, output_raw),
            "session.id": thread_id,
        }
        if cwd:
            tool_attrs["codex.cwd"] = cwd
        if workspace:
            tool_attrs["codex.workspace"] = workspace
        call_id = _metadata_text(entry.get("call_id"))
        if call_id:
            tool_attrs["codex.tool.call_id"] = call_id
        child_start = entry.get("start_ts") or turn["turn_start_ms"]
        child_end = max(child_start, entry.get("end_ts") or child_start)
        return build_span(
            tool_name,
            "TOOL",
            generate_span_id(),
            trace_id,
            parent_id,
            child_start,
            child_end,
            tool_attrs,
            SERVICE_NAME,
        )

    if high_fidelity:
        for index, llm_call in enumerate(turn["llm_calls"], start=1):
            llm_span_id = generate_span_id()
            llm_usage = llm_call.get("token_usage") or {}
            llm_model = _metadata_text(llm_usage.get("model") or turn_model)
            llm_input = redact_llm_input(llm_call)
            llm_output = _logged_content(env.log_prompts, llm_call.get("assistant_output"))
            llm_attrs: dict = {
                "openinference.span.kind": "LLM",
                "session.id": thread_id,
                "input.value": llm_input,
                "output.value": llm_output,
                "codex.thread_id": thread_id,
                "codex.turn_id": turn_id,
                "codex.response.number": index,
            }
            if llm_model:
                llm_attrs["llm.model_name"] = llm_model
            for src, dst in (
                ("prompt_tokens", "llm.token_count.prompt"),
                ("completion_tokens", "llm.token_count.completion"),
                ("total_tokens", "llm.token_count.total"),
            ):
                value = llm_usage.get(src)
                if isinstance(value, int):
                    llm_attrs[dst] = value
            cache_read = llm_usage.get("cached_input_tokens")
            if isinstance(cache_read, int) and cache_read:
                llm_attrs["llm.token_count.prompt_details.cache_read"] = cache_read
            reasoning = llm_usage.get("reasoning_output_tokens")
            if isinstance(reasoning, int) and reasoning:
                llm_attrs["llm.token_count.completion_details.reasoning"] = reasoning
            if llm_usage:
                llm_attrs["codex.token_usage"] = json.dumps(llm_usage)
            if llm_output:
                llm_attrs["llm.output_messages"] = _logged_content(
                    True,
                    json.dumps([{"message.role": "assistant", "message.content": llm_output}]),
                )
            child_spans.append(
                build_span(
                    f"LLM call {index}{f': {llm_model}' if llm_model else ''}",
                    "LLM",
                    llm_span_id,
                    trace_id,
                    parent_span_id,
                    llm_call.get("start_ts") or turn["turn_start_ms"],
                    llm_call.get("end_ts") or turn["turn_end_ms"],
                    llm_attrs,
                    SERVICE_NAME,
                    SCOPE_NAME,
                )
            )
            child_spans.extend(build_tool_child(entry, llm_span_id) for entry in llm_call.get("tool_calls") or [])
    else:
        child_spans.extend(build_tool_child(entry, parent_span_id) for entry in turn.get("tool_calls") or [])

    parent_span = build_span(
        f"Turn {turn.get('trace_count') or 1}",
        root_kind,
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
        prompt_parts = []
        for message in user_msgs:
            if isinstance(message, str) and message:
                prompt_parts.append(message)
                continue
            if not isinstance(message, dict) or message.get("role") != "user":
                continue
            content = message.get("content")
            if isinstance(content, str) and content:
                prompt_parts.append(content)
            elif isinstance(content, list):
                prompt_parts.extend(
                    piece["text"]
                    for piece in content
                    if isinstance(piece, dict) and isinstance(piece.get("text"), str) and piece["text"]
                )
        user_prompt = "\n".join(prompt_parts)
    elif isinstance(user_msgs, str):
        user_prompt = user_msgs

    user_prompt = _logged_content(env.log_prompts, user_prompt)
    assistant_output = _logged_content(env.log_prompts, assistant_msg if isinstance(assistant_msg, str) else "")
    final_output = assistant_output or "(No response)"
    thread_id = _metadata_text(thread_id, 128)
    turn_id = _metadata_text(turn_id, 128)
    project_name = _metadata_text(env.project_name_for(SERVICE_NAME)) or "codex"

    trace_id = generate_trace_id()
    span_id = generate_span_id()
    now = get_timestamp_ms()

    attrs = {
        "session.id": thread_id,
        "project.name": project_name,
        "openinference.span.kind": "LLM",
        "input.value": user_prompt,
        "output.value": final_output,
        "codex.thread_id": thread_id,
        "codex.turn_id": turn_id,
        "codex.notify_fallback": "true",
    }
    user_id = _metadata_text(env.get_user_id(SERVICE_NAME))
    if user_id:
        attrs["user.id"] = user_id
    if assistant_output:
        attrs["llm.output_messages"] = _logged_content(
            True,
            json.dumps([{"message.role": "assistant", "message.content": assistant_output}]),
        )

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
        load_env_file(get_codex_home() / "arize-env.sh")
        if not check_requirements():
            return
        raw = sys.argv[1] if len(sys.argv) > 1 else "{}"
        input_json = json.loads(raw)
        _handle_notify(input_json)
    except Exception as e:
        error(f"codex notify hook failed: {e}")


if __name__ == "__main__":
    notify()
