#!/usr/bin/env python3
"""Antigravity hook handlers — transcript-driven span emission.

Antigravity hooks are a control plane: the hook stdin payload carries
``conversationId`` and ``transcriptPath`` but no model or tool content. The
actual conversation lives in the transcript file. So hooks here are only
*triggers* — the transcript parser is the source of truth.

Two hook events drive emission:

* ``PreInvocation`` — fires before each model call. Acts as a backstop: emits
  spans for any earlier turn whose ``Stop`` was missed (crash/kill). The
  in-progress (final) turn is intentionally skipped so we don't double-emit
  once ``Stop`` arrives.
* ``Stop`` — fires when the agent loop ends. Emits the just-finished turn.

A high-water mark stored in state (``last_emitted_step``) makes the emission
idempotent: turns whose ``max_step_index`` is already ``<=`` the watermark are
skipped on subsequent runs.

Stdout discipline: each entry point prints exactly ``{}`` (never
``{"decision": "continue"}``, which would force Antigravity's agent loop to
re-enter). All diagnostics go through ``core.common.log``/``error`` (stderr).
"""
from __future__ import annotations

import json
import os
import sys

from core.common import (
    build_span,
    debug_dump,
    env,
    error,
    generate_span_id,
    generate_trace_id,
    log,
    redact_content,
    send_span,
)
from tracing.antigravity.hooks.adapter import (
    SCOPE_NAME,
    SERVICE_NAME,
    check_requirements,
    ensure_session_initialized,
    gc_stale_state_files,
    resolve_session,
)
from tracing.antigravity.hooks.transcript import parse_transcript

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _read_stdin() -> dict:
    """Read JSON from stdin. Returns {} on empty/invalid input."""
    try:
        raw = sys.stdin.read()
        return json.loads(raw) if raw else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _print_response() -> None:
    """Print exactly ``{}`` to stdout. Never ``{"decision": "continue"}``."""
    print(json.dumps({}))


def _send_span_async(span_dict: dict) -> None:
    """Send a span without blocking the hook process.

    Antigravity invokes hooks synchronously and waits for the subprocess to
    exit before resuming. The slowest part of the hook is the OTLP POST in
    send_span (up to ~10s). Double-fork detaches a grandchild reparented to
    init/launchd; the parent returns immediately so the hook exits in
    milliseconds.

    Falls back to synchronous send when ``fork()`` is unavailable (Windows)
    or when ``ARIZE_DISABLE_FORK=true`` (used by tests so spans are visible
    to ``captured_spans`` fixtures in the parent process).
    """
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
            # Best-effort reap: if the child is already gone/reaped, continue.
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
                # Best-effort stdio redirection in detached child; continue even if one fd cannot be remapped.
                pass
        os.close(devnull)
    except OSError:
        # Best-effort stdio detachment; continue even if /dev/null setup fails.
        pass
    try:
        send_span(span_dict)
    except Exception as exc:
        # Detached grandchild must never raise into the hook path; log and exit.
        error(f"[hooks] async span send failed in detached child: {exc}")
    os._exit(0)


# ---------------------------------------------------------------------------
# Span emission
# ---------------------------------------------------------------------------


def _emit_turn_spans(state, turn: dict, session_id: str, project_name: str, user_id: str) -> None:
    """Emit one CHAIN Turn span plus its LLM and TOOL children for a single turn."""
    trace_id = generate_trace_id()
    root_span_id = generate_span_id()
    model_name = turn.get("model_name", "") or ""
    user_input = turn.get("user_input", "") or ""
    final_response = turn.get("final_response", "") or ""

    root_attrs: dict = {
        "session.id": session_id,
        "project.name": project_name,
        "openinference.span.kind": "CHAIN",
        "input.value": redact_content(env.log_prompts, user_input),
        "output.value": redact_content(env.log_prompts, final_response),
    }
    if user_id:
        root_attrs["user.id"] = user_id

    _send_span_async(
        build_span(
            "Turn",
            "CHAIN",
            root_span_id,
            trace_id,
            "",
            turn.get("start_ms", 0),
            turn.get("end_ms", 0),
            root_attrs,
            SERVICE_NAME,
            SCOPE_NAME,
        )
    )

    llm_steps = turn.get("llm_steps") or []
    for idx, llm in enumerate(llm_steps):
        content = llm.get("content", "") or ""
        thinking = llm.get("thinking", "") or ""

        span_name = f"LLM: {model_name}" if model_name else "LLM"
        llm_attrs: dict = {
            "session.id": session_id,
            "project.name": project_name,
            "openinference.span.kind": "LLM",
            "llm.model_name": model_name,
            "input.value": redact_content(env.log_prompts, user_input) if idx == 0 else "",
            "output.value": redact_content(env.log_prompts, content),
        }
        if thinking:
            llm_attrs["llm.reasoning"] = redact_content(env.log_prompts, thinking)
        if user_id:
            llm_attrs["user.id"] = user_id

        _send_span_async(
            build_span(
                span_name,
                "LLM",
                generate_span_id(),
                trace_id,
                root_span_id,
                llm.get("start_ms", 0),
                llm.get("end_ms", 0),
                llm_attrs,
                SERVICE_NAME,
                SCOPE_NAME,
            )
        )

    tool_steps = turn.get("tool_steps") or []
    for tool in tool_steps:
        tool_name = tool.get("name", "") or ""
        args = tool.get("args") or {}
        output_text = tool.get("output", "") or ""

        try:
            args_json = json.dumps(args)
        except (TypeError, ValueError):
            args_json = str(args)

        # Build a short tool description from the most informative arg value
        # (command/file_path/url/query) so Arize lists are scannable without
        # opening every span. Fall back to the args blob when no canonical
        # key is present.
        description = ""
        if isinstance(args, dict):
            for key in ("command", "file_path", "absolute_path", "path", "url", "query", "pattern"):
                val = args.get(key)
                if isinstance(val, str) and val:
                    description = val
                    break
            if not description:
                description = args_json
        else:
            description = args_json
        description = description[:200]

        tool_attrs: dict = {
            "session.id": session_id,
            "project.name": project_name,
            "openinference.span.kind": "TOOL",
            "tool.name": tool_name,
            "input.value": redact_content(env.log_tool_content, args_json),
            "output.value": redact_content(env.log_tool_content, output_text),
            "tool.description": redact_content(env.log_tool_details, description),
        }
        if user_id:
            tool_attrs["user.id"] = user_id

        _send_span_async(
            build_span(
                tool_name,
                "TOOL",
                generate_span_id(),
                trace_id,
                root_span_id,
                tool.get("start_ms", 0),
                tool.get("end_ms", 0),
                tool_attrs,
                SERVICE_NAME,
                SCOPE_NAME,
            )
        )


def _emit_completed_turns(state, turns: list[dict], include_last: bool) -> None:
    """Emit spans for every completed turn whose step index is past the watermark.

    A turn is "complete" when it is not the most recent turn in ``turns``, or
    when ``include_last`` is True (set by ``Stop``). ``PreInvocation`` calls
    this with ``include_last=False`` so the in-progress final turn isn't
    emitted twice.
    """
    if not turns:
        return

    try:
        last = int(state.get("last_emitted_step") or "-1")
    except ValueError:
        last = -1

    session_id = state.get("session_id") or ""
    project_name = state.get("project_name") or ""
    user_id = state.get("user_id") or ""

    final_idx = len(turns) - 1
    for i, turn in enumerate(turns):
        is_final = i == final_idx
        if is_final and not include_last:
            continue
        max_step = int(turn.get("max_step_index", 0) or 0)
        if max_step <= last:
            continue
        _emit_turn_spans(state, turn, session_id, project_name, user_id)
        last = max_step

    state.set("last_emitted_step", str(last))


# ---------------------------------------------------------------------------
# Internal handler implementations
# ---------------------------------------------------------------------------


def _handle_pre_invocation(input_json: dict) -> None:
    """Backstop: emit spans for any earlier turn whose Stop was missed."""
    debug_dump("antigravity_pre_invocation", input_json)
    state = resolve_session(input_json)
    ensure_session_initialized(state, input_json)
    turns = parse_transcript(input_json.get("transcriptPath", "") or "")
    _emit_completed_turns(state, turns, include_last=False)


def _handle_stop(input_json: dict) -> None:
    """Emit spans for the just-finished turn (and any earlier missed turns)."""
    debug_dump("antigravity_stop", input_json)
    state = resolve_session(input_json)
    ensure_session_initialized(state, input_json)
    turns = parse_transcript(input_json.get("transcriptPath", "") or "")
    _emit_completed_turns(state, turns, include_last=True)
    gc_stale_state_files()
    session_id = state.get("session_id") or ""
    if session_id:
        log(f"antigravity stop: emitted up to step {state.get('last_emitted_step')}")


# ---------------------------------------------------------------------------
# CLI entry points
# ---------------------------------------------------------------------------


def pre_invocation() -> None:
    """Entry point for arize-hook-antigravity-pre-invocation."""
    try:
        input_json = _read_stdin()
        if check_requirements():
            _handle_pre_invocation(input_json)
    except Exception as e:
        error(f"antigravity pre_invocation hook failed: {e}")
    finally:
        _print_response()


def stop() -> None:
    """Entry point for arize-hook-antigravity-stop."""
    try:
        input_json = _read_stdin()
        if check_requirements():
            _handle_stop(input_json)
    except Exception as e:
        error(f"antigravity stop hook failed: {e}")
    finally:
        _print_response()


def main() -> None:
    """Manual execution dispatcher."""
    if len(sys.argv) < 2:
        print(f"usage: {sys.argv[0]} <handler_name>", file=sys.stderr)
        sys.exit(1)

    handler_name = sys.argv[1]
    handlers = {
        "pre_invocation": pre_invocation,
        "stop": stop,
    }

    handler = handlers.get(handler_name)
    if not handler:
        print(f"unknown handler: {handler_name}", file=sys.stderr)
        sys.exit(1)

    handler()


if __name__ == "__main__":
    main()
