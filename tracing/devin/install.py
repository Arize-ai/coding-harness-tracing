"""Devin harness install/uninstall, invoked by the installer router.

Registers/removes our Stop/SessionEnd command hooks in Devin's user config
(``~/.config/devin/config.json``, ``%APPDATA%\\devin\\config.json`` on Windows —
see ``constants.config_dir``). Devin hooks are Claude-Code-compatible command
hooks.

All config mutation uses stdlib ``json`` and preserves unrelated keys. Devin
accepts comments in its config; we strip them to parse, so a config we rewrite
comes back as plain JSON without its comments. Anything we cannot parse is left
alone rather than rebuilt, so a hand-edited config is never silently wiped.
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
    err,
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
from tracing.devin.constants import (
    CONFIG_FILE,
    DISPLAY_NAME,
    HARNESS_BIN,
    HARNESS_HOME,
    HARNESS_NAME,
    HOOK_BIN_NAME,
    HOOK_EVENTS,
)

# Devin hook events we register. Stop fires per agent response (per-turn
# emission); SessionEnd is a final flush for an interrupted last turn.
HOOK_EVENT_NAMES = HOOK_EVENTS

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


def _strip_json_comments(text: str) -> str:
    """Strip ``//`` and ``/* */`` comments from JSONC text.

    Devin's config files are JSON *with comment support*, which stdlib ``json``
    rejects. Comments are replaced with equivalent whitespace (newlines kept) so
    line/column numbers in any parse error still point at the real file.
    Comment-like sequences inside string literals are left untouched.
    """
    out: list[str] = []
    i = 0
    n = len(text)
    in_string = False
    while i < n:
        ch = text[i]
        if in_string:
            if ch == "\\" and i + 1 < n:
                out.append(text[i : i + 2])
                i += 2
                continue
            if ch == '"':
                in_string = False
            out.append(ch)
            i += 1
            continue
        if ch == '"':
            in_string = True
            out.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            end = text.find("\n", i)
            end = n if end == -1 else end
            out.append(" " * (end - i))
            i = end
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            close = text.find("*/", i + 2)
            end = n if close == -1 else close + 2
            out.append("".join("\n" if c == "\n" else " " for c in text[i:end]))
            i = end
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _parse_config(text: str) -> dict:
    """Parse Devin config text (JSON or JSONC) into a dict.

    Raises ``ValueError`` if the text is not a JSON object once comments are
    stripped. Callers decide whether that is fatal.
    """
    data = json.loads(_strip_json_comments(text))
    if not isinstance(data, dict):
        raise ValueError("config root is not a JSON object")
    return data


def _load_config() -> dict:
    """Load Devin's global config as a dict.

    Missing file -> ``{}``. Unreadable or unparseable file -> ``SystemExit(1)``
    (Gemini/OMP installer pattern): rewriting the hooks block onto ``{}`` would
    silently wipe every other setting in a hand-edited config.

    Comments are stripped for parsing and are therefore dropped when the file is
    written back.
    """
    if not CONFIG_FILE.exists():
        return {}
    try:
        text = CONFIG_FILE.read_text()
    except OSError as exc:
        err(f"Cannot read {CONFIG_FILE}: {exc}")
        sys.exit(1)
    if not text.strip():
        return {}
    try:
        return _parse_config(text)
    except (json.JSONDecodeError, ValueError) as exc:
        err(
            f"{CONFIG_FILE} contains invalid JSON; aborting so it is not overwritten. Please fix the file and retry.\n  {exc}"
        )
        sys.exit(1)


def _save_config(data: dict) -> None:
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(data, indent=2) + "\n")


def _hook_entry(hook_cmd: str) -> dict:
    """Devin command-hook matcher entry wrapping our hook command."""
    return {"hooks": [{"type": "command", "command": hook_cmd, "timeout": HOOK_TIMEOUT}]}


def _register_hooks() -> None:
    """Ensure each event in ``HOOK_EVENT_NAMES`` contains our command exactly once.

    Idempotent: re-installing does not duplicate the entry. All other config
    keys and hook events are preserved untouched. Honors dry-run.
    """
    config = _load_config()
    original = json.dumps(config, sort_keys=True)
    hook_cmd = str(venv_bin(HOOK_BIN_NAME))

    hooks = config.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        hooks = {}
        config["hooks"] = hooks

    for event in HOOK_EVENT_NAMES:
        event_list = hooks.setdefault(event, [])
        if not isinstance(event_list, list):
            event_list = []
            hooks[event] = event_list
        if not any(_matcher_has_command(matcher, hook_cmd) for matcher in event_list):
            event_list.append(_hook_entry(hook_cmd))

    if json.dumps(config, sort_keys=True) == original:
        # Already registered: leave the file (and any comments in it) untouched.
        info(f"Devin hooks already registered in {CONFIG_FILE}")
        return

    if dry_run():
        info(f"would write Devin hook config to {CONFIG_FILE}")
        return
    _save_config(config)


def _unregister_hooks() -> None:
    """Remove only our command from each event in ``HOOK_EVENT_NAMES``.

    Drops an event list if it becomes empty, and drops the ``hooks`` key if it
    becomes empty. Everything else in the config is preserved.
    """
    if not CONFIG_FILE.exists():
        return
    try:
        config = _parse_config(CONFIG_FILE.read_text())
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        info(f"Warning: cannot parse {CONFIG_FILE} ({exc}); leaving it untouched")
        return

    hooks = config.get("hooks")
    if not isinstance(hooks, dict):
        return

    hook_cmd = str(venv_bin(HOOK_BIN_NAME))
    changed = False
    for event in HOOK_EVENT_NAMES:
        event_list = hooks.get(event)
        if not isinstance(event_list, list):
            continue
        filtered = [matcher for matcher in event_list if not _matcher_has_command(matcher, hook_cmd)]
        if filtered == event_list:
            continue  # our command wasn't present in this event
        changed = True
        if filtered:
            hooks[event] = filtered
        else:
            del hooks[event]
    if not changed:
        return
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
