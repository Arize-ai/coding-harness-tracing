#!/usr/bin/env python3
"""Gemini tracing harness installer.

Handles install and uninstall for Gemini CLI tracing hooks.
Gemini uses a single ~/.gemini/settings.json with a hooks dict
keyed by event name.

Usage (called by the shell router):
    python tracing/gemini/install.py install
    python tracing/gemini/install.py uninstall
"""

from __future__ import annotations

import json
import sys

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
    unlink_skills,
    venv_bin,
    write_config,
    write_logging_config,
)
from tracing.gemini.constants import EVENTS, HARNESS_NAME, HOOK_NAME, HOOK_TIMEOUT_MS

# ---------------------------------------------------------------------------
# JSON helpers
# ---------------------------------------------------------------------------


def _settings_file():
    """Return the current SETTINGS_FILE path (re-read each call for testability)."""
    import tracing.gemini.constants as _c

    return _c.SETTINGS_FILE


def _settings_dir():
    """Return the current SETTINGS_DIR path (re-read each call for testability)."""
    import tracing.gemini.constants as _c

    return _c.SETTINGS_DIR


def _read_settings() -> dict:
    """Read settings.json, returning empty dict on missing or empty files.

    Raises ``SystemExit(1)`` on malformed JSON or permission errors so we
    never silently overwrite a user file.
    """
    path = _settings_file()
    if not path.is_file():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        from core.setup import err as _err

        _err(f"Cannot read {path}: {exc}")
        sys.exit(1)
    if not text.strip():
        info("settings.json is empty, treating as {}")
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        from core.setup import err as _err

        _err(f"{path} contains invalid JSON; aborting. Please fix the file and retry.\n  {exc}")
        sys.exit(1)


def _write_settings(data: dict) -> None:
    """Write *data* as pretty-printed JSON to settings.json."""
    path = _settings_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Install / uninstall hooks in settings.json
# ---------------------------------------------------------------------------


def _install_hooks() -> None:
    """Write/merge our hook entries into ~/.gemini/settings.json."""
    if dry_run():
        info(f"would write Gemini hooks to {_settings_file()}")
        return

    data = _read_settings()
    hooks_map: dict = data.setdefault("hooks", {})

    for event, entry_point in EVENTS.items():
        cmd = str(venv_bin(entry_point))
        event_list: list = hooks_map.setdefault(event, [])

        # Remove any existing matcher-block that contains a hook with our name
        event_list[:] = [
            block for block in event_list if not any(h.get("name") == HOOK_NAME for h in block.get("hooks", []))
        ]

        # Append our block
        event_list.append(
            {
                "matcher": "",
                "hooks": [
                    {
                        "type": "command",
                        "name": HOOK_NAME,
                        "command": cmd,
                        "timeout": HOOK_TIMEOUT_MS,
                    }
                ],
            }
        )

    _write_settings(data)


def _uninstall_hooks() -> None:
    """Remove our hook entries from ~/.gemini/settings.json."""
    path = _settings_file()
    if not path.is_file():
        return

    if dry_run():
        info(f"would remove Gemini hooks from {path}")
        return

    data = _read_settings()
    hooks_map = data.get("hooks", {})

    for event in EVENTS:
        event_list = hooks_map.get(event, [])
        filtered = [
            block for block in event_list if not any(h.get("name") == HOOK_NAME for h in block.get("hooks", []))
        ]
        if filtered:
            hooks_map[event] = filtered
        else:
            hooks_map.pop(event, None)

    if not hooks_map:
        data.pop("hooks", None)

    if not data:
        path.unlink()
    else:
        _write_settings(data)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def install_noninteractive(
    *,
    target: str,
    credentials: dict,
    project_name: str,
    user_id: str = "",
    with_skills: bool = False,
    logging_block: "dict | None" = None,
) -> None:
    """Install with no prompts. All decisions made by caller."""
    ensure_shared_runtime()

    config = load_config()
    existing_entry = get_value(config, f"harnesses.{HARNESS_NAME}")

    if existing_entry and isinstance(existing_entry, dict) and "target" in existing_entry:
        merge_harness_entry(HARNESS_NAME, project_name)
    else:
        if not dry_run():
            write_config(target, credentials, HARNESS_NAME, project_name, user_id=user_id)
        else:
            info("would write config.yaml with backend credentials")

    # Logging: use caller-supplied block, or default if absent from config.
    config = load_config()
    if (config.get("logging") if config else None) is None:
        effective_logging = (
            logging_block
            if logging_block is not None
            else {
                "prompts": True,
                "tool_details": True,
                "tool_content": True,
            }
        )
        write_logging_config(effective_logging)

    _install_hooks()

    info("Gemini tracing installed")


def uninstall_noninteractive() -> None:
    """Uninstall with no prompts."""
    _uninstall_hooks()

    remove_harness_entry(HARNESS_NAME)
    unlink_skills(HARNESS_NAME)
    info("Gemini tracing uninstalled")


def install() -> None:
    """Install Gemini tracing hooks and register in config.yaml."""
    config = load_config()
    existing_entry = get_value(config, f"harnesses.{HARNESS_NAME}")

    if not existing_entry or not isinstance(existing_entry, dict) or "target" not in existing_entry:
        existing_harnesses = config.get("harnesses") if config else None
        target, credentials = prompt_backend(existing_harnesses)
        project_name = prompt_project_name(HARNESS_NAME)
        user_id = prompt_user_id()
    else:
        project_name = prompt_project_name(existing_entry.get("project_name") or HARNESS_NAME)
        target = existing_entry.get("target", "phoenix")
        credentials = {
            "endpoint": existing_entry.get("endpoint", ""),
            "api_key": existing_entry.get("api_key", ""),
        }
        if existing_entry.get("space_id"):
            credentials["space_id"] = existing_entry["space_id"]
        user_id = ""

    logging_block = None
    if (config.get("logging") if config else None) is None:
        logging_block = prompt_content_logging()
    else:
        info("Using existing logging settings from config.yaml")

    install_noninteractive(
        target=target,
        credentials=credentials,
        project_name=project_name,
        user_id=user_id,
        logging_block=logging_block,
    )


def uninstall() -> None:
    """Remove Gemini tracing hooks and deregister from config.yaml."""
    uninstall_noninteractive()


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
