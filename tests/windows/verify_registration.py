"""Verify Claude hook registration from an installed Windows environment."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def _under(path: str | None, root: str) -> bool:
    if not path:
        return False
    try:
        return os.path.commonpath([os.path.abspath(path), os.path.abspath(root)]) == os.path.abspath(root)
    except ValueError:
        return False


def main() -> int:
    if os.name != "nt":
        print("Windows-only verification must run on Windows", file=sys.stderr)
        return 1
    test_home = os.environ.get("WINDOWS_HOOK_TEST_HOME")
    if not test_home:
        print("WINDOWS_HOOK_TEST_HOME must identify the isolated test profile", file=sys.stderr)
        return 1
    home = Path.home().resolve()
    if os.path.normcase(os.path.abspath(test_home)) != os.path.normcase(str(home)):
        print(f"WINDOWS_HOOK_TEST_HOME {test_home!r} does not match Path.home() {str(home)!r}", file=sys.stderr)
        return 1
    expected_prefix = home / ".arize" / "harness" / "venv"
    if os.path.normcase(os.path.abspath(sys.prefix)) != os.path.normcase(str(expected_prefix)):
        print(f"sys.prefix {sys.prefix!r} is not the isolated venv {str(expected_prefix)!r}", file=sys.stderr)
        return 1

    from core.setup import venv_bin
    from tracing.claude_code.constants import HOOK_EVENTS, SETTINGS_FILE
    from tracing.claude_code.install import _register_claude_hooks

    for name, module in tuple(sys.modules.items()):
        is_project_module = (
            name == "core" or name.startswith("core.") or name == "tracing" or name.startswith("tracing.")
        )
        if is_project_module and not _under(getattr(module, "__file__", None), sys.prefix):
            raise RuntimeError(f"{name} loaded outside installed venv: {getattr(module, '__file__', None)!r}")

    _register_claude_hooks()
    settings = json.loads(SETTINGS_FILE.read_text())
    hooks = settings.get("hooks", {})
    for event, entrypoint in HOOK_EVENTS.items():
        entries = hooks.get(event)
        if not isinstance(entries, list) or len(entries) != 1:
            raise RuntimeError(f"{event}: expected one hook entry, got {entries!r}")
        inner = entries[0].get("hooks")
        if not isinstance(inner, list) or len(inner) != 1:
            raise RuntimeError(f"{event}: expected one inner hook, got {inner!r}")
        command = inner[0].get("command")
        expected = venv_bin(entrypoint)
        if not isinstance(command, str) or not command:
            raise RuntimeError(f"{event}: expected a nonempty command, got {command!r}")
        if expected.suffix.lower() != ".exe" or not expected.exists():
            raise RuntimeError(f"{event}: generated executable is missing or not .exe: {expected!s}")
        print(f"{event}: {command!r}")
    print(f"Verified {len(HOOK_EVENTS)} Claude hook registrations in {SETTINGS_FILE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
