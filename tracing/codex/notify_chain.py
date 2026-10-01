"""Compose Codex notification commands without treating argv as multiple hooks."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

PREVIOUS = "--previous-notify"


def _command(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ValueError("Codex notify must be an array of strings")
    return list(value)


def _desktop(command: list[str]) -> bool:
    name = Path(command[0].replace("\\", "/")).name if command else ""
    return (
        len(command) >= 2
        and command[1] == "turn-ended"
        and name in {"SkyComputerUseClient", "codex-computer-use.exe", "codex-computer-use-arm64.exe"}
    )


def _previous(command: list[str]) -> list[str]:
    if len(command) == 1 or (len(command) == 2 and _desktop(command)):
        return []
    offset = 2 if _desktop(command) else 1
    if len(command) != offset + 2 or command[offset] != PREVIOUS:
        raise ValueError("Unrecognized notification chain; config was not changed")
    return _command(json.loads(command[offset + 1]))


def remove_notify(value: object, hook: str, depth: int = 0) -> list[str]:
    if depth > 10:
        raise ValueError("Notification chain is too deeply nested")
    command = _command(value)
    if not command:
        return []
    if command[0] == hook:
        if len(command) > 1 and command[1] != PREVIOUS:
            return command[1:]  # Recover old concatenated-array installs.
        return remove_notify(_previous(command), hook, depth + 1)
    # Strip the executable appended by old installers before parsing an
    # existing desktop wrapper that may already have --previous-notify.
    command = [part for part in command if part != hook]
    if _desktop(command) and len(command) > 2 and command[2] == PREVIOUS:
        previous = remove_notify(_previous(command), hook, depth + 1)
        return command[:2] + ([PREVIOUS, json.dumps(previous)] if previous else [])
    # Older Arize installers appended an executable to an existing argv.
    return command


def install_notify(value: object, hook: str) -> list[str]:
    previous = remove_notify(value, hook)
    if _desktop(previous):
        inner = _previous(previous)
        arize = [hook] + ([PREVIOUS, json.dumps(inner)] if inner else [])
        # Keep the desktop-owned command outermost so its config repair can
        # recognize and preserve the supported previous-notify registration.
        return previous[:2] + [PREVIOUS, json.dumps(arize)]
    return [hook] + ([PREVIOUS, json.dumps(previous)] if previous else [])


def run_previous(args: list[str]) -> str:
    """Forward the unchanged JSON payload to the previous callback, then return it."""
    if len(args) == 1:
        return args[0]
    if len(args) != 3 or args[0] != PREVIOUS:
        raise ValueError("Expected notify payload and optional previous command")
    previous = _command(json.loads(args[1]))
    if previous:
        try:
            subprocess.run(
                previous + [args[2]], check=False, timeout=20, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
        except (OSError, subprocess.TimeoutExpired):
            pass  # A notification failure must not prevent trace delivery.
    return args[2]
