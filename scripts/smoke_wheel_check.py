"""Checks for scripts/smoke-wheel.sh.

    python -I -B scripts/smoke_wheel_check.py harnesses
    python -I -B scripts/smoke_wheel_check.py entries <arize-harness> <harness>...
    python -I -B scripts/smoke_wheel_check.py no-entries <harness>...
    python -I -B scripts/smoke_wheel_check.py hook-list <hook --list output> <harness>...
    python -I -B scripts/smoke_wheel_check.py json <file>
    python -I -B scripts/smoke_wheel_check.py snapshot <out> <dir>...
    python -I -B scripts/smoke_wheel_check.py same <snapshot> <snapshot>

Uses this checkout's ``core``, never the installed package's, and reads hook entries
with ``core.setup.status.registration_values``. HOME and the working directory must
be the throwaway ones. A failed check prints each problem and exits 1.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _fail(problems: list[str]) -> int:
    for problem in problems:
        print(problem, file=sys.stderr)
    return 1 if problems else 0


def harnesses() -> int:
    from core.constants import HARNESSES

    print(" ".join(HARNESSES))
    return 0


def _values(name: str) -> list[tuple[Path, list[object]]]:
    """``(file, values)`` for each file holding ``name``'s hook entries, plus Claude's plugin hooks."""
    from core.setup.assets import claude_plugin_dir
    from core.setup.status import registration_values

    extra = [claude_plugin_dir() / "hooks"] if name == "claude-code" else []
    return registration_values(name, extra)


def _ours(name: str) -> list[tuple[Path, object]]:
    from core.setup import is_our_hook_command

    return [(file, value) for file, values in _values(name) for value in values if is_our_hook_command(value, name)]


def _venv_forms() -> list[str]:
    from core.setup import VENV_DIR

    rel = ".arize/harness/venv"
    return [VENV_DIR.as_posix(), f"~/{rel}", f"$HOME/{rel}", f"${{HOME}}/{rel}"]


def _names_venv(value: object, forms: list[str]) -> bool:
    words = value if isinstance(value, list) else [value]
    return any(isinstance(w, str) and any(f in w.replace("\\", "/") for f in forms) for w in words)


def entries(command: str, names: list[str]) -> int:
    """Every hook entry of ours runs ``'<command>' hook <harness> ...`` and names no ``~/.arize/harness/venv`` path."""
    from core.setup import is_arize_hook_command, registered_command_path

    forms = _venv_forms()
    problems = []
    for name in names:
        for file, values in _values(name):
            # Every value, not only our entries. TOML yields an argv list and its strings; report each once.
            strings = {w for v in values for w in (v if isinstance(v, list) else [v])}
            bad = [f"{name}: {file}: names a venv path: {w!r}" for w in sorted(strings) if _names_venv(w, forms)]
            # values holds only a plugin's launched argv; the venv path may be anywhere in its source.
            if not bad and file.suffix == ".ts" and _names_venv(file.read_text(errors="ignore"), forms):
                bad.append(f"{name}: {file}: names a venv path in its source")
            problems += bad
        found = _ours(name)
        if not found:
            problems.append(f"{name}: no hook entry of ours")
        for file, value in found:
            if _names_venv(value, forms):
                continue
            if not is_arize_hook_command(value, name):
                problems.append(f"{name}: {file}: does not run `arize-harness hook {name}`: {value!r}")
            elif registered_command_path(value) != command:
                problems.append(f"{name}: {file}: runs {registered_command_path(value)}, not {command}: {value!r}")
    return _fail(problems)


def no_entries(names: list[str]) -> int:
    return _fail([f"{name}: {file}: entry left: {value!r}" for name in names for file, value in _ours(name)])


def hook_list(output: Path, names: list[str]) -> int:
    from core.hook_table import canonical_harness

    listed = {line.split()[0] for line in output.read_text().splitlines() if line.strip()}
    return _fail(
        [f"{name}: not listed as {canonical_harness(name)}" for name in names if canonical_harness(name) not in listed]
    )


def parse_json(path: Path) -> int:
    try:
        json.loads(path.read_text())
    except (OSError, ValueError) as e:
        return _fail([f"{path}: not JSON: {e}"])
    return 0


def snapshot(out: Path, dirs: list[Path]) -> int:
    """Each file, folder and symlink under ``dirs``, with its content hash (or link target) and mode."""
    files: dict[str, str] = {}
    problems: list[str] = []

    def walk(folder: Path) -> None:
        try:
            children = sorted(os.scandir(folder), key=lambda e: e.name)
        except OSError as e:
            problems.append(f"{folder}: cannot open: {e.strerror or e}")
            return
        for child in children:
            path = Path(child.path)
            try:
                if child.is_symlink():
                    files[str(path)] = f"link:{os.readlink(path)}"
                elif child.is_dir():
                    files[str(path)] = f"dir:{child.stat().st_mode:o}"
                    walk(path)
                elif child.is_file():
                    digest = hashlib.sha256(path.read_bytes()).hexdigest()
                    files[str(path)] = f"{digest}:{child.stat().st_mode:o}"
            except OSError as e:
                problems.append(f"{path}: cannot read: {e.strerror or e}")

    for root in dirs:
        walk(root)
    if problems:
        return _fail(problems)
    out.write_text(json.dumps(files, indent=1, sort_keys=True))
    return 0


def same(a: Path, b: Path) -> int:
    before, after = json.loads(a.read_text()), json.loads(b.read_text())
    problems = [f"added: {p}" for p in sorted(after.keys() - before.keys())]
    problems += [f"removed: {p}" for p in sorted(before.keys() - after.keys())]
    problems += [f"changed: {p}" for p in sorted(before.keys() & after.keys()) if before[p] != after[p]]
    return _fail(problems)


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__, file=sys.stderr)
        return 2
    cmd, args = argv[0], argv[1:]
    if cmd == "harnesses" and not args:
        return harnesses()
    if cmd == "entries" and len(args) >= 2:
        return entries(args[0], args[1:])
    if cmd == "no-entries" and args:
        return no_entries(args)
    if cmd == "hook-list" and len(args) >= 2:
        return hook_list(Path(args[0]), args[1:])
    if cmd == "json" and len(args) == 1:
        return parse_json(Path(args[0]))
    if cmd == "snapshot" and len(args) >= 2:
        return snapshot(Path(args[0]), [Path(d) for d in args[1:]])
    if cmd == "same" and len(args) == 2:
        return same(Path(args[0]), Path(args[1]))
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
