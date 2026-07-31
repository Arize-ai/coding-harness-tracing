"""Constants for the Devin CLI tracing harness."""

from __future__ import annotations

import os
import platform
from pathlib import Path

HARNESS_NAME = "devin"
DISPLAY_NAME = "Devin"

# Self-contained service/scope identity (Kiro pattern — not in core HARNESSES).
SERVICE_NAME = "devin"
SCOPE_NAME = "arize-devin-plugin"
DEFAULT_PROJECT_NAME = "devin"

HARNESS_BIN = "devin"  # binary name for shutil.which() fallback


def _is_windows() -> bool:
    return platform.system() == "Windows"


def config_dir() -> Path:
    """Devin's user-wide config directory.

    macOS/Linux: ``~/.config/devin``. Windows: ``%APPDATA%\\devin`` — per Devin's
    docs the Windows config lives there and explicitly *not* under
    ``~\\.config\\devin``, so writing hooks to the POSIX path would register them
    in a file Devin never reads.
    """
    if _is_windows():
        appdata = os.environ.get("APPDATA")
        if appdata:
            return Path(appdata) / "devin"
        return Path.home() / "AppData" / "Roaming" / "devin"
    return Path.home() / ".config" / "devin"


def data_dir() -> Path:
    """Directory holding Devin's live sessions DB.

    Home-relative on every platform: ``~/.local/share/devin/cli``, which on
    Windows resolves under the user profile
    (``%USERPROFILE%\\.local\\share\\devin\\cli``) rather than ``%APPDATA%`` —
    unlike the config file, which *is* ``%APPDATA%``-based. See ``config_dir``.
    """
    return Path.home() / ".local" / "share" / "devin" / "cli"


def harness_home_subdir() -> str:
    """Home-relative config dir for the soft install check, per platform.

    ``is_harness_installed`` joins this onto ``Path.home()``. When the config
    dir is not under the home directory (e.g. a redirected Windows profile) the
    check falls back to finding ``devin`` on PATH.
    """
    try:
        return str(config_dir().relative_to(Path.home()))
    except ValueError:
        return ".config/devin"


# Soft install detection.
HARNESS_HOME = harness_home_subdir()

# Global hook config file (hooks nest under a top-level "hooks" key here).
CONFIG_FILE = config_dir() / "config.json"

# Local data dir where Devin persists the live sessions DB.
DATA_DIR = data_dir()
SESSIONS_DB = DATA_DIR / "sessions.db"

# Single hook binary; the handler dispatches by hook_event_name.
HOOK_BIN_NAME = "arize-hook-devin"

# Stop fires per agent response (per-turn emission); SessionEnd is a final
# flush for an interrupted last turn. Both dispatch to the same hook binary.
HOOK_EVENTS = ("Stop", "SessionEnd")

DEFAULT_LOG_FILE = Path.home() / ".arize" / "harness" / "logs" / "devin.log"
