"""Devin harness install/uninstall, invoked by the installer router.

Registers/removes our SessionEnd command hook in Devin's global config at
``~/.config/devin/config.json`` (Devin hooks are Claude-Code-compatible command
hooks). All config mutation uses stdlib ``json`` and preserves unrelated keys.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

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
    write_config,
    write_logging_config,
)
from tracing.devin.constants import CONFIG_FILE, DISPLAY_NAME, HARNESS_BIN, HARNESS_HOME, HARNESS_NAME, HOOK_BIN_NAME

# The single Devin hook event we register. The transcript is only complete at
# session end, so that is the only point we need to fire.
HOOK_EVENT = "SessionEnd"

# Devin command-hook timeout (seconds).
HOOK_TIMEOUT = 30


def install(with_skills: bool = False) -> None:
    """Install Devin tracing: configure backend, register the SessionEnd hook."""
    if not ensure_harness_installed(DISPLAY_NAME, home_subdir=HARNESS_HOME, bin_name=HARNESS_BIN):
        info("Aborted.")
        return

    ensure_shared_runtime()

    # Per-harness state dir
    state_dir = INSTALL_DIR / "state" / HARNESS_NAME
    if dry_run():
        info(f"would create {state_dir}")
    else:
        state_dir.mkdir(parents=True, exist_ok=True)

    config = load_config()
    existing_entry = get_value(config, f"harnesses.{HARNESS_NAME}")
    if not existing_entry:
        existing_harnesses = config.get("harnesses") if config else None
        target, credentials = prompt_backend(existing_harnesses)
        project_name = prompt_project_name(HARNESS_NAME)
        user_id = prompt_user_id()
        if not dry_run():
            write_config(target, credentials, HARNESS_NAME, project_name, user_id=user_id)
        else:
            info("would write config.json with backend credentials")
    else:
        project_name = prompt_project_name(get_value(config, f"harnesses.{HARNESS_NAME}.project_name") or HARNESS_NAME)
        merge_harness_entry(HARNESS_NAME, project_name)

    if (config.get("logging") if config else None) is None:
        logging_block = prompt_content_logging()
        write_logging_config(logging_block)
    else:
        info("Using existing logging settings from config.json")

    _register_hooks()

    info(f"Devin tracing installed.\n" f"  Hooks registered in: {CONFIG_FILE}\n")

    # This harness ships no skills yet; only symlink if a skills dir exists.
    if with_skills and _skills_dir_exists():
        symlink_skills(HARNESS_NAME)


def uninstall() -> None:
    """Remove our SessionEnd hook from Devin's global config, preserving the rest."""
    _unregister_hooks()
    remove_harness_entry(HARNESS_NAME)
    unlink_skills(HARNESS_NAME)
    info("Devin tracing uninstalled")


def _skills_dir_exists() -> bool:
    """True when this harness ships a skills directory to symlink."""
    return (Path(__file__).resolve().parent / "skills").is_dir()


def _load_config() -> dict:
    """Load Devin's global config as a dict.

    Missing file -> ``{}``. Malformed JSON -> ``{}`` with a warning (we rebuild
    only the hooks block; nothing else can be preserved from unparseable data).
    """
    if not CONFIG_FILE.exists():
        return {}
    try:
        data = json.loads(CONFIG_FILE.read_text())
        if not isinstance(data, dict):
            raise ValueError("not an object")
        return data
    except (json.JSONDecodeError, OSError, ValueError) as exc:
        info(f"Warning: {CONFIG_FILE} is malformed ({exc}); rebuilding hooks block only")
        return {}


def _save_config(data: dict) -> None:
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(data, indent=2) + "\n")


def _hook_entry(hook_cmd: str) -> dict:
    """Devin command-hook matcher entry wrapping our hook command."""
    return {"hooks": [{"type": "command", "command": hook_cmd, "timeout": HOOK_TIMEOUT}]}


def _register_hooks() -> None:
    """Ensure ``hooks.SessionEnd`` contains our command exactly once.

    Idempotent: re-installing does not duplicate the entry. All other config
    keys and hook events are preserved untouched. Honors dry-run.
    """
    config = _load_config()
    hook_cmd = str(venv_bin(HOOK_BIN_NAME))

    hooks = config.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        hooks = {}
        config["hooks"] = hooks
    event_list = hooks.setdefault(HOOK_EVENT, [])
    if not isinstance(event_list, list):
        event_list = []
        hooks[HOOK_EVENT] = event_list

    already = any(_matcher_has_command(matcher, hook_cmd) for matcher in event_list)
    if not already:
        event_list.append(_hook_entry(hook_cmd))

    if dry_run():
        info(f"would write Devin hook config to {CONFIG_FILE}")
        return
    _save_config(config)


def _unregister_hooks() -> None:
    """Remove only our command from ``hooks.SessionEnd``.

    Drops the event list if it becomes empty, and drops the ``hooks`` key if it
    becomes empty. Everything else in the config is preserved.
    """
    if not CONFIG_FILE.exists():
        return
    try:
        config = json.loads(CONFIG_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(config, dict):
        return

    hooks = config.get("hooks")
    if not isinstance(hooks, dict) or HOOK_EVENT not in hooks:
        return

    hook_cmd = str(venv_bin(HOOK_BIN_NAME))
    event_list = hooks.get(HOOK_EVENT, [])
    if not isinstance(event_list, list):
        return

    filtered = [matcher for matcher in event_list if not _matcher_has_command(matcher, hook_cmd)]
    if filtered == event_list:
        return  # our command wasn't present; nothing to do

    if filtered:
        hooks[HOOK_EVENT] = filtered
    else:
        del hooks[HOOK_EVENT]
    if not hooks:
        del config["hooks"]

    if dry_run():
        info(f"would clean Devin hook config in {CONFIG_FILE}")
        return
    _save_config(config)
    info(f"Cleaned tracing hooks from {CONFIG_FILE}")


def _matcher_has_command(matcher: object, hook_cmd: str) -> bool:
    """True if a Devin hook matcher entry contains our command."""
    if not isinstance(matcher, dict):
        return False
    inner = matcher.get("hooks")
    if not isinstance(inner, list):
        return False
    return any(isinstance(h, dict) and h.get("command") == hook_cmd for h in inner)


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
