"""Which ``arize-harness`` is running setup, and whether it may write anything.

Two helpers every installer shares:

- ``hook_command(harness, event)``: the command a harness config runs for a
  hook, ``'<path>' hook <harness> <event>``, where ``<path>`` is the
  ``arize-harness`` that ran setup.
- ``may_write_setup()``: whether that ``arize-harness`` is a persistent
  install, so the files setup writes keep pointing at something that exists.

Only ``core.setup`` and its callers import this; the hook path never does
(Arize-ai/coding-harness-tracing#161).
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

# Printed whenever setup refuses to write from a non-persistent install.
INSTALL_HINT = (
    f"install it with `uv tool install {DIST_NAME}` or `pipx install {DIST_NAME}`, "
    f"then run `{COMMAND_NAME} setup` from there"
)


class SetupRefused(SystemExit):
    """Raised by ``require_setup_writes``; exits 1 after printing the reason."""

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
    """``path`` made absolute without resolving symlinks: a uv tool link must stay the link."""
    return Path(os.path.abspath(path))


def _with_exe(path: Path) -> Path:
    """On Windows, the launcher ``.exe`` that ``sys.argv[0]`` may name without its suffix."""
    if _is_windows() and path.suffix.lower() != ".exe":
        return path.with_name(path.name + ".exe")
    return path


def arize_harness_path() -> Path:
    """The ``arize-harness`` that is running, as it was invoked.

    - A Nuitka binary: its own path.
    - ``arize-harness`` run from a uv tool / pipx command link: that link
      (``sys.argv[0]``), not the venv script it points at.
    - Otherwise (``python -m core.cli``, an installer run directly, tests): the
      ``arize-harness`` next to this interpreter, which is the one the
      interpreter's environment installed.
    """
    if is_binary():
        return _absolute(sys.argv[0] or sys.executable)
    argv0 = sys.argv[0] if sys.argv else ""
    if argv0 and _is_command_name(PurePath(argv0)):
        return _with_exe(_absolute(argv0))
    scripts = Path(sys.executable).parent
    return _with_exe(Path(os.path.abspath(scripts / COMMAND_NAME)))


def hook_argv(harness: str, event: Optional[str] = None) -> List[str]:
    """``[<path>, "hook", <harness>, <event>]`` (no event for single-entry harnesses).

    ``harness`` may be an install name (``claude-code``); the command carries
    the hook table's name (``claude``). Raises ValueError for a harness or
    event ``arize-harness hook`` would not dispatch.
    """
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
    """Single-quote ``word`` for a POSIX shell (always quoted, unlike ``shlex.quote``)."""
    return "'" + word.replace("'", "'\"'\"'") + "'"


def hook_command(harness: str, event: Optional[str] = None) -> str:
    """The shell command a harness config runs: ``'<path>' hook <harness> <event>``.

    The path is written with forward slashes and single-quoted, as the hook
    commands in settings files are run by a POSIX shell (Git Bash on Windows).
    Put the result in a JSON config with ``json.dumps``; for a TypeScript
    ``spawn`` call use ``ts_argv`` instead, which needs no shell.
    """
    path, *rest = hook_argv(harness, event)
    return " ".join([sh_quote(Path(path).as_posix()), *rest])


def ts_argv(harness: str, event: Optional[str] = None) -> tuple:
    """``(file, args)`` as TypeScript string literals, for ``spawn(file, args)``.

    JSON string syntax is valid TypeScript, and ``json.dumps`` escapes every
    quote, backslash and line separator, so a Windows path survives intact.
    """
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
    from core.setup import VENV_DIR  # late: core.setup imports this module's callers

    return VENV_DIR


# Files each tool writes into an environment it installed and will keep:
# ``uv tool install`` a receipt, ``pipx install`` its metadata. Ephemeral
# ``uvx`` environments have no receipt. Checking for them avoids guessing where
# each tool keeps its environments on each platform.
_PERSISTENT_MARKERS = ("uv-receipt.toml", "pipx_metadata.json")

# uv's cache layout for uvx / ``uv run --with`` environments.
_UV_CACHE_PARTS = ("archive-v0", "builds-v0")


def _in_tool_cache(prefix: Path) -> bool:
    """True for uv's cache (``.../archive-v0/<hash>``) or pipx run's (``.../pipx/.cache/<hash>``, ``~/.cache/pipx/<hash>``).

    Only those layouts: a directory merely named ``cache`` elsewhere in the
    path (``HOME=/cache`` in a container) is not a tool cache.
    """
    parts = _parts(prefix)
    if any(part in _UV_CACHE_PARTS for part in parts):
        return True
    pairs = list(zip(parts, parts[1:]))
    return ("pipx", ".cache") in pairs or (".cache", "pipx") in pairs


def _package_dir() -> Path:
    import core

    return _norm(Path(core.__file__).parent)


def setup_write_refusal() -> Optional[str]:
    """Why this ``arize-harness`` may not write setup files, or None when it may.

    Allowed only for installs known to persist:

    - a Nuitka binary outside the temp directory;
    - a ``uv tool install`` (its environment holds ``uv-receipt.toml``);
    - a ``pipx install`` (its environment holds ``pipx_metadata.json``);
    - on Windows, the ``install.bat`` venv at ``~/.arize/harness/venv``.

    Everything else is refused, including ``uvx`` and ``pipx run`` (a cache), ``uv run`` and
    any dev checkout (the package is not inside the environment), the old
    ``~/.arize/harness/venv`` on macOS/Linux, and anything under the temp
    directory. Unknown layouts are refused too.
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
    """True when this ``arize-harness`` is a persistent install that setup may write from."""
    return setup_write_refusal() is None


def require_setup_writes() -> None:
    """Raise ``SetupRefused`` (exit 1) with the install hint unless ``may_write_setup()``."""
    reason = setup_write_refusal()
    if reason is None:
        return
    from core.setup import err

    message = f"{reason}; {INSTALL_HINT}. Nothing was written."
    err(message)
    raise SetupRefused(message)
