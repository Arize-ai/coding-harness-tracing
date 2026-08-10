#!/usr/bin/env python3
"""Tests for install.bat, the Windows router.

These are text assertions, not execution: CI and most contributors are on Linux
or macOS, so `cmd` is unavailable. That makes them weaker than the install.sh
tests — nothing here proves the batch actually runs. What they do prove is
*parity*: the two routers accept the same flags and take the same decisions, and
Windows is where a missing flag goes unnoticed longest, because nobody runs it by
accident.

install.bat previously lacked --wheel-dir entirely, so the offline install that
`npx evals` bundles could not work on Windows at all while looking supported.
"""

from __future__ import annotations

import os
import re

import pytest

INSTALL_BAT = os.path.join(os.path.dirname(__file__), "..", "..", "install.bat")
INSTALL_SH = os.path.join(os.path.dirname(__file__), "..", "..", "install.sh")


def _bat() -> str:
    with open(INSTALL_BAT) as f:
        return f.read()


def _sh() -> str:
    with open(INSTALL_SH) as f:
        return f.read()


class TestFlagParity:
    """Every flag the shell router takes, the batch router must take too."""

    @pytest.mark.parametrize(
        "flag",
        ["--with-skills", "--non-interactive", "-y", "--json", "--branch", "--wheel-dir"],
    )
    def test_flag_accepted(self, flag):
        assert f'"%~1"=="{flag}"' in _bat(), f"install.bat does not parse {flag}"

    @pytest.mark.parametrize(
        "flag",
        ["--with-skills", "--non-interactive", "--json", "--branch", "--wheel-dir"],
    )
    def test_flag_documented(self, flag):
        usage = _bat().split(":usage", 1)[1]
        assert flag in usage, f"{flag} missing from install.bat usage"

    def test_env_var_aliases_match_the_shell_router(self):
        for var in ("ARIZE_WHEEL_DIR", "ARIZE_INSTALL_BRANCH", "ARIZE_NONINTERACTIVE"):
            assert var in _bat(), f"{var} not honoured by install.bat"
            assert var in _sh(), f"{var} not honoured by install.sh"

    def test_every_harness_in_the_shell_dispatch_is_also_here(self):
        """A harness added to one router must be added to the other."""
        dispatch = re.search(r"^        (claude\|[a-z|]+)\)$", _sh(), re.MULTILINE)
        assert dispatch, "could not find the harness dispatch line in install.sh"
        for harness in dispatch.group(1).split("|"):
            assert (
                f" {harness} " in _bat() or f"({harness} " in _bat() or f'"{harness}"' in _bat()
            ), f"install.bat does not know the {harness} harness"


class TestWheelMode:
    """--wheel-dir must make the same decisions as the shell router."""

    def test_pip_uses_no_index(self):
        """Otherwise a missing wheel quietly reaches PyPI, defeating the point."""
        assert '--no-index --find-links "%WHEEL_DIR%"' in _bat()

    def test_validates_the_directory_and_the_wheel(self):
        bat = _bat()
        assert "needs a directory" in bat
        assert "No coding_harness_tracing-*.whl" in bat

    def test_bootstrap_skips_the_download(self):
        """Wheel mode fetches nothing: that is the whole feature."""
        bootstrap = _bat().split(":bootstrap_repo", 1)[1].split(":download_tarball", 1)[0]
        assert "if defined WHEEL_DIR" in bootstrap
        assert "goto :eof" in bootstrap

    def test_places_itself_for_later_commands(self):
        """status, update and uninstall are documented as running from
        %INSTALL_DIR%\\install.bat, which repo mode gets via the extract. Without
        this copy the install works and then cannot be verified or removed."""
        bat = _bat()
        assert 'copy /y "%~f0" "%INSTALL_DIR%\\install.bat"' in bat
        # ...but not over itself, which would truncate the running script.
        assert 'if /i not "%~f0"=="%INSTALL_DIR%\\install.bat"' in bat

    def test_runs_install_py_as_a_module_without_a_source_tree(self):
        """A wheel install has no install.py on disk; the module is the same code."""
        bat = _bat()
        assert ":run_harness_py" in bat
        assert '"%VENV_PYTHON%" -m "!_MOD!"' in bat
        assert 'set "_MOD=!HARNESS_DIR:\\=.!.install"' in bat

    def test_update_refuses_without_a_source_tree(self):
        """Rather than quietly converting an offline install to a network one."""
        assert "no source tree to update" in _bat()

    def test_no_stale_install_py_path_checks(self):
        """Each was a guard that skipped the harness when the file was absent —
        which is always, in wheel mode."""
        assert "install.py not found" not in _bat()


class TestSelfDeletingUninstall:
    """A full uninstall removes the directory install.bat is running from.

    cmd reads a batch file from disk as it executes, so once that directory is
    gone there is no next line to read: the uninstall completed but reported
    "The system cannot find the path specified" and a non-zero exit. Keeping the
    removal, the final message and the exit on one already-parsed line avoids it.

    install.sh has no equivalent problem — unlinking a running script on Unix
    leaves the open inode readable — which is why this only showed up once the
    batch was executed on windows-latest.
    """

    def test_removal_and_exit_share_one_line(self):
        # Scoped to :cmd_uninstall — update and bootstrap_repo also rmdir the
        # install dir, but neither is running from inside it at the time.
        section = _bat().split(":cmd_uninstall", 1)[1].split("REM ===", 1)[0]
        lines = [ln for ln in section.splitlines() if 'rmdir /s /q "%INSTALL_DIR%"' in ln]
        assert lines, "full uninstall no longer removes the install dir"
        assert len(lines) == 1, f"expected one removal in cmd_uninstall, found {len(lines)}"
        assert "exit /b 0" in lines[0], "the exit must be on the same line as the removal"
