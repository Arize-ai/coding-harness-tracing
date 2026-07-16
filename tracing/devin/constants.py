"""Constants for the Devin CLI tracing harness."""

from __future__ import annotations

from pathlib import Path

HARNESS_NAME = "devin"
DISPLAY_NAME = "Devin"

# Self-contained service/scope identity (Kiro pattern — not in core HARNESSES).
SERVICE_NAME = "devin"
SCOPE_NAME = "arize-devin-plugin"
DEFAULT_PROJECT_NAME = "devin"

# Soft install detection.
HARNESS_HOME = ".config/devin"  # ~/.config/devin — presence check
HARNESS_BIN = "devin"  # binary name for shutil.which() fallback

# Global hook config file (hooks nest under a top-level "hooks" key here).
CONFIG_FILE = Path.home() / ".config" / "devin" / "config.json"

# Local data dir where Devin persists sessions + transcripts.
DATA_DIR = Path.home() / ".local" / "share" / "devin" / "cli"
TRANSCRIPTS_DIR = DATA_DIR / "transcripts"
SESSIONS_DB = DATA_DIR / "sessions.db"

# Single hook binary; the handler dispatches by hook_event_name.
HOOK_BIN_NAME = "arize-hook-devin"

# We only register SessionEnd — the transcript is complete at session end.
HOOK_EVENTS = ("SessionEnd",)

DEFAULT_LOG_FILE = Path.home() / ".arize" / "harness" / "logs" / "devin.log"
