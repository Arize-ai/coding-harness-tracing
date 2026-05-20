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
SESSION_BIN_NAME = "arize-hook-codex-session"
TOOL_BIN_NAME = "arize-hook-codex-tool"
STOP_BIN_NAME = "arize-hook-codex-stop"
BUFFER_BIN_NAME = "arize-codex-buffer"
BUFFER_PORT = 4318
BUFFER_PID_FILE = Path.home() / ".arize" / "harness" / "run" / "codex-buffer.pid"
BUFFER_LOG_FILE = Path.home() / ".arize" / "harness" / "logs" / "codex-buffer.log"
OTEL_ENDPOINT = f"http://127.0.0.1:{BUFFER_PORT}/v1/logs"
