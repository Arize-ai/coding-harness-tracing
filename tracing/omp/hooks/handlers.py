#!/usr/bin/env python3
"""omp hook handlers: stateful event-forward tracing.

The TypeScript shim forwards each once-fired omp lifecycle event to this entry
point. This module keeps per-session trace state and emits Turn, LLM, and TOOL
spans without snapshot reconciliation or deduplication.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

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
from tracing.omp.hooks.adapter import (
    SCOPE_NAME,
    SERVICE_NAME,
    check_requirements,
    ensure_session_initialized,
    resolve_session,
)


def _read_stdin() -> dict:
    """Read JSON from stdin. Return {} on empty or invalid input."""
    try:
        raw = sys.stdin.read()
        if not raw:
            return {}
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError, ValueError):
        return {}


def _send_span_async(span_dict: dict) -> None:
    """Send a span without blocking the host process."""
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
            # Best-effort reap: failure here should not interrupt the caller.
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
                # Best-effort stdio detachment in child process; ignore per-fd dup failures.
                pass
        os.close(devnull)
    except OSError as exc:
        # Best-effort stdio detachment in forked child; ignore failures to avoid
        # impacting host execution, but emit debug context for diagnostics.
        debug_dump("send_span", {"event": "omp_stdio_detach_failed", "error": str(exc)})

    try:
        send_span(span_dict)
    except Exception:
        # Best-effort tracing in detached child: never propagate failures.
        pass
    os._exit(0)


def _assistant_text(message: Any) -> str:
    """Concatenate text content items from an AssistantMessage."""
    if not isinstance(message, dict):
        return ""
    chunks = []
    for item in message.get("content") or []:
        if isinstance(item, dict) and item.get("type") == "text":
            text = item.get("text") or ""
            if text:
                chunks.append(str(text))
    return "".join(chunks)


def _user_prompt(prompt_field: Any) -> str:
    """Return before_agent_start.prompt as-is when it is a string."""
    return prompt_field if isinstance(prompt_field, str) else ""


def _tool_calls(message: Any) -> dict:
    """Map ToolCall.id to its name and arguments."""
    calls: dict = {}
    if not isinstance(message, dict):
        return calls
    for item in message.get("content") or []:
        if not isinstance(item, dict) or item.get("type") != "toolCall":
            continue
        call_id = item.get("id") or ""
        if not call_id:
            continue
        arguments = item.get("arguments") or {}
        calls[call_id] = {
            "name": item.get("name") or "",
            "arguments": arguments if isinstance(arguments, dict) else {},
        }
    return calls


def _text_of_content(content_list: Any) -> str:
    """Concatenate text content items from a ToolResultMessage content list."""
    if not isinstance(content_list, list):
        return ""
    chunks = []
    for item in content_list:
        if isinstance(item, dict) and item.get("type") == "text":
            text = item.get("text") or ""
            if text:
                chunks.append(str(text))
    return "".join(chunks)


def _int_or_zero(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _float_or_zero(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _timestamp_or_now(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return get_timestamp_ms()


def _per_tool_attrs(tool_name: str, tool_input: Any) -> dict:
    """Return raw specialized tool attrs for known omp tools."""
    attrs: dict = {}
    if not isinstance(tool_input, dict):
        return attrs
    if tool_name == "bash":
        command = tool_input.get("command") or ""
        if command:
            attrs["tool.command"] = str(command)
    elif tool_name in ("read", "edit", "write"):
        file_path = tool_input.get("filePath") or tool_input.get("path") or ""
        if file_path:
            attrs["tool.file_path"] = str(file_path)
    elif tool_name in ("grep", "glob"):
        query = tool_input.get("pattern") or ""
        if query:
            attrs["tool.query"] = str(query)
    elif tool_name in ("webfetch", "web_fetch", "fetch"):
        url = tool_input.get("url") or ""
        if url:
            attrs["tool.url"] = str(url)
    return attrs


def _open_trace(state: StateManager, prompt: str) -> None:
    state.set("current_trace_id", generate_trace_id())
    state.set("current_trace_span_id", generate_span_id())
    state.set("current_trace_start_time", str(get_timestamp_ms()))
    state.set("current_trace_prompt", prompt)
    state.set("current_final_output", "")
    state.increment("trace_count")


def _clear_trace_state(state: StateManager) -> None:
    for key in (
        "current_trace_id",
        "current_trace_span_id",
        "current_trace_start_time",
        "current_trace_prompt",
        "current_final_output",
    ):
        state.delete(key)


def _emit_turn_root(state: StateManager, output_value: str) -> None:
    trace_id = state.get("current_trace_id")
    span_id = state.get("current_trace_span_id")
    if not trace_id or not span_id:
        return

    attrs: dict[str, Any] = {
        "session.id": state.get("session_id") or "",
        "project.name": state.get("project_name") or "",
        "openinference.span.kind": "CHAIN",
        "input.value": redact_content(env.log_prompts, state.get("current_trace_prompt") or ""),
        "output.value": output_value,
    }
    user_id = state.get("user_id") or ""
    if user_id:
        attrs["user.id"] = user_id

    span = build_span(
        "Turn",
        "CHAIN",
        span_id,
        trace_id,
        "",
        state.get("current_trace_start_time") or str(get_timestamp_ms()),
        str(get_timestamp_ms()),
        attrs,
        SERVICE_NAME,
        SCOPE_NAME,
    )
    _send_span_async(span)
    _clear_trace_state(state)


def _close_pending_turn(state: StateManager, reason: str = "(closed by fail-safe)") -> None:
    """Emit the pending Turn root, preferring the latest assistant text."""
    if not state.get("current_trace_id") or not state.get("current_trace_span_id"):
        return
    final_output = state.get("current_final_output") or ""
    output_value = redact_content(env.log_prompts, final_output) if final_output else reason
    _emit_turn_root(state, output_value)


def _emit_llm_span(state: StateManager, message: Any) -> None:
    if not isinstance(message, dict):
        return
    trace_id = state.get("current_trace_id")
    parent_span_id = state.get("current_trace_span_id")
    if not trace_id or not parent_span_id:
        return

    usage = message.get("usage") or {}
    if not isinstance(usage, dict):
        usage = {}
    cost_block = usage.get("cost") or {}
    if not isinstance(cost_block, dict):
        cost_block = {}

    input_tokens = _int_or_zero(usage.get("input"))
    output_tokens = _int_or_zero(usage.get("output"))
    cache_read = _int_or_zero(usage.get("cacheRead"))
    cache_write = _int_or_zero(usage.get("cacheWrite"))
    prompt_tokens = input_tokens + cache_read + cache_write
    reasoning_tokens = _int_or_zero(usage.get("reasoningTokens"))
    cost = _float_or_zero(cost_block.get("total"))

    end_ms = _timestamp_or_now(message.get("timestamp"))
    duration_ms = _int_or_zero(message.get("duration"))
    start_ms = end_ms - duration_ms if duration_ms else end_ms

    model = message.get("model") or ""
    output_text = _assistant_text(message)
    attrs: dict[str, Any] = {
        "session.id": state.get("session_id") or "",
        "project.name": state.get("project_name") or "",
        "openinference.span.kind": "LLM",
        "llm.model_name": model,
        "llm.provider": message.get("provider") or "",
        "llm.token_count.prompt": prompt_tokens,
        "llm.token_count.completion": output_tokens,
        "llm.token_count.total": prompt_tokens + output_tokens,
        "input.value": redact_content(env.log_prompts, state.get("current_trace_prompt") or ""),
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

    user_id = state.get("user_id") or ""
    if user_id:
        attrs["user.id"] = user_id

    span = build_span(
        f"LLM: {model}" if model else "LLM",
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
    state.set("current_final_output", output_text)


def _emit_tool_span(state: StateManager, tool_result: dict, calls: dict) -> None:
    trace_id = state.get("current_trace_id")
    parent_span_id = state.get("current_trace_span_id")
    if not trace_id or not parent_span_id:
        return

    tool_name = tool_result.get("toolName") or "unknown"
    call_id = tool_result.get("toolCallId") or ""
    arguments = calls.get(call_id, {}).get("arguments", {})
    if not isinstance(arguments, dict):
        arguments = {}

    input_text = json.dumps(arguments)
    output_text = _text_of_content(tool_result.get("content"))
    attrs: dict[str, Any] = {
        "session.id": state.get("session_id") or "",
        "project.name": state.get("project_name") or "",
        "openinference.span.kind": "TOOL",
        "tool.name": tool_name,
        "input.value": redact_content(env.log_tool_content, input_text),
        "output.value": redact_content(env.log_tool_content, output_text),
    }
    for key, value in _per_tool_attrs(tool_name, arguments).items():
        attrs[key] = redact_content(env.log_tool_details, value)

    user_id = state.get("user_id") or ""
    if user_id:
        attrs["user.id"] = user_id

    is_error = bool(tool_result.get("isError"))
    status_message = redact_content(env.log_tool_content, output_text) if is_error else ""
    timestamp = _timestamp_or_now(tool_result.get("timestamp"))
    span = build_span(
        tool_name,
        "TOOL",
        generate_span_id(),
        trace_id,
        parent_span_id,
        timestamp,
        timestamp,
        attrs,
        SERVICE_NAME,
        SCOPE_NAME,
        status_code=2 if is_error else 1,
        status_message=status_message,
    )
    _send_span_async(span)
    state.increment("tool_count")


def _handle_before_agent_start(input_json: dict) -> None:
    debug_dump("omp_before_agent_start", input_json)
    state = resolve_session(input_json)
    ensure_session_initialized(state, input_json)
    if state.get("current_trace_id"):
        _close_pending_turn(state)
    _open_trace(state, _user_prompt(input_json.get("prompt")))


def _handle_turn_end(input_json: dict) -> None:
    debug_dump("omp_turn_end", input_json)
    state = resolve_session(input_json)
    ensure_session_initialized(state, input_json)
    if not state.get("current_trace_id"):
        _open_trace(state, "")

    message = input_json.get("message")
    if not isinstance(message, dict):
        message = {}
    _emit_llm_span(state, message)

    calls = _tool_calls(message)
    for tool_result in input_json.get("toolResults") or []:
        if isinstance(tool_result, dict):
            _emit_tool_span(state, tool_result, calls)


def _handle_agent_end(input_json: dict) -> None:
    debug_dump("omp_agent_end", input_json)
    state = resolve_session(input_json)
    ensure_session_initialized(state, input_json)
    if not state.get("current_trace_id") or not state.get("current_trace_span_id"):
        return

    # Prefer the final assistant text from agent_end's OWN payload. Each omp
    # lifecycle event is forwarded to a separate, detached Python process, so
    # the final turn_end and this agent_end race on the shared state file:
    # current_final_output may still hold a prior turn's text when this process
    # reads it. agent_end.messages is self-contained and authoritative — the
    # last assistant message is the run's final answer. Fall back to the state
    # accumulator only when the payload carries no assistant text.
    final_output = ""
    messages = input_json.get("messages") or []
    if isinstance(messages, list):
        for message in reversed(messages):
            if isinstance(message, dict) and message.get("role") == "assistant":
                text = _assistant_text(message)
                if text:
                    final_output = text
                    break
    if not final_output:
        final_output = state.get("current_final_output") or ""

    _emit_turn_root(state, redact_content(env.log_prompts, final_output))


def _handle_session_shutdown(input_json: dict) -> None:
    debug_dump("omp_session_shutdown", input_json)
    state = resolve_session(input_json)
    ensure_session_initialized(state, input_json)
    _close_pending_turn(state)


def main() -> None:
    """Hook entry point. Never raises."""
    try:
        if not check_requirements():
            return
        input_json = _read_stdin()
        if not input_json:
            return

        kind = input_json.get("type")
        try:
            if kind == "before_agent_start":
                _handle_before_agent_start(input_json)
            elif kind == "turn_end":
                _handle_turn_end(input_json)
            elif kind == "agent_end":
                _handle_agent_end(input_json)
            elif kind == "session_shutdown":
                _handle_session_shutdown(input_json)
            else:
                log(f"omp: unknown type {kind!r}")
        except Exception as exc:
            error(f"omp {kind!r} failed: {exc!r}")
    except Exception as exc:
        error(f"omp main() crashed: {exc!r}")


if __name__ == "__main__":
    main()
