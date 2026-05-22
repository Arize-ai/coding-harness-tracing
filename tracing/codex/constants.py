"""Constants for the Codex tracing harness."""

from __future__ import annotations

from pathlib import Path

HARNESS_NAME = "codex"
DISPLAY_NAME = "Codex CLI"
HARNESS_HOME = ".codex"  # ~/.codex — presence check for soft install detection
HARNESS_BIN = "codex"  # binary name for shutil.which() fallback

CODEX_CONFIG_DIR = Path.home() / ".codex"
CODEX_CONFIG_FILE = CODEX_CONFIG_DIR / "config.toml"
CODEX_ENV_FILE = CODEX_CONFIG_DIR / "arize-env.sh"

NOTIFY_BIN_NAME = "arize-hook-codex-notify"
