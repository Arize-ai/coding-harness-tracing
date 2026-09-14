"""Context shared by every Claude turn export, independent of span rendering."""

from __future__ import annotations

import os

from core.common import StateManager, env, redact_content

DENIED_COUNT_KEY = "permission_denied_count"


def remember_permission_mode(state: StateManager, payload: dict) -> None:
    """Retain the latest mode on every hook, including hooks outside a turn."""
    if "permission_mode" in payload:
        state.set("permission_mode", payload.get("permission_mode") or "")


def collect_transcript_context(entry: dict, context: dict[str, str]) -> None:
    """Keep the first available values from records already decoded for a turn."""
    for field, attribute in (
        ("entrypoint", "session.entrypoint"),
        ("version", "claude_code.version"),
        ("gitBranch", "git.branch"),
    ):
        value = entry.get(field)
        if isinstance(value, str) and value:
            context.setdefault(attribute, value)


def turn_context_attributes(
    state: StateManager, payload: dict, transcript_context: dict[str, str] | None = None
) -> dict:
    """Build turn-only attributes, applying the tool-path redaction policy."""
    attrs = dict(transcript_context or {})
    cwd = payload.get("cwd") or state.get("session_cwd") or ""
    if cwd:
        attrs["session.cwd"] = redact_content(env.log_tool_details, cwd)
    mode = payload.get("permission_mode") or state.get("permission_mode") or ""
    if mode:
        attrs["permission.mode"] = mode
    entrypoint = os.environ.get("CLAUDE_CODE_ENTRYPOINT", "")
    if entrypoint:
        attrs["session.entrypoint"] = entrypoint
    attrs["permission.denied_count"] = int(state.get(DENIED_COUNT_KEY) or "0")
    return attrs
