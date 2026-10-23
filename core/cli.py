"""``arize-harness`` command line entry point.

Usage:
    arize-harness hook <harness> [<event>] [args...]
    arize-harness hook --list
    arize-harness setup [<harness>] [--backend arize|phoenix] [--non-interactive|-y] [--with-skills]
    arize-harness uninstall <harness>|--all
    arize-harness status [--json]
    arize-harness update [--wheel-dir DIR]
    arize-harness --version

``hook`` runs the same handler as the matching ``arize-hook-*`` command, with
stdin and any remaining arguments passed through untouched. It is invoked on
every hook event, so it must start fast: keep this module's imports to the
standard library, and import anything heavier (``core.setup``, which pulls in
``python-dotenv``) inside the subcommand that needs it. The other subcommands
live in ``core.setup.commands`` and are parsed by argparse there, after
``hook`` has been ruled out, so a hook command never exits 2.
"""

import importlib
import os
import sys
from pathlib import Path
from typing import Any, Iterable, List, Optional, TextIO

from core.hook_table import ALIASES, EVENT_HOOKS, SINGLE_HOOKS, canonical_harness, iter_hooks, legacy_entry_point

_USAGE = (
    "usage: arize-harness hook <harness> [<event>] [args...] | arize-harness hook --list"
    " | arize-harness {setup,uninstall,status,update} ... | arize-harness --version"
)

# First arguments handed to core.setup.commands. Anything else that isn't
# ``hook`` gets the logged usage line and exit 0, like a bad hook command.
_COMMANDS = frozenset({"setup", "uninstall", "status", "update", "--version", "-h", "--help"})


# Default log name for dispatch errors without a recognized harness.
_FALLBACK_LOG_NAME = "arize-harness.log"


def _common(name: str) -> Any:
    """``core.common.<name>``, or None if nothing has imported ``core.common`` yet.

    Never imports ``core.common`` itself: that would slow every hook's startup.
    It only matters once a handler's adapter has imported it anyway.
    """
    return getattr(sys.modules.get("core.common"), name, None)


def _stderr() -> TextIO:
    """The harness's stderr, even if an adapter has already redirected ``sys.stderr`` to its log."""
    original_stderr = _common("original_stderr")
    return original_stderr() if original_stderr is not None else sys.stderr


def _warn(message: str) -> None:
    _stderr().write(f"[arize] {message}\n")


def _default_log_file(harness: str) -> Path:
    """The log file a harness's adapter would set as ``ARIZE_LOG_FILE``."""
    from core.constants import HARNESSES

    # HARNESSES is keyed by install.sh names (claude-code, not claude).
    key = next((alias for alias, name in ALIASES.items() if name == harness), harness)
    return HARNESSES[key]["default_log_file"]


def _log_path(harness: Optional[str]) -> Path:
    """``ARIZE_LOG_FILE`` if set, else the harness's default log, else the fallback log."""
    override = os.environ.get("ARIZE_LOG_FILE")
    if override:
        return Path(override)
    if harness is None:
        from core.constants import LOG_DIR

        return LOG_DIR / _FALLBACK_LOG_NAME
    return _default_log_file(harness)


def _log_failure(harness: Optional[str], message: str, with_traceback: bool = False) -> Optional[str]:
    """Append ``message`` (and the current traceback) to the log; return its path, or None if it can't be written.

    The hook still exits 0, and harnesses hide the stderr of a hook that exits
    0, so this log is the only lasting record of a broken install or a
    mistyped hook command. Only the message is logged, never the hook's other
    arguments: Codex's notify payload carries prompt text.
    """
    try:
        text = f"[arize:error] {message}\n"
        if with_traceback:
            import traceback

            text += traceback.format_exc()
        path = _log_path(harness)
        flush_redirected_stderr = _common("flush_redirected_stderr")
        if flush_redirected_stderr is not None:
            # The adapter may hold the same file open as sys.stderr; flush it so lines stay in order.
            flush_redirected_stderr()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as log:
            log.write(text)
        return str(path)
    except Exception:
        return None


def _shown(name: str) -> str:
    """A placeholder for an unrecognized argument in an error message.

    A misconfigured Codex ``notify`` puts its JSON payload, prompt text
    included, where the harness or event name should be, and a stray word or
    a name with text appended can land there too. Unknown arguments are never
    echoed to stderr or the log; only their length is shown.
    """
    return f"'<{len(name)}-character argument>'"


def _suggestion(name: str, known: Iterable[str]) -> str:
    """``; did you mean '<real name>'?`` if ``name`` is close to a name in ``known``, else empty.

    Only the name from ``known`` is shown, never ``name`` itself. Arguments
    over 64 characters are longer than any real name and skip the comparison.
    """
    from difflib import get_close_matches

    matches = get_close_matches(name, list(known), n=1, cutoff=0.6) if len(name) <= 64 else []
    return f"; did you mean '{matches[0]}'?" if matches else ""


def _harness_names() -> List[str]:
    return [*EVENT_HOOKS, *SINGLE_HOOKS, *ALIASES]


def _dispatch_error(harness: Optional[str], message: str) -> int:
    """Report a bad hook command on stderr and in the log, and exit 0."""
    _warn(message)
    _log_failure(harness, message)
    return 0


def _list_hooks() -> int:
    for harness, event, _target in iter_hooks():
        print(harness if event is None else f"{harness} {event}")
    return 0


def _hook(args: List[str]) -> Any:
    """Dispatch ``hook`` arguments to a handler.

    Lookup and import errors print one line to stderr, append to the log, and
    return 0. Exceptions raised by the handler propagate, as they do from the
    ``arize-hook-*`` commands.
    """
    if not args:
        return _dispatch_error(None, _USAGE)
    if args[0] == "--list":
        return _list_hooks()

    harness = canonical_harness(args[0])
    event: Optional[str] = None
    if harness in SINGLE_HOOKS:
        target = SINGLE_HOOKS[harness]
        rest = args[1:]
    elif harness in EVENT_HOOKS:
        if len(args) < 2:
            return _dispatch_error(harness, f"missing event for harness '{harness}' (see arize-harness hook --list)")
        event = args[1]
        events = EVENT_HOOKS[harness]
        if event not in events:
            return _dispatch_error(
                harness,
                f"unknown event {_shown(event)} for harness '{harness}'{_suggestion(event, events)}"
                " (see arize-harness hook --list)",
            )
        target = events[event]
        rest = args[2:]
    else:
        return _dispatch_error(
            None,
            f"unknown harness {_shown(args[0])}{_suggestion(args[0], _harness_names())}"
            " (see arize-harness hook --list)",
        )

    module_name, _, function_name = target.partition(":")
    try:
        handler = getattr(importlib.import_module(module_name), function_name)
    except (Exception, SystemExit) as e:  # a module calling sys.exit() at import must not block either
        log_path = _log_failure(harness, f"could not load hook {target}", with_traceback=True)
        _warn(f"could not load hook {target}: {e}" + (f" (traceback in {log_path})" if log_path else ""))
        return 0

    # Handlers read their own arguments from sys.argv (Codex's notify payload is
    # sys.argv[1]), so present them exactly as the legacy command would see them.
    sys.argv = [legacy_entry_point(harness, event), *rest]
    return handler()


def main(argv: Optional[List[str]] = None) -> Any:
    args = sys.argv[1:] if argv is None else argv
    if args and args[0] == "hook":
        return _hook(args[1:])
    if args and args[0] == "--version" and len(args) == 1:
        from core.runtime import installed_version

        print(f"arize-harness {installed_version()}")
        return 0
    if args and args[0] in _COMMANDS:
        from core.setup.commands import run

        return run(args)
    # Exit 0, not the conventional 2: Claude Code treats a hook's exit 2 as a
    # blocking error (on PreToolUse, it blocks the tool call). Logged as well as
    # printed: a hook command missing ``hook`` lands here, and the harness hides
    # its stderr. Only the fixed usage text is written, never the arguments.
    return _dispatch_error(None, _USAGE)


if __name__ == "__main__":
    sys.exit(main())
