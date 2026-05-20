#!/usr/bin/env python3
"""Codex notify hook handler.

Registered as the ``arize-hook-codex-notify`` CLI entry point. Codex passes
the notify event JSON on ``sys.argv[1]`` (NOT stdin); no stdout response is
expected.

Once Codex's lifecycle hooks (``SessionStart``/``UserPromptSubmit``/``Stop``)
are trusted, this hook only writes ``pending_token_usage`` and
``last_assistant_message`` into the per-thread state file so the ``Stop``
hook can attach exact token counts to its parent span. Before hooks are
trusted (first-run users who haven't approved ``/hooks`` yet), notify falls
back to building a single LLM span from the notify payload directly.
"""
import json
import os
import sys
from pathlib import Path
from typing import Any

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
from tracing.codex.hooks import span_buffer
from tracing.codex.hooks.adapter import (
    SCOPE_NAME,
    SERVICE_NAME,
    check_requirements,
    ensure_session_initialized,
    gc_stale_state_files,
    load_env_file,
    resolve_session,
)

# Root of Codex's per-session rollout transcripts. Files live under
# `<root>/<YYYY>/<MM>/<DD>/rollout-<timestamp>-<session_id>.jsonl` and contain
# the full event stream for the session, including structured token usage
# events that aren't carried in the notify payload itself.
_CODEX_SESSIONS_ROOT = Path.home() / ".codex" / "sessions"

# ---------------------------------------------------------------------------
# Payload helpers
# ---------------------------------------------------------------------------


def _flex_get(d: dict, *keys, default=""):
    """Try multiple key names, return first non-None/non-empty value."""
    for key in keys:
        val = d.get(key)
        if val is not None and val != "":
            return val
    return default


def _flex_get_obj(d: dict, *keys):
    """Like _flex_get but returns None instead of empty string default."""
    for key in keys:
        val = d.get(key)
        if val is not None and val != "":
            return val
    return None


def _nested_get(d: dict, *keys):
    """Walk nested dicts by key sequence. Returns None if any step fails."""
    current: Any = d
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _as_text(node) -> str:
    """Recursively extract text from a nested message structure."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "\n".join(_as_text(item) for item in node)
    if isinstance(node, dict):
        for key in ("text", "content", "message", "data", "value"):
            if key in node:
                result = _as_text(node[key])
                if result:
                    return result
        return json.dumps(node)
    return str(node)


def _extract_user_prompt(user_input) -> str:
    """Extract the last user message from input-messages."""
    if isinstance(user_input, list):
        for msg in reversed(user_input):
            if isinstance(msg, dict) and msg.get("role") == "user":
                text = _as_text(msg.get("content", ""))
                if text:
                    return text
        for msg in reversed(user_input):
            if isinstance(msg, str) and msg:
                return msg
        return ""
    if isinstance(user_input, str):
        return user_input
    return str(user_input) if user_input else ""


# ---------------------------------------------------------------------------
# Token enrichment
# ---------------------------------------------------------------------------


def _find_token_usage(input_json: dict):
    """Search for token usage dict in multiple payload locations."""
    usage_keys = ("token_usage", "token-usage", "usage")
    search_locations = [
        input_json,
        _flex_get_obj(input_json, "last-assistant-message", "last_assistant_message", "lastAssistantMessage"),
        _nested_get(input_json, "last-assistant-message", "message"),
    ]
    for obj in search_locations:
        if not isinstance(obj, dict):
            continue
        for key in usage_keys:
            val = obj.get(key)
            if isinstance(val, dict):
                return val
    return None


def _extract_token_counts(usage: dict) -> dict:
    """Extract prompt/completion/total counts, trying multiple key variants."""

    def pick_first(*keys):
        for k in keys:
            val = usage.get(k)
            if val is not None:
                try:
                    return int(val)
                except (ValueError, TypeError):
                    pass
        return None

    prompt = pick_first(
        "prompt_tokens",
        "input_tokens",
        "promptTokens",
        "inputTokens",
        "prompt",
        "input",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
    )
    completion = pick_first(
        "completion_tokens",
        "output_tokens",
        "completionTokens",
        "outputTokens",
        "completion",
        "output",
    )
    total = pick_first(
        "total_tokens",
        "totalTokens",
        "tokens",
        "token_count",
        "overall",
        "sum",
    )
    if total is None and prompt is not None and completion is not None:
        total = prompt + completion

    return {"prompt": prompt, "completion": completion, "total": total}


# ---------------------------------------------------------------------------
# Codex rollout JSONL parser -- token-usage source
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


def _read_tokens_from_rollout(
    session_id: str,
    turn_id: str,
    rollout_path: "Path | None" = None,
) -> "dict | None":
    """Extract a turn's token usage from Codex's session rollout JSONL.

    The rollout contains structured `event_msg` records. We locate the
    `task_started` event for our `turn_id`, then accumulate `last_token_usage`
    from every `token_count` event up to the next `task_started` (or EOF).
    This sums all LLM calls that ran during the turn.

    Returns a dict shaped like `notify`'s expected token_usage (with
    `prompt_tokens`, `completion_tokens`, `total_tokens`, plus the raw Codex
    keys), or None if no matching turn or no token events are found.
    """
    if not session_id or not turn_id:
        return None
    path = rollout_path if rollout_path is not None else _find_rollout_file(session_id)
    if path is None or not path.is_file():
        return None

    fields = (
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "cached_input_tokens",
        "non_cached_input_tokens",
        "reasoning_output_tokens",
    )
    sums: dict = {k: 0 for k in fields}
    saw_any = False
    in_turn = False

    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") != "event_msg":
                    continue
                payload = obj.get("payload") or {}
                ptype = payload.get("type")
                if ptype == "task_started":
                    if payload.get("turn_id") == turn_id:
                        in_turn = True
                    elif in_turn:
                        # Reached the next turn; stop accumulating.
                        break
                elif ptype == "token_count" and in_turn:
                    last = (payload.get("info") or {}).get("last_token_usage") or {}
                    for k in fields:
                        v = last.get(k)
                        if isinstance(v, int):
                            sums[k] += v
                            saw_any = True
    except OSError:
        return None

    if not saw_any:
        return None

    return {
        "prompt_tokens": sums["input_tokens"] or None,
        "completion_tokens": sums["output_tokens"] or None,
        "total_tokens": sums["total_tokens"] or None,
        "cached_input_tokens": sums["cached_input_tokens"],
        "non_cached_input_tokens": sums["non_cached_input_tokens"],
        "reasoning_output_tokens": sums["reasoning_output_tokens"],
        "model": "",
    }


# ---------------------------------------------------------------------------
# Span sending
# ---------------------------------------------------------------------------


def _send_span(payload: dict) -> None:
    """Send the completed span payload directly to the configured backend."""
    if not send_span_to_backend(payload):
        error("Failed to send span to backend")


# ---------------------------------------------------------------------------
# Fallback: legacy single-span path
# ---------------------------------------------------------------------------


def _send_legacy_single_span(
    state,
    thread_id: str,
    turn_id: str,
    user_prompt: str,
    assistant_output: str,
    usage,
) -> None:
    """Build a single LLM span from the notify payload alone.

    Used when Codex lifecycle hooks haven't been trusted yet (`/hooks` not
    approved). Once Stop is wired up, this path is unused for that session.
    """
    state.increment("trace_count")
    trace_count = state.get("trace_count")
    session_id = state.get("session_id")
    project_name = state.get("project_name")
    user_id = state.get("user_id")

    trace_id = generate_trace_id()
    span_id = generate_span_id()
    start_time = get_timestamp_ms()
    end_time = start_time

    output_messages = [{"message.role": "assistant", "message.content": assistant_output}]

    attrs = {
        "session.id": session_id,
        "trace.number": trace_count,
        "project.name": project_name,
        "openinference.span.kind": "LLM",
        "input.value": user_prompt,
        "output.value": assistant_output,
        "codex.turn_id": turn_id,
        "codex.thread_id": thread_id,
        "llm.output_messages": json.dumps(output_messages),
    }
    if user_id:
        attrs["user.id"] = user_id

    if usage:
        attrs["codex.token_usage"] = json.dumps(usage)
        counts = _extract_token_counts(usage)
        if counts["prompt"] is not None:
            attrs["llm.token_count.prompt"] = counts["prompt"]
        if counts["completion"] is not None:
            attrs["llm.token_count.completion"] = counts["completion"]
        if counts["total"] is not None:
            attrs["llm.token_count.total"] = counts["total"]

    parent_span = build_span(
        f"Turn {trace_count}",
        "LLM",
        span_id,
        trace_id,
        "",
        start_time,
        end_time,
        attrs,
        SERVICE_NAME,
        SCOPE_NAME,
    )
    debug_dump(f"notify_{thread_id or 'unknown'}_{turn_id or 'unknown'}_span", parent_span)
    _send_span(parent_span)

    log(f"Turn {trace_count} sent via legacy notify path (thread={thread_id}, turn={turn_id})")

    try:
        tc = int(trace_count or "0")
    except (ValueError, TypeError):
        tc = 0
    if tc % 10 == 0:
        gc_stale_state_files()


# ---------------------------------------------------------------------------
# Shared finalize: builds and ships the multi-span tree for a turn
# ---------------------------------------------------------------------------


def _parse_int(v) -> "int | None":
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _finalize_turn_and_ship(
    state,
    thread_id: str,
    assistant_output: "str | None" = None,
    token_usage: "dict | None" = None,
    turn_id: str = "",
) -> None:
    """Build and ship the full multi-span tree for a completed turn.

    Reads ``pending_trace_id`` and ``pending_parent_span_id`` from state — set by
    ``UserPromptSubmit``. Reads turn-scoped state (``turn_start_ms``,
    ``user_prompt``, ``model``, etc.) plus the tool-span JSONL. Builds parent
    LLM span + TOOL children, ships them together, then clears turn-scoped
    state and deletes the JSONL.

    Called by:
      - ``_handle_notify`` when ``agent-turn-complete`` fires (with payload data)
      - ``session._finalize_pending_turn`` for orphaned turns (no payload data)

    Falls back to ``_send_legacy_single_span`` when ``pending_trace_id`` isn't
    set — this covers first-run users who haven't approved ``/hooks`` yet, so
    a notify-only signal still emits a single LLM span.
    """
    pending_trace_id = state.get("pending_trace_id")
    pending_parent_span_id = state.get("pending_parent_span_id")

    if not pending_trace_id or not pending_parent_span_id:
        _send_legacy_single_span(
            state,
            thread_id,
            turn_id,
            state.get("user_prompt") or "",
            assistant_output or "(No response)",
            token_usage,
        )
        return

    state.increment("trace_count")
    new_trace_count = state.get("trace_count") or "0"

    session_id = state.get("session_id") or thread_id
    project_name = state.get("project_name") or ""
    user_id = state.get("user_id")
    model = state.get("model")
    permission_mode = state.get("permission_mode")
    sandbox_mode = state.get("sandbox_mode")
    turn_start_ms = state.get("turn_start_ms")
    user_prompt = state.get("user_prompt") or ""

    rows = span_buffer.read_all(thread_id)
    tool_entries = span_buffer.join_by_call_id(rows)

    start_time = _parse_int(turn_start_ms)
    if start_time is None:
        start_time = get_timestamp_ms()
    end_time = get_timestamp_ms()

    final_output = assistant_output or "(No response)"

    attrs: dict = {
        "session.id": session_id,
        "trace.number": new_trace_count,
        "project.name": project_name,
        "openinference.span.kind": "LLM",
        "input.value": user_prompt,
        "output.value": final_output,
        "codex.thread_id": thread_id,
    }
    if turn_id:
        attrs["codex.turn_id"] = turn_id
    if user_id:
        attrs["user.id"] = user_id
    if model:
        attrs["llm.model_name"] = model
    if permission_mode:
        attrs["codex.approval_mode"] = permission_mode
    if sandbox_mode:
        attrs["codex.sandbox_mode"] = sandbox_mode
    if assistant_output:
        attrs["llm.output_messages"] = json.dumps([{"message.role": "assistant", "message.content": assistant_output}])

    if token_usage:
        counts = _extract_token_counts(token_usage)
        if counts["prompt"] is not None:
            attrs["llm.token_count.prompt"] = counts["prompt"]
        if counts["completion"] is not None:
            attrs["llm.token_count.completion"] = counts["completion"]
        if counts["total"] is not None:
            attrs["llm.token_count.total"] = counts["total"]
        if "llm.model_name" not in attrs and token_usage.get("model"):
            attrs["llm.model_name"] = token_usage["model"]
        attrs["codex.token_usage"] = json.dumps(token_usage)

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
            pending_trace_id,
            pending_parent_span_id,
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
        pending_parent_span_id,
        pending_trace_id,
        "",
        start_time,
        end_time,
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

    _send_span(payload)
    log(f"Turn {new_trace_count} sent (thread={thread_id}, children={len(child_spans)})")

    for key in ("turn_start_ms", "user_prompt", "pending_trace_id", "pending_parent_span_id"):
        state.delete(key)
    span_buffer.delete(thread_id)

    try:
        tc = int(new_trace_count or "0")
    except (ValueError, TypeError):
        tc = 0
    if tc % 10 == 0:
        gc_stale_state_files()


# ---------------------------------------------------------------------------
# Main handler
# ---------------------------------------------------------------------------


def _handle_notify(input_json: dict) -> None:
    """Notify handler: builds and ships the full span tree for the completed turn.

    With the deferred-parent architecture, notify is the single point that
    emits parent + tool child spans together. Stop's hook is a no-op now;
    UserPromptSubmit pre-generates the trace/parent IDs so this handler can
    reference them.
    """
    if input_json.get("type") != "agent-turn-complete":
        log(f"Ignoring event type: {input_json.get('type')}")
        return

    thread_id = _flex_get(input_json, "thread-id", "thread_id", "threadId")
    turn_id = _flex_get(input_json, "turn-id", "turn_id", "turnId")
    cwd = _flex_get(input_json, "cwd", "working-directory", "working_directory")
    assistant_msg = _flex_get_obj(
        input_json, "last-assistant-message", "last_assistant_message", "lastAssistantMessage"
    )

    debug_prefix = f"notify_{thread_id or 'unknown'}_{turn_id or 'unknown'}"
    debug_dump(f"{debug_prefix}_raw", input_json)

    assistant_output = redact_content(env.log_prompts, _as_text(assistant_msg)) or "(No response)"
    debug_dump(f"{debug_prefix}_text", {"assistant": assistant_output})

    state = resolve_session(thread_id)
    ensure_session_initialized(state, thread_id, cwd or os.getcwd())

    usage = _find_token_usage(input_json)
    if not usage:
        # Codex's notify payload doesn't carry token counts; recover them from
        # the session rollout JSONL by summing the turn's `last_token_usage`
        # events.
        usage = _read_tokens_from_rollout(thread_id, turn_id)
        if usage:
            debug_dump(f"{debug_prefix}_tokens_from_rollout", usage)

    _finalize_turn_and_ship(
        state,
        thread_id,
        assistant_output=assistant_output,
        token_usage=usage,
        turn_id=turn_id,
    )


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def notify():
    """Entry point for arize-hook-codex-notify.

    Input contract: JSON as sys.argv[1] (NOT stdin -- Codex passes notify
    JSON as a CLI arg). No stdout output -- Codex doesn't expect a response.
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
