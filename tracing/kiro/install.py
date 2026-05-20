"""Kiro harness install/uninstall, invoked by the installer router."""

from __future__ import annotations

import copy
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

from core.config import get_value, load_config, save_config, set_value
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
from tracing.kiro.constants import (
    AGENT_SKELETON,
    DEFAULT_AGENT_NAME,
    DISPLAY_NAME,
    HARNESS_BIN,
    HARNESS_HOME,
    HARNESS_NAME,
    HOOK_BIN_NAME,
    HOOK_EVENTS,
    KIRO_AGENTS_DIR,
)

_VALID_AGENT_NAME_RE = re.compile(r"[A-Za-z0-9_.-]+")


def _resolve_kiro_bin() -> str | None:
    """Return path to kiro-cli binary, or None if not findable."""
    return shutil.which(HARNESS_BIN) or _macos_app_kiro_path()


def install_noninteractive(
    *,
    target: str,
    credentials: dict,
    project_name: str,
    user_id: str = "",
    with_skills: bool = False,
    logging_block: "dict | None" = None,
    agent_name: str | None = None,
    set_default: bool = False,
) -> None:
    """Install Kiro tracing with no prompts. All decisions made by caller."""
    chosen_name = agent_name or DEFAULT_AGENT_NAME
    if not _VALID_AGENT_NAME_RE.fullmatch(chosen_name):
        raise ValueError("invalid agent name")

    if _resolve_kiro_bin() is None:
        raise RuntimeError("kiro-cli not found; install the Kiro CLI before configuring Arize tracing for Kiro")

    ensure_shared_runtime()

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
            write_config(target, credentials, HARNESS_NAME, project_name, user_id=user_id)
        else:
            info("would write config.yaml with backend credentials")

    # Persist agent_name under harnesses.kiro.agent_name
    if not dry_run():
        config = load_config()
        set_value(config, f"harnesses.{HARNESS_NAME}.agent_name", chosen_name)
        save_config(config)
    else:
        info(f"would set harnesses.{HARNESS_NAME}.agent_name = {chosen_name}")

    # Logging: only write when absent from config.
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

    # Single-agent-traced semantics: clear our hooks from every existing agent
    # before installing fresh into the chosen one.
    _unregister_all_kiro_hooks()

    agent_path = _resolve_agent_path(chosen_name)
    _register_kiro_hooks(agent_path, chosen_name)
    info(f"Hooks registered in {agent_path}")

    if set_default and not dry_run():
        kiro_bin = _resolve_kiro_bin()
        if kiro_bin:
            result = subprocess.run(
                [kiro_bin, "agent", "set-default", chosen_name],
                check=False,
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                info(f"'{chosen_name}' is now Kiro's default agent")
            else:
                info(f"Failed to set default: {result.stderr.strip() or result.stdout.strip()}")

    if with_skills:
        symlink_skills(HARNESS_NAME)

    info(
        f"Kiro tracing installed.\n"
        f"  Agent file: {agent_path}\n"
        f"  Run:        kiro-cli chat --agent {chosen_name}\n"
    )


def uninstall_noninteractive() -> None:
    """Remove Kiro tracing with no prompts."""
    _unregister_all_kiro_hooks()
    remove_harness_entry(HARNESS_NAME)
    unlink_skills(HARNESS_NAME)
    info("Kiro tracing uninstalled")


def install(with_skills: bool = False, agent_name: str | None = None) -> None:
    """Install Kiro tracing: configure backend, write hooks into an agent config."""
    if not ensure_harness_installed(DISPLAY_NAME, home_subdir=HARNESS_HOME, bin_name=HARNESS_BIN):
        info("Aborted.")
        return

    config = load_config()
    existing_entry = get_value(config, f"harnesses.{HARNESS_NAME}")

    if existing_entry:
        project_name = prompt_project_name(get_value(config, f"harnesses.{HARNESS_NAME}.project_name") or HARNESS_NAME)
        target = existing_entry.get("target", "phoenix")
        credentials = {
            "endpoint": existing_entry.get("endpoint", ""),
            "api_key": existing_entry.get("api_key", ""),
        }
        if existing_entry.get("space_id"):
            credentials["space_id"] = existing_entry["space_id"]
        user_id = ""
    else:
        existing_harnesses = config.get("harnesses") if config else None
        target, credentials = prompt_backend(existing_harnesses)
        project_name = prompt_project_name(HARNESS_NAME)
        user_id = prompt_user_id()

    logging_block = None
    if (config.get("logging") if config else None) is None:
        logging_block = prompt_content_logging()
    else:
        info("Using existing logging settings from config.yaml")

    chosen = agent_name or _prompt_agent_name()

    install_noninteractive(
        target=target,
        credentials=credentials,
        project_name=project_name,
        user_id=user_id,
        with_skills=with_skills,
        logging_block=logging_block,
        agent_name=chosen,
        set_default=False,
    )

    _maybe_set_default(chosen)


def uninstall() -> None:
    """Remove our hooks from every Kiro agent file. Delete the agent file
    only when we created it (matches AGENT_SKELETON description)."""
    uninstall_noninteractive()


def _prompt_agent_name() -> str:
    """Ask the user which agent to install hooks into. Default arize-traced."""
    raw = input(f"Agent name to install tracing into [{DEFAULT_AGENT_NAME}]: ").strip()
    return raw or DEFAULT_AGENT_NAME


def _resolve_agent_path(name: str) -> Path:
    return KIRO_AGENTS_DIR / f"{name}.json"


def _load_agent(path: Path, fallback_name: str) -> dict:
    """Load an existing agent JSON or return a fresh skeleton."""
    if path.exists():
        try:
            data = json.loads(path.read_text())
            if not isinstance(data, dict):
                raise ValueError("not an object")
            data.setdefault("hooks", {})
            return data
        except (json.JSONDecodeError, OSError, ValueError) as exc:
            info(f"Warning: {path} is malformed ({exc}); rebuilding from skeleton")
    skel = copy.deepcopy(AGENT_SKELETON)
    skel["name"] = fallback_name
    skel["hooks"] = {}
    return skel


def _save_agent(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")


def _register_kiro_hooks(agent_path: Path, name: str) -> None:
    """Add an entry for each HOOK_EVENT pointing at our hook binary."""
    data = _load_agent(agent_path, name)
    hook_cmd = str(venv_bin(HOOK_BIN_NAME))
    hooks = data.setdefault("hooks", {})
    for event in HOOK_EVENTS:
        event_list = hooks.setdefault(event, [])
        if not any(h.get("command") == hook_cmd for h in event_list):
            event_list.append({"command": hook_cmd})

    if dry_run():
        info(f"would write Kiro agent config to {agent_path}")
        return
    _save_agent(agent_path, data)

    # Best-effort validation; non-fatal if Kiro CLI isn't on PATH.
    kiro_bin = _resolve_kiro_bin()
    if kiro_bin:
        result = subprocess.run(
            [kiro_bin, "agent", "validate", "--path", str(agent_path)],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            info(f"Warning: kiro-cli reported validation issue:\n{result.stderr}")


def _unregister_all_kiro_hooks() -> None:
    """Walk KIRO_AGENTS_DIR; in each agent JSON, remove entries whose
    command equals our HOOK_BIN_NAME (resolved via venv_bin). Drop empty
    event lists. If a file ended up empty AND was created by us
    (description matches the skeleton), delete it."""
    if not KIRO_AGENTS_DIR.is_dir():
        return
    hook_cmd = str(venv_bin(HOOK_BIN_NAME))
    skeleton_desc = AGENT_SKELETON["description"]

    for agent_file in KIRO_AGENTS_DIR.glob("*.json"):
        try:
            data = json.loads(agent_file.read_text())
        except (OSError, json.JSONDecodeError):
            continue

        hooks = data.get("hooks") if isinstance(data, dict) else None
        if not isinstance(hooks, dict):
            continue

        modified = False
        for event in list(hooks.keys()):
            event_list = hooks.get(event, [])
            filtered = [h for h in event_list if isinstance(h, dict) and h.get("command") != hook_cmd]
            if filtered != event_list:
                modified = True
            if filtered:
                hooks[event] = filtered
            else:
                del hooks[event]

        if not modified:
            continue

        we_created = data.get("description") == skeleton_desc and not hooks
        if we_created and not dry_run():
            agent_file.unlink(missing_ok=True)
            info(f"Removed agent file {agent_file} (created by Arize tracing install)")
        elif not dry_run():
            _save_agent(agent_file, data)
            info(f"Cleaned tracing hooks from {agent_file}")
        else:
            info(f"would clean tracing hooks from {agent_file}")


def _maybe_set_default(name: str) -> None:
    """Ask the user whether to set this agent as Kiro's default."""
    raw = input(f"Set '{name}' as Kiro's default agent? [y/N]: ").strip().lower()
    if raw not in ("y", "yes"):
        return
    if dry_run():
        info(f"would run: kiro-cli agent set-default {name}")
        return
    kiro_bin = _resolve_kiro_bin()
    if not kiro_bin:
        info(
            "Could not find kiro-cli on PATH; skipping set-default. " f"Run manually: kiro-cli agent set-default {name}"
        )
        return
    result = subprocess.run(
        [kiro_bin, "agent", "set-default", name],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        info(f"'{name}' is now Kiro's default agent")
    else:
        info(f"Failed to set default: {result.stderr.strip() or result.stdout.strip()}")


def _macos_app_kiro_path() -> str | None:
    """macOS-only: resolve Kiro CLI bundled binary if not on PATH."""
    candidate = Path("/Applications/Kiro CLI.app/Contents/MacOS/kiro-cli")
    return str(candidate) if candidate.exists() else None


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
