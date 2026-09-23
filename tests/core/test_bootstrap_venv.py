#!/usr/bin/env python3
"""Tests for the shared venv bootstrap at core/scripts/bootstrap-venv.

Both the Cursor and Claude Code plugin run-hook wrappers delegate to this one
script (reached via each plugin's core symlink). It must: keep a POSIX sh
shebang, fail open (exit 0, silent stdout) when it cannot build a venv, and
reserve stdout for the host's control protocol so a tracing hook never blocks
the host.
"""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.parent
BOOTSTRAP = REPO_ROOT / "core" / "scripts" / "bootstrap-venv"


def test_exists_and_executable():
    assert BOOTSTRAP.is_file()
    assert os.access(BOOTSTRAP, os.X_OK), "bootstrap-venv must be executable"


def test_has_sh_shebang():
    first_line = BOOTSTRAP.read_text().splitlines()[0]
    assert first_line.startswith("#!/bin/sh"), f"expected POSIX sh shebang, got: {first_line!r}"


def test_sh_syntax_check_passes():
    if not shutil.which("sh"):
        pytest.skip("sh not available")
    result = subprocess.run(["sh", "-n", str(BOOTSTRAP)], capture_output=True)
    assert result.returncode == 0, f"sh -n failed: {result.stderr.decode(errors='replace')}"


def test_missing_args_is_a_usage_error():
    """The arg guards (${1:?...}) surface a programming error loudly — this is
    not the fail-open path (that's for runtime venv failures, not bad calls)."""
    if not shutil.which("sh"):
        pytest.skip("sh not available")
    result = subprocess.run([str(BOOTSTRAP)], capture_output=True, stdin=subprocess.DEVNULL)
    assert result.returncode != 0
    assert b"usage" in result.stderr.lower()


def test_fails_open_with_empty_stdout_when_no_python():
    """With no Python on PATH, bootstrap-venv must exit 0 and write nothing to
    stdout — stdout is reserved for the host's control protocol."""
    if not shutil.which("sh"):
        pytest.skip("sh not available")
    with tempfile.TemporaryDirectory() as tmp:
        venv_dir = os.path.join(tmp, "data", "venv")
        # PATH = empty dir → no python/python3/py discoverable.
        env = {"HOME": tmp, "PATH": tmp}
        result = subprocess.run(
            [str(BOOTSTRAP), str(REPO_ROOT / "tracing" / "cursor"), venv_dir, "arize-hook-cursor"],
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=30,
        )
    assert result.returncode == 0, (
        f"must fail open (exit 0); got {result.returncode}. " f"stderr: {result.stderr.decode(errors='replace')}"
    )
    assert result.stdout == b"", f"must write nothing to stdout; got {result.stdout!r}"


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-v"]))
