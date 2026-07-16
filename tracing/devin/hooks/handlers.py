"""Devin CLI hook handlers — the ``SessionEnd`` entry point.

Span model (transcript-driven, deferred to session end):
- Devin hook payloads are thin, so no live per-turn spans are emitted. When
  ``SessionEnd`` fires we resolve the session's ATIF-v1.7 transcript, parse it,
  and emit the full OpenInference span tree in one shot:
    * one root AGENT span for the whole session (with session-level token totals)
    * one LLM span per ``source == "agent"`` step (with per-step token counts)
    * one TOOL span per tool call issued by a step
  Tokens only exist at session end and OTLP spans are immutable once exported,
  so deferred one-shot emission is correct.

Like every hook here, ``main()`` never raises: it catches all exceptions and
returns 0 so a bug can never crash Devin.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

from core.common import build_span, env, generate_span_id, generate_trace_id, log, redact_content, send_span
from tracing.devin.constants import SCOPE_NAME, SERVICE_NAME
from tracing.devin.hooks.adapter import already_emitted, check_requirements, mark_emitted, resolve_transcript_path
from tracing.devin.transcript import ParsedSession, parse_transcript

# ---------------------------------------------------------------------------
# Token attributes
# ---------------------------------------------------------------------------


def _token_attrs(prompt: int, completion: int, cached: int = 0) -> dict:
    """Build OpenInference token-count attributes, omitting any that are 0.

    Follows the convention: ``prompt`` is the inclusive prompt total,
    ``total = prompt + completion``, and cached tokens are a subset of the
    prompt reported on ``prompt_details.cache_read`` — never a flat key.
    """
    attrs: dict[str, Any] = {}
    if prompt:
        attrs["llm.token_count.prompt"] = prompt
    if completion:
        attrs["llm.token_count.completion"] = completion
    total = prompt + completion
    if total:
        attrs["llm.token_count.total"] = total
    if cached:
        attrs["llm.token_count.prompt_details.cache_read"] = cached
    return attrs


# ---------------------------------------------------------------------------
# Span emission
# ---------------------------------------------------------------------------


def _resolve_project_name() -> str:
    """Project name from config/env, falling back to the working dir basename."""
    if env.project_name:
        return env.project_name
    project_dir = os.environ.get("DEVIN_PROJECT_DIR") or os.getcwd()
    base = os.path.basename(os.path.normpath(project_dir)) if project_dir else ""
    return base or "devin"


def emit_session_spans(parsed: ParsedSession) -> None:
    """Emit the full span tree for one parsed session: root + steps + tools."""
    trace_id = generate_trace_id()
    root_span_id = generate_span_id()

    project_name = _resolve_project_name()
    user_id = env.get_user_id(SERVICE_NAME)

    # --- Root AGENT span -----------------------------------------------------
    last_output = parsed.steps[-1].assistant_text if parsed.steps else ""
    root_attrs: dict[str, Any] = {
        "session.id": parsed.session_id,
        "openinference.span.kind": "AGENT",
        "input.value": redact_content(env.log_prompts, "\n\n".join(parsed.user_prompts)),
        "output.value": redact_content(env.log_prompts, last_output),
    }
    if parsed.model_name:
        root_attrs["llm.model_name"] = parsed.model_name
    root_attrs.update(
        _token_attrs(
            parsed.total_prompt_tokens,
            parsed.total_completion_tokens,
            parsed.total_cached_tokens,
        )
    )
    if project_name:
        root_attrs["project.name"] = project_name
    if user_id:
        root_attrs["user.id"] = user_id
    if parsed.backend:
        root_attrs["devin.backend"] = parsed.backend
    if parsed.agent_version:
        root_attrs["devin.agent_version"] = parsed.agent_version

    root_span = build_span(
        f"Devin Session {parsed.session_id}",
        "AGENT",
        root_span_id,
        trace_id,
        "",  # root — no parent
        parsed.start_ms,
        parsed.end_ms,
        root_attrs,
        SERVICE_NAME,
        SCOPE_NAME,
    )
    send_span(root_span)

    # --- Per-step LLM spans (+ their TOOL spans) -----------------------------
    for step in parsed.steps:
        step_span_id = generate_span_id()
        redacted_text = redact_content(env.log_prompts, step.assistant_text)
        output_messages = [{"message.role": "assistant", "message.content": redacted_text}]

        step_attrs: dict[str, Any] = {
            "session.id": parsed.session_id,
            "openinference.span.kind": "LLM",
            "output.value": redacted_text,
            "llm.output_messages": json.dumps(output_messages),
        }
        if step.model_name:
            step_attrs["llm.model_name"] = step.model_name
        if step.reasoning:
            step_attrs["llm.reasoning"] = redact_content(env.log_prompts, step.reasoning)
        step_attrs.update(_token_attrs(step.prompt_tokens, step.completion_tokens))

        step_span = build_span(
            f"LLM step {step.step_id}",
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
                "session.id": parsed.session_id,
                "openinference.span.kind": "TOOL",
                "tool.name": tc.name,
                "input.value": redact_content(env.log_tool_content, json.dumps(tc.arguments)),
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


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


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
        if event != "SessionEnd":
            log(f"Ignoring hook_event_name: {event!r} (only SessionEnd is registered)")
            return 0

        project_dir = os.environ.get("DEVIN_PROJECT_DIR") or os.getcwd()
        path = resolve_transcript_path(project_dir)
        if path is None:
            log("SessionEnd: no transcript found")
            return 0

        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            log(f"SessionEnd: could not read transcript {path}: {exc!r}")
            return 0

        parsed = parse_transcript(data)

        if parsed.session_id and already_emitted(parsed.session_id):
            log(f"SessionEnd: already emitted session {parsed.session_id}")
            return 0

        emit_session_spans(parsed)

        if parsed.session_id:
            mark_emitted(parsed.session_id)

        log(f"SessionEnd: emitted spans for session {parsed.session_id!r}")
    except Exception as exc:  # noqa: BLE001 — never block Devin on a bug
        log(f"hook main() crashed: {exc!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
