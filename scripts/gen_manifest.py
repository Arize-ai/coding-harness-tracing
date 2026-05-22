#!/usr/bin/env python3
"""Generate core/manifest.json from per-harness constants modules.

Reads each tracing/<harness>/constants.py and extracts the fields listed in
EXTRACTED_FIELDS. Every field is optional: if a constants module does not
define a field, the manifest emits the per-field default (None for scalars,
empty list for collection fields) and a warning to stderr. The plan that
created this generator forbids adding fields to constants.py to fill gaps —
downstream consumers (the Go binary in cmd/ax-trace) must tolerate null
values until the constants modules themselves grow the missing fields.

Writes a stable, sorted JSON file at core/manifest.json. Designed to produce
identical output on every run so a CI diff check is reliable.

Schema notes for downstream consumers:
  - `harnesses.<name>.hook_events` is heterogeneous across harnesses:
    * dict {event_name: handler_basename} for harnesses that bind each event
      to a distinct entry point (claude_code, copilot).
    * list of event_name strings for harnesses where a single dispatcher
      handles every event (cursor, kiro).
    * empty list for harnesses that don't expose hooks (codex, gemini).
    Consumers must handle all three shapes (or treat the field as opaque).
  - `display_name`, `harness_bin`, `settings_file` may be `null` for any
    harness whose constants module hasn't defined them yet. Consumers must
    check for null before dereferencing.

Usage: python scripts/gen_manifest.py [--check]
  --check: exit 1 if the generated content differs from the existing file.
"""

from __future__ import annotations

import argparse
import difflib
import importlib
import json
import sys
from pathlib import Path

HARNESSES = ["claude_code", "codex", "copilot", "cursor", "gemini", "kiro"]

# All extracted fields are optional. Missing fields get the per-field default
# (None for scalar fields, empty tuple for collection fields). Use immutable
# tuple defaults so the shared instance can't be mutated by accident.
EXTRACTED_FIELDS: dict[str, object] = {
    "DISPLAY_NAME": None,
    "HARNESS_BIN": None,
    "SETTINGS_FILE": None,
    "HOOK_EVENTS": (),
    "ARIZE_ENV_KEYS": (),
}

SHARED = {
    "config_file": "~/.arize/harness/config.yaml",
    "install_dir": "~/.arize/harness",
    "venv_dir": "~/.arize/harness/venv",
    "otlp_endpoint_default": "otlp.arize.com:443",
}

SCHEMA_VERSION = 1

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT_PATH = REPO_ROOT / "core" / "manifest.json"


def _coerce(value: object) -> object:
    """Convert non-JSON-serializable values into stable JSON-friendly forms.

    Path handling rules (to keep the manifest portable across machines):
      * Relative paths (e.g. project-local `.github/hooks/hooks.json`) are
        emitted as posix strings verbatim.
      * Absolute paths under `$HOME` are rewritten with a `~/` prefix.
      * Absolute paths outside `$HOME` are an error — they would bake a
        developer's machine layout into the committed manifest.
    """
    if isinstance(value, Path):
        if not value.is_absolute():
            return value.as_posix()
        try:
            rel = value.relative_to(Path.home())
        except ValueError:
            raise ValueError(
                f"refusing to serialize non-home absolute Path {value!s}: "
                "manifest must be portable across developer machines"
            )
        return "~/" + rel.as_posix()
    if isinstance(value, tuple):
        return [_coerce(v) for v in value]
    if isinstance(value, dict):
        return {k: _coerce(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_coerce(v) for v in value]
    return value


def build_manifest() -> dict:
    """Load each harness's constants module and assemble the manifest dict."""
    inserted = False
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
        inserted = True
    try:
        harnesses: dict[str, dict] = {}
        for name in HARNESSES:
            module_path = f"tracing.{name}.constants"
            try:
                mod = importlib.import_module(module_path)
            except ImportError as e:
                print(
                    f"warning: could not import {module_path}: {e}",
                    file=sys.stderr,
                )
                continue

            entry: dict[str, object] = {}
            for field, default in EXTRACTED_FIELDS.items():
                if hasattr(mod, field):
                    entry[field.lower()] = _coerce(getattr(mod, field))
                else:
                    print(
                        f"warning: {module_path} does not define {field}; " f"emitting default ({default!r})",
                        file=sys.stderr,
                    )
                    entry[field.lower()] = _coerce(default)
            harnesses[name] = entry

        return {
            "schema_version": SCHEMA_VERSION,
            "harnesses": harnesses,
            "shared": SHARED,
        }
    finally:
        if inserted:
            try:
                sys.path.remove(str(REPO_ROOT))
            except ValueError:
                pass


def serialize(manifest: dict) -> str:
    """Stable JSON serialization: sorted keys, 2-space indent, trailing newline."""
    return json.dumps(manifest, sort_keys=True, indent=2) + "\n"


def generate(output_path: Path | None = None) -> str:
    """Build the manifest, write it to disk, and return the serialized content.

    Pass `output_path=None` to skip the disk write (useful for tests).
    """
    content = serialize(build_manifest())
    if output_path is not None:
        output_path.write_text(content)
    return content


def _check(output_path: Path) -> int:
    """Return 0 if `output_path` matches a freshly generated manifest, else 1."""
    new_content = serialize(build_manifest())
    if not output_path.exists():
        print(
            f"error: {output_path} does not exist; run scripts/gen_manifest.py to create it",
            file=sys.stderr,
        )
        return 1
    existing = output_path.read_text()
    if existing == new_content:
        return 0
    diff = difflib.unified_diff(
        existing.splitlines(keepends=True),
        new_content.splitlines(keepends=True),
        fromfile=str(output_path),
        tofile="generated",
    )
    sys.stderr.write(f"error: {output_path} is stale; run scripts/gen_manifest.py to regenerate\n")
    sys.stderr.writelines(diff)
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit 1 if output differs from existing file.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT_PATH,
        help="Where to write (or check against) the manifest. Defaults to core/manifest.json.",
    )
    args = parser.parse_args(argv)

    if args.check:
        return _check(args.output)

    generate(args.output)
    print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
