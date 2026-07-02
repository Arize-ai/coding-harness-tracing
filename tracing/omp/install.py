#!/usr/bin/env python3
"""omp (Oh My Pi) tracing harness installer.

A hybrid of two existing installers:

* The config-prompt flow + ``.ts`` file-drop come from
  ``tracing/opencode/install.py`` (omp loads extensions **in-process** inside its
  Bun runtime, so the shim is a file drop).
* The JSON ``settings.json`` read-merge-write comes from
  ``tracing/gemini/install.py`` — unlike opencode, omp does NOT auto-discover a
  plugin dir. We must register the shim's absolute path in the ``extensions``
  array of ``~/.omp/agent/settings.json``.

Usage (called by the shell router):
    python tracing/omp/install.py install [--with-skills]
    python tracing/omp/install.py uninstall
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import tracing.omp.constants as omp_constants
from core.config import get_value, load_config
from core.setup import dry_run, ensure_shared_runtime
from core.setup import err as _err
from core.setup import (
    info,
    merge_harness_entry,
    prompt_backend,
    prompt_content_logging,
    prompt_project_name,
    prompt_user_id,
    remove_harness_entry,
    symlink_skills,
    unlink_skills,
    write_config,
    write_logging_config,
)

# Header-marker the installer writes into the shim and checks on uninstall so
# we never delete a user's own extension file.
_HEADER_MARKER = "// Arize omp tracing hook (shim)."

# ---------------------------------------------------------------------------
# Path helpers (re-read constants each call so tests can monkeypatch them)
# ---------------------------------------------------------------------------


def _extensions_dir():
    import tracing.omp.constants as _c

    return _c.EXTENSIONS_DIR


def _plugin_file():
    import tracing.omp.constants as _c

    return _c.PLUGIN_FILE


def _settings_dir():
    import tracing.omp.constants as _c

    return _c.SETTINGS_DIR


def _settings_file():
    import tracing.omp.constants as _c

    return _c.SETTINGS_FILE


def _plugin_source():
    """Resolve the bundled shim asset relative to THIS installer file.

    Deliberately NOT via ``constants.PLUGIN_SOURCE``: at runtime the shell router
    executes install.py from the rsynced ~/.arize/harness tree (where the .ts
    ships alongside it), while ``tracing.omp.constants`` is imported from the venv
    site-packages copy, which does not carry the data asset. Resolving from
    install.py's own location works in every delivery (repo, INSTALL_DIR) and
    avoids the FileNotFoundError seen in real installs.
    """
    return Path(__file__).resolve().parent / "plugin" / "arize-tracing.ts"


# ---------------------------------------------------------------------------
# JSON settings helpers
# ---------------------------------------------------------------------------


def _read_settings() -> dict:
    """Read settings.json, returning ``{}`` on a missing or empty file.

    Like Gemini's installer, an unreadable file or malformed JSON raises
    ``SystemExit(1)`` rather than being treated as ``{}``: rewriting an empty
    dict back would silently wipe a hand-edited ``settings.json``. The user must
    fix the file and retry.
    """
    path = _settings_file()
    if not path.is_file():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:

        _err(f"Cannot read {path}: {exc}")
        sys.exit(1)
    if not text.strip():
        info("settings.json is empty, treating as {}")
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:

        _err(f"{path} contains invalid JSON; aborting. Please fix the file and retry.\n  {exc}")
        sys.exit(1)
    return data if isinstance(data, dict) else {}


def _write_settings(data: dict) -> None:
    """Write *data* as pretty-printed JSON to settings.json."""
    _settings_dir().mkdir(parents=True, exist_ok=True)
    _settings_file().write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Shim file-drop / removal
# ---------------------------------------------------------------------------


def _install_plugin() -> None:
    """Copy the shipped shim source into omp's extensions dir."""
    plugin_file = _plugin_file()

    if dry_run():
        info(f"would write omp hook to {plugin_file}")
        return

    _extensions_dir().mkdir(parents=True, exist_ok=True)
    shutil.copyfile(_plugin_source(), plugin_file)


def _uninstall_plugin() -> None:
    """Delete the shim file only when it carries our header marker."""
    plugin_file = _plugin_file()
    if not plugin_file.is_file():
        return

    try:
        text = plugin_file.read_text(encoding="utf-8")
    except OSError:
        return

    if not text.startswith(_HEADER_MARKER):
        return

    if dry_run():
        info(f"would remove omp hook {plugin_file}")
        return

    plugin_file.unlink()


# ---------------------------------------------------------------------------
# extensions-array registration in settings.json
# ---------------------------------------------------------------------------


def _register_extension() -> None:
    """Append the shim's absolute path to the ``extensions`` array (idempotent)."""
    if dry_run():
        info(f"would register omp extension in {_settings_file()}")
        return

    data = _read_settings()
    exts = data.get("extensions")
    if not isinstance(exts, list):
        if exts is not None:
            info("settings.json 'extensions' was not a list; overwriting with a fresh array")
        exts = []
        data["extensions"] = exts

    plugin_path = str(_plugin_file())
    if plugin_path not in exts:
        exts.append(plugin_path)

    _write_settings(data)


def _unregister_extension() -> None:
    """Remove the shim's path from the ``extensions`` array. No-op if absent."""
    path = _settings_file()
    if not path.is_file():
        return

    if dry_run():
        info(f"would unregister omp extension from {path}")
        return

    data = _read_settings()
    exts = data.get("extensions")
    if not isinstance(exts, list):
        return

    plugin_path = str(_plugin_file())
    remaining = [e for e in exts if e != plugin_path]
    if remaining:
        data["extensions"] = remaining
    else:
        # Don't leave a stray {"extensions": []} behind; drop the now-empty key
        # and remove the file entirely if we were the only thing in it.
        data.pop("extensions", None)

    if not data:
        path.unlink()
    else:
        _write_settings(data)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def install(with_skills: bool = False) -> None:
    """Install the omp tracing shim and register in config.json."""
    ensure_shared_runtime()

    config = load_config()
    existing_entry = get_value(config, f"harnesses.{omp_constants.HARNESS_NAME}")

    if not existing_entry or not isinstance(existing_entry, dict) or "target" not in existing_entry:
        existing_harnesses = config.get("harnesses") if config else None
        target, credentials = prompt_backend(existing_harnesses)
        project_name = prompt_project_name(omp_constants.HARNESS_NAME)
        user_id = prompt_user_id()
        if not dry_run():
            write_config(target, credentials, omp_constants.HARNESS_NAME, project_name, user_id=user_id)
        else:
            info("would write config.json with backend credentials")
    else:
        project_name = prompt_project_name(existing_entry.get("project_name") or omp_constants.HARNESS_NAME)
        merge_harness_entry(omp_constants.HARNESS_NAME, project_name)

    # Logging settings are global. Prompt only if no `logging:` block exists yet.
    if (config.get("logging") if config else None) is None:
        logging_block = prompt_content_logging()
        write_logging_config(logging_block)
    else:
        info("Using existing logging settings from config.json")

    _install_plugin()
    _register_extension()

    if with_skills:
        symlink_skills(omp_constants.HARNESS_NAME)

    info("omp tracing installed")


def uninstall() -> None:
    """Remove the omp tracing shim and deregister from config.json."""
    _unregister_extension()
    _uninstall_plugin()

    remove_harness_entry(omp_constants.HARNESS_NAME)
    unlink_skills(omp_constants.HARNESS_NAME)
    info("omp tracing uninstalled")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Dispatch install / uninstall from the command line."""
    if len(sys.argv) < 2 or sys.argv[1] not in ("install", "uninstall"):
        print(f"usage: {sys.argv[0]} {{install|uninstall}} [--with-skills]", file=sys.stderr)
        sys.exit(1)

    action = sys.argv[1]
    flags = set(sys.argv[2:])

    if action == "install":
        install(with_skills="--with-skills" in flags)
    else:
        uninstall()


if __name__ == "__main__":
    main()
