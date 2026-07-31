"""Devin CLI hook handlers — per-turn span emission.

Span model (live, incremental, one trace per interaction):
- Devin fires a thin ``Stop`` hook at the end of each agent response. When it
  fires we resolve the session from ``DEVIN_PROJECT_DIR`` -> ``sessions.db``,
  read the generations that have appeared since we last emitted (deduped by
  ``request_id``), and emit one self-contained OpenInference trace for that
  interaction:
    * one root AGENT span (input = user prompt, output = final assistant text)
    * one LLM span per generation (content/reasoning, per-step tokens, model)
    * one TOOL span per tool call issued by a generation
  ``SessionEnd`` runs the same flush so a final/interrupted turn is not lost.
  Interactions are grouped in Arize by ``session.id``.

Like every hook here, ``main()`` never raises: it catches all exceptions and
returns 0 so a bug can never crash Devin.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

from core.common import build_span, env, generate_span_id, generate_trace_id, log, redact_content, send_span
from tracing.devin.constants import SCOPE_NAME, SERVICE_NAME, SESSIONS_DB
from tracing.devin.hooks.adapter import already_emitted, check_requirements, mark_emitted
from tracing.devin.session_db import (
    LlmStep,
    connect_readonly,
    latest_user_prompt,
    read_session_meta,
    read_steps,
    resolve_session_id,
)

# Events that trigger a flush of newly-appeared generations. Stop is the
# per-turn trigger; SessionEnd is a final safety-net flush.
_TRIGGER_EVENTS = ("Stop", "SessionEnd")


def _token_attrs(prompt: int, completion: int, cache_read: int = 0, cache_write: int = 0) -> dict:
    """Build OpenInference token-count attributes, omitting any that are 0.

    ``prompt`` is the inclusive prompt total, ``total = prompt + completion``,
    and cached tokens are subsets of the prompt reported on
    ``prompt_details.cache_read`` / ``prompt_details.cache_write`` — never flat
    keys.
    """
    attrs: dict[str, Any] = {}
    if prompt:
        attrs["llm.token_count.prompt"] = prompt
    if completion:
        attrs["llm.token_count.completion"] = completion
    total = prompt + completion
    if total:
        attrs["llm.token_count.total"] = total
    if cache_read:
        attrs["llm.token_count.prompt_details.cache_read"] = cache_read
    if cache_write:
        attrs["llm.token_count.prompt_details.cache_write"] = cache_write
    return attrs


def _resolve_project_name() -> str:
    """Project name from config/env, falling back to the working dir basename."""
    if env.project_name:
        return env.project_name
    project_dir = os.environ.get("DEVIN_PROJECT_DIR") or os.getcwd()
    base = os.path.basename(os.path.normpath(project_dir)) if project_dir else ""
    return base or "devin"


def _step_output(step: LlmStep) -> str:
    """Output text for an LLM span: visible content, or reasoning when the
    generation produced only thinking + tool calls (so the span still renders —
    ``llm.reasoning`` alone does not display in Arize)."""
    return step.content or step.thinking


def emit_interaction(session_id: str, steps: list[LlmStep], user_prompt: str, meta: dict) -> None:
    """Emit one trace (root AGENT + LLM/TOOL children) for a batch of new steps."""
    if not steps:
        return

    trace_id = generate_trace_id()
    root_span_id = generate_span_id()
    project_name = _resolve_project_name()
    user_id = env.get_user_id(SERVICE_NAME)

    start_ms = steps[0].start_ms
    end_ms = steps[-1].end_ms or start_ms
    final_output = _step_output(steps[-1])
    model_name = meta.get("model") or next((s.model_name for s in reversed(steps) if s.model_name), "")

    root_attrs: dict[str, Any] = {
        "session.id": session_id,
        "openinference.span.kind": "AGENT",
        "input.value": redact_content(env.log_prompts, user_prompt),
        "output.value": redact_content(env.log_prompts, final_output),
    }
    if model_name:
        root_attrs["llm.model_name"] = model_name
    root_attrs.update(
        _token_attrs(
            sum(s.prompt_tokens for s in steps),
            sum(s.completion_tokens for s in steps),
            sum(s.cache_read_tokens for s in steps),
            sum(s.cache_write_tokens for s in steps),
        )
    )
    if project_name:
        root_attrs["project.name"] = project_name
    if user_id:
        root_attrs["user.id"] = user_id
    if meta.get("backend"):
        root_attrs["devin.backend"] = meta["backend"]

    root_span = build_span(
        f"Devin Interaction {session_id}",
        "AGENT",
        root_span_id,
        trace_id,
        "",  # root — no parent
        start_ms,
        end_ms,
        root_attrs,
        SERVICE_NAME,
        SCOPE_NAME,
    )
    send_span(root_span)

    for step in steps:
        step_span_id = generate_span_id()
        output_text = redact_content(env.log_prompts, _step_output(step))
        output_messages = [{"message.role": "assistant", "message.content": output_text}]

        step_attrs: dict[str, Any] = {
            "session.id": session_id,
            "openinference.span.kind": "LLM",
            "output.value": output_text,
            "llm.output_messages": json.dumps(output_messages),
        }
        if step.model_name:
            step_attrs["llm.model_name"] = step.model_name
        if step.thinking:
            step_attrs["llm.reasoning"] = redact_content(env.log_prompts, step.thinking)
        step_attrs.update(
            _token_attrs(
                step.prompt_tokens,
                step.completion_tokens,
                step.cache_read_tokens,
                step.cache_write_tokens,
            )
        )

        step_span = build_span(
            f"LLM {step.request_id}",
            "LLM",
            step_span_id,
            trace_id,
            root_span_id,
            step.start_ms,
            step.end_ms,
            step_attrs,
            SERVICE_NAME,
            SCOPE_NAME,
        )
        send_span(step_span)

        for tc in step.tool_calls:
            tool_attrs: dict[str, Any] = {
                "session.id": session_id,
                "openinference.span.kind": "TOOL",
                "tool.name": tc.name,
                # Arguments are what the tool was asked to do -> tool_details;
                # the result is what it returned -> tool_content.
                "input.value": redact_content(env.log_tool_details, json.dumps(tc.arguments)),
                "output.value": redact_content(env.log_tool_content, tc.result),
            }
            tool_span = build_span(
                f"Tool: {tc.name}",
                "TOOL",
                generate_span_id(),
                trace_id,
                step_span_id,
                step.start_ms,
                step.end_ms,
                tool_attrs,
                SERVICE_NAME,
                SCOPE_NAME,
            )
            send_span(tool_span)


def flush_session(project_dir: str) -> int:
    """Resolve the session for ``project_dir`` and emit any not-yet-emitted
    generations. Returns the number of interactions emitted (0 if none / on any
    soft failure)."""
    con = None
    try:
        con = connect_readonly(SESSIONS_DB)
    except Exception as exc:  # noqa: BLE001 — DB open is best-effort
        log(f"flush_session: could not open {SESSIONS_DB}: {exc!r}")
        return 0
    if con is None:
        return 0
    try:
        session_id = resolve_session_id(con, project_dir)
        if not session_id:
            log(f"flush_session: no session for project_dir {project_dir!r}")
            return 0

        all_steps = read_steps(con, session_id)
        new_steps = [s for s in all_steps if not already_emitted(session_id, s.request_id)]
        if not new_steps:
            log(f"flush_session: no new generations for session {session_id}")
            return 0

        user_prompt = latest_user_prompt(con, session_id)
        meta = read_session_meta(con, session_id)
        emit_interaction(session_id, new_steps, user_prompt, meta)
        for step in new_steps:
            mark_emitted(session_id, step.request_id)
        log(f"flush_session: emitted {len(new_steps)} generation(s) for session {session_id}")
        return 1
    finally:
        try:
            con.close()
        except Exception:  # noqa: BLE001
            pass


def main() -> int:
    """Hook entry point. NEVER raises — always exits 0 even on internal error."""
    try:
        if not check_requirements():
            return 0

        try:
            input_json = json.load(sys.stdin)
        except (json.JSONDecodeError, ValueError) as exc:
            log(f"Could not parse stdin JSON: {exc}")
            return 0
        if not isinstance(input_json, dict):
            log(f"stdin payload not a dict: {type(input_json).__name__}")
            return 0

        event = input_json.get("hook_event_name") or ""
        if event not in _TRIGGER_EVENTS:
            log(f"Ignoring hook_event_name: {event!r} (triggers: {_TRIGGER_EVENTS})")
            return 0

        project_dir = os.environ.get("DEVIN_PROJECT_DIR") or os.getcwd()
        flush_session(project_dir)
    except Exception as exc:  # noqa: BLE001 — never block Devin on a bug
        log(f"hook main() crashed: {exc!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
