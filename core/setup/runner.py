"""Find the ``arize-harness`` running setup, and decide whether setup may write files.

``hook_command`` builds the hook command installers write into harness configs.
``may_write_setup`` is False for temporary installs such as uvx, whose path won't last.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path, PurePath
from typing import List, Optional

from core.hook_table import EVENT_HOOKS, SINGLE_HOOKS, canonical_harness
from core.runtime import is_binary

COMMAND_NAME = "arize-harness"
DIST_NAME = "coding-harness-tracing"

# Shown when setup refuses to write from a temporary install.
INSTALL_HINT = (
    f"install it with `uv tool install {DIST_NAME}` or `pipx install {DIST_NAME}`, "
    f"then run `{COMMAND_NAME} setup` from there"
)


class SetupRefused(SystemExit):

    def __init__(self, reason: str) -> None:
        super().__init__(1)
        self.reason = reason


# ---------------------------------------------------------------------------
# <path>: the arize-harness that ran setup
# ---------------------------------------------------------------------------


def _is_windows() -> bool:
    return os.name == "nt"


def _is_command_name(path: PurePath) -> bool:
    name = path.name.lower() if _is_windows() else path.name
    return name in (COMMAND_NAME, f"{COMMAND_NAME}.exe")


def _absolute(path: str) -> Path:
    """Make ``path`` absolute without resolving symlinks, so a uv tool link stays a link."""
    return Path(os.path.abspath(path))


def _with_exe(path: Path) -> Path:
    if _is_windows() and path.suffix.lower() != ".exe":
        return path.with_name(path.name + ".exe")
    return path


def arize_harness_path() -> Path:
    """Path of the running ``arize-harness``.

    The binary itself, the command link it was run from, or else the one next to
    this Python.
    """
    if is_binary():
        return _absolute(sys.argv[0] or sys.executable)
    argv0 = sys.argv[0] if sys.argv else ""
    if argv0 and _is_command_name(PurePath(argv0)):
        return _with_exe(_absolute(argv0))
    scripts = Path(sys.executable).parent
    return _with_exe(Path(os.path.abspath(scripts / COMMAND_NAME)))


def hook_argv(harness: str, event: Optional[str] = None) -> List[str]:
    """``[<path>, "hook", <harness>, <event>]`` (no event for single-entry harnesses)."""
    name = canonical_harness(harness.replace("_", "-"))
    if name in SINGLE_HOOKS:
        if event is not None:
            raise ValueError(f"harness '{name}' takes no event")
        return [str(arize_harness_path()), "hook", name]
    if name in EVENT_HOOKS:
        if event not in EVENT_HOOKS[name]:
            raise ValueError(f"unknown event {event!r} for harness '{name}'")
        return [str(arize_harness_path()), "hook", name, event]
    raise ValueError(f"unknown harness '{harness}'")


def sh_quote(word: str) -> str:
    return "'" + word.replace("'", "'\"'\"'") + "'"


def hook_command(harness: str, event: Optional[str] = None) -> str:
    """The hook command for a harness config: ``'<path>' hook <harness> <event>``.

    Quoted for a POSIX shell (Git Bash on Windows). Use ``ts_argv`` for TypeScript.
    """
    path, *rest = hook_argv(harness, event)
    return " ".join([sh_quote(Path(path).as_posix()), *rest])


def ts_argv(harness: str, event: Optional[str] = None) -> tuple:
    """The hook command as TypeScript string literals ``(file, args)`` for ``spawn()``."""
    path, *rest = hook_argv(harness, event)
    return json.dumps(path), json.dumps(rest)


# ---------------------------------------------------------------------------
# may_write_setup: is this a persistent install?
# ---------------------------------------------------------------------------


def _parts(path: Path) -> List[str]:
    return [p.lower() for p in path.parts] if _is_windows() else list(path.parts)


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _norm(path: Path) -> Path:
    try:
        return path.resolve()
    except OSError:
        return Path(os.path.abspath(path))


def _temp_roots() -> List[Path]:
    roots = {_norm(Path(tempfile.gettempdir()))}
    for var in ("TMPDIR", "TEMP", "TMP"):
        if os.environ.get(var):
            roots.add(_norm(Path(os.environ[var])))
    return sorted(roots)


def _old_venv_dir() -> Path:
    from core.setup import VENV_DIR  # imported here to avoid a circular import

    return VENV_DIR


# Files that ``uv tool install`` and ``pipx install`` leave in environments they keep.
_PERSISTENT_MARKERS = ("uv-receipt.toml", "pipx_metadata.json")

# uv cache folders used by uvx and ``uv run --with``.
_UV_CACHE_PARTS = ("archive-v0", "builds-v0")


def _in_tool_cache(prefix: Path) -> bool:
    """True if ``prefix`` is inside uv's or pipx's cache (uvx, pipx run)."""
    parts = _parts(prefix)
    if any(part in _UV_CACHE_PARTS for part in parts):
        return True
    pairs = list(zip(parts, parts[1:]))
    return ("pipx", ".cache") in pairs or (".cache", "pipx") in pairs


def _package_dir() -> Path:
    import core

    return _norm(Path(core.__file__).parent)


def setup_write_refusal() -> Optional[str]:
    """Why setup may not write from this install, or None if it may.

    Allowed: a uv tool or pipx install, the Windows ``install.bat`` venv, or a
    binary outside the temp dir. Everything else is refused.
    """
    if is_binary():
        binary = _norm(arize_harness_path())
        if any(_is_under(binary, t) for t in _temp_roots()):
            return f"{binary} is in a temporary directory"
        return None

    prefix = _norm(Path(sys.prefix))
    package = _package_dir()
    if not _is_under(package, prefix):
        return f"{COMMAND_NAME} is running from a source checkout ({package.parent})"
    if any(_is_under(prefix, t) for t in _temp_roots()):
        return f"{COMMAND_NAME} is running from a temporary environment ({prefix})"

    if prefix == _norm(_old_venv_dir()):
        if _is_windows():
            return None
        return f"{prefix} is the old install.sh venv"

    if _in_tool_cache(prefix):
        return f"{COMMAND_NAME} is running from a temporary environment ({prefix})"
    if any((prefix / marker).is_file() for marker in _PERSISTENT_MARKERS):
        return None
    return f"{COMMAND_NAME} is not running from a uv tool or pipx install ({prefix})"


def may_write_setup() -> bool:
    return setup_write_refusal() is None


def require_setup_writes() -> None:
    reason = setup_write_refusal()
    if reason is None:
        return
    from core.setup import err

    message = f"{reason}; {INSTALL_HINT}. Nothing was written."
    err(message)
    raise SetupRefused(message)
