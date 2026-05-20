"""Claude Code harness install/uninstall, invoked by the installer router."""

from __future__ import annotations

import json
import sys

from core.config import load_config
from core.setup import (
    dry_run,
    ensure_harness_installed,
    ensure_shared_runtime,
    harness_dir,
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
from tracing.claude_code.constants import (
    ARIZE_ENV_KEYS,
    DISPLAY_NAME,
    HARNESS_BIN,
    HARNESS_HOME,
    HARNESS_NAME,
    HOOK_EVENTS,
    SETTINGS_FILE,
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

    config = load_config()
    existing_entry = (config.get("harnesses") or {}).get(HARNESS_NAME)

    if existing_entry:
        merge_harness_entry(HARNESS_NAME, project_name)
    else:
        if not dry_run():
            write_config(
                target=target,
                credentials=credentials,
                harness_name=HARNESS_NAME,
                project_name=project_name,
                user_id=user_id,
            )
        else:
            info("would write config.yaml with harness entry")

    # Logging: use caller-supplied block, or default if absent from config.
    config = load_config()
    if config.get("logging") is None:
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

    _register_claude_hooks(project_name)
    if with_skills:
        symlink_skills(HARNESS_NAME)
    info(f"Claude Code tracing installed ({SETTINGS_FILE})")


def uninstall_noninteractive() -> None:
    """Uninstall with no prompts."""
    _unregister_claude_hooks()
    remove_harness_entry(HARNESS_NAME)
    unlink_skills(HARNESS_NAME)
    info("Claude Code tracing uninstalled")


def install(with_skills: bool = False) -> None:
    """Install Claude Code tracing: configure backend, register hooks, optionally symlink skills."""
    if not ensure_harness_installed(DISPLAY_NAME, home_subdir=HARNESS_HOME, bin_name=HARNESS_BIN):
        info("Aborted.")
        return

    config = load_config()
    existing_entry = (config.get("harnesses") or {}).get(HARNESS_NAME)

    if existing_entry:
        project_name = prompt_project_name(existing_entry.get("project_name") or HARNESS_NAME)
        target = existing_entry.get("target", "phoenix")
        credentials = {
            "endpoint": existing_entry.get("endpoint", ""),
            "api_key": existing_entry.get("api_key", ""),
        }
        if existing_entry.get("space_id"):
            credentials["space_id"] = existing_entry["space_id"]
        user_id = ""
    else:
        existing_harnesses = config.get("harnesses", {})
        target, credentials = prompt_backend(existing_harnesses=existing_harnesses)
        project_name = prompt_project_name(HARNESS_NAME)
        user_id = prompt_user_id()

    logging_block = None
    if config.get("logging") is None:
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
    """Remove Claude Code tracing hooks, harness entry, and skill symlinks."""
    uninstall_noninteractive()


def _load_settings() -> dict:
    """Load SETTINGS_FILE as JSON, returning {} if missing or malformed."""
    if not SETTINGS_FILE.exists():
        return {}
    try:
        return json.loads(SETTINGS_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save_settings(settings: dict) -> None:
    """Write settings dict as formatted JSON with trailing newline."""
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(settings, indent=2) + "\n")


def _register_claude_hooks(project_name: str = HARNESS_NAME) -> None:
    """Read SETTINGS_FILE (or init to {}), add plugin reference + hook commands.

    Registering the local plugin (path → ~/.arize/harness/tracing/claude_code)
    makes Claude Code auto-load its bundled hooks even in non-interactive
    (-p) mode, where ``--setting-sources`` defaults to ``project,local`` and
    user-level hooks would otherwise be skipped.

    Merges with existing entries without duplicating. Uses venv_bin() for each
    HOOK_EVENTS entry point. Honors dry_run().
    """
    settings = _load_settings()
    plugin_dir = str(harness_dir("claude-code"))

    # Add plugin reference (idempotent — skip if the path is already listed
    # under either the string or {path: ...} shape used by the marketplace).
    plugins = settings.setdefault("plugins", [])
    has_plugin = any(
        (isinstance(p, str) and p == plugin_dir) or (isinstance(p, dict) and p.get("path") == plugin_dir)
        for p in plugins
    )
    if not has_plugin:
        plugins.append({"type": "local", "path": plugin_dir})

    # Set env vars (only if absent)
    env_block = settings.setdefault("env", {})
    if not env_block.get("ARIZE_PROJECT_NAME"):
        env_block["ARIZE_PROJECT_NAME"] = project_name
    env_block.setdefault("ARIZE_TRACE_ENABLED", "true")

    # Register hooks
    hooks = settings.setdefault("hooks", {})
    for event, entry_point in HOOK_EVENTS.items():
        hook_cmd = str(venv_bin(entry_point))
        event_hooks = hooks.setdefault(event, [])
        already = any(h.get("command", "") == hook_cmd for entry in event_hooks for h in entry.get("hooks", []))
        if not already:
            event_hooks.append({"hooks": [{"type": "command", "command": hook_cmd}]})

    if dry_run():
        info(f"would write Claude hooks to {SETTINGS_FILE}")
        return

    _save_settings(settings)


def _unregister_claude_hooks() -> None:
    """Remove our hook entries and plugin reference from SETTINGS_FILE.

    Keeps other hooks, plugins, and env vars intact. No-op if file doesn't
    exist. Honors dry_run().
    """
    if not SETTINGS_FILE.exists():
        return

    settings = _load_settings()
    if not settings:
        return

    plugin_dir = str(harness_dir("claude-code"))

    # Remove our plugin entries (drops empty list).
    if "plugins" in settings:
        settings["plugins"] = [
            p
            for p in settings["plugins"]
            if not ((isinstance(p, str) and p == plugin_dir) or (isinstance(p, dict) and p.get("path") == plugin_dir))
        ]
        if not settings["plugins"]:
            del settings["plugins"]

    # Remove our hook entries
    if "hooks" in settings:
        our_commands = {str(venv_bin(ep)) for ep in HOOK_EVENTS.values()}
        hooks = settings["hooks"]
        for event in list(hooks.keys()):
            event_hooks = hooks[event]
            filtered = [
                entry
                for entry in event_hooks
                if not all(h.get("command", "") in our_commands for h in entry.get("hooks", []))
            ]
            if filtered:
                hooks[event] = filtered
            else:
                del hooks[event]
        if not hooks:
            del settings["hooks"]

    # Remove our env keys so stale values don't linger post-uninstall.
    if "env" in settings and isinstance(settings["env"], dict):
        env_block = settings["env"]
        for key in ARIZE_ENV_KEYS:
            env_block.pop(key, None)
        if not env_block:
            del settings["env"]

    if dry_run():
        info(f"would remove Claude hooks from {SETTINGS_FILE}")
        return

    _save_settings(settings)


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
