#!/usr/bin/env python3
"""Report installed-tracing state, for humans or as JSON.

Exists so a caller — CI, a script, or a coding agent that just ran a
non-interactive install — can *verify* the result instead of parsing prose out
of the installer's output.

Secrets are never included. An API key is reported only as ``api_key_present``.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import re
import sys
import tomllib
from contextlib import nullcontext, redirect_stdout
from pathlib import Path
from typing import Iterator, List, Optional, Tuple

import core.setup as _setup
from core.config import load_config
from core.setup import CONFIG_FILE, INSTALL_DIR, is_our_hook_command, registered_command_path
from core.setup.runner import arize_harness_path

# Where each harness registers itself, as (module, constant names). Every
# candidate is checked — a harness may register through any one of them. Kept
# declarative so a new harness is one line rather than a bespoke check.
_REGISTRATION = {
    "antigravity": ("tracing.antigravity.constants", ("SETTINGS_FILE",)),
    "claude-code": ("tracing.claude_code.constants", ("SETTINGS_FILE",)),
    # A callable, not a constant: Codex honours CODEX_HOME, so its location is
    # resolved per call. It yields the home directory, which _references_install
    # scans — the same treatment Kiro's agents directory gets.
    "codex": ("tracing.codex.constants", ("get_codex_home",)),
    "devin": ("tracing.devin.constants", ("CONFIG_FILE",)),
    "copilot": ("tracing.copilot.constants", ("HOOKS_FILE",)),
    "cursor": ("tracing.cursor.constants", ("HOOKS_FILE",)),
    "gemini": ("tracing.gemini.constants", ("SETTINGS_FILE",)),
    "kiro": ("tracing.kiro.constants", ("KIRO_AGENTS_DIR",)),
    "opencode": ("tracing.opencode.constants", ("PLUGIN_FILE",)),
    "omp": ("tracing.omp.constants", ("SETTINGS_FILE", "PLUGIN_FILE")),
}


def _registration_paths(harness: str) -> list:
    """Resolve the candidate registration paths for a harness.

    A name may be a ``Path`` constant or a zero-argument callable returning one,
    because a harness whose location depends on the environment — Codex reads
    ``CODEX_HOME`` — cannot express it as a module constant.

    Returns [] when the harness is unknown, its constants can't be imported, or
    an accessor refuses to resolve. All three are reported as an unknown
    registration rather than a crash: `status` exists to describe the install, so
    it must not be the thing that fails. An invalid ``CODEX_HOME`` raises here,
    and saying "cannot tell" beats taking the command down.
    """
    entry = _REGISTRATION.get(harness)
    if not entry:
        return []

    module_name, names = entry
    try:
        module = importlib.import_module(module_name)
    except ImportError:
        return []

    paths = []
    for name in names:
        value = getattr(module, name, None)
        if callable(value):
            try:
                value = value()
            except Exception:
                continue
        if isinstance(value, Path):
            paths.append(value)
    return paths


# Files a registration may live in. A directory candidate (Kiro's agents dir,
# the Codex home) is scanned one level deep for these.
_CONFIG_SUFFIXES = (".json", ".toml", ".ts")

# The generated opencode / omp plugin: binaryPath() returns the arize-harness
# path, and spawn() gets ["hook", <harness>].
_TS_BINARY = re.compile(r'function binaryPath\(\): string \{\s*return ("(?:[^"\\]|\\.)*");?\s*\}')
_TS_ARGS = re.compile(r"spawn\(binaryPath\(\), (\[[^\]]*\])")
# The plugin older installs copied, which joined the venv path in code.
_TS_OLD_ENTRY = re.compile(r'"(arize-hook-[a-z0-9-]+)(?:\.exe)?"')


def _strings(value: object) -> Iterator[object]:
    """Every string, and every list of strings, inside a parsed config."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        if value and all(isinstance(item, str) for item in value):
            yield value
        for item in value:
            yield from _strings(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)


def _ts_commands(text: str) -> List[object]:
    """The command the opencode / omp plugin spawns, as an argv list (or an old venv path)."""
    binary, args = _TS_BINARY.search(text), _TS_ARGS.search(text)
    if binary and args:
        try:
            argv = [json.loads(binary.group(1)), *json.loads(args.group(1))]
        except (json.JSONDecodeError, TypeError):
            return []
        return [argv] if all(isinstance(word, str) for word in argv) else []
    if '".arize", "harness", "venv"' in text:
        if os.name == "nt":
            return [str(_setup.VENV_DIR / "Scripts" / f"{m.group(1)}.exe") for m in _TS_OLD_ENTRY.finditer(text)]
        return [str(_setup.VENV_DIR / "bin" / m.group(1)) for m in _TS_OLD_ENTRY.finditer(text)]
    return []


def _json_string_literals(text: str) -> Iterator[str]:
    """Each string literal in JSON / JSONC text, quotes included.

    ``//`` and ``/* */`` comments are skipped when they start outside a string,
    so a ``"`` inside a comment does not pair up with the next string's quote.
    """
    i, n = 0, len(text)
    while i < n:
        char = text[i]
        if char == '"':
            start = i
            i += 1
            while i < n and text[i] != '"':
                i += 2 if text[i] == "\\" else 1
            i += 1
            if i <= n:
                yield text[start:i]
        elif text.startswith("//", i):
            end = text.find("\n", i)
            i = n if end < 0 else end + 1
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
        else:
            i += 1


def _file_commands(path: Path) -> List[object]:
    """Every value in ``path`` that could be a hook command: strings, and argv lists in TOML.

    JSON (and JSONC, which Devin allows) is read as its string literals, found
    by ``_json_string_literals``, so comments or a trailing comma never hide a
    registration.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return []
    if path.suffix == ".ts":
        return _ts_commands(text)
    if path.suffix == ".toml":
        try:
            return list(_strings(tomllib.loads(text)))
        except tomllib.TOMLDecodeError:
            return []
    values: List[object] = []
    for literal in _json_string_literals(text):
        try:
            values.append(json.loads(literal))
        except json.JSONDecodeError:
            continue
    return values


def _candidate_files(path: Path) -> List[Path]:
    if path.is_dir():
        return [child for child in sorted(path.iterdir()) if child.is_file() and child.suffix in _CONFIG_SUFFIXES]
    return [path] if path.is_file() else []


def _our_commands(harness: str, path: Path) -> List[Tuple[Path, str]]:
    """``(file, executable)`` for each hook entry of ours under ``path``.

    Ours: ``arize-harness hook <harness> ...`` from any ``arize-harness``, and
    the old ``~/.arize/harness/venv`` ``arize-hook-*`` commands.
    """
    found = []
    for file in _candidate_files(path):
        for value in _file_commands(file):
            if is_our_hook_command(value, harness):
                executable = registered_command_path(value)
                if executable:
                    found.append((file, executable))
    return found


def _registration_state(harness: str) -> tuple:
    """Return (registered, path, command) for a harness.

    Registered means at least one hook entry of ours is in the harness's
    config, and every one of them runs a command that exists: a hook whose
    ``arize-harness`` was removed (``uv tool uninstall``, a deleted binary)
    fires nothing, so it counts as not registered. ``path`` is the file that
    decided it; ``command`` the missing command, when that was the reason.

    Every existing candidate is checked, not just the first: a harness can
    register through any one of them.

    ``registered`` is None when we have no way to tell — an unknown harness, or
    one whose constants failed to import. Never guess True.
    """
    paths = _registration_paths(harness)
    if not paths:
        return (None, None, None)

    existing = [path for path in paths if path.exists()]
    found = [entry for path in existing for entry in _our_commands(harness, path)]
    if not found:
        # Nothing of ours. Point at what we actually inspected when something
        # was there, otherwise at where we looked first.
        return (False, str(existing[0]) if existing else str(paths[0]), None)
    for file, executable in found:
        if not Path(executable).exists():
            return (False, str(file), executable)
    return (True, str(found[0][0]), None)


def collect_status() -> dict:
    """Build the full status payload. Contains no secrets."""
    config = load_config(str(CONFIG_FILE)) or {}
    harnesses = config.get("harnesses")
    if not isinstance(harnesses, dict):
        harnesses = {}

    entries = []
    for name in sorted(harnesses):
        entry = harnesses[name]
        if not isinstance(entry, dict):
            continue

        registered, path, missing = _registration_state(name)
        item = {
            "name": name,
            "project_name": entry.get("project_name") or name,
            "target": entry.get("target"),
            "endpoint": entry.get("endpoint"),
            "api_key_present": bool(entry.get("api_key")),
            "registered": registered,
            "registration_path": path,
            "missing_command": missing,
        }
        if entry.get("target") == "arize":
            item["space_id"] = entry.get("space_id")
        entries.append(item)

    # A harness we cannot check is not counted as broken — that would be
    # guessing in the other direction. Only an explicit False counts.
    unregistered = [item["name"] for item in entries if item["registered"] is False]

    return {
        "install_dir": str(INSTALL_DIR),
        "installed": INSTALL_DIR.exists(),
        "arize_harness": str(arize_harness_path()),
        "config_file": str(CONFIG_FILE),
        "config_exists": CONFIG_FILE.exists(),
        "user_id": config.get("user_id") or "",
        "logging": config.get("logging"),
        "harnesses": entries,
        "unregistered": unregistered,
        "healthy": bool(entries) and not unregistered,
    }


def _format_human(status: dict) -> str:
    """Render the payload as indented text."""
    lines = [
        f"Install dir:  {status['install_dir']}",
        f"Installed:    {'yes' if status['installed'] else 'no'}",
        f"Config:       {status['config_file']}" f"{'' if status['config_exists'] else ' (missing)'}",
    ]

    if status["user_id"]:
        lines.append(f"User ID:      {status['user_id']}")

    logging_block = status["logging"]
    if isinstance(logging_block, dict):
        flags = ", ".join(f"{k}={'on' if v else 'off'}" for k, v in sorted(logging_block.items()))
        lines.append(f"Logging:      {flags}")

    if not status["harnesses"]:
        lines.append("")
        lines.append("No harnesses configured.")
        return "\n".join(lines)

    if status["unregistered"]:
        lines.append("")
        lines.append(
            "Hooks are missing for: " + ", ".join(status["unregistered"]) + " — re-run the install for those harnesses."
        )

    lines.append("")
    lines.append("Harnesses:")
    for item in status["harnesses"]:
        if item["registered"] is None:
            reg = "registration unknown"
        elif item["registered"]:
            reg = "registered"
        else:
            reg = "NOT registered"

        lines.append(f"  {item['name']}")
        lines.append(f"    project:  {item['project_name']}")
        lines.append(f"    backend:  {item['target']} → {item['endpoint']}")
        if "space_id" in item:
            lines.append(f"    space:    {item['space_id']}")
        lines.append(f"    API key:  {'present' if item['api_key_present'] else 'MISSING'}")
        lines.append(f"    hooks:    {reg}" + (f" ({item['registration_path']})" if item["registration_path"] else ""))
        if item.get("missing_command"):
            lines.append(f"    missing:  {item['missing_command']} does not exist")

    return "\n".join(lines)


def main(argv: Optional[list] = None) -> int:
    """Print the report and return an exit code a caller can gate on.

    0  every configured harness is wired up
    1  nothing configured
    2  configured, but at least one harness's hooks are missing

    2 exists because 0 previously covered it: a harness whose hooks had been
    removed reported ``"registered": false`` in the payload while the process
    still exited successfully, so anything gating on the exit code alone
    concluded the install was fine.
    """
    parser = argparse.ArgumentParser(
        prog="install.sh status",
        description="Report configured harnesses and whether their hooks are wired up.",
    )
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = parser.parse_args(argv)

    # Rewrite files another package version recorded, here so every caller
    # (arize-harness status, install.sh / install.bat status) gets it. Messages
    # go to stderr under --json so stdout stays one JSON document.
    from core.setup.assets import refresh_and_report

    with redirect_stdout(sys.stderr) if args.json else nullcontext():
        refresh_and_report()

    status = collect_status()

    if args.json:
        print(json.dumps(status, indent=2, sort_keys=True))
    else:
        print(_format_human(status))

    if not status["harnesses"]:
        return 1
    return 2 if status["unregistered"] else 0


if __name__ == "__main__":
    sys.exit(main())
