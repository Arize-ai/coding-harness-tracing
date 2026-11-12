"""Claude Code harness install/uninstall, invoked by the installer router."""

from __future__ import annotations

import json
import sys
from typing import Optional

from core.config import load_config
from core.setup import (
    dry_run,
    ensure_harness_installed,
    ensure_shared_runtime,
    filter_our_nested_hooks,
    harness_dir,
    hook_command_for,
    info,
    install_skills,
    is_our_hook_command,
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
from core.setup.assets import claude_plugin_dir, install_claude_plugin
from tracing.claude_code.constants import (
    ARIZE_ENV_KEYS,
    DISPLAY_NAME,
    HARNESS_BIN,
    HARNESS_HOME,
    HARNESS_NAME,
    HOOK_EVENTS,
    SETTINGS_FILE,
)


def install(with_skills: bool = False) -> None:
    """Install Claude Code tracing: configure backend, register hooks, optionally copy skills."""
    if not ensure_harness_installed(DISPLAY_NAME, home_subdir=HARNESS_HOME, bin_name=HARNESS_BIN):
        info("Aborted.")
        return

    ensure_shared_runtime()

    config = load_config()
    existing_entry = (config.get("harnesses") or {}).get(HARNESS_NAME)

    if isinstance(existing_entry, dict) and "target" in existing_entry:
        # Already configured — just let user update project_name.
        project_name = prompt_project_name(HARNESS_NAME, existing_entry["target"], config)
        merge_harness_entry(HARNESS_NAME, project_name)
    else:
        # New install. Pass existing harnesses so prompt_backend can offer copy-from.
        existing_harnesses = config.get("harnesses", {})
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
            info("would write config.json with harness entry")

    # Logging settings are global. Prompt only if no `logging:` block exists yet —
    # subsequent harness installs reuse what the first wizard wrote.
    if config.get("logging") is None:
        logging_block = prompt_content_logging()
        write_logging_config(logging_block)
    else:
        info("Using existing logging settings from config.json")

    install_claude_plugin()
    _register_claude_hooks()
    if with_skills:
        install_skills(HARNESS_NAME)
    info(f"Claude Code tracing installed ({SETTINGS_FILE})")


def uninstall() -> None:
    """Remove Claude Code tracing hooks, harness entry, plugin folder and skills."""
    _unregister_claude_hooks()
    remove_harness_entry(HARNESS_NAME)
    remove_harness_files(HARNESS_NAME)
    info("Claude Code tracing uninstalled")


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


def _is_our_plugin(entry: object, plugin_dirs: set) -> bool:
    """True for a ``plugins`` entry (a path, or ``{"path": ...}``) naming one of ``plugin_dirs``."""
    if isinstance(entry, str):
        return entry in plugin_dirs
    return isinstance(entry, dict) and entry.get("path") in plugin_dirs


def _is_our_hook(hook: dict) -> bool:
    """Ours: a command hook running ``arize-harness hook claude ...`` or an old venv ``arize-hook-*``."""
    return hook.get("type") == "command" and is_our_hook_command(hook.get("command"), HARNESS_NAME)


def _filter_our_hooks(event_hooks: list, keep: Optional[str]) -> list:
    """``event_hooks`` without our commands, except the first one equal to ``keep`` (see ``filter_our_nested_hooks``)."""
    return filter_our_nested_hooks(event_hooks, HARNESS_NAME, keep=keep, is_ours=_is_our_hook)


def _register_claude_hooks() -> None:
    """Add the plugin entry and hook commands to settings.json without duplicating them.

    The plugin entry (``~/.arize/harness/claude_plugin``) makes Claude run our hooks
    in ``claude -p`` too, which skips user-level hooks. Each event runs
    ``hook_command()``; our older entries (another ``arize-harness``'s commands,
    the old venv's ``arize-hook-*``, the ``~/.arize/harness/tracing/claude_code``
    plugin entry) are replaced. Idempotent. Honors dry_run().
    """
    settings = _load_settings()
    plugin_dir = str(claude_plugin_dir())

    # Drop the plugin entry older installs registered, then add ours once
    # (either the string or {path: ...} shape used by the marketplace counts).
    plugins = settings.setdefault("plugins", [])
    plugins[:] = [p for p in plugins if not _is_our_plugin(p, {str(harness_dir("claude-code"))})]
    if not any(_is_our_plugin(p, {plugin_dir}) for p in plugins):
        plugins.append({"type": "local", "path": plugin_dir})

    # Set env vars (only if absent). We deliberately do NOT bake
    # ARIZE_PROJECT_NAME here: the project name lives in config.json
    # (harnesses.claude-code.project_name) so edits there take effect, and a
    # baked env var would otherwise shadow config and leak into the Phoenix
    # backend (which honors PHOENIX_PROJECT instead) — see issue #74.
    env_block = settings.setdefault("env", {})
    env_block.setdefault("ARIZE_TRACE_ENABLED", "true")

    # Register hooks. Events we no longer register lose our old entries too.
    hooks = settings.setdefault("hooks", {})
    for event in list(hooks):
        if event not in HOOK_EVENTS and isinstance(hooks[event], list):
            hooks[event] = _filter_our_hooks(hooks[event], keep=None)
            if not hooks[event]:
                del hooks[event]
    for event, entry_point in HOOK_EVENTS.items():
        hook_cmd = hook_command_for(HARNESS_NAME, entry_point)
        event_hooks = hooks.setdefault(event, [])
        event_hooks[:] = _filter_our_hooks(event_hooks, keep=hook_cmd)
        already = any(
            isinstance(h, dict) and h.get("command") == hook_cmd
            for entry in event_hooks
            if isinstance(entry, dict)
            for h in entry.get("hooks", [])
        )
        if not already:
            event_hooks.append({"hooks": [{"type": "command", "command": hook_cmd}]})

    if dry_run():
        info(f"would write Claude hooks to {SETTINGS_FILE}")
        return

    _save_settings(settings)


def _unregister_claude_hooks() -> None:
    """Remove our hook entries and plugin reference from SETTINGS_FILE.

    Removes today's ``arize-harness hook claude`` commands and the old venv
    ones. Keeps other hooks, plugins, and env vars intact. No-op if file
    doesn't exist. Honors dry_run().
    """
    if not SETTINGS_FILE.exists():
        return

    settings = _load_settings()
    if not settings:
        return

    # the current plugin folder and the one older installs used
    plugin_dirs = {str(claude_plugin_dir()), str(harness_dir("claude-code"))}

    # Remove our plugin entries (drops empty list).
    if "plugins" in settings:
        settings["plugins"] = [p for p in settings["plugins"] if not _is_our_plugin(p, plugin_dirs)]
        if not settings["plugins"]:
            del settings["plugins"]

    # Remove our hook entries
    if "hooks" in settings:
        hooks = settings["hooks"]
        for event in list(hooks.keys()):
            if not isinstance(hooks[event], list):
                continue
            filtered = _filter_our_hooks(hooks[event], keep=None)
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
