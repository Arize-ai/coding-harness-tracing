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
from tracing.codex.hooks.adapter import (
    SCOPE_NAME,
    SERVICE_NAME,
    check_requirements,
    ensure_session_initialized,
    gc_stale_state_files,
    load_env_file,
    resolve_session,
)

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
# Main handler
# ---------------------------------------------------------------------------


def _handle_notify(input_json: dict) -> None:
    """Slim notify handler: writes token usage + last assistant message to state.

    When Codex hooks are active (SessionStart/UserPromptSubmit have run), this
    just records the notify payload into state and lets the Stop hook build
    the span tree. When hooks are not yet trusted, falls back to a single-span
    send so first-run users still get traces.
    """
    # Phase 1: only handle agent-turn-complete
    if input_json.get("type") != "agent-turn-complete":
        log(f"Ignoring event type: {input_json.get('type')}")
        return

    # Phase 2: parse the payload
    thread_id = _flex_get(input_json, "thread-id", "thread_id", "threadId")
    turn_id = _flex_get(input_json, "turn-id", "turn_id", "turnId")
    cwd = _flex_get(input_json, "cwd", "working-directory", "working_directory")
    user_input = _flex_get_obj(input_json, "input-messages", "input_messages", "inputMessages")
    assistant_msg = _flex_get_obj(
        input_json, "last-assistant-message", "last_assistant_message", "lastAssistantMessage"
    )

    debug_prefix = f"notify_{thread_id or 'unknown'}_{turn_id or 'unknown'}"
    debug_dump(f"{debug_prefix}_raw", input_json)

    user_prompt = redact_content(env.log_prompts, _extract_user_prompt(user_input))
    assistant_output = redact_content(env.log_prompts, _as_text(assistant_msg)) or "(No response)"

    debug_dump(f"{debug_prefix}_text", {"input": user_prompt, "assistant": assistant_output})

    state = resolve_session(thread_id)
    ensure_session_initialized(state, thread_id, cwd or os.getcwd())

    # Phase 3: detect whether lifecycle hooks have already populated state.
    # Hook entry points write `model` (SessionStart) and `turn_start_ms`
    # (UserPromptSubmit). Either being present means hooks are active and
    # Stop will build the span — notify just stages data into state.
    hooks_active = bool(state.get("model")) or bool(state.get("turn_start_ms"))

    usage = _find_token_usage(input_json)
    if usage:
        counts = _extract_token_counts(usage)
        pending = {
            "prompt_tokens": counts["prompt"],
            "completion_tokens": counts["completion"],
            "total_tokens": counts["total"],
            "model": usage.get("model") or "",
        }
        state.set("pending_token_usage", json.dumps(pending))
        debug_dump(f"{debug_prefix}_token_usage", usage)
    if assistant_output:
        state.set("last_assistant_message", assistant_output)

    # Phase 4: fall through to legacy single-span path when hooks aren't
    # active yet, so first-run users still get traces before approving /hooks.
    if not hooks_active:
        _send_legacy_single_span(state, thread_id, turn_id, user_prompt, assistant_output, usage)


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
