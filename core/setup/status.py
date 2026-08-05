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
import sys
from pathlib import Path
from typing import Optional

from core.config import load_config
from core.setup import CONFIG_FILE, INSTALL_DIR, VENV_DIR

# Where each harness registers itself, as (module, constant names). Checked in
# order; the first path that exists decides. Kept declarative so a new harness
# is one line rather than a bespoke check.
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
    """
    marker = str(INSTALL_DIR)

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

    ``registered`` is None when we have no way to tell — an unknown harness, or
    one whose constants failed to import. Never guess True.
    """
    paths = _registration_paths(harness)
    if not paths:
        return (None, None)

    for path in paths:
        if path.exists():
            return (_references_install(path), str(path))

    # Nothing on disk: not registered, but report where we looked.
    return (False, str(paths[0]))


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

    return {
        "install_dir": str(INSTALL_DIR),
        "installed": VENV_DIR.exists(),
        "config_file": str(CONFIG_FILE),
        "config_exists": CONFIG_FILE.exists(),
        "user_id": config.get("user_id") or "",
        "logging": config.get("logging"),
        "harnesses": entries,
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

    # Non-zero when nothing is set up, so a caller can gate on it.
    return 0 if status["harnesses"] else 1


if __name__ == "__main__":
    sys.exit(main())
