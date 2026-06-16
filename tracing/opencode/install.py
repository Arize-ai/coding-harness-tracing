#!/usr/bin/env python3
"""opencode tracing harness installer.

Handles install and uninstall for opencode plugin tracing. Unlike Gemini /
Claude Code, opencode loads plugins **in-process** inside its Bun runtime —
delivery is a file drop into the global plugin dir, not a JSON settings merge.

Usage (called by the shell router):
    python tracing/opencode/install.py install
    python tracing/opencode/install.py uninstall
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

from core.config import get_value, load_config
from core.setup import (
    dry_run,
    ensure_shared_runtime,
    info,
    merge_harness_entry,
    prompt_backend,
    prompt_content_logging,
    prompt_project_name,
    prompt_user_id,
    remove_harness_entry,
    symlink_skills,
    unlink_skills,
    write_config,
    write_logging_config,
)
from tracing.opencode.constants import HARNESS_NAME

# Header-marker the installer writes into the shim and checks on uninstall so
# we never delete a user's own plugin file.
_HEADER_MARKER = "// Arize opencode tracing plugin (shim)."


# ---------------------------------------------------------------------------
# Path helpers (re-read constants each call so tests can monkeypatch them)
# ---------------------------------------------------------------------------


def _plugin_dir():
    from tracing.opencode.constants import PLUGIN_DIR

    return PLUGIN_DIR


def _plugin_file():
    from tracing.opencode.constants import PLUGIN_FILE

    return PLUGIN_FILE


def _plugin_source():
    """Resolve the bundled plugin asset relative to THIS installer file.

    Deliberately NOT via ``constants.PLUGIN_SOURCE``: at runtime the shell router
    executes install.py from the rsynced ~/.arize/harness tree (where the .ts
    ships alongside it), while ``tracing.opencode.constants`` is imported from the
    venv site-packages copy, which does not carry the data asset. Resolving from
    install.py's own location works in every delivery (repo, INSTALL_DIR) and
    avoids the FileNotFoundError seen in real installs.
    """
    return Path(__file__).resolve().parent / "plugin" / "arize-tracing.ts"


# ---------------------------------------------------------------------------
# Plugin file-drop / removal
# ---------------------------------------------------------------------------


def _install_plugin() -> None:
    """Copy the shipped plugin source into opencode's global plugin dir."""
    plugin_file = _plugin_file()

    if dry_run():
        info(f"would write opencode plugin to {plugin_file}")
        return

    _plugin_dir().mkdir(parents=True, exist_ok=True)
    shutil.copyfile(_plugin_source(), plugin_file)


def _uninstall_plugin() -> None:
    """Delete the plugin file only when it carries our header marker."""
    plugin_file = _plugin_file()
    if not plugin_file.is_file():
        return

    try:
        text = plugin_file.read_text(encoding="utf-8")
    except OSError:
        return

    if not text.startswith(_HEADER_MARKER):
        return

    if dry_run():
        info(f"would remove opencode plugin {plugin_file}")
        return

    plugin_file.unlink()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def install(with_skills: bool = False) -> None:
    """Install the opencode plugin shim and register in config.yaml."""
    ensure_shared_runtime()

    config = load_config()
    existing_entry = get_value(config, f"harnesses.{HARNESS_NAME}")

    if not existing_entry or not isinstance(existing_entry, dict) or "target" not in existing_entry:
        existing_harnesses = config.get("harnesses") if config else None
        target, credentials = prompt_backend(existing_harnesses)
        project_name = prompt_project_name(HARNESS_NAME)
        user_id = prompt_user_id()
        if not dry_run():
            write_config(target, credentials, HARNESS_NAME, project_name, user_id=user_id)
        else:
            info("would write config.yaml with backend credentials")
    else:
        project_name = prompt_project_name(existing_entry.get("project_name") or HARNESS_NAME)
        merge_harness_entry(HARNESS_NAME, project_name)

    # Logging settings are global. Prompt only if no `logging:` block exists yet.
    if (config.get("logging") if config else None) is None:
        logging_block = prompt_content_logging()
        write_logging_config(logging_block)
    else:
        info("Using existing logging settings from config.yaml")

    _install_plugin()

    if with_skills:
        symlink_skills(HARNESS_NAME)

    info("opencode tracing installed")


def uninstall() -> None:
    """Remove the opencode plugin shim and deregister from config.yaml."""
    _uninstall_plugin()

    remove_harness_entry(HARNESS_NAME)
    unlink_skills(HARNESS_NAME)
    info("opencode tracing uninstalled")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Dispatch install / uninstall from the command line."""
    if len(sys.argv) < 2 or sys.argv[1] not in ("install", "uninstall"):
        print(f"usage: {sys.argv[0]} {{install|uninstall}} [--with-skills]", file=sys.stderr)
        sys.exit(1)

    action = sys.argv[1]
    flags = set(sys.argv[2:])

    if action == "install":
        install(with_skills="--with-skills" in flags)
    else:
        uninstall()


if __name__ == "__main__":
    main()
