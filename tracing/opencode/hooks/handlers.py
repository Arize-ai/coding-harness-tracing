#!/usr/bin/env python3
"""opencode hook handlers — snapshot reconciler.

opencode is in-process: the TypeScript plugin shim pulls authoritative
session snapshots via the SDK on lifecycle events and pipes them to this
Python entry point as a single JSON payload.

Payload shape:
    { "type": "reconcile" | "close",
      "sessionID": "ses_xyz",
      "messages": [ { "info": <Message>, "parts": [ <Part>, ... ] }, ... ] }

`reconcile` emits any newly-completed LLM and TOOL child spans deduped by
message ID and tool callID. `close` does the same and then emits the
Turn CHAIN root for the pending turn.
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any, Optional

from core.common import (
    StateManager,
    build_span,
    debug_dump,
    env,
    error,
    generate_span_id,
    generate_trace_id,
    get_timestamp_ms,
    log,
    redact_content,
    send_span,
)
from tracing.opencode.hooks.adapter import (
    SCOPE_NAME,
    SERVICE_NAME,
    check_requirements,
    ensure_session_initialized,
    resolve_session,
)

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _read_stdin() -> dict:
    """Read JSON from stdin. Returns {} on empty/invalid input."""
    try:
        raw = sys.stdin.read()
        if not raw:
            return {}
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError, ValueError):
        return {}


def _send_span_async(span_dict: dict) -> None:
    """Send a span without blocking the host. Double-fork detached unless
    ARIZE_DISABLE_FORK=true (tests) or fork() is unavailable (Windows)."""
    if os.environ.get("ARIZE_DISABLE_FORK", "").lower() == "true":
        send_span(span_dict)
        return
    if not hasattr(os, "fork"):
        send_span(span_dict)
        return

    try:
        pid = os.fork()
    except OSError:
        send_span(span_dict)
        return

    if pid > 0:
        try:
            os.waitpid(pid, 0)
        except OSError:
            # Best-effort detach: failure to reap here must not impact host flow.
            pass
        return

    try:
        if os.fork() > 0:
            os._exit(0)
    except OSError:
        os._exit(0)

    try:
        devnull = os.open(os.devnull, os.O_RDWR)
        for fd in (0, 1, 2):
            try:
                os.dup2(devnull, fd)
            except OSError:
                # Best-effort stdio redirection in detached child; continue if remap fails.
                pass
        os.close(devnull)
    except OSError:
        # Best-effort stdio detachment; if this fails, continue and still emit the span.
        pass
    try:
        send_span(span_dict)
    except Exception as _exc:
        # Intentionally suppress all errors in the detached child process:
        # span export is best-effort and must never impact the host process.
        pass
    os._exit(0)


# ---------------------------------------------------------------------------
# Snapshot helpers
# ---------------------------------------------------------------------------


def _text_of(parts: list) -> str:
    """Concatenate the text of all TextPart entries in `parts`."""
    chunks = []
    for p in parts or []:
        if not isinstance(p, dict):
            continue
        if p.get("type") == "text":
            t = p.get("text") or ""
            if t:
                chunks.append(t)
    return "".join(chunks)


def _tool_parts(parts: list) -> list:
    return [p for p in (parts or []) if isinstance(p, dict) and p.get("type") == "tool"]


# ---------------------------------------------------------------------------
# Turn open / fail-safe close
# ---------------------------------------------------------------------------


def _close_pending_turn(state: StateManager, reason: str = "(closed by reconcile fail-safe)") -> None:
    """Emit a CHAIN root for any pending turn, then clear trace state.

    Called when a new user message arrives while a prior turn is still open
    (idle never fired — crash, abort, etc.). Ensures child LLM/TOOL spans
    always end up with a root span.
    """
    pending_trace_id = state.get("current_trace_id")
    pending_span_id = state.get("current_trace_span_id")
    if not pending_trace_id or not pending_span_id:
        return

    session_id = state.get("session_id") or ""
    project_name = state.get("project_name") or ""
    user_id = state.get("user_id") or ""
    start_time = state.get("current_trace_start_time") or str(get_timestamp_ms())
    prompt = state.get("current_trace_prompt") or ""
    pending_uid = state.get("current_user_message_id") or ""

    attrs: dict[str, Any] = {
        "session.id": session_id,
        "openinference.span.kind": "CHAIN",
        "project.name": project_name,
        "input.value": redact_content(env.log_prompts, prompt),
        "output.value": reason,
    }
    if user_id:
        attrs["user.id"] = user_id

    span = build_span(
        "Turn",
        "CHAIN",
        pending_span_id,
        pending_trace_id,
        "",
        start_time,
        str(get_timestamp_ms()),
        attrs,
        SERVICE_NAME,
        SCOPE_NAME,
    )
    _send_span_async(span)

    if pending_uid:
        state.set(f"closed_user_{pending_uid}", "1")

    state.delete("current_trace_id")
    state.delete("current_trace_span_id")
    state.delete("current_trace_start_time")
    state.delete("current_trace_prompt")
    state.delete("current_user_message_id")


def _open_turn_if_new(state: StateManager, user_info: dict, user_parts: list) -> None:
    """Open a new turn keyed by the user message id. No-op if already open or
    already closed (the SDK returns full session history on every snapshot,
    so prior-turn user messages reappear and must not re-open closed turns)."""
    uid = user_info.get("id") or ""
    if not uid:
        return

    if state.get(f"closed_user_{uid}") is not None:
        return

    if state.get("current_user_message_id") == uid:
        return

    # A different user message id with a prior turn still set -> force close.
    if state.get("current_trace_id"):
        _close_pending_turn(state)

    state.set("current_user_message_id", uid)
    state.set("current_trace_id", generate_trace_id())
    state.set("current_trace_span_id", generate_span_id())

    t_created = (user_info.get("time") or {}).get("created")
    start_ms = str(t_created) if t_created is not None else str(get_timestamp_ms())
    state.set("current_trace_start_time", start_ms)
    state.set("current_trace_prompt", _text_of(user_parts))
    state.increment("trace_count")


# ---------------------------------------------------------------------------
# Per-tool specialized attribute mapping (opencode tool names + arg keys)
# ---------------------------------------------------------------------------


def _per_tool_attrs(tool_name: str, tool_input: dict) -> dict:
    """Return per-tool specialized attrs. Values returned RAW (not redacted)."""
    out: dict[str, str] = {}
    if not isinstance(tool_input, dict):
        return out
    if tool_name == "bash":
        cmd = tool_input.get("command") or ""
        if cmd:
            out["tool.command"] = str(cmd)
    elif tool_name in ("read", "edit", "write"):
        fp = tool_input.get("filePath") or ""
        if fp:
            out["tool.file_path"] = str(fp)
    elif tool_name == "grep":
        q = tool_input.get("pattern") or ""
        if q:
            out["tool.query"] = str(q)
    elif tool_name == "glob":
        q = tool_input.get("pattern") or ""
        if q:
            out["tool.query"] = str(q)
    elif tool_name == "webfetch":
        url = tool_input.get("url") or ""
        if url:
            out["tool.url"] = str(url)
    return out


# ---------------------------------------------------------------------------
# Emit LLM span for assistant message
# ---------------------------------------------------------------------------


def _emit_llm_span(state: StateManager, info: dict, parts: list) -> None:
    msg_id = info.get("id") or ""
    if not msg_id:
        return
    if state.get(f"emitted_msg_{msg_id}") is not None:
        return

    trace_id = state.get("current_trace_id")
    parent_span_id = state.get("current_trace_span_id")
    if not trace_id or not parent_span_id:
        return

    session_id = state.get("session_id") or ""
    project_name = state.get("project_name") or ""
    user_id = state.get("user_id") or ""

    model_id = info.get("modelID") or ""
    provider_id = info.get("providerID") or ""
    tokens = info.get("tokens") or {}
    cache = tokens.get("cache") or {}
    try:
        input_tokens = int(tokens.get("input") or 0)
    except (TypeError, ValueError):
        input_tokens = 0
    try:
        output_tokens = int(tokens.get("output") or 0)
    except (TypeError, ValueError):
        output_tokens = 0
    try:
        reasoning_tokens = int(tokens.get("reasoning") or 0)
    except (TypeError, ValueError):
        reasoning_tokens = 0
    try:
        cache_read = int(cache.get("read") or 0)
    except (TypeError, ValueError):
        cache_read = 0
    try:
        cache_write = int(cache.get("write") or 0)
    except (TypeError, ValueError):
        cache_write = 0
    try:
        cost = float(info.get("cost") or 0)
    except (TypeError, ValueError):
        cost = 0.0

    time_block = info.get("time") or {}
    start_ms = time_block.get("created")
    end_ms = time_block.get("completed")
    if start_ms is None:
        start_ms = get_timestamp_ms()
    if end_ms is None:
        end_ms = start_ms

    prompt = state.get("current_trace_prompt") or ""
    output_text = _text_of(parts)

    # OpenInference: ``prompt`` is the total prompt and the cache buckets are
    # reported as ``prompt_details.*`` subsets of it. OpenCode's ``tokens.input``
    # is the fresh/uncached input only (cache reads/writes are tracked as separate
    # disjoint buckets — see OpenCode's own cost formula), so the total prompt is
    # input + cache_read + cache_write.
    prompt_tokens = input_tokens + cache_read + cache_write

    attrs: dict[str, Any] = {
        "session.id": session_id,
        "project.name": project_name,
        "openinference.span.kind": "LLM",
        "llm.model_name": model_id,
        "llm.provider": provider_id,
        "llm.token_count.prompt": prompt_tokens,
        "llm.token_count.completion": output_tokens,
        "llm.token_count.total": prompt_tokens + output_tokens,
        "input.value": redact_content(env.log_prompts, prompt),
        "output.value": redact_content(env.log_prompts, output_text),
    }
    if reasoning_tokens:
        attrs["llm.token_count.completion_details.reasoning"] = reasoning_tokens
    if cache_read:
        attrs["llm.token_count.prompt_details.cache_read"] = cache_read
    if cache_write:
        attrs["llm.token_count.prompt_details.cache_write"] = cache_write
    if cost:
        attrs["llm.cost"] = cost
    if user_id:
        attrs["user.id"] = user_id

    span_name = f"LLM: {model_id}" if model_id else "LLM"
    span = build_span(
        span_name,
        "LLM",
        generate_span_id(),
        trace_id,
        parent_span_id,
        start_ms,
        end_ms,
        attrs,
        SERVICE_NAME,
        SCOPE_NAME,
    )
    _send_span_async(span)
    state.set(f"emitted_msg_{msg_id}", "1")


# ---------------------------------------------------------------------------
# Emit TOOL spans for completed/error tool parts
# ---------------------------------------------------------------------------


def _emit_tool_span(state: StateManager, tool_part: dict) -> None:
    call_id = tool_part.get("callID") or ""
    if not call_id:
        return
    if state.get(f"emitted_tool_{call_id}") is not None:
        return

    tstate = tool_part.get("state") or {}
    status = tstate.get("status") or ""
    if status not in ("completed", "error"):
        return

    trace_id = state.get("current_trace_id")
    parent_span_id = state.get("current_trace_span_id")
    if not trace_id or not parent_span_id:
        return

    session_id = state.get("session_id") or ""
    project_name = state.get("project_name") or ""
    user_id = state.get("user_id") or ""

    tool_name = tool_part.get("tool") or "unknown"
    tool_input = tstate.get("input") or {}

    time_block = tstate.get("time") or {}
    start_ms = time_block.get("start")
    end_ms = time_block.get("end")
    if start_ms is None:
        start_ms = get_timestamp_ms()
    if end_ms is None:
        end_ms = start_ms

    is_error = status == "error"
    if is_error:
        output_raw = tstate.get("error") or ""
    else:
        output_raw = tstate.get("output") or ""
    title_raw = tstate.get("title") or ""

    input_json_str = json.dumps(tool_input) if isinstance(tool_input, (dict, list)) else str(tool_input)

    specialized = _per_tool_attrs(tool_name, tool_input if isinstance(tool_input, dict) else {})

    attrs: dict[str, Any] = {
        "session.id": session_id,
        "project.name": project_name,
        "openinference.span.kind": "TOOL",
        "tool.name": tool_name,
        "input.value": redact_content(env.log_tool_content, input_json_str),
        "output.value": redact_content(env.log_tool_content, output_raw),
    }
    if title_raw:
        attrs["tool.description"] = redact_content(env.log_tool_details, title_raw)
    for k, v in specialized.items():
        attrs[k] = redact_content(env.log_tool_details, v)
    if user_id:
        attrs["user.id"] = user_id

    status_code = 2 if is_error else 1
    status_message = redact_content(env.log_tool_content, output_raw) if is_error else ""

    span = build_span(
        tool_name,
        "TOOL",
        generate_span_id(),
        trace_id,
        parent_span_id,
        start_ms,
        end_ms,
        attrs,
        SERVICE_NAME,
        SCOPE_NAME,
        status_code=status_code,
        status_message=status_message,
    )
    _send_span_async(span)
    state.set(f"emitted_tool_{call_id}", "1")
    state.increment("tool_count")


# ---------------------------------------------------------------------------
# Core reconcile walk
# ---------------------------------------------------------------------------


def _reconcile_messages(state: StateManager, messages: list) -> Optional[dict]:
    """Walk messages: open turn, emit LLM + TOOL spans. Returns final
    assistant message info dict (for the Turn CHAIN output), or None."""
    final_assistant_info: Optional[dict] = None
    final_assistant_parts: Optional[list] = None

    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        info = msg.get("info") or {}
        parts = msg.get("parts") or []
        role = info.get("role")

        if role == "user":
            _open_turn_if_new(state, info, parts)
        elif role == "assistant":
            # Only emit LLM if the message completed.
            completed = (info.get("time") or {}).get("completed")
            if completed is not None:
                _emit_llm_span(state, info, parts)
                # Track as the "final" assistant ONLY if it belongs to the
                # currently open turn — otherwise a stale assistant from a
                # closed turn replayed in the snapshot would poison the
                # output.value of the next Turn CHAIN.
                if info.get("parentID") == state.get("current_user_message_id"):
                    final_assistant_info = info
                    final_assistant_parts = parts
            # Always process the tool parts (they may be completed even if
            # the assistant message has not yet completed in some snapshots).
            for tp in _tool_parts(parts):
                _emit_tool_span(state, tp)

    if final_assistant_info is None:
        return None
    return {"info": final_assistant_info, "parts": final_assistant_parts or []}


# ---------------------------------------------------------------------------
# Top-level dispatch handlers
# ---------------------------------------------------------------------------


def _handle_reconcile(input_json: dict) -> None:
    """Process a reconcile snapshot: emit any new LLM/TOOL child spans."""
    debug_dump("opencode_reconcile", input_json)
    state = resolve_session(input_json)
    ensure_session_initialized(state, input_json)

    messages = input_json.get("messages") or []
    _reconcile_messages(state, messages)


def _handle_close(input_json: dict) -> None:
    """Process a close (session.idle) snapshot: reconcile, then emit Turn root."""
    debug_dump("opencode_close", input_json)
    state = resolve_session(input_json)
    ensure_session_initialized(state, input_json)

    messages = input_json.get("messages") or []
    final = _reconcile_messages(state, messages)

    trace_id = state.get("current_trace_id")
    span_id = state.get("current_trace_span_id")
    if not trace_id or not span_id:
        return

    session_id = state.get("session_id") or ""
    project_name = state.get("project_name") or ""
    user_id = state.get("user_id") or ""
    start_time = state.get("current_trace_start_time") or str(get_timestamp_ms())
    prompt = state.get("current_trace_prompt") or ""
    final_output = _text_of(final["parts"]) if final else ""

    attrs: dict[str, Any] = {
        "session.id": session_id,
        "openinference.span.kind": "CHAIN",
        "project.name": project_name,
        "input.value": redact_content(env.log_prompts, prompt),
        "output.value": redact_content(env.log_prompts, final_output),
    }
    if user_id:
        attrs["user.id"] = user_id

    span = build_span(
        "Turn",
        "CHAIN",
        span_id,
        trace_id,
        "",
        start_time,
        str(get_timestamp_ms()),
        attrs,
        SERVICE_NAME,
        SCOPE_NAME,
    )
    _send_span_async(span)

    pending_uid = state.get("current_user_message_id") or ""
    if pending_uid:
        state.set(f"closed_user_{pending_uid}", "1")

    state.delete("current_trace_id")
    state.delete("current_trace_span_id")
    state.delete("current_trace_start_time")
    state.delete("current_trace_prompt")
    state.delete("current_user_message_id")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Hook entry point. NEVER raises — always returns even on internal error."""
    try:
        if not check_requirements():
            return
        input_json = _read_stdin()
        if not isinstance(input_json, dict) or not input_json:
            return

        kind = input_json.get("type")
        if kind == "reconcile":
            try:
                _handle_reconcile(input_json)
            except Exception as exc:  # noqa: BLE001
                error(f"opencode reconcile failed: {exc!r}")
        elif kind == "close":
            try:
                _handle_close(input_json)
            except Exception as exc:  # noqa: BLE001
                error(f"opencode close failed: {exc!r}")
        else:
            log(f"opencode: unknown type {kind!r}")
    except Exception as exc:  # noqa: BLE001
        error(f"opencode main() crashed: {exc!r}")


if __name__ == "__main__":
    main()
