#!/usr/bin/env python3
"""Single source of truth for all filesystem paths used by coding-harness-tracing.

Every module that needs a path imports it from here. Tests monkeypatch these
values via the tmp_harness_dir fixture to avoid touching the real filesystem.
"""
from pathlib import Path
from typing import TypedDict


class HarnessMetadata(TypedDict):
    service_name: str
    scope_name: str
    default_project_name: str
    state_subdir: str
    default_log_file: Path


# --- Base layout ---
BASE_DIR = Path.home() / ".arize" / "harness"
CONFIG_FILE = BASE_DIR / "config.yaml"

# --- Runtime directories ---
PID_DIR = BASE_DIR / "run"
LOG_DIR = BASE_DIR / "logs"
BIN_DIR = BASE_DIR / "bin"
VENV_DIR = BASE_DIR / "venv"

# --- Per-harness state ---
STATE_BASE_DIR = BASE_DIR / "state"

# --- Harness metadata ---
# Used by adapters to look up service_name, scope_name, state_subdir, etc.
# Keys match the harness names used in config.yaml harnesses section.
HARNESSES: dict[str, HarnessMetadata] = {
    "claude-code": {
        "service_name": "claude-code",
        "scope_name": "arize-claude-plugin",
        "default_project_name": "claude-code",
        "state_subdir": "claude-code",
        "default_log_file": LOG_DIR / "claude-code.log",
    },
    "codex": {
        "service_name": "codex",
        "scope_name": "arize-codex-plugin",
        "default_project_name": "codex",
        "state_subdir": "codex",
        "default_log_file": LOG_DIR / "codex.log",
    },
    "cursor": {
        "service_name": "cursor",
        "scope_name": "arize-cursor-plugin",
        "default_project_name": "cursor",
        "state_subdir": "cursor",
        "default_log_file": LOG_DIR / "cursor.log",
    },
    "copilot": {
        "service_name": "copilot",
        "scope_name": "arize-copilot-plugin",
        "default_project_name": "copilot",
        "state_subdir": "copilot",
        "default_log_file": LOG_DIR / "copilot.log",
    },
    "gemini": {
        "service_name": "gemini",
        "scope_name": "arize-gemini-plugin",
        "default_project_name": "gemini",
        "state_subdir": "gemini",
        "default_log_file": LOG_DIR / "gemini.log",
    },
}
