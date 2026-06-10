#!/usr/bin/env python3
"""Copilot hook handlers. One exported function per hook event.

Each entry point reads stdin JSON (snake_case schema), resolves session state,
and delegates to the corresponding _handle_* implementation.
"""
import json
import sys

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
    send_span,
)
from tracing.copilot.hooks.adapter import (
    SCOPE_NAME,
    SERVICE_NAME,
    check_requirements,
    ensure_session_initialized,
    gc_stale_state_files,
    resolve_session,
)
from tracing.copilot.hooks.transcript import parse_transcript

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _read_stdin(event: str) -> dict:
    """Read JSON from stdin. Returns {} on empty/invalid input.

    When ARIZE_TRACE_DEBUG=true, the parsed payload is written to
    ~/.arize/harness/state/debug/copilot_<event>_<ts>.json so we can
    inspect the actual field schema Copilot is sending.
    """
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw else {}
    except (json.JSONDecodeError, OSError):
        data = {}
    debug_dump(f"copilot_{event}", data)
    return data


def _print_response(event: str) -> None:
    """Print the hook's stdout response.

    PreToolUse must emit a permission decision; all other events emit a
    ``{"continue": true}`` marker so the agent does not block.
    """
    if event == "PreToolUse":
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "allow",
                    }
                }
            )
        )
    else:
        print(json.dumps({"continue": True}))


# ---------------------------------------------------------------------------
# Internal handler implementations
# ---------------------------------------------------------------------------


def _handle_session_start(input_json: dict) -> None:
    """Handle session start: initialize session."""
    state = resolve_session(input_json)
    ensure_session_initialized(state, input_json)
    gc_stale_state_files()

    source = input_json.get("source", "")
    initial_prompt = input_json.get("initial_prompt", "")
    log(f"copilot session_start: source={source!r} prompt_len={len(initial_prompt)}")


def _handle_user_prompt_submitted(input_json: dict) -> None:
    """Handle user prompt submission: open a fresh trace."""
    state = resolve_session(input_json)
    ensure_session_initialized(state, input_json)
    session_id = state.get("session_id")
    if session_id is None:
        return

    prompt = input_json.get("prompt", "") or ""

    trace_id = generate_trace_id()
    span_id = generate_span_id()
    now_ms = get_timestamp_ms()

    state.set("current_trace_id", trace_id)
    state.set("current_trace_span_id", span_id)
    state.set("current_trace_start_time", str(now_ms))
    state.set("current_trace_prompt", prompt)
    state.increment("trace_count")
    state.set("tool_count", "0")

    log(f"copilot user_prompt_submitted: prompt_len={len(prompt)}")


def _handle_pre_tool_use(input_json: dict) -> None:
    """Handle pre_tool_use: record tool start time."""
    state = resolve_session(input_json)
    tool_id = input_json.get("tool_use_id") or input_json.get("tool_name", "") or generate_trace_id()
    state.set(f"tool_{tool_id}_start", str(get_timestamp_ms()))


def _handle_post_tool_use(input_json: dict) -> None:
    """Handle post_tool_use: build and send a TOOL span."""
    state = resolve_session(input_json)
    session_id = state.get("session_id")
    if session_id is None:
        return

    trace_id = state.get("current_trace_id")
    parent_span_id = state.get("current_trace_span_id")
    state.increment("tool_count")

    tool_name = input_json.get("tool_name", "unknown")
    tool_id = input_json.get("tool_use_id") or tool_name or generate_trace_id()
    tool_input_raw = input_json.get("tool_input") or {}
    tool_input = json.dumps(tool_input_raw) if isinstance(tool_input_raw, dict) else str(tool_input_raw)

    tool_result_obj = input_json.get("tool_result") or {}
    tool_response = str(tool_result_obj.get("text_result_for_llm", ""))
    result_type = tool_result_obj.get("result_type", "")

    # Tool-specific enrichment. Match case-insensitively because Copilot uses
    # lowercase tool names (`bash`, `read`) where Claude Code uses TitleCase.
    tool_command = ""
    tool_file_path = ""
    tool_url = ""
    tool_query = ""
    tool_description = ""
    tool_name_lc = tool_name.lower()

    if isinstance(tool_input_raw, dict):
        if tool_name_lc == "bash":
            tool_command = tool_input_raw.get("command", "")
            tool_description = tool_command[:200]
        elif tool_name_lc in ("read", "write", "edit", "glob"):
            tool_file_path = tool_input_raw.get("file_path") or tool_input_raw.get("pattern", "")
            tool_description = tool_file_path[:200]
        elif tool_name_lc == "websearch":
            tool_query = tool_input_raw.get("query", "")
            tool_description = tool_query[:200]
        elif tool_name_lc == "webfetch":
            tool_url = tool_input_raw.get("url", "")
            tool_description = tool_url[:200]
        elif tool_name_lc == "grep":
            tool_query = tool_input_raw.get("pattern", "")
            tool_file_path = tool_input_raw.get("path", "")
            tool_description = f"grep: {tool_query[:100]}"
        else:
            tool_description = tool_input[:200]
    else:
        tool_description = tool_input[:200]

    # Timing
    start_time = state.get(f"tool_{tool_id}_start") or str(get_timestamp_ms())
    end_time = str(get_timestamp_ms())
    state.delete(f"tool_{tool_id}_start")

    # Redaction
    tool_input = redact_content(env.log_tool_content, tool_input)
    tool_response = redact_content(env.log_tool_content, tool_response)
    tool_description = redact_content(env.log_tool_details, tool_description)
    if tool_command:
        tool_command = redact_content(env.log_tool_details, tool_command)
    if tool_file_path:
        tool_file_path = redact_content(env.log_tool_details, tool_file_path)
    if tool_url:
        tool_url = redact_content(env.log_tool_details, tool_url)
    if tool_query:
        tool_query = redact_content(env.log_tool_details, tool_query)

    # Build attributes
    user_id = state.get("user_id") or ""
    project_name = state.get("project_name") or ""
    attrs = {
        "session.id": session_id,
        "openinference.span.kind": "TOOL",
        "project.name": project_name,
        "tool.name": tool_name,
        "input.value": tool_input,
        "output.value": tool_response,
        "tool.description": tool_description,
    }
    if user_id:
        attrs["user.id"] = user_id
    if tool_command:
        attrs["tool.command"] = tool_command
    if tool_file_path:
        attrs["tool.file_path"] = tool_file_path
    if tool_url:
        attrs["tool.url"] = tool_url
    if tool_query:
        attrs["tool.query"] = tool_query
    if result_type:
        attrs["tool.result_type"] = result_type

    span = build_span(
        tool_name,
        "TOOL",
        generate_span_id(),
        trace_id or "",
        parent_span_id or "",
        start_time,
        end_time,
        attrs,
        SERVICE_NAME,
        SCOPE_NAME,
    )
    send_span(span)


def _handle_stop(input_json: dict) -> None:
    """Handle stop: parse transcript and send LLM span for the completed turn."""
    state = resolve_session(input_json)
    session_id = state.get("session_id")
    trace_id = state.get("current_trace_id")
    if session_id is None or trace_id is None:
        return

    trace_span_id = state.get("current_trace_span_id") or generate_span_id()
    trace_start_time = state.get("current_trace_start_time") or str(get_timestamp_ms())
    user_prompt = state.get("current_trace_prompt") or ""
    project_name = state.get("project_name") or ""
    user_id = state.get("user_id") or ""

    transcript_path = input_json.get("transcript_path", "")
    summary = parse_transcript(transcript_path) if transcript_path else {}

    model_name = summary.get("model_name", "")
    # TODO(transcript): extract assistant turn text once events.jsonl assistant
    # event shape is captured. Until then, output_text is empty.
    output_text = summary.get("output_text", "")
    tool_count = state.get("tool_count") or "0"

    end_time = str(get_timestamp_ms())

    user_prompt = redact_content(env.log_prompts, user_prompt)
    output_text = redact_content(env.log_tool_content, output_text)

    attrs = {
        "session.id": session_id,
        "openinference.span.kind": "LLM",
        "project.name": project_name,
        "input.value": user_prompt,
        "output.value": output_text,
        "metadata": json.dumps(
            {
                "stop_reason": input_json.get("stop_reason", ""),
                "tool_count": int(tool_count or 0),
            }
        ),
    }
    if model_name:
        attrs["llm.model_name"] = model_name
    if user_id:
        attrs["user.id"] = user_id

    span = build_span(
        "Agent Stop",
        "LLM",
        trace_span_id,
        trace_id,
        "",
        trace_start_time,
        end_time,
        attrs,
        SERVICE_NAME,
        SCOPE_NAME,
    )
    send_span(span)

    # Clear per-turn state so the next user prompt starts a fresh trace
    state.delete("current_trace_id")
    state.delete("current_trace_span_id")
    state.delete("current_trace_start_time")
    state.delete("current_trace_prompt")


def _handle_subagent_stop(input_json: dict) -> None:
    """Handle subagent_stop: build and send CHAIN span for subagent."""
    state = resolve_session(input_json)
    session_id = state.get("session_id")
    if session_id is None:
        return

    agent_id = input_json.get("agent_id", "")
    agent_type = input_json.get("agent_type", "")
    transcript_path = input_json.get("transcript_path", "")

    summary = parse_transcript(transcript_path) if transcript_path else {}
    model_name = summary.get("model_name", "")

    project_name = state.get("project_name") or ""
    user_id = state.get("user_id") or ""
    end_time = str(get_timestamp_ms())

    attrs = {
        "session.id": session_id,
        "openinference.span.kind": "CHAIN",
        "project.name": project_name,
        "metadata": json.dumps({"agent_type": agent_type, "agent_id": agent_id}),
    }
    if model_name:
        attrs["llm.model_name"] = model_name
    if user_id:
        attrs["user.id"] = user_id

    span_name = f"Subagent: {agent_id}" if agent_id else "Subagent"

    span = build_span(
        span_name,
        "CHAIN",
        generate_span_id(),
        state.get("current_trace_id") or generate_trace_id(),
        state.get("current_trace_span_id") or "",
        end_time,
        end_time,
        attrs,
        SERVICE_NAME,
        SCOPE_NAME,
    )
    send_span(span)


# ---------------------------------------------------------------------------
# CLI entry points
# ---------------------------------------------------------------------------


def session_start():
    """Entry point for arize-hook-copilot-session-start."""
    try:
        input_json = _read_stdin("session_start")
        if check_requirements():
            _handle_session_start(input_json)
    except Exception as e:
        error(f"copilot session_start hook failed: {e}")
    finally:
        _print_response("SessionStart")


def user_prompt_submitted():
    """Entry point for arize-hook-copilot-user-prompt."""
    try:
        input_json = _read_stdin("user_prompt_submitted")
        if check_requirements():
            _handle_user_prompt_submitted(input_json)
    except Exception as e:
        error(f"copilot user_prompt_submitted hook failed: {e}")
    finally:
        _print_response("UserPromptSubmit")


def pre_tool_use():
    """Entry point for arize-hook-copilot-pre-tool."""
    try:
        input_json = _read_stdin("pre_tool_use")
        if check_requirements():
            _handle_pre_tool_use(input_json)
    except Exception as e:
        error(f"copilot pre_tool_use hook failed: {e}")
    finally:
        _print_response("PreToolUse")


def post_tool_use():
    """Entry point for arize-hook-copilot-post-tool."""
    try:
        input_json = _read_stdin("post_tool_use")
        if check_requirements():
            _handle_post_tool_use(input_json)
    except Exception as e:
        error(f"copilot post_tool_use hook failed: {e}")
    finally:
        _print_response("PostToolUse")


def stop():
    """Entry point for arize-hook-copilot-stop."""
    try:
        input_json = _read_stdin("stop")
        if check_requirements():
            _handle_stop(input_json)
    except Exception as e:
        error(f"copilot stop hook failed: {e}")
    finally:
        _print_response("Stop")


def subagent_stop():
    """Entry point for arize-hook-copilot-subagent-stop."""
    try:
        input_json = _read_stdin("subagent_stop")
        if check_requirements():
            _handle_subagent_stop(input_json)
    except Exception as e:
        error(f"copilot subagent_stop hook failed: {e}")
    finally:
        _print_response("SubagentStop")
