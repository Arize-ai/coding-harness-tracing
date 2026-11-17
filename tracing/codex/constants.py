"""Constants for the Codex tracing harness."""

from __future__ import annotations

import os
from pathlib import Path

HARNESS_NAME = "codex"
DISPLAY_NAME = "Codex CLI"
HARNESS_HOME = ".codex"  # ~/.codex — presence check for soft install detection
HARNESS_BIN = "codex"  # binary name for shutil.which() fallback

NOTIFY_BIN_NAME = "arize-hook-codex-notify"

# Where the installer saves the notify command it displaced (see install.py).
PREVIOUS_NOTIFY_KEY = f"harnesses.{HARNESS_NAME}.previous_notify"

# Set in the environment of a forwarded notify command. If that command calls
# our hook again (e.g. a wrapper that runs both), the nested call does nothing.
FORWARD_GUARD_ENV = "ARIZE_CODEX_NOTIFY_FORWARDED"


def notify_argv(value: object) -> list[str]:
    """Normalize a TOML ``notify`` value to an argv list."""
    if isinstance(value, list):
        return [v for v in value if isinstance(v, str)]
    if isinstance(value, str) and value:
        return [value]
    return []


def valid_previous_notify(value: object) -> list[str] | None:
    """Return *value* if it is a usable saved notify argv, else None."""
    if isinstance(value, list) and value and all(isinstance(a, str) and a for a in value):
        return list(value)
    return None


def is_notify_entry_point(arg: object) -> bool:
    """Return True if *arg* is a path to our notify entry point, from any install.

    Matches the exact executable name (``.exe`` on Windows), never a prefix, so
    a user's ``arize-hook-codex-notify-wrapper.sh`` is not mistaken for ours.
    """
    if not isinstance(arg, str) or not arg:
        return False
    name = arg.replace("\\", "/").rsplit("/", 1)[-1]
    return name == NOTIFY_BIN_NAME or name.lower() == f"{NOTIFY_BIN_NAME}.exe"


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
