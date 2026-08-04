"""Constants for the Codex tracing harness."""

from __future__ import annotations

import os
from pathlib import Path

HARNESS_NAME = "codex"
DISPLAY_NAME = "Codex CLI"
HARNESS_HOME = ".codex"  # ~/.codex — presence check for soft install detection
HARNESS_BIN = "codex"  # binary name for shutil.which() fallback

CODEX_CONFIG_DIR = Path.home() / ".codex"
CODEX_CONFIG_FILE = CODEX_CONFIG_DIR / "config.toml"
CODEX_ENV_FILE = CODEX_CONFIG_DIR / "arize-env.sh"

NOTIFY_BIN_NAME = "arize-hook-codex-notify"


def get_codex_home() -> Path:
    """Return the active Codex home, matching Codex's ``CODEX_HOME`` rules.

    An explicitly set home must already exist and be a directory.  This is
    important for installers: an invalid override must fail rather than
    silently modifying the default ``~/.codex`` profile.
    """
    raw_home = os.environ.get("CODEX_HOME")
    if raw_home is None or raw_home == "":
        return Path.home() / ".codex"

    requested = Path(raw_home)
    try:
        home = requested.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"CODEX_HOME points to an invalid path: {raw_home!r}") from exc

    if not home.is_dir():
        raise ValueError(f"CODEX_HOME points to a non-directory: {raw_home!r}")
    return home
