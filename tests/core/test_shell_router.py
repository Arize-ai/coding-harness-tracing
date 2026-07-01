"""Tests for the rewritten install.sh shell router.

Validates the thin shell router structure, dispatch logic, and smoke-test
behaviors specified in the task: help, no-args, and bogus-command.
"""

from __future__ import annotations

import os
import re
import subprocess

import pytest

INSTALL_SH = os.path.join(os.path.dirname(__file__), "..", "..", "install.sh")


def _read_install_sh() -> str:
    with open(INSTALL_SH) as f:
        return f.read()


# ---------------------------------------------------------------------------
# Syntax & structure tests
# ---------------------------------------------------------------------------


class TestShellSyntax:
    """Verify the script is syntactically valid bash."""

    def test_bash_syntax_check(self):
        """bash -n parses the file without errors."""
        result = subprocess.run(
            ["bash", "-n", INSTALL_SH],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, f"Syntax error:\n{result.stderr}"

    def test_starts_with_shebang(self):
        text = _read_install_sh()
        assert text.startswith("#!/bin/bash"), "Missing bash shebang"

    def test_set_euo_pipefail(self):
        text = _read_install_sh()
        assert "set -euo pipefail" in text, "Missing strict mode"

    def test_line_count_under_400(self):
        """Router should be ~200-330 lines, well under the old 1919."""
        text = _read_install_sh()
        lines = text.strip().splitlines()
        assert len(lines) <= 400, f"install.sh has {len(lines)} lines — should be under 400"


# ---------------------------------------------------------------------------
# Function definition tests
# ---------------------------------------------------------------------------


class TestFunctionsDefined:
    """Verify that all required shell functions exist."""

    @pytest.fixture(autouse=True)
    def _load(self):
        self.text = _read_install_sh()

    @pytest.mark.parametrize(
        "func",
        [
            "info",
            "warn",
            "err",
            "header",
            "command_exists",
            "tty_input",
            "tty_read_masked_line",
            "find_python",
            "venv_python",
            "venv_pip",
            "git_sync_harness_repo",
            "install_repo_tarball",
            "install_repo",
            "setup_venv",
            "harness_dir",
            "usage",
            "main",
        ],
    )
    def test_function_defined(self, func):
        # Match "funcname() {" or "funcname ()" patterns
        pattern = rf"^{func}\s*\(\)"
        assert re.search(pattern, self.text, re.MULTILINE), f"Function {func}() not defined in install.sh"

    def test_no_old_setup_functions(self):
        """Old monolith functions should be removed."""
        for old_func in [
            "setup_claude",
            "setup_cursor",
            "setup_codex",
            "setup_copilot",
            "setup_shared_runtime",
            "do_uninstall",
            "update_install",
            "write_config",
            "collect_backend_credentials",
            "install_skills",
        ]:
            pattern = rf"^{old_func}\s*\(\)"
            assert not re.search(
                pattern, self.text, re.MULTILINE
            ), f"Old function {old_func}() should be removed from the router"


# ---------------------------------------------------------------------------
# Harness name mapping tests
# ---------------------------------------------------------------------------


class TestHarnessMapping:
    """Verify the harness_dir case statement maps correctly."""

    @pytest.fixture(autouse=True)
    def _load(self):
        self.text = _read_install_sh()

    def test_claude_maps_to_tracing_claude_code(self):
        assert 'claude)  echo "tracing/claude_code"' in self.text

    def test_codex_maps_to_tracing_codex(self):
        assert 'codex)   echo "tracing/codex"' in self.text

    def test_copilot_maps_to_tracing_copilot(self):
        assert 'copilot) echo "tracing/copilot"' in self.text

    def test_cursor_maps_to_tracing_cursor(self):
        assert 'cursor)  echo "tracing/cursor"' in self.text

    def test_opencode_maps_to_tracing_opencode(self):
        assert "opencode)" in self.text and '"tracing/opencode"' in self.text


# ---------------------------------------------------------------------------
# Usage output tests
# ---------------------------------------------------------------------------


class TestUsageOutput:
    """Verify the usage() function includes all required content."""

    @pytest.fixture(autouse=True)
    def _load(self):
        self.text = _read_install_sh()

    def test_title(self):
        assert "Arize Coding Harness Tracing Installer" in self.text

    @pytest.mark.parametrize(
        "cmd",
        ["claude", "codex", "copilot", "cursor", "opencode", "update", "uninstall"],
    )
    def test_command_listed(self, cmd):
        assert cmd in self.text

    def test_with_skills_flag(self):
        assert "--with-skills" in self.text

    def test_branch_flag(self):
        assert "--branch NAME" in self.text


# ---------------------------------------------------------------------------
# Smoke tests (subprocess execution)
# ---------------------------------------------------------------------------


class TestSmokeTests:
    """Run the actual script with safe arguments."""

    def _run(self, *args: str, env_extra: dict | None = None) -> subprocess.CompletedProcess:
        env = {**os.environ, "NO_COLOR": "1"}
        if env_extra:
            env.update(env_extra)
        return subprocess.run(
            ["bash", INSTALL_SH, *args],
            capture_output=True,
            text=True,
            timeout=10,
            env=env,
        )

    def test_help_exits_zero(self):
        result = self._run("--help")
        assert result.returncode == 0
        assert "Arize Coding Harness Tracing Installer" in result.stdout

    def test_help_flag_h(self):
        result = self._run("-h")
        assert result.returncode == 0
        assert "Usage:" in result.stdout

    def test_help_word(self):
        result = self._run("help")
        assert result.returncode == 0

    def test_no_args_exits_nonzero(self):
        result = self._run()
        assert result.returncode != 0
        assert "Usage:" in result.stdout

    def test_bogus_command_exits_nonzero(self):
        result = self._run("bogus")
        assert result.returncode != 0
        assert "Unknown command" in result.stderr

    def test_uninstall_bogus_harness_exits_nonzero(self):
        """uninstall <invalid> should fail."""
        result = self._run("uninstall", "invalid-harness")
        assert result.returncode != 0

    def test_update_without_install_fails(self):
        """update should fail if no venv exists at ~/.arize/harness/venv."""
        # Use a fake HOME so we don't touch real install
        result = self._run("update", env_extra={"HOME": "/tmp/arize-test-nonexistent"})
        assert result.returncode != 0


# ---------------------------------------------------------------------------
# Dispatch logic tests
# ---------------------------------------------------------------------------


class TestDispatchLogic:
    """Verify that the main() case statement dispatches correctly."""

    @pytest.fixture(autouse=True)
    def _load(self):
        self.text = _read_install_sh()

    def test_dispatches_harness_commands(self):
        """claude|codex|copilot|cursor|gemini|kiro (+ any later additions) should be dispatched."""
        # Match the alternation prefix and any tail of extra harnesses (e.g. ``|antigravity|opencode``).
        assert re.search(
            r"claude\|codex\|copilot\|cursor\|gemini\|kiro(\)|\|[a-z|]+\))", self.text
        ), "main() case dispatch must include the core six harnesses in order"

    def test_install_harness_called(self):
        """install_harness function should be called for harness commands."""
        assert 'install_harness "$cmd"' in self.text

    def test_install_harness_defined(self):
        """install_harness must be defined if it's called."""
        # This is a critical check: the function is called but must exist
        calls = re.findall(r"install_harness\b", self.text)
        definitions = re.findall(r"^install_harness\s*\(\)", self.text, re.MULTILINE)
        if calls:
            assert len(definitions) > 0, (
                "install_harness is called but never defined — "
                "this will cause claude/codex/copilot/cursor commands to fail"
            )

    def test_uninstall_dispatches_to_python(self):
        """Uninstall with harness should dispatch to <dir>/install.py uninstall."""
        # The actual line is: "$vp" "${INSTALL_DIR}/${dir}/install.py" uninstall
        assert "install.py" in self.text and "uninstall" in self.text

    def test_full_uninstall_dispatches_to_wipe(self):
        """Uninstall without harness should call core.setup.wipe."""
        assert "core.setup.wipe" in self.text

    def test_full_uninstall_runs_per_harness_uninstall_before_wipe(self):
        """Full uninstall must iterate installed harnesses and call each
        harness's install.py uninstall before the shared-runtime wipe.

        Regression guard: wipe.py intentionally does NOT touch
        ~/.claude/settings.json, ~/.cursor/hooks.json, ~/.codex/config.toml,
        or .github/hooks/*. Callers must run each harness uninstall first to
        clean those external registrations. install.bat does this; install.sh
        previously omitted it, leaving orphaned hook entries after full
        uninstall.
        """
        # Extract the full-uninstall branch (the `else` clause after
        # `if [[ -n "$subcmd" ]]`). It must list harnesses and invoke
        # each harness install.py with uninstall BEFORE running wipe.
        text = self.text
        wipe_idx = text.find('"$vp" -m core.setup.wipe')
        assert wipe_idx >= 0, "wipe call not found"

        # The list_installed_harnesses invocation must appear before the
        # wipe call, and an install.py uninstall dispatch must appear
        # between them.
        pre_wipe = text[:wipe_idx]
        assert "list_installed_harnesses" in pre_wipe, "Full uninstall does not iterate installed harnesses before wipe"
        assert (
            'install.py" uninstall' in pre_wipe
        ), "Full uninstall does not invoke per-harness install.py uninstall before wipe"

    def test_update_calls_pip_install(self):
        assert "pip" in self.text and "install" in self.text

    def test_update_lists_installed_harnesses(self):
        assert "list_installed_harnesses" in self.text


# ---------------------------------------------------------------------------
# Flag parsing tests
# ---------------------------------------------------------------------------


class TestFlagParsing:
    """Verify flag parsing in main()."""

    @pytest.fixture(autouse=True)
    def _load(self):
        self.text = _read_install_sh()

    def test_with_skills_flag_parsed(self):
        assert "--with-skills)" in self.text
        assert "with_skills=true" in self.text

    def test_branch_flag_parsed(self):
        assert "--branch)" in self.text
        assert "INSTALL_BRANCH=" in self.text

    def test_env_var_default_branch(self):
        assert "ARIZE_INSTALL_BRANCH" in self.text


# ---------------------------------------------------------------------------
# Constants tests
# ---------------------------------------------------------------------------


class TestConstants:
    """Verify the script declares expected constants."""

    @pytest.fixture(autouse=True)
    def _load(self):
        self.text = _read_install_sh()

    def test_repo_url(self):
        assert "https://github.com/Arize-ai/coding-harness-tracing.git" in self.text

    def test_install_dir(self):
        assert "${HOME}/.arize/harness" in self.text

    def test_venv_dir(self):
        assert "${INSTALL_DIR}/venv" in self.text

    def test_tarball_url(self):
        assert "archive/refs/heads/" in self.text
