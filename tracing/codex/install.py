#!/usr/bin/env python3
"""Codex harness install / uninstall module.

Self-contained module that handles:
- Writing ~/.codex/arize-env.sh (env file)
- Updating ~/.codex/config.toml (notify runs ``arize-harness hook codex notify``)
- Managing the shared config.json harness entry
- Copying skills
- Migrating legacy v1 installs via tracing.codex.install_legacy
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

from core.config import get_value, load_config
from core.setup import (
    CONFIG_FILE,
    NotRegistered,
    dry_run,
    ensure_harness_installed,
    ensure_shared_runtime,
    err,
    filter_our_nested_hooks,
    info,
    install_skills,
    is_arize_hook_command,
    is_old_venv_hook,
    merge_harness_entry,
    prompt_backend,
    prompt_content_logging,
    prompt_project_name,
    prompt_user_id,
    remove_harness_entry,
    remove_harness_files,
    write_config,
    write_logging_config,
)
from core.setup.runner import hook_argv
from tracing.codex._toml import _toml_load_strict, _toml_write
from tracing.codex.constants import DISPLAY_NAME, HARNESS_BIN, HARNESS_HOME, HARNESS_NAME, get_codex_home
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


def _strip_arize_hooks(data: dict) -> bool:
    """Remove our hooks from each ``[[hooks.<Event>]]`` group; returns True if anything changed.

    A group is dropped only when removing ours empties it.
    """
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return False
    changed = False
    for event in _HOOK_EVENTS:
        existing = hooks.get(event)
        if not isinstance(existing, list):
            continue
        kept = filter_our_nested_hooks(existing, HARNESS_NAME)
        if kept != existing:
            changed = True
            if kept:
                hooks[event] = kept
            else:
                del hooks[event]
    if not hooks:
        data.pop("hooks", None)
        changed = True
    return changed


def _is_our_notify(value: object) -> bool:
    """Whether ``notify`` is only our command, today's or the old venv's."""
    return is_arize_hook_command(value, HARNESS_NAME) or is_old_venv_hook(value)


def _without_old_notify_words(value: list) -> list:
    """Drop the old venv command older installers appended to the user's ``notify`` list."""
    return [word for word in value if not is_old_venv_hook(word)]


def _foreign_notify_message(path: Path, notify: object) -> str:
    return (
        f"{path} already sets notify = {notify!r}. Codex runs one notify program, so tracing "
        "was not registered; remove that notify line and re-run setup to trace Codex."
    )


def _codex_toml_apply(path: Path, notify_argv: list) -> bool:
    """Set ``notify`` in ~/.codex/config.toml and remove our legacy hook entries.

    Returns False, leaving it alone, when the user already set their own ``notify``.
    """
    data = _toml_load_strict(path)
    changed = _strip_arize_hooks(data)
    existing = data.get("notify")
    registered = True

    if existing in (None, "", []) or _is_our_notify(existing):
        data["notify"] = list(notify_argv)
        changed = True
    else:
        remaining = _without_old_notify_words(existing) if isinstance(existing, list) else existing
        if remaining in ([], ""):
            data["notify"] = list(notify_argv)
            changed = True
        else:
            registered = False
            if remaining != existing:
                data["notify"] = remaining
                changed = True

    if dry_run():
        info(f"would set notify in {path}" if registered else f"would leave notify in {path} as it is")
    elif changed:
        path.parent.mkdir(parents=True, exist_ok=True)
        _toml_write(data, path)
    return registered


def _codex_toml_remove(path: Path) -> None:
    """Remove our ``notify`` and any legacy hook entries from ~/.codex/config.toml."""
    if not path.is_file():
        return

    if dry_run():
        info(f"would revert {path}: remove our notify and any legacy hook entries")
        return

    data = _toml_load_strict(path)
    changed = False

    existing = data.get("notify")
    if existing is not None and _is_our_notify(existing):
        del data["notify"]
        changed = True
    elif isinstance(existing, list):
        remaining = _without_old_notify_words(existing)
        if remaining != existing:
            changed = True
            if remaining:
                data["notify"] = remaining
            else:
                del data["notify"]

    if _strip_arize_hooks(data):
        changed = True

    if changed:
        _toml_write(data, path)


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

    # 4. Write the notify-only TOML layout.
    registered = _codex_toml_apply(codex_config_file, hook_argv(HARNESS_NAME, "notify"))
    info(f"Updated TOML config: {codex_config_file}")

    # 5. Skills.
    if with_skills:
        install_skills(HARNESS_NAME)
        info("Copied skills")

    if not registered:
        notify = _toml_load_strict(codex_config_file)["notify"]
        if isinstance(notify, list):
            notify = _without_old_notify_words(notify)
        raise NotRegistered(_foreign_notify_message(codex_config_file, notify))
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

    # 2. Revert TOML — remove our notify entry and any legacy hook entries.
    _codex_toml_remove(codex_config_file)
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
    except NotRegistered as e:
        err(str(e))
        sys.exit(1)
    except (KeyboardInterrupt, EOFError):
        print("\nCancelled.")
        sys.exit(1)
