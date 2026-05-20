#!/usr/bin/env python3
"""Codex SessionStart + UserPromptSubmit hook handler.

Reads one JSON object on stdin (per the Codex hook contract), inspects
``hook_event_name``, and updates the per-thread state file at
``~/.arize/harness/state/codex/state_<thread_id>.yaml``.

Orphaned-turn handling: a ``UserPromptSubmit`` arriving while
``turn_start_ms`` is still set (or a ``SessionStart`` with
``source in {clear, resume}``) finalizes the previous turn first via
``finalize_turn`` from ``tracing.codex.hooks.stop`` — same path Stop uses.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from core.common import env, error, get_timestamp_ms, log, redact_content
from tracing.codex.hooks.adapter import check_requirements, ensure_session_initialized, load_env_file, resolve_session


def _extract_thread_id(payload: dict) -> str:
    """Codex calls this ``session_id`` in newer payloads; older fields also accepted."""
    return payload.get("session_id") or payload.get("thread_id") or ""


def _finalize_pending_turn(state, thread_id: str) -> None:
    """Finalize a previous turn that never reached Stop.

    Delegates to ``finalize_turn`` so the same payload Stop would produce is
    sent. Lazy-imports to keep this module usable during partial installs
    where ``stop.py`` may not yet be present.
    """
    try:
        from tracing.codex.hooks.stop import finalize_turn
    except ImportError as e:
        log(f"session hook: stop module unavailable, skipping finalize: {e}")
        return
    finalize_turn(state, thread_id)


def _handle_session_start(payload: dict) -> None:
    thread_id = _extract_thread_id(payload)
    cwd = payload.get("cwd") or os.getcwd()

    state = resolve_session(thread_id)
    ensure_session_initialized(state, thread_id, cwd)

    source = payload.get("source")
    if state.get("turn_start_ms") and source in {"clear", "resume"}:
        _finalize_pending_turn(state, thread_id)

    model = payload.get("model")
    if model:
        state.set("model", model)

    permission_mode = payload.get("permission_mode")
    if permission_mode:
        state.set("permission_mode", permission_mode)

    sandbox_mode = payload.get("sandbox_mode") or os.environ.get("CODEX_SANDBOX_MODE")
    if sandbox_mode:
        state.set("sandbox_mode", sandbox_mode)

    payload_cwd = payload.get("cwd") or os.getcwd()
    if payload_cwd:
        state.set("cwd", payload_cwd)

    log(f"session start: thread={thread_id} source={source} model={model}")


def _handle_user_prompt_submit(payload: dict) -> None:
    thread_id = _extract_thread_id(payload)
    cwd = payload.get("cwd") or os.getcwd()

    state = resolve_session(thread_id)
    ensure_session_initialized(state, thread_id, cwd)

    if state.get("turn_start_ms"):
        _finalize_pending_turn(state, thread_id)

    state.set("turn_start_ms", str(get_timestamp_ms()))

    prompt_text = payload.get("prompt") or payload.get("user_prompt") or payload.get("input") or ""
    if not isinstance(prompt_text, str):
        prompt_text = str(prompt_text)
    redacted = redact_content(env.log_prompts, prompt_text)
    state.set("user_prompt", redacted)

    model = payload.get("model")
    if model:
        state.set("model", model)


def main() -> None:
    """Read JSON on stdin, dispatch on ``hook_event_name``, exit 0 always."""
    try:
        load_env_file(Path.home() / ".codex" / "arize-env.sh")
        if not check_requirements():
            return
        raw = sys.stdin.read()
        if not raw.strip():
            return
        payload = json.loads(raw)
        event = payload.get("hook_event_name") or payload.get("hook_event") or ""
        if event == "SessionStart":
            _handle_session_start(payload)
        elif event == "UserPromptSubmit":
            _handle_user_prompt_submit(payload)
        else:
            log(f"session hook: ignoring event {event!r}")
    except Exception as e:
        error(f"codex session hook failed: {e}")


if __name__ == "__main__":
    main()
