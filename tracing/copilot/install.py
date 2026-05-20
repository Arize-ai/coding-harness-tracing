#!/usr/bin/env python3
"""Copilot tracing harness installer.

Handles install and uninstall for GitHub Copilot tracing hooks. Writes a
single .github/hooks/hooks.json in the format VS Code Copilot Chat expects:
    {"hooks": {"<EventName>": [{"type": "command", "command": "<cmd>"}]}}

Usage (called by the shell router):
    python tracing/copilot/install.py install   [--project NAME]
    python tracing/copilot/install.py uninstall
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from core.config import get_value, load_config, save_config, set_value
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
from tracing.copilot.constants import HARNESS_NAME, HOOK_EVENTS, HOOKS_DIR, HOOKS_FILE

# ---------------------------------------------------------------------------
# JSON helpers
# ---------------------------------------------------------------------------


def _read_json(path: Path) -> dict:
    """Read a JSON file, returning empty dict on missing or malformed files."""
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _write_json(path: Path, data: dict) -> None:
    """Write *data* as pretty-printed JSON to *path*."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Hook file (.github/hooks/hooks.json)
# ---------------------------------------------------------------------------
#
# VS Code Copilot Chat schema:
#   {"hooks": {"<EventName>": [{"type": "command", "command": "<cmd>"}]}}
# Docs: https://code.visualstudio.com/docs/copilot/customization/hooks


def _install_hooks(hooks_dir: Path) -> None:
    """Merge our hook entries into hooks_dir/hooks.json."""
    filepath = hooks_dir / HOOKS_FILE.name

    if dry_run():
        info(f"would write hooks to {filepath}")
        return

    data = _read_json(filepath)
    hooks_map: dict = data.setdefault("hooks", {})

    for event, entry_point in HOOK_EVENTS.items():
        cmd = str(venv_bin(entry_point))
        event_list: list = hooks_map.setdefault(event, [])

        already = any(h.get("command") == cmd and h.get("type") == "command" for h in event_list)
        if already:
            continue

        event_list.append({"type": "command", "command": cmd})

    _write_json(filepath, data)


def _uninstall_hooks(hooks_dir: Path) -> None:
    """Remove our hook entries from hooks.json. Removes the file if empty."""
    filepath = hooks_dir / HOOKS_FILE.name
    if not filepath.is_file():
        return

    if dry_run():
        info(f"would remove hooks from {filepath}")
        return

    data = _read_json(filepath)
    hooks_map = data.get("hooks", {})

    for event, entry_point in HOOK_EVENTS.items():
        cmd = str(venv_bin(entry_point))
        event_list = hooks_map.get(event, [])
        filtered = [h for h in event_list if h.get("command") != cmd]
        if filtered:
            hooks_map[event] = filtered
        else:
            hooks_map.pop(event, None)

    if not hooks_map:
        filepath.unlink()
    else:
        data["hooks"] = hooks_map
        _write_json(filepath, data)


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
    repo_path: "str | None" = None,
) -> None:
    """Install with no prompts. All decisions made by caller.

    Copilot hooks are written into ``<repo>/.github/hooks/hooks.json``. When
    ``repo_path`` is ``None``, the current working directory is used — this
    keeps the CLI flow (``./install.sh copilot`` from inside a repo) working
    without changes.
    """
    ensure_shared_runtime()

    repo = Path(repo_path).expanduser().resolve() if repo_path else Path.cwd().resolve()

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

    # Record this repo under harnesses.copilot.repo_paths. write_config /
    # merge_harness_entry above either replace the whole entry or only touch
    # project_name/collector, so neither preserves repo_paths — we re-load
    # and re-apply here.
    if dry_run():
        info(f"would record repo_path {repo}")
    else:
        config = load_config()
        existing_paths = get_value(config, f"harnesses.{HARNESS_NAME}.repo_paths") or []
        repo_str = str(repo)
        if repo_str not in existing_paths:
            existing_paths = [*existing_paths, repo_str]
        set_value(config, f"harnesses.{HARNESS_NAME}.repo_paths", existing_paths)
        save_config(config)

    hooks_dir = repo / HOOKS_DIR

    if not dry_run():
        hooks_dir.mkdir(parents=True, exist_ok=True)

    _install_hooks(hooks_dir)

    info(f"Copilot tracing installed at {repo}")


def uninstall_noninteractive() -> None:
    """Uninstall with no prompts.

    Removes hooks from every repo previously recorded under
    ``harnesses.copilot.repo_paths``. Falls back to ``Path.cwd()`` for legacy
    installs that predate per-repo tracking.
    """
    config = load_config()
    paths = get_value(config, f"harnesses.{HARNESS_NAME}.repo_paths") or []

    if not paths:
        info("no repo_paths recorded; falling back to current directory")
        paths = [str(Path.cwd().resolve())]

    for path in paths:
        hooks_dir = Path(path) / HOOKS_DIR
        try:
            _uninstall_hooks(hooks_dir)
        except OSError as exc:
            info(f"could not remove hooks from {path}: {exc}")
            continue

    remove_harness_entry(HARNESS_NAME)
    unlink_skills(HARNESS_NAME)
    info(f"Copilot tracing uninstalled from {len(paths)} repo(s)")


def install() -> None:
    """Install Copilot tracing hooks (VS Code + CLI) and register in config.yaml."""
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
        repo_path=None,
    )


def uninstall() -> None:
    """Remove Copilot tracing hooks and deregister from config.yaml."""
    uninstall_noninteractive()


# ---------------------------------------------------------------------------
# CLI entry point (called by the shell router)
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
