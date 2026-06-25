"""Cursor harness install/uninstall, invoked by the installer router."""

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

PROJECT_HOOKS_FILE = Path(".cursor") / "hooks.json"
PROJECT_HOOK_SCRIPT = Path(".cursor") / "hooks" / "arize-hook-cursor.sh"
PROJECT_CLOUD_SETUP_SCRIPT = Path(".cursor") / "hooks" / "arize-cursor-cloud-setup.sh"
PROJECT_CLOUD_ENV_EXAMPLE = Path(".cursor") / "hooks" / "arize-cloud-env.example"
PROJECT_ENVIRONMENT_FILE = Path(".cursor") / "environment.json"
PROJECT_HOOK_COMMAND = f"bash {PROJECT_HOOK_SCRIPT.as_posix()}"
CLOUD_SETUP_COMMAND = (
    'ARIZE_API_KEY="${ARIZE_API_KEY:-}" '
    'ARIZE_SPACE_ID="${ARIZE_SPACE_ID:-}" '
    'PHOENIX_ENDPOINT="${PHOENIX_ENDPOINT:-}" '
    'ARIZE_PROJECT_NAME="${ARIZE_PROJECT_NAME:-cursor}" '
    'ARIZE_INSTALL_BRANCH="${ARIZE_INSTALL_BRANCH:-main}" '
    'ARIZE_INSTALL_URL="${ARIZE_INSTALL_URL:-}" '
    f"bash {PROJECT_CLOUD_SETUP_SCRIPT.as_posix()}"
)


def install(with_skills: bool = False, project_hooks: bool = False, cloud_agent: bool = False) -> None:
    """Install Cursor tracing: configure backend, register hooks, optionally symlink skills."""
    if cloud_agent:
        project_hooks = True

    if not project_hooks and not ensure_harness_installed(DISPLAY_NAME, home_subdir=HARNESS_HOME, bin_name=HARNESS_BIN):
        info("Aborted.")
        return

    ensure_shared_runtime()

    # Create cursor state dir
    state_dir = INSTALL_DIR / "state" / HARNESS_NAME
    if dry_run():
        info(f"would create {state_dir}")
    else:
        state_dir.mkdir(parents=True, exist_ok=True)

    config = load_config()
    if cloud_agent:
        info("Skipping interactive backend configuration for Cursor Cloud Agent mode; use environment secrets.")
    else:
        # If this harness has no entry yet, prompt for backend; otherwise just update project_name.
        existing_entry = get_value(config, f"harnesses.{HARNESS_NAME}")
        if not existing_entry:
            existing_harnesses = config.get("harnesses") if config else None
            target, credentials = prompt_backend(existing_harnesses)
            project_name = prompt_project_name(HARNESS_NAME)
            user_id = prompt_user_id()
            if not dry_run():
                from core.setup import write_config

                write_config(target, credentials, HARNESS_NAME, project_name, user_id=user_id)
            else:
                info("would write config.yaml with backend credentials")
        else:
            project_name = prompt_project_name(
                get_value(config, f"harnesses.{HARNESS_NAME}.project_name") or HARNESS_NAME
            )
            merge_harness_entry(HARNESS_NAME, project_name)

        # Logging settings are global. Prompt only if no `logging:` block exists yet —
        # subsequent harness installs reuse what the first wizard wrote.
        if (config.get("logging") if config else None) is None:
            logging_block = prompt_content_logging()
            write_logging_config(logging_block)
        else:
            info("Using existing logging settings from config.yaml")

    if project_hooks:
        _register_project_cursor_hooks(cloud_agent=cloud_agent)
    else:
        _register_cursor_hooks()
    if with_skills:
        symlink_skills(HARNESS_NAME)
    hooks_file = PROJECT_HOOKS_FILE if project_hooks else HOOKS_FILE
    info(f"Cursor tracing installed ({hooks_file})")


def uninstall() -> None:
    """Remove Cursor tracing hooks, harness entry, and skill symlinks."""
    _unregister_cursor_hooks()
    _unregister_project_cursor_hooks()
    remove_harness_entry(HARNESS_NAME)
    unlink_skills(HARNESS_NAME)
    info("Cursor tracing uninstalled")


def _load_hooks(path: Path = HOOKS_FILE) -> dict:
    """Load HOOKS_FILE as JSON, returning a fresh skeleton if missing or malformed."""
    if not path.exists():
        return {"version": 1, "hooks": {}}
    try:
        data = json.loads(path.read_text())
        if not isinstance(data, dict):
            return {"version": 1, "hooks": {}}
        data.setdefault("version", 1)
        data.setdefault("hooks", {})
        return data
    except (json.JSONDecodeError, OSError):
        return {"version": 1, "hooks": {}}


def _save_hooks(data: dict, path: Path = HOOKS_FILE) -> None:
    """Write hooks dict as formatted JSON with trailing newline."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")


def _register_cursor_hooks() -> None:
    """Add hook entries for all HOOK_EVENTS to ~/.cursor/hooks.json.

    For each event, ensure an entry with ``command == venv_bin(HOOK_BIN_NAME)``
    exists — skip if already there.  Merges with existing entries without
    duplicating.  Honors dry_run().
    """
    data = _load_hooks(HOOKS_FILE)
    hooks = data["hooks"]
    hook_cmd = str(venv_bin(HOOK_BIN_NAME))

    _add_hook_entries(hooks, hook_cmd)

    if dry_run():
        info(f"would write Cursor hooks to {HOOKS_FILE}")
        return

    _save_hooks(data, HOOKS_FILE)


def _add_hook_entries(hooks: dict, hook_cmd: str) -> None:
    """Ensure one hook command is registered for every Cursor hook event."""
    for event in HOOK_EVENTS:
        event_list = hooks.setdefault(event, [])
        already = any(h.get("command") == hook_cmd for h in event_list)
        if not already:
            event_list.append({"command": hook_cmd})


def _register_project_cursor_hooks(cloud_agent: bool = False) -> None:
    """Write repo-local Cursor hooks for Cloud/Background Agent checkouts."""
    _write_project_hook_script()
    if cloud_agent:
        _write_cloud_setup_script()
        _write_cloud_env_example()
        _ensure_cloud_environment_bootstrap()

    data = _load_hooks(PROJECT_HOOKS_FILE)
    hooks = data["hooks"]
    _add_hook_entries(hooks, PROJECT_HOOK_COMMAND)

    if dry_run():
        info(f"would write project Cursor hooks to {PROJECT_HOOKS_FILE}")
        return

    _save_hooks(data, PROJECT_HOOKS_FILE)


def _write_project_hook_script() -> None:
    """Write a repo-local hook wrapper that works in local and Cloud Agent checkouts."""
    if dry_run():
        info(f"would write Cursor project hook wrapper to {PROJECT_HOOK_SCRIPT}")
        return

    PROJECT_HOOK_SCRIPT.parent.mkdir(parents=True, exist_ok=True)
    PROJECT_HOOK_SCRIPT.write_text(
        """#!/usr/bin/env bash
set -euo pipefail

HOOK_BIN="${ARIZE_HOOK_CURSOR:-$HOME/.arize/harness/venv/bin/arize-hook-cursor}"
LOG_FILE="${ARIZE_LOG_FILE:-$HOME/.arize/harness/logs/cursor.log}"

if [[ ! -x "$HOOK_BIN" ]]; then
    mkdir -p "$(dirname "$LOG_FILE")"
    printf '[arize] Cursor hook binary not found: %s\\n' "$HOOK_BIN" >> "$LOG_FILE"
    exit 0
fi

exec "$HOOK_BIN"
""",
        encoding="utf-8",
    )
    PROJECT_HOOK_SCRIPT.chmod(0o755)


def _write_cloud_setup_script() -> None:
    """Write a Cloud Agent bootstrap script that installs the harness in the remote VM."""
    if dry_run():
        info(f"would write Cursor Cloud bootstrap script to {PROJECT_CLOUD_SETUP_SCRIPT}")
        return

    PROJECT_CLOUD_SETUP_SCRIPT.parent.mkdir(parents=True, exist_ok=True)
    PROJECT_CLOUD_SETUP_SCRIPT.write_text(
        """#!/usr/bin/env bash
set -euo pipefail

if [[ -z "${PHOENIX_ENDPOINT:-}" && ( -z "${ARIZE_API_KEY:-}" || -z "${ARIZE_SPACE_ID:-}" ) ]]; then
    echo "[arize] Warning: set ARIZE_API_KEY+ARIZE_SPACE_ID or PHOENIX_ENDPOINT as Cursor Cloud secrets." >&2
fi

run_as_root() {
    if [[ "${EUID:-$(id -u)}" -eq 0 ]]; then
        "$@"
    elif command -v sudo >/dev/null 2>&1; then
        sudo "$@"
    else
        return 1
    fi
}

ensure_python_venv_support() {
    if ! command -v python3 >/dev/null 2>&1; then
        return 0
    fi

    probe_dir="$(mktemp -d)"
    if python3 -m venv "$probe_dir/venv" >/dev/null 2>&1; then
        rm -rf "$probe_dir"
        return 0
    fi
    rm -rf "$probe_dir"

    if ! command -v apt-get >/dev/null 2>&1; then
        echo "[arize] Warning: python3 venv support is missing and apt-get is unavailable." >&2
        return 0
    fi

    echo "[arize] Installing python3-venv for Cursor Cloud bootstrap..." >&2
    run_as_root apt-get update
    if ! run_as_root env DEBIAN_FRONTEND=noninteractive apt-get install -y python3-venv; then
        versioned_pkg="$(python3 - <<'PY'
import sys
print(f"python{sys.version_info.major}.{sys.version_info.minor}-venv")
PY
)"
        run_as_root env DEBIAN_FRONTEND=noninteractive apt-get install -y "$versioned_pkg"
    fi
}

remove_incomplete_harness_venv() {
    venv_dir="$HOME/.arize/harness/venv"
    if [[ -d "$venv_dir" && ! -x "$venv_dir/bin/pip" && ! -x "$venv_dir/Scripts/pip.exe" ]]; then
        echo "[arize] Removing incomplete harness venv at $venv_dir" >&2
        rm -rf "$venv_dir"
    fi
}

ensure_python_venv_support
remove_incomplete_harness_venv

branch="${ARIZE_INSTALL_BRANCH:-main}"
url="${ARIZE_INSTALL_URL:-https://raw.githubusercontent.com/Arize-ai/coding-harness-tracing/${branch}/install.sh}"
tmp="$(mktemp)"
cleanup() { rm -f "$tmp"; }
trap cleanup EXIT

if command -v curl >/dev/null 2>&1; then
    curl -sSfL "$url" -o "$tmp"
elif command -v wget >/dev/null 2>&1; then
    wget -qO "$tmp" "$url"
else
    echo "[arize] Neither curl nor wget found; cannot install Cursor tracing." >&2
    exit 1
fi

bash "$tmp" cursor --cloud-agent --branch "$branch"
""",
        encoding="utf-8",
    )
    PROJECT_CLOUD_SETUP_SCRIPT.chmod(0o755)


def _write_cloud_env_example() -> None:
    """Write non-secret env var documentation next to the Cloud bootstrap script."""
    if dry_run():
        info(f"would write Cursor Cloud env example to {PROJECT_CLOUD_ENV_EXAMPLE}")
        return

    PROJECT_CLOUD_ENV_EXAMPLE.parent.mkdir(parents=True, exist_ok=True)
    PROJECT_CLOUD_ENV_EXAMPLE.write_text(
        """# Configure these as Cursor Cloud Agent environment secrets.
# Do not commit real credential values.

# Arize AX:
ARIZE_API_KEY=
ARIZE_SPACE_ID=
ARIZE_PROJECT_NAME=cursor

# Optional: pin Cloud Agents to a branch or raw installer URL while testing
# unmerged bootstrap changes. Omit these when main has the desired installer.
# ARIZE_INSTALL_BRANCH=main
# ARIZE_INSTALL_URL=

# Phoenix alternative:
# PHOENIX_ENDPOINT=http://localhost:6006
# ARIZE_API_KEY=
""",
        encoding="utf-8",
    )


def _ensure_cloud_environment_bootstrap() -> None:
    """Create or update .cursor/environment.json so Cloud VMs install tracing."""
    data = {}
    if PROJECT_ENVIRONMENT_FILE.exists():
        try:
            loaded = json.loads(PROJECT_ENVIRONMENT_FILE.read_text())
            data = loaded if isinstance(loaded, dict) else {}
        except (json.JSONDecodeError, OSError):
            data = {}

    existing_install = data.get("install")
    if isinstance(existing_install, str) and existing_install.strip():
        if PROJECT_CLOUD_SETUP_SCRIPT.as_posix() not in existing_install:
            data["install"] = f"{CLOUD_SETUP_COMMAND} && {existing_install}"
    else:
        data["install"] = CLOUD_SETUP_COMMAND

    if dry_run():
        info(f"would write Cursor Cloud environment bootstrap to {PROJECT_ENVIRONMENT_FILE}")
        return

    PROJECT_ENVIRONMENT_FILE.parent.mkdir(parents=True, exist_ok=True)
    PROJECT_ENVIRONMENT_FILE.write_text(json.dumps(data, indent=2) + "\n")


def _unregister_cursor_hooks() -> None:
    """Remove our hook entries from ~/.cursor/hooks.json.

    Keeps other hooks intact.  Removes event keys that become empty after
    filtering.  No-op if file doesn't exist.  Honors dry_run().
    """
    if not HOOKS_FILE.exists():
        return

    data = _load_hooks(HOOKS_FILE)
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

    _save_hooks(data, HOOKS_FILE)


def _unregister_project_cursor_hooks() -> None:
    """Remove repo-local Arize project hook entries when present."""
    if not PROJECT_HOOKS_FILE.exists():
        return

    data = _load_hooks(PROJECT_HOOKS_FILE)
    hooks = data.get("hooks", {})
    if not hooks:
        return

    for event in list(hooks.keys()):
        event_list = hooks[event]
        filtered = [h for h in event_list if h.get("command") != PROJECT_HOOK_COMMAND]
        if filtered:
            hooks[event] = filtered
        else:
            del hooks[event]

    if dry_run():
        info(f"would remove project Cursor hooks from {PROJECT_HOOKS_FILE}")
        return

    _save_hooks(data, PROJECT_HOOKS_FILE)


def main() -> None:
    """Dispatch install / uninstall from the command line."""
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    flags = set(sys.argv[2:])
    if cmd == "install":
        install(
            with_skills="--with-skills" in flags,
            project_hooks="--project-hooks" in flags,
            cloud_agent="--cloud-agent" in flags,
        )
    elif cmd == "uninstall":
        uninstall()
    else:
        print(
            "usage: install.py {install|uninstall} [--with-skills] [--project-hooks] [--cloud-agent]",
            file=sys.stderr,
        )
        sys.exit(2)


if __name__ == "__main__":
    main()
