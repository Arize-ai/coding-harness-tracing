"""Constants for the Codex tracing harness."""

from __future__ import annotations

import os
from pathlib import Path

HARNESS_NAME = "codex"
DISPLAY_NAME = "Codex CLI"
HARNESS_HOME = ".codex"  # ~/.codex — presence check for soft install detection
HARNESS_BIN = "codex"  # binary name for shutil.which() fallback

NOTIFY_BIN_NAME = "arize-hook-codex-notify"


def get_codex_home() -> Path:
    """Return the active Codex home, matching Codex's ``CODEX_HOME`` rules.

    ``CODEX_HOME`` is expanded the way a shell would (``~`` and ``$VAR``)
    before use.  An explicitly set home must resolve to an existing
    directory — this is important for installers: an invalid override must
    fail rather than silently modifying the default ``~/.codex`` profile.
    """
    raw_home = os.path.expandvars(os.path.expanduser(os.environ.get("CODEX_HOME") or ""))
    if raw_home == "":
        return Path.home() / ".codex"

    try:
        home = Path(raw_home).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"CODEX_HOME points to an invalid path: {raw_home!r}") from exc

    if not home.is_dir():
        raise ValueError(f"CODEX_HOME points to a non-directory: {raw_home!r}")
    return home
