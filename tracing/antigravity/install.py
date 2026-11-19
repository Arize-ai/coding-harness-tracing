#!/usr/bin/env python3
"""Antigravity tracing harness installer.

Handles install and uninstall for Antigravity CLI tracing hooks. Antigravity
uses ``~/.gemini/config/hooks.json`` (a separate file from Gemini's
``~/.gemini/settings.json``). The schema is *inverted* relative to Gemini:
the top level maps ``hookName -> { event -> [handlers] }``, and the per-event
value is a flat list of handler dicts (no matcher wrapper). The ``timeout``
field is in **seconds**, not milliseconds.

Usage (called by the shell router):
    python tracing/antigravity/install.py install
    python tracing/antigravity/install.py uninstall
"""

from __future__ import annotations

import json
import sys

from core.config import load_config, write_text_if_changed
from core.setup import configure_harness_backend, dry_run, ensure_shared_runtime
from core.setup import err as _err
from core.setup import (
    hook_command_for,
    info,
    preflight_backend_replacement,
    prompt_content_logging,
    remove_harness_entry,
    remove_harness_files,
    write_logging_config,
)
from tracing.antigravity import constants as _c

# ---------------------------------------------------------------------------
# JSON helpers
# ---------------------------------------------------------------------------


def _settings_file():
    """Return the current SETTINGS_FILE path (re-read each call for testability)."""
    return _c.SETTINGS_FILE


def _settings_dir():
    """Return the current SETTINGS_DIR path (re-read each call for testability)."""
    return _c.SETTINGS_DIR


def _read_settings() -> dict:
    """Read hooks.json, returning empty dict on missing or empty files.

    Raises ``SystemExit(1)`` on malformed JSON or permission errors so we
    never silently overwrite a user file.
    """
    path = _settings_file()
    if not path.is_file():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        _err(f"Cannot read {path}: {exc}")
        raise SystemExit(1)
    if not text.strip():
        info("hooks.json is empty, treating as {}")
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        _err(f"{path} contains invalid JSON; aborting. Please fix the file and retry.\n  {exc}")
        raise SystemExit(1)
    if not isinstance(data, dict):
        _err(f"{path} does not contain a JSON object; aborting. Please fix the file and retry.")
        raise SystemExit(1)
    return data


def _write_settings(data: dict) -> None:
    """Write *data* as pretty-printed JSON to hooks.json."""
    path = _settings_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    write_text_if_changed(path, json.dumps(data, indent=2) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Install / uninstall hooks in hooks.json
# ---------------------------------------------------------------------------


def _install_hooks() -> None:
    """Write/merge our hook entries into ~/.gemini/config/hooks.json.

    Antigravity's schema puts the hook name at the top level and maps it to
    a dict of ``event -> [handlers]``. We replace our whole ``HOOK_NAME``
    block on install so re-install is idempotent and all other top-level
    keys are preserved.
    """
    if dry_run():
        info(f"would write Antigravity hooks to {_settings_file()}")
        return

    data = _read_settings()
    data[_c.HOOK_NAME] = {
        event: [
            {
                "type": "command",
                "command": hook_command_for(_c.HARNESS_NAME, entry_point),
                "timeout": _c.HOOK_TIMEOUT_SECONDS,
            }
        ]
        for event, entry_point in _c.EVENTS.items()
    }

    _write_settings(data)


def _uninstall_hooks() -> None:
    """Remove our hook entries from ~/.gemini/config/hooks.json."""
    path = _settings_file()
    if not path.is_file():
        return

    if dry_run():
        info(f"would remove Antigravity hooks from {path}")
        return

    data = _read_settings()
    data.pop(_c.HOOK_NAME, None)

    if not data:
        path.unlink()
    else:
        _write_settings(data)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def install() -> None:
    """Install Antigravity tracing hooks and register in config.json."""
    backend = preflight_backend_replacement(_c.HARNESS_NAME)
    ensure_shared_runtime()

    config = load_config()
    configure_harness_backend(_c.HARNESS_NAME, config, backend=backend)

    # Logging settings are global. Prompt only if no `logging:` block exists yet.
    if (config.get("logging") if config else None) is None:
        logging_block = prompt_content_logging()
        write_logging_config(logging_block)
    else:
        info("Using existing logging settings from config.json")

    _install_hooks()

    info("Antigravity tracing installed")


def uninstall() -> None:
    """Remove Antigravity tracing hooks and deregister from config.json."""
    _uninstall_hooks()

    remove_harness_entry(_c.HARNESS_NAME)
    remove_harness_files(_c.HARNESS_NAME)
    info("Antigravity tracing uninstalled")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Dispatch install / uninstall from the command line."""
    if len(sys.argv) < 2 or sys.argv[1] not in ("install", "uninstall"):
        print(f"usage: {sys.argv[0]} {{install|uninstall}}", file=sys.stderr)
        sys.exit(1)

    action = sys.argv[1]

    if action == "install":
        install()
    else:
        uninstall()


if __name__ == "__main__":
    main()
