#!/usr/bin/env python3
"""Generate core/manifest.json from per-harness constants modules.

Reads each tracing/<harness>/constants.py and extracts:
  - DISPLAY_NAME, HARNESS_BIN, SETTINGS_FILE, HOOK_EVENTS, ARIZE_ENV_KEYS

Writes a stable, sorted JSON file at core/manifest.json. Designed to produce
identical output on every run so a CI diff check is reliable.

Usage: python scripts/gen_manifest.py [--check]
  --check: exit 1 if the generated content differs from the existing file.
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from pathlib import Path

HARNESSES = ["claude_code", "codex", "copilot", "cursor", "gemini", "kiro"]

REQUIRED_FIELDS = ["DISPLAY_NAME", "HARNESS_BIN", "SETTINGS_FILE"]
OPTIONAL_FIELDS: dict[str, object] = {
    "HOOK_EVENTS": [],
    "ARIZE_ENV_KEYS": [],
}

SHARED = {
    "config_file": "~/.arize/harness/config.yaml",
    "install_dir": "~/.arize/harness",
    "venv_dir": "~/.arize/harness/venv",
    "otlp_endpoint_default": "otlp.arize.com:443",
}

SCHEMA_VERSION = 1


def _coerce(value: object) -> object:
    """Convert non-JSON-serializable values into stable JSON-friendly forms.

    Path objects under the current user's home are rewritten with a `~` prefix
    so the generated manifest is identical across developers' machines.
    """
    if isinstance(value, Path):
        home = Path.home()
        try:
            rel = value.relative_to(home)
            return "~/" + rel.as_posix()
        except ValueError:
            return value.as_posix()
    if isinstance(value, tuple):
        return [_coerce(v) for v in value]
    if isinstance(value, dict):
        return {k: _coerce(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_coerce(v) for v in value]
    return value


def build_manifest() -> dict:
    repo_root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(repo_root))

    harnesses: dict[str, dict] = {}
    for name in HARNESSES:
        module_path = f"tracing.{name}.constants"
        try:
            mod = importlib.import_module(module_path)
        except ImportError as e:
            print(f"warning: could not import {module_path}: {e}", file=sys.stderr)
            continue

        entry: dict[str, object] = {}
        for field in REQUIRED_FIELDS:
            if not hasattr(mod, field):
                print(
                    f"error: {module_path} missing required field {field}",
                    file=sys.stderr,
                )
                sys.exit(1)
            entry[field.lower()] = _coerce(getattr(mod, field))
        for field, default in OPTIONAL_FIELDS.items():
            entry[field.lower()] = _coerce(getattr(mod, field, default))
        harnesses[name] = entry

    return {
        "schema_version": SCHEMA_VERSION,
        "harnesses": harnesses,
        "shared": SHARED,
    }


def serialize(manifest: dict) -> str:
    return json.dumps(manifest, sort_keys=True, indent=2) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit 1 if output differs from existing file.",
    )
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parent.parent
    output_path = repo_root / "core" / "manifest.json"
    new_content = serialize(build_manifest())

    if args.check:
        if not output_path.exists():
            print(
                f"error: {output_path} does not exist; run scripts/gen_manifest.py to create it",
                file=sys.stderr,
            )
            return 1
        existing = output_path.read_text()
        if existing != new_content:
            print(
                f"error: {output_path} is stale; run scripts/gen_manifest.py to regenerate",
                file=sys.stderr,
            )
            return 1
        return 0

    output_path.write_text(new_content)
    print(f"wrote {output_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
