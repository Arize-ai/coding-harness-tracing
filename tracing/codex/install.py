#!/usr/bin/env python3
"""Codex harness install / uninstall module.

Self-contained module that handles:
- Writing ~/.codex/arize-env.sh (env file)
- Updating ~/.codex/config.toml (the single ``notify`` command)
- Managing the shared config.json harness entry
- Copying skills
- Migrating legacy v1 installs via tracing.codex.install_legacy
"""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Callable
from pathlib import Path

from core.config import get_value, load_config, save_config, set_value
from core.setup import (
    CONFIG_FILE,
    dry_run,
    ensure_harness_installed,
    ensure_shared_runtime,
    info,
    install_skills,
    merge_harness_entry,
    prompt_backend,
    prompt_content_logging,
    prompt_project_name,
    prompt_user_id,
    remove_harness_entry,
    remove_harness_files,
    venv_bin,
    write_config,
    write_logging_config,
)
from tracing.codex._toml import _toml_load_strict, _toml_write
from tracing.codex.constants import (
    DISPLAY_NAME,
    HARNESS_BIN,
    HARNESS_HOME,
    HARNESS_NAME,
    NOTIFY_BIN_NAME,
    PREVIOUS_NOTIFY_KEY,
    get_codex_home,
    is_notify_entry_point,
    notify_argv,
    valid_previous_notify,
)
from tracing.codex.install_legacy import cleanup_legacy_install

# Hook events from the legacy installer; used only for cleanup
_HOOK_EVENTS = (
    "SessionStart",
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "PermissionRequest",
    "Stop",
)


# ---------------------------------------------------------------------------
# Codex TOML config management
# ---------------------------------------------------------------------------


def _entry_is_arize_managed(entry: object) -> bool:
    """Return True if *entry* is a hook-array element whose ``command`` path
    looks like one of our managed entry points (``arize-hook-codex-*``)."""
    if not isinstance(entry, dict):
        return False
    inner = entry.get("hooks")
    if not isinstance(inner, list):
        return False
    for h in inner:
        if not isinstance(h, dict):
            continue
        cmd = h.get("command") or ""
        if isinstance(cmd, str) and "arize-hook-codex-" in cmd:
            return True
    return False


def _strip_arize_hooks(data: dict) -> bool:
    """Remove any ``[[hooks.<Event>]]`` entries we previously wrote.

    Walks every known event and drops entries whose command path matches an
    ``arize-hook-codex-*`` pattern (covers session/tool/stop from older
    installer versions). Returns True if anything was removed.
    """
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return False
    changed = False
    for event in _HOOK_EVENTS:
        existing = hooks.get(event)
        if not isinstance(existing, list):
            continue
        kept = [e for e in existing if not _entry_is_arize_managed(e)]
        if len(kept) != len(existing):
            changed = True
            if kept:
                hooks[event] = kept
            else:
                del hooks[event]
    if not hooks:
        data.pop("hooks", None)
        changed = True
    return changed


def _is_our_notify(arg: object, notify_cmd: str) -> bool:
    """Return True if *arg* is our notify entry point, from this install or an
    earlier one at another path (e.g. a since-deleted system Python)."""
    return (isinstance(arg, str) and arg == notify_cmd) or is_notify_entry_point(arg)


def _codex_toml_apply(
    path: Path,
    notify_cmd: str,
    save_displaced: Callable[[list[str]], None] | None = None,
) -> list[str] | None:
    """Write the notify-only layout to ~/.codex/config.toml. Idempotent.

    Codex's ``notify`` is ONE command line, not a list of hooks: Codex runs
    ``notify[0]`` with ``notify[1:]`` as its arguments and appends the event
    JSON as the final argument. So ``notify`` is always set to exactly
    ``[notify_cmd]``.

    Anything else in ``notify`` -- another program's command, or one a user
    appended after our entry point -- is the displaced command. It is passed
    to *save_displaced* BEFORE config.toml is rewritten, so a failed save
    aborts without losing it, and then returned. The notify hook forwards
    every event to it, and uninstall puts it back. Returns None when there
    was nothing to preserve.

    Does not touch any existing ``[[hooks.<Event>]]`` entries -- if a prior
    install left some behind, manage them out-of-band or run ``uninstall``.
    """
    if dry_run():
        info(f"would set notify in {path}")
        return None

    data = _toml_load_strict(path)

    foreign = [a for a in notify_argv(data.get("notify")) if not _is_our_notify(a, notify_cmd)]
    displaced = foreign or None
    if displaced and save_displaced is not None:
        save_displaced(displaced)
    data["notify"] = [notify_cmd]

    path.parent.mkdir(parents=True, exist_ok=True)
    _toml_write(data, path)
    return displaced


def _codex_toml_remove(path: Path, notify_cmd: str, previous_notify: list[str] | None = None) -> None:
    """Remove our notify entry and any legacy hook entries. Idempotent.

    When ``notify`` runs our hook, it is replaced by *previous_notify* (the
    command our install displaced), else by whatever followed our entry point,
    else removed. When another program owns ``notify``, only copies of our
    entry point that older installers appended to its argv are dropped.
    """
    if not path.is_file():
        return

    if dry_run():
        info(f"would revert {path}: remove notify={notify_cmd} and any legacy hook entries")
        return

    data = _toml_load_strict(path)
    changed = False

    existing = notify_argv(data.get("notify"))
    foreign = [a for a in existing if not _is_our_notify(a, notify_cmd)]
    if existing and _is_our_notify(existing[0], notify_cmd):
        restored = previous_notify or foreign
        if restored:
            data["notify"] = list(restored)
        else:
            del data["notify"]
        changed = True
    elif len(foreign) != len(existing):
        data["notify"] = foreign
        changed = True

    if _strip_arize_hooks(data):
        changed = True

    if changed:
        _toml_write(data, path)


def _save_previous_notify(argv: list[str]) -> None:
    """Record the notify command our install displaced, for forwarding and uninstall."""
    if dry_run():
        info(f"would save previous notify command {argv} to {CONFIG_FILE}")
        return
    config = load_config(str(CONFIG_FILE)) or {"harnesses": {}}
    set_value(config, PREVIOUS_NOTIFY_KEY, argv)
    save_config(config, str(CONFIG_FILE))


# ---------------------------------------------------------------------------
# Env file management
# ---------------------------------------------------------------------------


def _write_env_file(path: Path, user_id: str = "") -> None:
    """Write the codex env file with ARIZE env exports."""
    if dry_run():
        info(f"would write env file {path}")
        return

    lines = ["export ARIZE_TRACE_ENABLED=true"]
    if user_id:
        lines.append(f"export ARIZE_USER_ID={user_id}")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _is_our_env_file(path: Path) -> bool:
    """Check if the env file is one we wrote (safe heuristic)."""
    if not path.is_file():
        return False
    try:
        text = path.read_text()
        lines = [ln for ln in text.strip().splitlines() if ln.strip()]
        if len(lines) > 10:
            return False
        return all(re.match(r"^export ARIZE_", line) for line in lines)
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Install / Uninstall
# ---------------------------------------------------------------------------


def install(with_skills: bool = False) -> None:
    """Install codex tracing harness (hooks-based v2 layout)."""
    codex_home = get_codex_home()
    codex_config_file = codex_home / "config.toml"
    codex_env_file = codex_home / "arize-env.sh"
    load_config(str(CONFIG_FILE))
    _toml_load_strict(codex_config_file)

    if not ensure_harness_installed(DISPLAY_NAME, home_subdir=HARNESS_HOME, bin_name=HARNESS_BIN):
        info("Aborted.")
        return

    # 1. Migrate any v1 artifacts (idempotent; no-op on fresh installs).
    cleanup_legacy_install(codex_config_file)

    # 2. Shared runtime + harness entry.
    ensure_shared_runtime()
    config = load_config(str(CONFIG_FILE))
    existing_entry = get_value(config, f"harnesses.{HARNESS_NAME}")
    # write_config replaces the whole harness entry, so keep any saved notify
    # command to put back afterwards.
    saved_previous = valid_previous_notify(get_value(config, PREVIOUS_NOTIFY_KEY))

    if isinstance(existing_entry, dict) and "target" in existing_entry:
        info(f"Reusing existing backend: {existing_entry.get('target')}")
        project_name = prompt_project_name(HARNESS_NAME, existing_entry["target"], config)
        merge_harness_entry(HARNESS_NAME, project_name)
        user_id = get_value(config, "user_id") or ""
    else:
        existing_harnesses = config.get("harnesses", {}) if config else {}
        target, credentials = prompt_backend(existing_harnesses=existing_harnesses)
        user_id = prompt_user_id()
        project_name = prompt_project_name(HARNESS_NAME, target, config, user_id)
        if not dry_run():
            write_config(
                target=target,
                credentials=credentials,
                harness_name=HARNESS_NAME,
                project_name=project_name,
                user_id=user_id,
            )
        else:
            info("would write config.json with backend credentials")
    if saved_previous:
        _save_previous_notify(saved_previous)

    # Logging settings are global. Prompt only if no `logging:` block exists yet.
    if (config.get("logging") if config else None) is None:
        write_logging_config(prompt_content_logging())
    else:
        info("Using existing logging settings from config.json")

    # 3. Codex config dir + env file.
    if not dry_run():
        codex_home.mkdir(parents=True, exist_ok=True)
    else:
        info(f"would create {codex_home}")
    _write_env_file(codex_env_file, user_id=user_id)

    # 4. Write the notify-only TOML layout. Codex runs a single notify command,
    #    so if another program owned it, keep its command so the hook can
    #    forward events to it and uninstall can restore it.
    notify_cmd = str(venv_bin(NOTIFY_BIN_NAME))
    displaced = _codex_toml_apply(codex_config_file, notify_cmd, save_displaced=_save_previous_notify)
    if displaced:
        info(f"Codex runs one notify command; events will be forwarded to the previous one: {displaced}")
    info(f"Updated TOML config: {codex_config_file}")

    # 5. Skills.
    if with_skills:
        install_skills(HARNESS_NAME)
        info("Copied skills")

    info("")
    info("Codex tracing installed.")


def uninstall() -> None:
    """Uninstall codex tracing harness."""
    codex_home = get_codex_home()
    codex_config_file = codex_home / "config.toml"
    codex_env_file = codex_home / "arize-env.sh"
    load_config(str(CONFIG_FILE))
    _toml_load_strict(codex_config_file)

    # 1. Clean up any lingering v1 artifacts first (no-op if absent).
    cleanup_legacy_install(codex_config_file)

    # 2. Revert TOML — remove our notify entry (restoring the command it
    #    displaced, if any) and any legacy hook entries.
    notify_cmd = str(venv_bin(NOTIFY_BIN_NAME))
    previous_notify = valid_previous_notify(get_value(load_config(str(CONFIG_FILE)), PREVIOUS_NOTIFY_KEY))
    _codex_toml_remove(codex_config_file, notify_cmd, previous_notify)
    info(f"Reverted TOML config: {codex_config_file}")

    # 3. Remove env file if ours.
    if codex_env_file.is_file():
        if _is_our_env_file(codex_env_file):
            if dry_run():
                info(f"would remove {codex_env_file}")
            else:
                codex_env_file.unlink()
                info(f"Removed env file: {codex_env_file}")
        else:
            info(f"Skipping {codex_env_file} — does not look like our file")

    # 4. Remove harness entry + unlink skills.
    remove_harness_entry(HARNESS_NAME)
    info("Removed codex harness entry from config.json")
    remove_harness_files(HARNESS_NAME)
    info("Unlinked skills")
    info("Codex tracing uninstalled")


# ---------------------------------------------------------------------------
# CLI dispatch
# ---------------------------------------------------------------------------


def cli_main(argv: list[str] | None = None) -> None:
    """Parse argv and dispatch to install/uninstall."""
    if argv is None:
        argv = sys.argv
    if len(argv) < 2 or argv[1] not in ("install", "uninstall"):
        print(f"usage: {argv[0]} <install|uninstall> [--with-skills]", file=sys.stderr)
        sys.exit(1)

    action = argv[1]
    flags = argv[2:]

    if action == "install":
        install(with_skills="--with-skills" in flags)
    else:
        uninstall()


if __name__ == "__main__":
    try:
        cli_main()
    except (KeyboardInterrupt, EOFError):
        print("\nCancelled.")
        sys.exit(1)
