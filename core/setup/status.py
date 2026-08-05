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
import sys
from pathlib import Path
from typing import Optional

from core.config import load_config
from core.setup import CONFIG_FILE, INSTALL_DIR, VENV_DIR

# Where each harness registers itself, as (module, constant names). Every
# candidate is checked — a harness may register through any one of them. Kept
# declarative so a new harness is one line rather than a bespoke check.
_REGISTRATION = {
    "claude-code": ("tracing.claude_code.constants", ("SETTINGS_FILE",)),
    "codex": ("tracing.codex.constants", ("CODEX_CONFIG_FILE",)),
    "copilot": ("tracing.copilot.constants", ("HOOKS_FILE",)),
    "cursor": ("tracing.cursor.constants", ("HOOKS_FILE",)),
    "gemini": ("tracing.gemini.constants", ("SETTINGS_FILE",)),
    "kiro": ("tracing.kiro.constants", ("KIRO_AGENTS_DIR",)),
    "opencode": ("tracing.opencode.constants", ("PLUGIN_FILE",)),
    "omp": ("tracing.omp.constants", ("SETTINGS_FILE", "PLUGIN_FILE")),
}


def _registration_paths(harness: str) -> list:
    """Resolve the candidate registration paths for a harness.

    Returns [] when the harness is unknown or its constants can't be imported —
    reported as an unknown registration rather than a crash.
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
        if isinstance(value, Path):
            paths.append(value)
    return paths


def _references_install(path: Path) -> bool:
    """True when `path` points at, or mentions, our install directory.

    Every harness wires itself up by writing an absolute path into its own
    settings/config file, or by linking a plugin out of the install dir — so a
    mention of INSTALL_DIR is the common signal across all of them.

    The marker carries a trailing separator so a sibling directory cannot match
    on prefix: ``~/.arize/harness-old`` is not ``~/.arize/harness``, and
    reporting a stale install as wired up would be a false positive in the one
    command whose job is to tell you the truth about that.
    """
    marker = str(INSTALL_DIR).rstrip(os.sep) + os.sep

    if path.is_symlink():
        try:
            if str(path.resolve()).startswith(marker):
                return True
        except OSError:
            return False

    if path.is_dir():
        for child in sorted(path.iterdir()):
            if child.is_file() and _references_install(child):
                return True
        return False

    try:
        return marker in path.read_text(errors="ignore")
    except OSError:
        return False


def _registration_state(harness: str) -> tuple:
    """Return (registered, path) for a harness.

    Every existing candidate is checked, not just the first: a harness can
    register through any one of them, so stopping at the first existing path
    would report a false negative when a later path is the one that matched.

    ``registered`` is None when we have no way to tell — an unknown harness, or
    one whose constants failed to import. Never guess True.
    """
    paths = _registration_paths(harness)
    if not paths:
        return (None, None)

    existing = [path for path in paths if path.exists()]
    for path in existing:
        if _references_install(path):
            return (True, str(path))

    # Nothing references us. Point at what we actually inspected when something
    # was there, otherwise at where we looked first.
    return (False, str(existing[0]) if existing else str(paths[0]))


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

        registered, path = _registration_state(name)
        item = {
            "name": name,
            "project_name": entry.get("project_name") or name,
            "target": entry.get("target"),
            "endpoint": entry.get("endpoint"),
            "api_key_present": bool(entry.get("api_key")),
            "registered": registered,
            "registration_path": path,
        }
        if entry.get("target") == "arize":
            item["space_id"] = entry.get("space_id")
        entries.append(item)

    # A harness we cannot check is not counted as broken — that would be
    # guessing in the other direction. Only an explicit False counts.
    unregistered = [item["name"] for item in entries if item["registered"] is False]

    return {
        "install_dir": str(INSTALL_DIR),
        "installed": VENV_DIR.exists(),
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
