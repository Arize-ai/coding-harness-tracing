#!/usr/bin/env python3
"""Codex harness install / uninstall module.

Self-contained module that handles:
- Writing ~/.codex/arize-env.sh (env file)
- Updating ~/.codex/config.toml (notify + five hook entry points)
- Managing the shared config.json harness entry
- Symlinking skills
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
from tracing.codex._toml import _toml_load, _toml_write
from tracing.codex.constants import (
    CODEX_CONFIG_DIR,
    CODEX_CONFIG_FILE,
    CODEX_ENV_FILE,
    DISPLAY_NAME,
    HARNESS_BIN,
    HARNESS_HOME,
    HARNESS_NAME,
    NOTIFY_BIN_NAME,
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


def _codex_toml_apply(path: Path, notify_cmd: str) -> None:
    """Write the notify-only layout to ~/.codex/config.toml. Idempotent.

    Ensures our ``notify_cmd`` is present exactly once in ``notify = [...]``.
    Does not touch any existing ``[[hooks.<Event>]]`` entries -- if a prior
    install left some behind, manage them out-of-band or run ``uninstall``.
    """
    if dry_run():
        info(f"would add notify entry to {path}")
        return

    data = _toml_load(path)

    existing_notify = data.get("notify", [])
    if not isinstance(existing_notify, list):
        existing_notify = [existing_notify] if existing_notify else []
    if notify_cmd not in existing_notify:
        existing_notify.append(notify_cmd)
    data["notify"] = existing_notify

    path.parent.mkdir(parents=True, exist_ok=True)
    _toml_write(data, path)


def _codex_toml_remove(path: Path, notify_cmd: str) -> None:
    """Remove our notify entry and any legacy hook entries. Idempotent."""
    if not path.is_file():
        return

    if dry_run():
        info(f"would revert {path}: remove notify={notify_cmd} and any legacy hook entries")
        return

    data = _toml_load(path)
    changed = False

    existing_notify = data.get("notify", [])
    if isinstance(existing_notify, list) and notify_cmd in existing_notify:
        existing_notify.remove(notify_cmd)
        if existing_notify:
            data["notify"] = existing_notify
        else:
            del data["notify"]
        changed = True
    elif isinstance(existing_notify, str) and existing_notify == notify_cmd:
        del data["notify"]
        changed = True

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
    if not ensure_harness_installed(DISPLAY_NAME, home_subdir=HARNESS_HOME, bin_name=HARNESS_BIN):
        info("Aborted.")
        return

    # 1. Migrate any v1 artifacts (idempotent; no-op on fresh installs).
    cleanup_legacy_install(CODEX_CONFIG_FILE)

    # 2. Shared runtime + harness entry.
    ensure_shared_runtime()
    config = load_config(str(CONFIG_FILE))
    existing_entry = get_value(config, f"harnesses.{HARNESS_NAME}")
    project_name = prompt_project_name("codex")

    if existing_entry:
        info(f"Reusing existing backend: {existing_entry.get('target')}")
        merge_harness_entry(HARNESS_NAME, project_name)
        user_id = get_value(config, "user_id") or ""
    else:
        existing_harnesses = config.get("harnesses", {}) if config else {}
        target, credentials = prompt_backend(existing_harnesses=existing_harnesses)
        user_id = prompt_user_id()
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
        CODEX_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    else:
        info(f"would create {CODEX_CONFIG_DIR}")
    _write_env_file(CODEX_ENV_FILE, user_id=user_id)

    # 4. Write the notify-only TOML layout.
    notify_cmd = str(venv_bin(NOTIFY_BIN_NAME))
    _codex_toml_apply(CODEX_CONFIG_FILE, notify_cmd)
    info(f"Updated TOML config: {CODEX_CONFIG_FILE}")

    # 5. Skills.
    if with_skills:
        symlink_skills(HARNESS_NAME)
        info("Symlinked skills")

    info("")
    info("Codex tracing installed.")


def uninstall() -> None:
    """Uninstall codex tracing harness."""
    # 1. Clean up any lingering v1 artifacts first (no-op if absent).
    cleanup_legacy_install(CODEX_CONFIG_FILE)

    # 2. Revert TOML — remove our notify entry and any legacy hook entries.
    notify_cmd = str(venv_bin(NOTIFY_BIN_NAME))
    _codex_toml_remove(CODEX_CONFIG_FILE, notify_cmd)
    info(f"Reverted TOML config: {CODEX_CONFIG_FILE}")

    # 3. Remove env file if ours.
    if CODEX_ENV_FILE.is_file():
        if _is_our_env_file(CODEX_ENV_FILE):
            if dry_run():
                info(f"would remove {CODEX_ENV_FILE}")
            else:
                CODEX_ENV_FILE.unlink()
                info(f"Removed env file: {CODEX_ENV_FILE}")
        else:
            info(f"Skipping {CODEX_ENV_FILE} — does not look like our file")

    # 4. Remove harness entry + unlink skills.
    remove_harness_entry(HARNESS_NAME)
    info("Removed codex harness entry from config.json")
    unlink_skills(HARNESS_NAME)
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
