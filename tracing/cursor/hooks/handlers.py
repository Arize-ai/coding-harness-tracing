#!/usr/bin/env python3
"""Cursor hook handler: single entry point dispatching all 15 Cursor hook events.

Replaces tracing/cursor/hooks/hook-handler.sh (475 lines).

Input contract: JSON on stdin, all 15 events (IDE + CLI) routed here.
stdout: MUST print permissive JSON response, even on error.
stderr: redirected to ARIZE_LOG_FILE before dispatch.
"""
import hashlib
import json
import os
import sys

from core.common import build_span, env, error, get_timestamp_ms, log, redact_content
from core.common import send_span as _send_span_to_backend
from tracing.cursor.hooks.adapter import (
    SCOPE_NAME,
    SERVICE_NAME,
    active_turn_add_response,
    active_turn_add_thought_hash,
    active_turn_clear,
    active_turn_clear_if_matches,
    active_turn_get,
    active_turn_record_alias,
    active_turn_save,
    active_turn_update,
    check_requirements,
    conversation_cleanup,
    gen_root_span_get,
    gen_root_span_save,
    sanitize,
    span_id_16,
    state_cleanup_generation,
    state_pop,
    state_push,
    terminal_nogen_claim,
    terminal_nogen_clear,
    terminal_turn_claim,
    terminal_turn_is_marked,
    terminal_turn_mark,
    terminal_turn_mark_many,
    trace_id_from_generation,
    truncate_attr,
    turn_matches_generation,
)

# ---------------------------------------------------------------------------
# Span send (with project.name injection)
# ---------------------------------------------------------------------------


def _resolve_project_name() -> str:
    """Project name for Cursor spans: framework env override or config.json,
    else cwd basename, else the service name.

    Cursor builds many span dicts across 15 handlers and keeps no per-session
    project state, so it resolves the project centrally at send time — matching
    the framework-scoped resolution the other harnesses do in their adapters.
    """
    return env.project_name_for(SERVICE_NAME) or os.path.basename(os.getcwd()) or SERVICE_NAME


def send_span(payload: dict) -> bool:
    """Inject ``project.name`` onto every span in the payload, then send.

    Wraps ``core.common.send_span`` so all handler send sites get the attribute
    without threading project name through each attrs dict.
    """
    project_name = _resolve_project_name()
    attr = {"key": "project.name", "value": {"stringValue": project_name}}
    for rs in payload.get("resourceSpans", []):
        for ss in rs.get("scopeSpans", []):
            for span in ss.get("spans", []):
                span.setdefault("attributes", []).append(attr)
    return _send_span_to_backend(payload)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _print_permissive(event: str) -> None:
    """Print the permissive JSON response to stdout.

    before* events -> {"permission": "allow"}
    all others     -> {"continue": true}

    Uses sys.__stdout__ (the original stdout saved by Python) in case
    sys.stdout has been redirected.
    """
    stdout = sys.__stdout__ or sys.stdout
    if event.startswith("before"):
        stdout.write('{"permission": "allow"}')
    else:
        stdout.write('{"continue": true}')
    stdout.flush()


def _jq_str(input_json: dict, *keys, default: str = "") -> str:
    """Try multiple keys in order, return first non-None/non-empty string value.

    Matches bash: echo "$INPUT" | jq -r "$1" 2>/dev/null || echo "${2:-}"
    """
    for key in keys:
        val = input_json.get(key)
        if val is not None and val != "":
            return str(val)
    return default


def _resolve_user_id(input_json: dict) -> str:
    """env.get_user_id(SERVICE_NAME) (global config < harnesses.cursor.user_id < ARIZE_USER_ID env)
    > payload `user_email` > "".

    Cursor has no per-session state for user_id, so each handler resolves it
    inline. Configured user_id wins over the implicit `user_email` payload field
    so an explicitly set user takes precedence on shared workstations.
    """
    return env.get_user_id(SERVICE_NAME) or _jq_str(input_json, "user_email")


def _to_int(v):
    """Coerce *v* to int if possible; return None for None, empty, or ``"--"``."""
    try:
        return int(v) if v not in (None, "", "--") else None
    except (TypeError, ValueError):
        return None


def _event_name(input_json: dict) -> str:
    """Extract event name from payload, tolerant of IDE and CLI key variants.

    Cursor IDE uses ``hook_event_name``; Cursor CLI uses ``hookEventName``.
    """
    return _jq_str(input_json, "hook_event_name", "hookEventName", "event_name", "eventName", "event")


def _is_cursor_ide_hook_payload(input_json: dict) -> bool:
    """Return True when the stdin JSON looks like Cursor IDE (vs CLI) hook payloads.

    IDE emits ``hook_event_name``; CLI emits ``hookEventName``. The lifecycle is
    now identical for both payload forms. This helper remains for compatibility.

    If neither key is set, default to IDE so existing payloads without a discriminator keep
    the original semantics.
    """
    if input_json.get("hook_event_name"):
        return True
    if input_json.get("hookEventName"):
        return False
    return True


def _trace_id_from_event(gen_id: str, conversation_id: str) -> str:
    """Derive a trace ID from generation or conversation ID.

    Prefers gen_id; falls back to conversation_id for CLI events that may
    lack a generation_id.
    """
    if gen_id:
        return trace_id_from_generation(gen_id)
    if conversation_id:
        return trace_id_from_generation(conversation_id)
    return ""


def _turn_state_key(conversation_id: str, gen_id: str) -> str:
    """Storage key for one turn's active-turn/terminal state.

    Cursor normally supplies conversation_id, which is shared across a
    conversation's turns (enabling next-prompt/sessionEnd fallback closure).
    Some CLI flows can omit it entirely; without a key to store under, the
    turn's state would simply never be persisted. Falling back to a
    generation-id-derived key still lets it be stored and later resolved by
    `stop` (keyed on the same generation id), so its root is eventually
    emitted instead of silently dropped. The "__gen__:" prefix keeps this
    synthetic key namespace-distinct from any real conversation_id.
    """
    if conversation_id:
        return conversation_id
    if gen_id:
        return f"__gen__:{gen_id}"
    return ""


def _resolve_turn_readonly(conversation_id: str, gen_id: str, trace_id: str, now_ms: int):
    """Resolve an event to its active conversation turn, or its direct
    generation root when no active turn exists — without mutating state.

    An active prompt turn always takes priority over a merely-saved
    generation root (e.g. sessionStart's CLI fallback root, or a stale root
    left over from a turn that's no longer active): a saved root only wins
    when there is no active turn to prefer, or when it happens to be that
    active turn's own root anyway. Using the saved root instead of a
    mismatched active turn would attach new events to whatever a leftover
    root points at rather than the turn that's actually in progress.
    """
    turn_key = _turn_state_key(conversation_id, gen_id)
    active = active_turn_get(turn_key)
    if active and active.get("root_span_id") and active.get("trace_id"):
        if gen_id and not turn_matches_generation(active, gen_id) and terminal_turn_is_marked(turn_key, gen_id):
            direct_parent = gen_root_span_get(gen_id)
            return trace_id, direct_parent, gen_id, None
        return (
            active["trace_id"],
            active["root_span_id"],
            active.get("generation_id", gen_id),
            active,
        )
    direct_parent = gen_root_span_get(gen_id)
    if direct_parent:
        return trace_id, direct_parent, gen_id, None
    return trace_id, "", gen_id, None


def _resolve_turn(
    conversation_id: str,
    gen_id: str,
    trace_id: str,
    now_ms: int,
    touch: bool = True,
):
    """`_resolve_turn_readonly`, plus recording this generation id as a known
    alias of the resolved turn and bumping its activity timestamp.

    Every handler EXCEPT `stop` calls this: a mismatched incoming generation
    id for an in-progress turn is legitimately treated as a newly observed
    alias of it (Cursor rotates generation ids for sub-events mid-turn).
    `stop` must NOT do this via this path — recording the alias and then
    immediately checking membership against it would make every stop
    trivially "match" by construction, defeating strict matching entirely.
    `stop` calls `_resolve_turn_readonly` directly, and separately checks
    membership against the alias set *as it stood before this event*.
    """
    turn_key = _turn_state_key(conversation_id, gen_id)
    resolved_trace, parent, canonical_gen, active = _resolve_turn_readonly(
        conversation_id, gen_id, trace_id, now_ms
    )
    if active:
        if gen_id and not turn_matches_generation(active, gen_id):
            alias_time = now_ms if touch else int(active.get("last_activity_ms") or now_ms)
            updated = active_turn_record_alias(
                turn_key, gen_id, alias_time, active.get("root_span_id", "")
            )
            if updated:
                active = updated
                resolved_trace = active.get("trace_id", resolved_trace)
                parent = active.get("root_span_id", parent)
                canonical_gen = active.get("generation_id", canonical_gen)
        elif touch:
            active_turn_update(turn_key, {"last_activity_ms": now_ms})
    return resolved_trace, parent, canonical_gen, active


def _token_attrs(input_json: dict) -> dict:
    """Build one turn's OpenInference token attributes from a stop payload."""
    raw_input = input_json.get("input_tokens")
    prompt_tokens = _to_int(raw_input if raw_input is not None else input_json.get("inputTokens"))
    raw_output = input_json.get("output_tokens")
    completion_tokens = _to_int(raw_output if raw_output is not None else input_json.get("outputTokens"))
    raw_read = input_json.get("cache_read_tokens")
    cache_read = _to_int(raw_read if raw_read is not None else input_json.get("cacheReadTokens"))
    raw_write = input_json.get("cache_write_tokens")
    cache_write = _to_int(raw_write if raw_write is not None else input_json.get("cacheWriteTokens"))

    attrs = {}
    prompt_total = None
    if prompt_tokens is not None:
        prompt_total = prompt_tokens + (cache_read or 0) + (cache_write or 0)
        attrs["llm.token_count.prompt"] = prompt_total
    if completion_tokens is not None:
        attrs["llm.token_count.completion"] = completion_tokens
    if cache_read is not None:
        attrs["llm.token_count.prompt_details.cache_read"] = cache_read
    if cache_write is not None:
        attrs["llm.token_count.prompt_details.cache_write"] = cache_write
    if prompt_total is not None and completion_tokens is not None:
        attrs["llm.token_count.total"] = prompt_total + completion_tokens
    model = _jq_str(input_json, "model", "model_name")
    if model:
        attrs["llm.model_name"] = model
    return attrs


def _response_text(turn: dict) -> str:
    """Join a turn's response fragments in observation order, bounded in size.

    Fragment count is already bounded on write (``active_turn_add_response``);
    this also bounds the joined result so the final attribute value can't
    grow unbounded even when many fragments are each near the per-fragment
    limit.
    """
    responses = turn.get("responses")
    if not isinstance(responses, list):
        return ""
    joined = "\n".join(str(item.get("text", "")) for item in responses if isinstance(item, dict))
    return truncate_attr(joined)


def _emit_closed_turn(turn: dict, end_ms: int, token_attrs: "dict | None" = None) -> None:
    """Emit one deferred root and, when present, one turn-level LLM span."""
    trace_id = turn.get("trace_id", "")
    root_span_id = turn.get("root_span_id", "")
    conversation_id = turn.get("conversation_id", "")
    start_ms = int(turn.get("start_ms") or end_ms)
    output = _response_text(turn)
    prompt = redact_content(env.log_prompts, str(turn.get("prompt", "")))
    user_id = turn.get("user_id", "")
    model = turn.get("model", "")

    root_attrs = {
        "openinference.span.kind": "CHAIN",
        "input.value": prompt,
        "output.value": output,
        "session.id": conversation_id,
    }
    if conversation_id:
        root_attrs["cursor.conversation.id"] = conversation_id
    if user_id:
        root_attrs["user.id"] = user_id
    if model:
        root_attrs["llm.model_name"] = model
    send_span(
        build_span(
            "User Prompt",
            "CHAIN",
            root_span_id,
            trace_id,
            "",
            start_ms,
            end_ms,
            root_attrs,
            SERVICE_NAME,
            SCOPE_NAME,
        )
    )

    responses = turn.get("responses")
    if not isinstance(responses, list):
        responses = []
    if responses or token_attrs or prompt:
        llm_start_ms = min(
            (int(item.get("start_ms") or start_ms) for item in responses if isinstance(item, dict)),
            default=start_ms,
        )
        final_response_ms = max(
            (int(item.get("observed_ms") or start_ms) for item in responses if isinstance(item, dict)),
            default=end_ms,
        )
        attrs = {
            "openinference.span.kind": "LLM",
            "input.value": prompt,
            "output.value": output,
            "session.id": conversation_id,
            "cursor.llm.usage.scope": "turn",
            "cursor.llm.timing.scope": "turn",
        }
        if conversation_id:
            attrs["cursor.conversation.id"] = conversation_id
        if user_id:
            attrs["user.id"] = user_id
        if model:
            attrs["llm.model_name"] = model
        if token_attrs:
            attrs.update(token_attrs)
        send_span(
            build_span(
                "Agent Response",
                "LLM",
                turn.get("llm_span_id") or span_id_16(),
                trace_id,
                root_span_id,
                llm_start_ms,
                final_response_ms,
                attrs,
                SERVICE_NAME,
                SCOPE_NAME,
            )
        )


def _flush_active_turn(conversation_id: str, end_ms: int) -> "dict | None":
    """Claim and emit a pending turn without stop token data.

    Marks every generation id this turn is known by — its canonical id plus
    every alias observed during its lifetime — terminal, not just the
    canonical one. Otherwise a later event still carrying one of those
    aliases (the turn is closing via fallback precisely because its own
    `stop` never arrived) would not be recognized as belonging to an
    already-closed turn.
    """
    turn = active_turn_clear(conversation_id)
    if not turn:
        return None
    canonical_gen = str(turn.get("generation_id", ""))
    aliases = turn.get("generation_aliases") or []
    terminal_turn_mark_many(conversation_id, [canonical_gen, *aliases])
    _emit_closed_turn(turn, end_ms)
    if canonical_gen:
        state_cleanup_generation(canonical_gen)
    for alias in aliases:
        state_cleanup_generation(alias)
    return turn


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def _dispatch(event: str, input_json: dict) -> None:
    """Route event to the appropriate handler."""
    conversation_id = input_json.get("conversation_id", "")
    gen_id = input_json.get("generation_id", "")

    # Early exit: tracing disabled
    if not env.trace_enabled:
        return

    trace_id = _trace_id_from_event(gen_id, conversation_id)
    now_ms = get_timestamp_ms()

    handlers = {
        "beforeSubmitPrompt": _handle_before_submit_prompt,
        "afterAgentResponse": _handle_after_agent_response,
        "afterAgentThought": _handle_after_agent_thought,
        "beforeShellExecution": _handle_before_shell_execution,
        "afterShellExecution": _handle_after_shell_execution,
        "beforeMCPExecution": _handle_before_mcp_execution,
        "afterMCPExecution": _handle_after_mcp_execution,
        "beforeReadFile": _handle_before_read_file,
        "afterFileEdit": _handle_after_file_edit,
        "beforeTabFileRead": _handle_before_tab_file_read,
        "afterTabFileEdit": _handle_after_tab_file_edit,
        "stop": _handle_stop,
        "sessionStart": _handle_session_start,
        "sessionEnd": _handle_session_end,
        "postToolUse": _handle_post_tool_use,
    }

    handler = handlers.get(event)
    if handler:
        handler(input_json, conversation_id, gen_id, trace_id, now_ms)
    else:
        log(f"Unknown hook event: {event}")


# ---------------------------------------------------------------------------
# Event handlers
# ---------------------------------------------------------------------------


def _handle_before_submit_prompt(input_json, conversation_id, gen_id, trace_id, now_ms):
    """Start one deferred root for the new turn.

    Normally keyed by conversation_id, which is shared across a
    conversation's turns and lets next-prompt fallback closure flush a
    previous pending turn. When conversation_id is absent, the turn is
    instead keyed by its own generation id (see `_turn_state_key`) so it is
    still persisted and its root still eventually emitted via `stop` — but
    there is then no shared key to flush an *earlier* pending turn against,
    so next-prompt fallback simply doesn't apply in that fallback mode; each
    such turn is independent and must close via its own `stop`.
    """
    turn_key = _turn_state_key(conversation_id, gen_id)
    if conversation_id:
        _flush_active_turn(conversation_id, now_ms)
        if not gen_id:
            # Reset only the generation-less dedup sentinel for this new
            # turn — never the durable, bounded per-generation marker list,
            # which must keep remembering prior real generations' claims.
            terminal_nogen_clear(conversation_id)

    sid = span_id_16()
    if not trace_id:
        trace_id = trace_id_from_generation(gen_id or sid)
    if gen_id:
        gen_root_span_save(gen_id, sid)

    prompt = _jq_str(input_json, "prompt", "input", "text")
    model = _jq_str(input_json, "model", "model_name")
    turn = {
        "generation_id": gen_id,
        "trace_id": trace_id,
        "root_span_id": sid,
        "llm_span_id": span_id_16(),
        "conversation_id": conversation_id,
        "start_ms": now_ms,
        "last_activity_ms": now_ms,
        "prompt": prompt,
        "model": model,
        "user_id": _resolve_user_id(input_json),
        "thought_hashes": [],
        "responses": [],
        "generation_aliases": [],
    }
    if not turn_key:
        # No conversation_id and no generation_id: nothing to key this
        # turn's state by at all, so there is no later event that could ever
        # resolve back to it. Emit it now rather than losing it silently.
        error("beforeSubmitPrompt: no conversation_id or generation_id available; closing turn immediately")
        _emit_closed_turn(turn, now_ms)
        return
    if not active_turn_save(turn_key, turn):
        # Persisting the deferred turn failed (disk error, permissions, ...).
        # There is nowhere to defer afterAgentResponse/stop data to, so close
        # the turn now through the same closure path a later stop/sessionEnd/
        # next-prompt would use — not a separate "send immediately" branch —
        # rather than silently dropping the turn.
        error(f"beforeSubmitPrompt: active_turn_save failed for key={turn_key}; closing turn immediately")
        _emit_closed_turn(turn, now_ms)
        if gen_id:
            state_cleanup_generation(gen_id)
        terminal_turn_mark(turn_key, gen_id)
        return
    log(f"beforeSubmitPrompt: deferred root span {sid} (trace={trace_id}, key={turn_key})")


def _handle_after_agent_response(input_json, conversation_id, gen_id, trace_id, now_ms):
    """Append response text to the canonical turn for one later LLM span."""
    resolved_trace, parent, _, turn = _resolve_turn(
        conversation_id, gen_id, trace_id, now_ms, touch=False
    )
    response = redact_content(
        env.log_prompts,
        _jq_str(input_json, "text", "response", "output"),
    )
    model = _jq_str(input_json, "model", "model_name")
    # Cursor may not have resolved a user identity yet at beforeSubmitPrompt
    # time (e.g. config/email lookup lagging session start); copy in a later
    # identity afterAgentResponse supplies so the closed turn reflects it.
    user_id = _resolve_user_id(input_json)
    if turn:
        updated = active_turn_add_response(
            _turn_state_key(conversation_id, gen_id),
            response,
            now_ms,
            model,
            user_id,
            turn.get("root_span_id", ""),
        )
        if updated:
            log("afterAgentResponse: appended response to active turn")
            return
        log("afterAgentResponse: active turn closed before response append")

    attrs = {
        "openinference.span.kind": "LLM",
        "output.value": response,
        "session.id": conversation_id,
        "cursor.llm.usage.scope": "turn",
        "cursor.llm.timing.scope": "turn",
    }
    if conversation_id:
        attrs["cursor.conversation.id"] = conversation_id
    if user_id:
        attrs["user.id"] = user_id
    if model:
        attrs["llm.model_name"] = model
    send_span(
        build_span(
            "Agent Response",
            "LLM",
            span_id_16(),
            resolved_trace,
            parent,
            now_ms,
            now_ms,
            attrs,
            SERVICE_NAME,
            SCOPE_NAME,
        )
    )
    log("afterAgentResponse: sent standalone point span")


def _handle_after_agent_thought(input_json, conversation_id, gen_id, trace_id, now_ms):
    """Emit one point CHAIN span for each exact thought within a turn."""
    resolved_trace, parent, _, turn = _resolve_turn(conversation_id, gen_id, trace_id, now_ms)
    raw_thought = _jq_str(input_json, "thought", "thinking", "text")
    thought = redact_content(env.log_prompts, raw_thought)
    normalized = raw_thought.replace("\r\n", "\n").strip()
    salt = str(turn.get("root_span_id", "")) if turn else ""
    thought_hash = hashlib.sha256(f"{salt}\0{thought}\0{normalized}".encode()).hexdigest()
    if turn and not active_turn_add_thought_hash(
        _turn_state_key(conversation_id, gen_id),
        thought_hash,
        now_ms,
        turn.get("root_span_id", ""),
    ):
        log("afterAgentThought: skipped exact duplicate")
        return
    sid = span_id_16()

    user_id = _resolve_user_id(input_json)

    attrs = {
        "openinference.span.kind": "CHAIN",
        "output.value": thought,
        "session.id": conversation_id,
    }
    if conversation_id:
        attrs["cursor.conversation.id"] = conversation_id
    if user_id:
        attrs["user.id"] = user_id

    span = build_span(
        "Agent Thinking",
        "CHAIN",
        sid,
        resolved_trace,
        parent,
        now_ms,
        now_ms,
        attrs,
        SERVICE_NAME,
        SCOPE_NAME,
    )
    send_span(span)
    log(f"afterAgentThought: span {sid}")


def _handle_before_shell_execution(input_json, conversation_id, gen_id, trace_id, now_ms):
    """State push only, no span. Replaces bash lines 163-179."""
    resolved_trace, _, canonical_gen, _ = _resolve_turn(conversation_id, gen_id, trace_id, now_ms)
    if not canonical_gen:
        return

    command = _jq_str(input_json, "command", "shell_command")
    cwd = _jq_str(input_json, "cwd", "working_directory")

    state_push(
        f"shell_{sanitize(canonical_gen)}",
        {
            "command": command,
            "cwd": cwd,
            "start_ms": str(now_ms),
            "trace_id": resolved_trace,
            "conversation_id": conversation_id,
        },
    )
    log(f"beforeShellExecution: pushed state for gen={canonical_gen}")


def _handle_after_shell_execution(input_json, conversation_id, gen_id, trace_id, now_ms):
    """Merge with before state, create TOOL span. Replaces bash lines 184-232."""
    sid = span_id_16()
    resolved_trace, parent, canonical_gen, _ = _resolve_turn(conversation_id, gen_id, trace_id, now_ms)
    popped = state_pop(f"shell_{sanitize(canonical_gen)}") if canonical_gen else None

    if popped:
        start_ms = popped.get("start_ms", "")
        command = popped.get("command", "")
    else:
        start_ms = ""
        command = ""
    start_ms = start_ms or str(now_ms)

    # Override command from after-event if present
    after_cmd = _jq_str(input_json, "command", "shell_command")
    if after_cmd:
        command = after_cmd

    output = _jq_str(input_json, "output", "stdout", "result")
    exit_code = _jq_str(input_json, "exit_code", "exitCode")

    command = redact_content(env.log_tool_details, command)
    output = redact_content(env.log_tool_content, output)

    parsed_exit_code = _to_int(exit_code)
    status_code = 0
    status_message = ""
    if parsed_exit_code == 0:
        status_code = 1
    elif parsed_exit_code is not None:
        status_code = 2
        status_message = truncate_attr(output, 1024)

    user_id = _resolve_user_id(input_json)

    attrs = {
        "openinference.span.kind": "TOOL",
        "tool.name": "shell",
        "input.value": command,
        "output.value": output,
        "session.id": conversation_id,
    }
    if conversation_id:
        attrs["cursor.conversation.id"] = conversation_id
    if user_id:
        attrs["user.id"] = user_id
    if exit_code:
        attrs["shell.exit_code"] = exit_code

    span = build_span(
        "Shell",
        "TOOL",
        sid,
        resolved_trace,
        parent,
        start_ms,
        now_ms,
        attrs,
        SERVICE_NAME,
        SCOPE_NAME,
        status_code=status_code,
        status_message=status_message,
    )
    send_span(span)
    log(f"afterShellExecution: span {sid} (merged)")


def _handle_before_mcp_execution(input_json, conversation_id, gen_id, trace_id, now_ms):
    """State push only, no span. Replaces bash lines 237-257."""
    resolved_trace, _, canonical_gen, _ = _resolve_turn(conversation_id, gen_id, trace_id, now_ms)
    if not canonical_gen:
        return

    tool_name = _jq_str(input_json, "tool_name", "toolName", "name")
    tool_input = _jq_str(input_json, "tool_input", "toolInput", "input", "arguments")
    mcp_url = _jq_str(input_json, "url", "server_url", "serverUrl")
    mcp_cmd = _jq_str(input_json, "command")

    state_push(
        f"mcp_{sanitize(canonical_gen)}",
        {
            "tool_name": tool_name,
            "tool_input": redact_content(env.log_tool_content, tool_input),
            "url": redact_content(env.log_tool_details, mcp_url),
            "command": redact_content(env.log_tool_details, mcp_cmd),
            "start_ms": str(now_ms),
            "trace_id": resolved_trace,
            "conversation_id": conversation_id,
        },
    )
    log(f"beforeMCPExecution: pushed state for gen={canonical_gen}")


def _handle_after_mcp_execution(input_json, conversation_id, gen_id, trace_id, now_ms):
    """Merge with before state, create TOOL span. Replaces bash lines 262-312."""
    sid = span_id_16()
    resolved_trace, parent, canonical_gen, _ = _resolve_turn(conversation_id, gen_id, trace_id, now_ms)
    popped = state_pop(f"mcp_{sanitize(canonical_gen)}") if canonical_gen else None

    if popped:
        start_ms = popped.get("start_ms", "")
        tool_name = popped.get("tool_name", "")
        tool_input = popped.get("tool_input", "")
    else:
        start_ms = ""
        tool_name = ""
        tool_input = ""
    start_ms = start_ms or str(now_ms)

    # Override tool name from after-event if present
    after_tool = _jq_str(input_json, "tool_name", "toolName", "name")
    if after_tool:
        tool_name = after_tool
    tool_name = tool_name or "unknown"

    result = redact_content(env.log_tool_content, _jq_str(input_json, "result", "output", "result_json"))

    user_id = _resolve_user_id(input_json)

    attrs = {
        "openinference.span.kind": "TOOL",
        "tool.name": tool_name,
        "input.value": tool_input,
        "output.value": result,
        "session.id": conversation_id,
    }
    if conversation_id:
        attrs["cursor.conversation.id"] = conversation_id
    if user_id:
        attrs["user.id"] = user_id

    span = build_span(
        f"MCP: {tool_name}",
        "TOOL",
        sid,
        resolved_trace,
        parent,
        start_ms,
        now_ms,
        attrs,
        SERVICE_NAME,
        SCOPE_NAME,
    )
    send_span(span)
    log(f"afterMCPExecution: span {sid} (merged, tool={tool_name})")


def _handle_before_read_file(input_json, conversation_id, gen_id, trace_id, now_ms):
    """TOOL span for file read. Replaces bash lines 317-339."""
    sid = span_id_16()
    resolved_trace, parent, _, _ = _resolve_turn(conversation_id, gen_id, trace_id, now_ms)

    file_path = redact_content(env.log_tool_details, _jq_str(input_json, "file_path", "filePath", "path"))

    user_id = _resolve_user_id(input_json)

    attrs = {
        "openinference.span.kind": "TOOL",
        "tool.name": "read_file",
        "input.value": file_path,
        "session.id": conversation_id,
    }
    if conversation_id:
        attrs["cursor.conversation.id"] = conversation_id
    if user_id:
        attrs["user.id"] = user_id

    span = build_span(
        "Read File",
        "TOOL",
        sid,
        resolved_trace,
        parent,
        now_ms,
        now_ms,
        attrs,
        SERVICE_NAME,
        SCOPE_NAME,
    )
    send_span(span)
    log(f"beforeReadFile: span {sid}")


def _handle_after_file_edit(input_json, conversation_id, gen_id, trace_id, now_ms):
    """TOOL span for file edit. Replaces bash lines 344-371."""
    sid = span_id_16()
    resolved_trace, parent, _, _ = _resolve_turn(conversation_id, gen_id, trace_id, now_ms)

    file_path = redact_content(env.log_tool_details, _jq_str(input_json, "file_path", "filePath", "path"))
    edits = redact_content(env.log_tool_content, _jq_str(input_json, "edits", "changes", "diff"))
    input_val = f"{file_path}: {edits}" if edits else file_path

    user_id = _resolve_user_id(input_json)

    attrs = {
        "openinference.span.kind": "TOOL",
        "tool.name": "edit_file",
        "input.value": input_val,
        "session.id": conversation_id,
    }
    if conversation_id:
        attrs["cursor.conversation.id"] = conversation_id
    if user_id:
        attrs["user.id"] = user_id

    span = build_span(
        "File Edit",
        "TOOL",
        sid,
        resolved_trace,
        parent,
        now_ms,
        now_ms,
        attrs,
        SERVICE_NAME,
        SCOPE_NAME,
    )
    send_span(span)
    log(f"afterFileEdit: span {sid}")


def _handle_before_tab_file_read(input_json, conversation_id, gen_id, trace_id, now_ms):
    """TOOL span for tab file read. Replaces bash lines 376-398."""
    sid = span_id_16()
    resolved_trace, parent, _, _ = _resolve_turn(conversation_id, gen_id, trace_id, now_ms)

    file_path = redact_content(env.log_tool_details, _jq_str(input_json, "file_path", "filePath", "path"))

    user_id = _resolve_user_id(input_json)

    attrs = {
        "openinference.span.kind": "TOOL",
        "tool.name": "read_file_tab",
        "input.value": file_path,
        "session.id": conversation_id,
    }
    if conversation_id:
        attrs["cursor.conversation.id"] = conversation_id
    if user_id:
        attrs["user.id"] = user_id

    span = build_span(
        "Tab Read File",
        "TOOL",
        sid,
        resolved_trace,
        parent,
        now_ms,
        now_ms,
        attrs,
        SERVICE_NAME,
        SCOPE_NAME,
    )
    send_span(span)
    log(f"beforeTabFileRead: span {sid}")


def _handle_after_tab_file_edit(input_json, conversation_id, gen_id, trace_id, now_ms):
    """TOOL span for tab file edit. Replaces bash lines 403-430."""
    sid = span_id_16()
    resolved_trace, parent, _, _ = _resolve_turn(conversation_id, gen_id, trace_id, now_ms)

    file_path = redact_content(env.log_tool_details, _jq_str(input_json, "file_path", "filePath", "path"))
    edits = redact_content(env.log_tool_content, _jq_str(input_json, "edits", "changes", "diff"))
    input_val = f"{file_path}: {edits}" if edits else file_path

    user_id = _resolve_user_id(input_json)

    attrs = {
        "openinference.span.kind": "TOOL",
        "tool.name": "edit_file_tab",
        "input.value": input_val,
        "session.id": conversation_id,
    }
    if conversation_id:
        attrs["cursor.conversation.id"] = conversation_id
    if user_id:
        attrs["user.id"] = user_id

    span = build_span(
        "Tab File Edit",
        "TOOL",
        sid,
        resolved_trace,
        parent,
        now_ms,
        now_ms,
        attrs,
        SERVICE_NAME,
        SCOPE_NAME,
    )
    send_span(span)
    log(f"afterTabFileEdit: span {sid}")


def _handle_stop(input_json, conversation_id, gen_id, trace_id, now_ms):
    """Close the turn this stop actually resolves to, and emit its terminal
    point span.

    Only clears the active turn when the incoming generation id is empty, or
    matches that turn's canonical id or one of its *previously* recorded
    aliases — never whatever turn happens to be active, and never an alias
    this very event would be the first to establish (that would make every
    mismatched stop trivially match). A mismatched generation stays
    standalone: resolution still (unchanged) picks its parent/trace through
    the active turn when one exists, so a stray standalone span can still
    land in that trace, but it is never allowed to close the turn.
    """
    turn_key = _turn_state_key(conversation_id, gen_id)
    resolved_trace, parent, canonical_gen, active = _resolve_turn_readonly(
        conversation_id, gen_id, trace_id, now_ms
    )

    if active:
        active_has_generation = bool(active.get("generation_id") or active.get("generation_aliases"))
        if gen_id and not turn_matches_generation(active, gen_id):
            log(
                f"stop: ignored generation {gen_id!r} because it does not match "
                "the active turn's canonical generation or observed aliases"
            )
            return
        elif not gen_id and active_has_generation:
            log("stop: ignored generation-less terminal event for a generated active turn")
            return

    terminal_generation = gen_id or (canonical_gen if active else "")
    if terminal_generation:
        if not terminal_turn_claim(turn_key, terminal_generation):
            log(f"stop: skipped duplicate terminal event for gen={gen_id}")
            return
    elif not terminal_nogen_claim(turn_key):
        log("stop: skipped duplicate no-generation terminal event")
        return

    turn = None
    if active:
        turn = active_turn_clear_if_matches(
            turn_key,
            root_span_id=active.get("root_span_id", ""),
            generation_id=active.get("generation_id", ""),
        )
        if turn is None:
            log(f"stop: active turn was closed concurrently for gen={gen_id}")
            return

    token_attrs = _token_attrs(input_json)
    has_token_counts = any(key.startswith("llm.token_count.") for key in token_attrs)
    emitted_llm_span = False
    if turn:
        resolved_trace = turn.get("trace_id", resolved_trace)
        parent = turn.get("root_span_id", parent)
        _emit_closed_turn(turn, now_ms, token_attrs)
        emitted_llm_span = True
    elif has_token_counts:
        llm_attrs = {
            "openinference.span.kind": "LLM",
            "session.id": conversation_id,
            "cursor.llm.usage.scope": "turn",
            "cursor.llm.timing.scope": "turn",
            **token_attrs,
        }
        if conversation_id:
            llm_attrs["cursor.conversation.id"] = conversation_id
        standalone_user_id = _resolve_user_id(input_json)
        if standalone_user_id:
            llm_attrs["user.id"] = standalone_user_id
        send_span(
            build_span(
                "Agent Response",
                "LLM",
                span_id_16(),
                resolved_trace,
                parent,
                now_ms,
                now_ms,
                llm_attrs,
                SERVICE_NAME,
                SCOPE_NAME,
            )
        )
        emitted_llm_span = True

    sid = span_id_16()
    status = _jq_str(input_json, "status", "reason")
    loop_count = _jq_str(input_json, "loop_count", "loopCount", "iterations")
    duration_raw = input_json.get("duration_ms")
    duration_ms = _to_int(duration_raw if duration_raw is not None else input_json.get("durationMs"))
    attrs = {
        "openinference.span.kind": "CHAIN",
        "session.id": conversation_id,
    }
    if conversation_id:
        attrs["cursor.conversation.id"] = conversation_id
    user_id = _resolve_user_id(input_json)
    if user_id:
        attrs["user.id"] = user_id
    if status:
        attrs["cursor.stop.status"] = status
    if loop_count:
        attrs["cursor.stop.loop_count"] = loop_count
    if duration_ms is not None:
        attrs["cursor.stop.duration_ms"] = duration_ms
    # No turn and no token counts means nothing else carries the model name
    # for this stop — keep it on the standalone Agent Stop span rather than
    # losing it.
    if not emitted_llm_span and "llm.model_name" in token_attrs:
        attrs["llm.model_name"] = token_attrs["llm.model_name"]

    # If no turn state exists, retain a useful standalone terminal span.
    send_span(
        build_span(
            "Agent Stop",
            "CHAIN",
            sid,
            resolved_trace,
            parent,
            now_ms,
            now_ms,
            attrs,
            SERVICE_NAME,
            SCOPE_NAME,
        )
    )

    # Clean up only the turn actually closed here (its own canonical id plus
    # every alias it accumulated) — never `canonical_gen` from `_resolve_turn`
    # when that was merely a mismatched active turn's identity this stop did
    # NOT close; deleting that turn's gen_root/state out from under it would
    # disconnect its still-in-progress spans from their parent.
    closed_gen = turn.get("generation_id", "") if turn else ""
    aliases = (turn.get("generation_aliases") if turn else None) or []
    if closed_gen:
        state_cleanup_generation(closed_gen)
    for alias in aliases:
        state_cleanup_generation(alias)
    if gen_id and gen_id != closed_gen and gen_id not in aliases:
        # The raw incoming id's own root/stack state is always safe to clean
        # up — it is keyed by exactly that id regardless of which turn (if
        # any) this stop actually closed.
        state_cleanup_generation(gen_id)
    if turn:
        terminal_turn_mark_many(turn_key, [closed_gen, *aliases, gen_id])
    log(f"stop: span {sid}, cleaned up gen={closed_gen or gen_id}")


def _handle_session_start(input_json, conversation_id, gen_id, trace_id, now_ms):
    """CHAIN span for Cursor CLI sessionStart event.

    Also saves this span as the gen_root for ``gen_id`` when present. Pure
    CLI flows can omit ``beforeSubmitPrompt`` entirely (no "turn" concept),
    so without this, shell/stop/other CLI spans that carry the same
    generation_id as sessionStart would resolve no parent at all and become
    disconnected trace roots instead of children of the session.
    """
    if _is_cursor_ide_hook_payload(input_json):
        log("sessionStart: skipped standalone IDE session trace")
        return

    sid = span_id_16()

    attrs = {
        "openinference.span.kind": "CHAIN",
        "session.id": conversation_id,
    }
    if conversation_id:
        attrs["cursor.conversation.id"] = conversation_id

    cwd = _jq_str(input_json, "cwd", "workspace_root")
    if cwd:
        attrs["cursor.session.cwd"] = cwd

    user_id = _resolve_user_id(input_json)
    if user_id:
        attrs["user.id"] = user_id

    span = build_span(
        "Session Start",
        "CHAIN",
        sid,
        trace_id,
        "",
        now_ms,
        now_ms,
        attrs,
        SERVICE_NAME,
        SCOPE_NAME,
    )
    send_span(span)

    if gen_id:
        gen_root_span_save(gen_id, sid)

    log(f"sessionStart: span {sid} (trace={trace_id})")


def _handle_session_end(input_json, conversation_id, gen_id, trace_id, now_ms):
    """Flush a pending turn, then emit one point span for the CLI session end."""
    turn_key = _turn_state_key(conversation_id, gen_id)
    _flush_active_turn(turn_key, now_ms)
    sid = span_id_16()
    duration_raw = input_json.get("duration_ms")
    duration_ms = _to_int(duration_raw if duration_raw is not None else input_json.get("durationMs"))
    final_status = _jq_str(input_json, "final_status", "finalStatus", "status")
    reason = _jq_str(input_json, "reason")
    attrs = {
        "openinference.span.kind": "CHAIN",
        "session.id": conversation_id,
    }
    if conversation_id:
        attrs["cursor.conversation.id"] = conversation_id
    user_id = _resolve_user_id(input_json)
    if user_id:
        attrs["user.id"] = user_id
    if duration_ms is not None:
        attrs["cursor.session.duration_ms"] = duration_ms
    if final_status:
        attrs["cursor.session.final_status"] = final_status
    if reason:
        attrs["cursor.session.reason"] = reason

    raw_input = input_json.get("input_tokens")
    input_tokens = _to_int(raw_input if raw_input is not None else input_json.get("inputTokens"))
    raw_output = input_json.get("output_tokens")
    output_tokens = _to_int(raw_output if raw_output is not None else input_json.get("outputTokens"))
    raw_read = input_json.get("cache_read_tokens")
    cache_read = _to_int(raw_read if raw_read is not None else input_json.get("cacheReadTokens"))
    raw_write = input_json.get("cache_write_tokens")
    cache_write = _to_int(raw_write if raw_write is not None else input_json.get("cacheWriteTokens"))
    prompt_total = None
    if input_tokens is not None:
        attrs["cursor.session.token_count.input"] = input_tokens
        prompt_total = input_tokens + (cache_read or 0) + (cache_write or 0)
        attrs["cursor.session.token_count.prompt"] = prompt_total
    if output_tokens is not None:
        attrs["cursor.session.token_count.output"] = output_tokens
    if cache_read is not None:
        attrs["cursor.session.token_count.cache_read"] = cache_read
    if cache_write is not None:
        attrs["cursor.session.token_count.cache_write"] = cache_write
    if prompt_total is not None and output_tokens is not None:
        attrs["cursor.session.token_count.total"] = prompt_total + output_tokens

    send_span(
        build_span(
            "Session End",
            "CHAIN",
            sid,
            trace_id,
            "",
            now_ms,
            now_ms,
            attrs,
            SERVICE_NAME,
            SCOPE_NAME,
        )
    )
    if gen_id:
        state_cleanup_generation(gen_id)
    # The conversation itself is over: make sure no active turn lingers.
    # Terminal-marker history and lock files are deliberately left alone —
    # see `conversation_cleanup`'s docstring for why.
    conversation_cleanup(conversation_id)
    log(f"sessionEnd: span {sid}, cleaned up gen={gen_id}")


_DEDICATED_TOOL_NAMES = frozenset(
    {
        "shell",
        "terminal",
        "bash",
        "run_command",
        "run_shell",
        "read_file",
        "read",
        "view_file",
        "view",
        "edit_file",
        "edit",
        "write_file",
        "write",
        "create_file",
        "delete_file",
        "tab_file_read",
        "tab_file_edit",
        "mcp",
        "mcp_execution",
    }
)


def _handle_post_tool_use(input_json, conversation_id, gen_id, trace_id, now_ms):
    """TOOL span for Cursor CLI postToolUse event."""
    tool_name = _jq_str(input_json, "tool_name", "toolName", "name", "tool")

    # Dedup: skip tools that have dedicated before*/after* handlers
    if tool_name.lower() in _DEDICATED_TOOL_NAMES:
        log(f"postToolUse: skipping {tool_name!r} — covered by dedicated handler")
        return

    sid = span_id_16()
    resolved_trace, parent, _, _ = _resolve_turn(conversation_id, gen_id, trace_id, now_ms)

    tool_input = _jq_str(input_json, "tool_input", "toolInput", "input", "arguments", "args")
    output = _jq_str(input_json, "result", "output", "response", "stdout")

    tool_input = redact_content(env.log_tool_content, tool_input)
    output = redact_content(env.log_tool_content, output)

    user_id = _resolve_user_id(input_json)

    attrs = {
        "openinference.span.kind": "TOOL",
        "session.id": conversation_id,
    }
    if conversation_id:
        attrs["cursor.conversation.id"] = conversation_id
    if user_id:
        attrs["user.id"] = user_id
    if tool_name:
        attrs["tool.name"] = tool_name
    if tool_input:
        attrs["input.value"] = tool_input
    if output:
        attrs["output.value"] = output

    span_name = f"Tool: {tool_name}" if tool_name else "Tool Use"

    span = build_span(
        span_name,
        "TOOL",
        sid,
        resolved_trace,
        parent,
        now_ms,
        now_ms,
        attrs,
        SERVICE_NAME,
        SCOPE_NAME,
    )
    send_span(span)
    log(f"postToolUse: span {sid} (tool={tool_name})")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main():
    """Entry point for arize-hook-cursor. Cursor hook.

    Input contract: JSON on stdin, all 15 events (IDE + CLI) routed here.
    stdout: MUST print permissive JSON response, even on error.
    stderr: redirected to ARIZE_LOG_FILE at adapter import time via
        core.common.redirect_stderr_to_log_file().
    """
    event = ""
    try:
        if not check_requirements():
            return

        input_json = json.loads(sys.stdin.read() or "{}")
        event = _event_name(input_json)
        _dispatch(event, input_json)
    except Exception as e:
        error(f"cursor hook failed ({event}): {e}")
    finally:
        # ALWAYS print permissive response — this is the LAST thing that happens
        _print_permissive(event)
