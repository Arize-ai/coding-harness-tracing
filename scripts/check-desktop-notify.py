#!/usr/bin/env python3
"""Read-only check of the installed notification chain; never print credentials."""

import json
import sys
from pathlib import Path

from core.config import load_config
from core.setup import CONFIG_FILE, venv_bin
from tracing.codex._toml import _toml_load_strict
from tracing.codex.constants import NOTIFY_BIN_NAME, get_codex_home
from tracing.codex.notify_chain import install_notify, remove_notify


def main():
    path = get_codex_home() / "config.toml"
    command = _toml_load_strict(path).get("notify", [])
    hook = str(venv_bin(NOTIFY_BIN_NAME))
    registered = remove_notify(command, hook) != command
    valid = registered and install_notify(command, hook) == command
    entry = (load_config(str(CONFIG_FILE)).get("harnesses") or {}).get("codex", {})
    print(
        json.dumps(
            {
                "config_path": str(path),
                "outer_callback": Path(command[0]).name if command else None,
                "desktop_helper_outermost": bool(command and Path(command[0]).name == "SkyComputerUseClient"),
                "arize_chained": registered,
                "chain_valid": valid,
                "hook_executable_exists": Path(hook).is_file(),
                "backend": entry.get("target"),
                "endpoint": entry.get("endpoint"),
                "project": entry.get("project_name"),
                "api_key_present": bool(entry.get("api_key")),
            },
            indent=2,
        )
    )
    return 0 if valid and Path(hook).is_file() else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, OSError) as exc:
        print(f"Cannot verify notification config: {type(exc).__name__}", file=sys.stderr)
        sys.exit(1)
