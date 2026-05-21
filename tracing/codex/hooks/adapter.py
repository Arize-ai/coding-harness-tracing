#!/usr/bin/env python3
"""Codex adapter -- shared constants and env-file loader for the notify handler.

The earlier hook-based architecture (SessionStart, UserPromptSubmit, PreToolUse,
PostToolUse, Stop) is gone -- the notify handler now derives everything from
Codex's rollout JSONL on disk. What remains here is the minimum: harness
constants and an env-file loader.
"""
from __future__ import annotations

import os
from pathlib import Path

from core.common import env, redirect_stderr_to_log_file
from core.constants import HARNESSES

_HARNESS = HARNESSES["codex"]
SERVICE_NAME = _HARNESS["service_name"]  # "codex"
SCOPE_NAME = _HARNESS["scope_name"]  # "arize-codex-plugin"

# Route hook stderr to a per-harness log file unless the user already set one.
os.environ.setdefault("ARIZE_LOG_FILE", str(_HARNESS["default_log_file"]))
redirect_stderr_to_log_file()


def load_env_file(path: Path) -> None:
    """Source a simple KEY=VALUE env file (no shell expansion).

    Reads lines of the form ``export KEY=VALUE`` or ``KEY=VALUE`` and sets
    them in ``os.environ``.
    """
    try:
        text = path.read_text()
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("\"'")
        if key:
            os.environ[key] = value


def check_requirements() -> bool:
    """Return True if tracing is enabled. Hooks should early-return on False."""
    return bool(env.trace_enabled)
