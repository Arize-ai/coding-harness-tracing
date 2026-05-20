"""Cursor harness install/uninstall, invoked by the installer router."""

from __future__ import annotations

import json
import sys

from core.config import get_value, load_config
from core.setup import (
    INSTALL_DIR,
    dry_run,
    ensure_harness_installed,
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
    venv_bin,
    write_logging_config,
)
from tracing.cursor.constants import (
    DISPLAY_NAME,
    HARNESS_BIN,
    HARNESS_HOME,
    HARNESS_NAME,
    HOOK_BIN_NAME,
    HOOK_EVENTS,
    HOOKS_FILE,
)


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

    # Create cursor state dir
    state_dir = INSTALL_DIR / "state" / HARNESS_NAME
    if dry_run():
        info(f"would create {state_dir}")
    else:
        state_dir.mkdir(parents=True, exist_ok=True)

    config = load_config()
    existing_entry = get_value(config, f"harnesses.{HARNESS_NAME}")

    if existing_entry:
        merge_harness_entry(HARNESS_NAME, project_name)
    else:
        if not dry_run():
            from core.setup import write_config

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

    _register_cursor_hooks()
    if with_skills:
        symlink_skills(HARNESS_NAME)
    info(f"Cursor tracing installed ({HOOKS_FILE})")


def uninstall_noninteractive() -> None:
    """Uninstall with no prompts."""
    _unregister_cursor_hooks()
    remove_harness_entry(HARNESS_NAME)
    unlink_skills(HARNESS_NAME)
    info("Cursor tracing uninstalled")


def install(with_skills: bool = False) -> None:
    """Install Cursor tracing: configure backend, register hooks, optionally symlink skills."""
    if not ensure_harness_installed(DISPLAY_NAME, home_subdir=HARNESS_HOME, bin_name=HARNESS_BIN):
        info("Aborted.")
        return

    config = load_config()
    existing_entry = get_value(config, f"harnesses.{HARNESS_NAME}")

    if not existing_entry:
        existing_harnesses = config.get("harnesses") if config else None
        target, credentials = prompt_backend(existing_harnesses)
        project_name = prompt_project_name(HARNESS_NAME)
        user_id = prompt_user_id()
    else:
        project_name = prompt_project_name(get_value(config, f"harnesses.{HARNESS_NAME}.project_name") or HARNESS_NAME)
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
        with_skills=with_skills,
        logging_block=logging_block,
    )


def uninstall() -> None:
    """Remove Cursor tracing hooks, harness entry, and skill symlinks."""
    uninstall_noninteractive()


def _load_hooks() -> dict:
    """Load HOOKS_FILE as JSON, returning a fresh skeleton if missing or malformed."""
    if not HOOKS_FILE.exists():
        return {"version": 1, "hooks": {}}
    try:
        data = json.loads(HOOKS_FILE.read_text())
        if not isinstance(data, dict):
            return {"version": 1, "hooks": {}}
        data.setdefault("version", 1)
        data.setdefault("hooks", {})
        return data
    except (json.JSONDecodeError, OSError):
        return {"version": 1, "hooks": {}}


def _save_hooks(data: dict) -> None:
    """Write hooks dict as formatted JSON with trailing newline."""
    HOOKS_FILE.parent.mkdir(parents=True, exist_ok=True)
    HOOKS_FILE.write_text(json.dumps(data, indent=2) + "\n")


def _register_cursor_hooks() -> None:
    """Add hook entries for all HOOK_EVENTS to ~/.cursor/hooks.json.

    For each event, ensure an entry with ``command == venv_bin(HOOK_BIN_NAME)``
    exists — skip if already there.  Merges with existing entries without
    duplicating.  Honors dry_run().
    """
    data = _load_hooks()
    hooks = data["hooks"]
    hook_cmd = str(venv_bin(HOOK_BIN_NAME))

    for event in HOOK_EVENTS:
        event_list = hooks.setdefault(event, [])
        already = any(h.get("command") == hook_cmd for h in event_list)
        if not already:
            event_list.append({"command": hook_cmd})

    if dry_run():
        info(f"would write Cursor hooks to {HOOKS_FILE}")
        return

    _save_hooks(data)


def _unregister_cursor_hooks() -> None:
    """Remove our hook entries from ~/.cursor/hooks.json.

    Keeps other hooks intact.  Removes event keys that become empty after
    filtering.  No-op if file doesn't exist.  Honors dry_run().
    """
    if not HOOKS_FILE.exists():
        return

    data = _load_hooks()
    hooks = data.get("hooks", {})
    if not hooks:
        return

    hook_cmd = str(venv_bin(HOOK_BIN_NAME))

    for event in list(hooks.keys()):
        event_list = hooks[event]
        filtered = [h for h in event_list if h.get("command") != hook_cmd]
        if filtered:
            hooks[event] = filtered
        else:
            del hooks[event]

    if dry_run():
        info(f"would remove Cursor hooks from {HOOKS_FILE}")
        return

    _save_hooks(data)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    flags = set(sys.argv[2:])
    if cmd == "install":
        install(with_skills="--with-skills" in flags)
    elif cmd == "uninstall":
        uninstall()
    else:
        print("usage: install.py {install|uninstall} [--with-skills]", file=sys.stderr)
        sys.exit(2)
