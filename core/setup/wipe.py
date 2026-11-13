#!/usr/bin/env python3
"""Remove ~/.arize/harness: config, state, logs, the files setup wrote, and old installs."""

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
    """Paths of the running ``arize-harness``: the binary, or its environment and package."""
    import core

    if is_binary():
        # sys.argv[0] may be a bare name found on PATH; sys.executable is the real path.
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

    With ``keep_running_package``, anything holding the running ``arize-harness`` is kept.
    install.sh and install.bat call it without, since they remove the venv too.

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
