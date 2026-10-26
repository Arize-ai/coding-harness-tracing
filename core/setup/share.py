"""Copy each harness's plugin, skill and data files to ~/.arize/harness/share/<harness>/.

The files are read from the installed package (wheel, checkout, tarball or
Nuitka bundle), never from a source-tree path, so ``share/`` holds the same
files whatever the install type, at a path that doesn't move on reinstall.
"""

from __future__ import annotations

import glob
import importlib
import os
import shutil
import sys
from pathlib import Path

import core
from core.runtime import is_binary
from core.setup import dry_run, err, harness_dir, info

# Files each harness ships, as globs relative to its tracing/<harness>/ package.
# Must match [tool.setuptools.package-data] in pyproject.toml. A harness that
# ships nothing (devin) is absent and gets no share/ folder.
SHARE_FILES: dict[str, list[str]] = {
    "antigravity": ["skills/**/*"],
    "claude_code": [".claude-plugin/plugin.json", "hooks/hooks.json", "scripts/run-hook", "pyproject.toml", "skills/**/*"],
    "codex": ["skills/**/*"],
    "copilot": ["skills/**/*"],
    "cursor": [".cursor-plugin/plugin.json", "hooks/hooks.json", "scripts/run-hook", "pyproject.toml", "skills/**/*"],
    "gemini": ["skills/**/*"],
    "kiro": ["skills/**/*"],
    "omp": ["plugin/arize-tracing.ts", "skills/**/*"],
    "opencode": ["plugin/arize-tracing.ts", "skills/**/*"],
}

# Plugin folders whose run-hook puts the folder on PYTHONPATH (claude) or
# pip-installs it (cursor), so they also need the Python sources. A binary
# install has none and runs the binary instead (see RUNTIME_FILE).
PLUGIN_HARNESSES = {"claude_code", "cursor"}

# Written into a plugin folder on a binary install: the binary's full path,
# which scripts/run-hook runs instead of bootstrapping Python.
RUNTIME_FILE = "runtime"


def _sub_name(harness: str) -> str:
    return harness.replace("-", "_")


def package_glob(root: Path, pattern: str) -> list[Path]:
    """Files under root matching pattern, skipping dotfiles like setuptools' package-data globs do."""
    matches = glob.glob(os.path.join(glob.escape(str(root)), pattern), recursive=True)
    return sorted(Path(m) for m in matches if os.path.isfile(m))


def share_sources(harness: str) -> dict[str, Path]:
    """Map each file's path inside share/<harness>/ to its source in the installed package.

    Raises FileNotFoundError naming the path when a listed file is missing.
    """
    sub = _sub_name(harness)
    package = Path(str(importlib.import_module(f"tracing.{sub}").__file__)).resolve().parent
    sources: dict[str, Path] = {}
    for pattern in SHARE_FILES.get(sub, []):
        matched = package_glob(package, pattern)
        if not matched:
            raise FileNotFoundError(f"missing from the installed package: {package / pattern}")
        for path in matched:
            sources[path.relative_to(package).as_posix()] = path
    if sub in PLUGIN_HARNESSES and not is_binary():
        for pattern in ("*.py", "hooks/*.py"):
            for path in package_glob(package, pattern):
                sources[path.relative_to(package).as_posix()] = path
        core_dir = Path(core.__file__).resolve().parent
        for path in package_glob(core_dir, "**/*.py"):
            sources["core/" + path.relative_to(core_dir).as_posix()] = path
    return sources


def install_share(harness: str) -> None:
    """Replace share/<harness>/ with a fresh copy of the harness's files.

    The copy is built in a temp folder beside it and renamed into place, so a
    hook never sees a half-written folder. Other harnesses' folders are left
    alone. Honors dry_run(). Exits 1 with the path when a file is missing or
    share/ can't be written.
    """
    sub = _sub_name(harness)
    if sub not in SHARE_FILES:
        return
    dest = harness_dir(harness)
    try:
        sources = share_sources(harness)
    except FileNotFoundError as exc:
        err(str(exc))
        sys.exit(1)

    if dry_run():
        info(f"would copy {len(sources)} files to {dest}")
        return

    tmp = dest.with_name(f".{sub}.tmp-{os.getpid()}")
    old = dest.with_name(f".{sub}.old-{os.getpid()}")
    try:
        shutil.rmtree(tmp, ignore_errors=True)
        for rel, src in sources.items():
            target = tmp / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, target)
        if sub in PLUGIN_HARNESSES:
            (tmp / "scripts" / "run-hook").chmod(0o755)
            if is_binary():
                (tmp / RUNTIME_FILE).write_text(sys.executable + "\n")
        if dest.exists():
            os.replace(dest, old)
        os.replace(tmp, dest)
    except OSError as exc:
        shutil.rmtree(tmp, ignore_errors=True)
        err(f"could not write {exc.filename or dest}: {exc.strerror or exc}")
        sys.exit(1)
    shutil.rmtree(old, ignore_errors=True)


def remove_share(harness: str) -> None:
    """Remove share/<harness>/. Honors dry_run(). No-op if it doesn't exist."""
    dest = harness_dir(harness)
    if not dest.exists():
        return
    if dry_run():
        info(f"would remove {dest}")
        return
    shutil.rmtree(dest, ignore_errors=True)
