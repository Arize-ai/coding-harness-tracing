"""Check that every harness config in a smoke-test HOME runs ``arize-harness hook``.

    python -I scripts/smoke_hook_commands.py <home> <cwd> [--no-venv]

Run with the installed package's interpreter. Exits 1, listing offenders, if any
config runs an old venv ``arize-hook-*`` command or none runs ``arize-harness hook``.
``--no-venv`` also fails an ``arize-harness hook`` command under the old venv.
"""

from __future__ import annotations

import json
import re
import sys
import tomllib
from pathlib import Path

from core.setup import is_arize_hook_command, is_old_venv_hook

_STRING = re.compile(r'"(?:[^"\\]|\\.)*"')
_OLD_NAME = re.compile(r"arize-hook-[a-z]")


def _values(data):
    if isinstance(data, str):
        yield data
    elif isinstance(data, list):
        if data and all(isinstance(x, str) for x in data):
            yield data
        for x in data:
            yield from _values(x)
    elif isinstance(data, dict):
        for x in data.values():
            yield from _values(x)


def _files(home: Path, cwd: Path):
    roots = [home / d for d in (".claude", ".codex", ".cursor", ".gemini", ".kiro", ".config", ".omp")]
    roots += [home / ".arize" / "harness" / "claude_plugin", cwd / ".github" / "hooks"]
    for root in roots:
        if root.is_dir():
            yield from (f for f in sorted(root.rglob("*")) if f.is_file() and f.suffix in (".json", ".toml", ".ts"))


def main(argv) -> int:
    home, cwd = Path(argv[1]), Path(argv[2])
    no_venv = "--no-venv" in argv[3:]
    venv = (home / ".arize" / "harness" / "venv").as_posix()
    ours, bad = [], []
    for path in _files(home, cwd):
        text = path.read_text(encoding="utf-8", errors="ignore")
        if path.suffix == ".toml":
            found = list(_values(tomllib.loads(text)))
        else:
            found = []
            for literal in _STRING.findall(text):
                try:
                    found.append(json.loads(literal))
                except json.JSONDecodeError:
                    pass
        if path.suffix == ".ts":
            binary = re.search(r"function binaryPath\(\): string \{\s*return (\"(?:[^\"\\\\]|\\\\.)*\")", text)
            args = re.search(r"spawn\(binaryPath\(\), (\[[^\]]*\])", text)
            if binary and args:
                found.append([json.loads(binary.group(1)), *json.loads(args.group(1))])
            if '".arize", "harness", "venv"' in text:
                bad.append((path, "plugin spawns the old venv's arize-hook-*"))
        for value in found:
            if is_arize_hook_command(value):
                ours.append((path, value))
                if no_venv and venv in json.dumps(value).replace("\\\\", "/"):
                    bad.append((path, value))
            elif is_old_venv_hook(value) or (isinstance(value, str) and "/" in value and _OLD_NAME.search(value)):
                bad.append((path, value))
    for path, value in bad:
        print(f"{path}: {value!r}", file=sys.stderr)
    if not ours:
        print("no `arize-harness hook` command in any harness config", file=sys.stderr)
    return 1 if bad or not ours else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
