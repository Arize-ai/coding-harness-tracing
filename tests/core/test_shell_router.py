"""Tests for the rewritten install.sh shell router.

Validates the thin shell router structure, dispatch logic, and smoke-test
behaviors specified in the task: help, no-args, and bogus-command.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

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

    def test_line_count_under_460(self):
        """Router should be ~350-450 lines, well under the old 1919.

        The cap is a ratchet against bash creeping back in: logic here is not
        unit-testable, not type-checked, and has to be mirrored in install.bat
        for Windows. Anything that can live in `core/setup/` belongs there.

        Raised from 400 when offline wheel install landed (--wheel-dir): flag
        parsing and the pip invocation both run before the venv exists, so they
        cannot move to Python. If you are raising it again, move something to
        Python first — and change this docstring, not just the number.
        """
        text = _read_install_sh()
        lines = text.strip().splitlines()
        assert len(lines) <= 460, f"install.sh has {len(lines)} lines — should be under 460"


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
        # Both spellings: "claude" is the CLI name, "claude-code" the config key
        # that list_installed_harnesses() hands back. See
        # TestConfigKeysResolveInHarnessDir for why the alias exists.
        assert 'claude|claude-code)  echo "tracing/claude_code"' in self.text

    def test_codex_maps_to_tracing_codex(self):
        assert 'codex)   echo "tracing/codex"' in self.text

    def test_copilot_maps_to_tracing_copilot(self):
        assert 'copilot) echo "tracing/copilot"' in self.text

    def test_cursor_maps_to_tracing_cursor(self):
        assert 'cursor)  echo "tracing/cursor"' in self.text

    def test_opencode_maps_to_tracing_opencode(self):
        assert "opencode)" in self.text and '"tracing/opencode"' in self.text

    def test_omp_maps_to_tracing_omp(self):
        assert "omp)" in self.text and '"tracing/omp"' in self.text


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
        ["claude", "codex", "copilot", "cursor", "opencode", "omp", "update", "uninstall"],
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
        """claude|codex|copilot|cursor|gemini|kiro|opencode|omp should be dispatched."""
        assert "claude|codex|copilot|cursor|gemini|kiro|opencode|omp)" in self.text

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
        # run_harness_py dispatches to install.py — as a file in repo mode, as a
        # module in wheel mode. Either way it must run before the wipe.
        assert (
            'run_harness_py "$key" "$vp" uninstall' in pre_wipe
        ), "Full uninstall does not invoke per-harness uninstall before wipe"

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

    def test_non_interactive_flag_parsed(self):
        assert "--non-interactive|-y)" in self.text
        assert "export ARIZE_NONINTERACTIVE=1" in self.text

    def test_json_flag_parsed(self):
        assert "--json)" in self.text
        assert 'status_args="--json"' in self.text


class TestUpdateNonInteractiveGate:
    """`update` re-registers harnesses, which prompts for each project name.

    With no terminal that used to die with an EOFError. The fallback must be
    gated on there being no terminal, so an interactive update keeps its prompts.
    """

    @pytest.fixture(autouse=True)
    def _load(self):
        self.text = _read_install_sh()

    def test_update_falls_back_when_no_terminal(self):
        assert '[[ -n "$_tty_in" ]] || export ARIZE_NONINTERACTIVE=1' in self.text

    def test_gate_lives_in_the_update_arm(self):
        """Guard against the export drifting somewhere it would always apply."""
        update_arm = self.text.split("        update)", 1)[1].split("        -h|--help|help)", 1)[0]
        assert '[[ -n "$_tty_in" ]] || export ARIZE_NONINTERACTIVE=1' in update_arm

    def test_gate_reuses_the_scripts_own_tty_detection(self):
        """_tty_in is how the rest of the script already decides if it can prompt."""
        assert '_tty_in="/dev/tty"' in self.text
        assert self.text.index('_tty_in="/dev/tty"') < self.text.index('[[ -n "$_tty_in" ]] || export')


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


class TestWheelDirParsing:
    """`--wheel-dir` installs from local wheels, fetching nothing."""

    @pytest.fixture(autouse=True)
    def _load(self):
        self.text = _read_install_sh()

    def test_flag_and_env_var_both_wired(self):
        assert "--wheel-dir)" in self.text
        assert "ARIZE_WHEEL_DIR" in self.text

    def test_pip_uses_no_index(self):
        """--no-index is what makes a missing wheel loud instead of a PyPI fetch."""
        assert '--no-index --find-links "$WHEEL_DIR" coding-harness-tracing' in self.text

    def test_documented_in_usage(self):
        assert "--wheel-dir DIR" in self.text

    def test_harness_py_falls_back_to_module(self):
        """A wheel install has no source tree, so install.py runs as a module."""
        assert 'run_with_tty "$vp" -m "${dir//\\//.}.install"' in self.text


class TestWheelDirBehaviour:
    """Run the script for real, with a throwaway HOME and no network."""

    def _run(self, *args: str, home, wheel_dir=None, timeout=90):
        env = {**os.environ, "NO_COLOR": "1", "HOME": str(home)}
        env.pop("ARIZE_WHEEL_DIR", None)
        cmd = ["bash", INSTALL_SH, *args]
        if wheel_dir is not None:
            cmd += ["--wheel-dir", str(wheel_dir)]
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env, stdin=subprocess.DEVNULL)

    def test_missing_directory_is_fatal(self, tmp_path):
        result = self._run("claude", home=tmp_path, wheel_dir=tmp_path / "nope")
        assert result.returncode != 0
        assert "needs a directory" in result.stderr

    def test_directory_without_a_wheel_is_fatal(self, tmp_path):
        """Fail before touching anything, rather than silently falling back."""
        empty = tmp_path / "wheels"
        empty.mkdir()
        result = self._run("claude", home=tmp_path, wheel_dir=empty)
        assert result.returncode != 0
        assert "No coding_harness_tracing-*.whl" in result.stderr
        assert not (tmp_path / ".arize").exists()

    def test_never_downloads_the_repo(self, tmp_path):
        """The whole point: no tarball, no clone, no source tree.

        A deliberately corrupt wheel makes pip fail, which is fine — what
        matters is that nothing was fetched on the way there.
        """
        wheels = tmp_path / "wheels"
        wheels.mkdir()
        (wheels / "coding_harness_tracing-0.1.0-py3-none-any.whl").write_bytes(b"not a wheel")

        result = self._run("claude", home=tmp_path, wheel_dir=wheels)

        assert result.returncode != 0
        combined = result.stdout + result.stderr
        assert "Downloading coding-harness-tracing tarball" not in combined
        assert "Extracted to" not in combined
        assert not (tmp_path / ".arize" / "harness" / "pyproject.toml").exists()
        assert not (tmp_path / ".arize" / "harness" / ".git").exists()

    def test_places_install_sh_for_later_commands(self, tmp_path):
        """`status`, `update` and `uninstall` are all documented as running from
        ~/.arize/harness/install.sh, which repo mode gets via the extract.
        Without this copy the install works and then cannot be verified."""
        wheels = tmp_path / "wheels"
        wheels.mkdir()
        (wheels / "coding_harness_tracing-0.1.0-py3-none-any.whl").write_bytes(b"not a wheel")

        self._run("claude", home=tmp_path, wheel_dir=wheels)

        placed = tmp_path / ".arize" / "harness" / "install.sh"
        assert placed.is_file()
        assert os.access(placed, os.X_OK)

    def test_update_refuses_without_a_source_tree(self, tmp_path):
        """Rather than quietly converting an offline install to a network one."""
        harness = tmp_path / ".arize" / "harness"
        (harness / "venv" / "bin").mkdir(parents=True)
        # A venv python that exists so update gets past its own guard.
        (harness / "venv" / "bin" / "python").symlink_to(sys.executable)
        (harness / "venv" / "bin" / "pip").symlink_to(sys.executable)

        result = self._run("update", home=tmp_path)

        assert result.returncode != 0
        assert "no source tree to update" in result.stderr


class TestConfigKeysResolveInHarnessDir:
    """Every key that can appear in config.json must resolve in harness_dir().

    `update` and full `uninstall` discover harnesses through
    list_installed_harnesses(), which yields *config keys* (each harness's
    HARNESS_NAME), then look each one up with harness_dir(), which knew only CLI
    names. Claude Code writes "claude-code" while its CLI name is "claude", so
    both loops skipped it with "Unknown harness: claude-code (skipping)".

    The consequence was not cosmetic: a full uninstall wiped the venv and left 16
    hook entries live in ~/.claude/settings.json pointing at the deleted path, so
    Claude Code then tried to exec missing binaries on every hook event. wipe.py
    deliberately does not touch settings.json — the per-harness uninstall is the
    only thing that cleans it, and it was the step being skipped.

    Discovered from the constants rather than hardcoded, so a new harness whose
    HARNESS_NAME differs from its CLI name fails here instead of in the field.
    """

    @staticmethod
    def _config_keys() -> list:
        import importlib

        repo_root = Path(INSTALL_SH).resolve().parent
        keys = []
        for constants in sorted((repo_root / "tracing").glob("*/constants.py")):
            module = importlib.import_module(f"tracing.{constants.parent.name}.constants")
            name = getattr(module, "HARNESS_NAME", None)
            if name:
                keys.append(name)
        return keys

    def test_constants_were_discovered(self):
        """Guard the guard: an empty list would make every test below vacuous."""
        keys = self._config_keys()
        assert len(keys) >= 8, f"expected every harness's HARNESS_NAME, found {keys}"
        assert "claude-code" in keys, "the regression case itself must be covered"

    def test_every_config_key_resolves(self, tmp_path):
        """Probe through `uninstall`, which is where the lookup actually happens.

        With an empty HOME the command still fails — there is no venv — but it
        fails *after* harness_dir(), so "Unknown harness" cleanly distinguishes a
        name that did not resolve from one that did.
        """
        for key in self._config_keys():
            result = subprocess.run(
                ["bash", INSTALL_SH, "uninstall", key],
                capture_output=True,
                text=True,
                timeout=30,
                env={**os.environ, "NO_COLOR": "1", "HOME": str(tmp_path)},
                stdin=subprocess.DEVNULL,
            )
            assert "Unknown harness" not in result.stderr, f"config key {key!r} does not resolve in harness_dir()"

    def test_cli_names_still_resolve(self):
        """The alias must not cost us the original spelling."""
        text = _read_install_sh()
        for cli_name in ("claude", "codex", "copilot", "cursor", "gemini", "kiro", "opencode", "omp"):
            assert re.search(rf"^\s+{cli_name}[)|]", text, re.MULTILINE), f"{cli_name} missing from harness_dir()"

    def test_install_dispatch_does_not_gain_an_alias(self):
        """`install.sh claude-code` should stay an unknown *command*.

        The alias is for resolving discovered config keys. Making it a second way
        to install the same harness would imply there are two harnesses.
        """
        assert "claude|codex|copilot|cursor|gemini|kiro|opencode|omp)" in _read_install_sh()
