#!/usr/bin/env python3
"""Full teardown: remove ~/.arize/harness (config, state, logs, the files setup wrote, and old installs)."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import List

import core.setup as _setup
from core.runtime import is_binary
from core.setup import dry_run, info


def _norm(path: Path) -> Path:
    try:
        return path.resolve()
    except OSError:
        return path.absolute()


def _running_package() -> List[Path]:
    """Where the running ``arize-harness`` lives: the binary, or the environment holding the package."""
    import core

    if is_binary():
        # sys.executable is the binary's real path; sys.argv[0] is only the
        # name it was started by, a bare word when it was found on PATH.
        return [_norm(Path(sys.executable))]
    return [_norm(Path(sys.prefix)), _norm(Path(core.__file__).parent)]


def _contains(root: Path, path: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


def wipe_shared_runtime(keep_running_package: bool = False) -> None:
    """Remove ~/.arize/harness.

    With ``keep_running_package`` (``arize-harness uninstall --all``), anything
    under it that holds the running package (``venv/`` when run from the old
    install.sh / install.bat venv, or a binary kept there) stays, and only the
    rest goes. install.sh and install.bat call it without, as their full
    uninstall removes the venv too.

    Respects ARIZE_DRY_RUN. Does NOT touch harness-specific config files
    (~/.claude/settings.json, .github/hooks/*, ~/.codex/config.toml,
    ~/.cursor/hooks.json) — those belong to per-harness uninstall.
    """
    install_dir = _setup.INSTALL_DIR

    if not install_dir.exists():
        info(f"{install_dir} does not exist, nothing to wipe")
        return

    root = _norm(install_dir)
    protected = [p for p in _running_package() if _contains(root, p)] if keep_running_package else []
    if not protected:
        if dry_run():
            info(f"would remove {install_dir}")
            return
        shutil.rmtree(install_dir)
        info(f"removed {install_dir}")
        return

    for child in sorted(install_dir.iterdir()):
        resolved = _norm(child)
        if any(_contains(resolved, p) for p in protected):
            info(f"kept {child}: the running arize-harness is installed there")
            continue
        if dry_run():
            info(f"would remove {child}")
            continue
        _remove(child)
        info(f"removed {child}")


if __name__ == "__main__":
    wipe_shared_runtime()
