"""Checks for scripts/smoke-wheel.sh.

    python -I -B scripts/smoke_wheel_check.py harnesses
    python -I -B scripts/smoke_wheel_check.py entries <arize-harness> <harness>...
    python -I -B scripts/smoke_wheel_check.py no-entries <harness>...
    python -I -B scripts/smoke_wheel_check.py hook-list <hook --list output> <harness>...
    python -I -B scripts/smoke_wheel_check.py json <file>
    python -I -B scripts/smoke_wheel_check.py snapshot <out> <dir>...
    python -I -B scripts/smoke_wheel_check.py same <snapshot> <snapshot>
    python -I -B scripts/smoke_wheel_check.py claude-turn <arize-harness>

Uses this checkout's ``core``, never the installed package's, and reads hook entries
with ``core.setup.status.registration_values``. HOME and the working directory must
be the throwaway ones. A failed check prints each problem and exits 1.
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CLAUDE_TURN = ROOT / "scripts" / "smoke_fixtures" / "claude_turn.json"
SPAN_LINE = "[dry-run] would send span"
HOOK_TIMEOUT = 60


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


def _claude_hooks(hooks: object) -> dict[str, list[str]]:
    """``{event: [our command, ...]}`` from a Claude ``hooks`` block."""
    from core.setup import is_arize_hook_command

    found: dict[str, list[str]] = {}
    if not isinstance(hooks, dict):
        return found
    for event, groups in hooks.items():
        for group in groups if isinstance(groups, list) else []:
            for hook in group.get("hooks", []) if isinstance(group, dict) else []:
                command = hook.get("command") if isinstance(hook, dict) else None
                if is_arize_hook_command(command, "claude-code"):
                    found.setdefault(event, []).append(command)
    return found


def _read_json(path: Path) -> tuple[object, str | None]:
    try:
        return json.loads(path.read_text()), None
    except (OSError, ValueError) as e:
        return None, f"{path}: cannot read: {e}"


def claude_turn(command: str) -> int:
    """Feed one Claude turn to the ``settings.json`` hook commands in dry-run and expect a logged span."""
    from core.setup import registered_command_path
    from core.setup.assets import claude_plugin_dir

    settings_file = Path.home() / ".claude" / "settings.json"
    settings, err = _read_json(settings_file)
    if err:
        return _fail([err])
    registered = _claude_hooks(settings.get("hooks") if isinstance(settings, dict) else None)
    if not registered:
        return _fail([f"{settings_file}: no Claude hook command registered"])

    problems = []
    plugin = claude_plugin_dir()
    plugins = settings.get("plugins")
    if not any(p == str(plugin) or (isinstance(p, dict) and p.get("path") == str(plugin)) for p in plugins or []):
        problems.append(f"{settings_file}: no plugin entry for {plugin}")
    hooks_json = plugin / "hooks" / "hooks.json"
    plugin_hooks, err = _read_json(hooks_json)
    plugin_registered = _claude_hooks(plugin_hooks.get("hooks") if isinstance(plugin_hooks, dict) else None)
    if err:
        problems.append(err)
    elif not plugin_registered:
        problems.append(f"{hooks_json}: no Claude hook command")
    for file, found in ((settings_file, registered), (hooks_json, plugin_registered)):
        for value in (v for values in found.values() for v in values):
            if registered_command_path(value) != command:
                problems.append(f"{file}: runs {registered_command_path(value)}, not {command}: {value!r}")
    if problems:
        return _fail(problems)

    env = {k: v for k, v in os.environ.items() if not k.startswith(("ARIZE_", "PHOENIX_", "OTEL_"))}
    env.update(ARIZE_DRY_RUN="true", ARIZE_VERBOSE="true")
    session = {"session_id": f"smoke-wheel-{uuid.uuid4().hex}", "cwd": os.getcwd()}
    for event in json.loads(CLAUDE_TURN.read_text()):
        name = event["hook_event_name"]
        if name not in registered:
            return _fail([f"{settings_file}: no {name} hook command registered"])
        for hook in registered[name]:
            # Own session, so a timeout kills the hook's whole process group.
            proc = subprocess.Popen(
                ["/bin/sh", "-c", hook],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                text=True,
                start_new_session=True,
            )
            try:
                _, stderr = proc.communicate(json.dumps({**event, **session}), timeout=HOOK_TIMEOUT)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.communicate()
                return _fail([f"{name}: {hook!r} did not exit within {HOOK_TIMEOUT}s"])
            if proc.returncode != 0:
                return _fail([f"{name}: {hook!r} exited {proc.returncode}: {stderr.strip()}"])

    logs = Path.home() / ".arize" / "harness" / "logs"
    for log in sorted(p for p in logs.rglob("*") if p.is_file()) if logs.is_dir() else []:
        if SPAN_LINE in log.read_text(errors="ignore"):
            print(f"span logged in {log}")
            return 0
    return _fail([f"no {SPAN_LINE!r} line in a log under {logs}"])


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
    if cmd == "claude-turn" and len(args) == 1:
        return claude_turn(args[0])
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
