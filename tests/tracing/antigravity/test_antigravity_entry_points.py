"""Tests for the antigravity entry-points wiring task.

Verifies that ``pyproject.toml`` declares the two antigravity hook scripts and
that ``install.sh`` routes the ``antigravity`` command through to the harness.
The scope of this test mirrors the kiro/shell-router pattern used elsewhere in
the suite.
"""

from __future__ import annotations

import ast
import importlib
import os
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
INSTALL_SH = REPO_ROOT / "install.sh"
PYPROJECT = REPO_ROOT / "pyproject.toml"


# ---------------------------------------------------------------------------
# pyproject.toml [project.scripts] — exact entry-point targets
# ---------------------------------------------------------------------------


EXPECTED_ANTIGRAVITY_SCRIPTS = {
    "arize-hook-antigravity-pre-invocation": "tracing.antigravity.hooks.handlers:pre_invocation",
    "arize-hook-antigravity-stop": "tracing.antigravity.hooks.handlers:stop",
}


def _parse_project_scripts(text: str) -> dict[str, str]:
    """Parse the ``[project.scripts]`` table from pyproject text without tomllib.

    ``tomllib`` is stdlib only on Python 3.11+, but this repo targets ``>=3.9``,
    so we parse the simple ``name = "value"`` table by hand. Entry-point names
    are TOML bare keys (letters, digits, ``_``, ``-``, ``.``).
    """
    scripts: dict[str, str] = {}
    in_table = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            in_table = line == "[project.scripts]"
            continue
        if not in_table:
            continue
        m = re.match(r'^([A-Za-z0-9_.\-]+)\s*=\s*"([^"]+)"', line)
        if m:
            scripts[m.group(1)] = m.group(2)
    return scripts


@pytest.fixture(scope="module")
def pyproject_scripts() -> dict[str, str]:
    return _parse_project_scripts(PYPROJECT.read_text())


class TestPyprojectAntigravityScripts:
    """The two antigravity scripts are declared and point at the right callables."""

    @pytest.mark.parametrize("name,target", list(EXPECTED_ANTIGRAVITY_SCRIPTS.items()))
    def test_script_target(self, pyproject_scripts: dict[str, str], name: str, target: str) -> None:
        assert name in pyproject_scripts, f"Missing entry point: {name}"
        assert pyproject_scripts[name] == target, f"{name}: expected '{target}', got '{pyproject_scripts[name]}'"

    @pytest.mark.parametrize("target", list(EXPECTED_ANTIGRAVITY_SCRIPTS.values()))
    def test_target_module_file_exists(self, target: str) -> None:
        module_path, _ = target.split(":")
        file_path = (REPO_ROOT / module_path.replace(".", "/")).with_suffix(".py")
        assert file_path.is_file(), f"Module file not found: {file_path.relative_to(REPO_ROOT)}"

    @pytest.mark.parametrize("target", list(EXPECTED_ANTIGRAVITY_SCRIPTS.values()))
    def test_target_function_defined(self, target: str) -> None:
        module_path, func_name = target.split(":")
        file_path = (REPO_ROOT / module_path.replace(".", "/")).with_suffix(".py")
        tree = ast.parse(file_path.read_text())
        func_names = [node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)]
        assert func_name in func_names, f"Function '{func_name}' not found in {file_path.relative_to(REPO_ROOT)}"

    @pytest.mark.parametrize("target", list(EXPECTED_ANTIGRAVITY_SCRIPTS.values()))
    def test_target_importable(self, target: str) -> None:
        module_path, func_name = target.split(":")
        mod = importlib.import_module(module_path)
        fn = getattr(mod, func_name, None)
        assert fn is not None, f"{func_name} not found in {module_path}"
        assert callable(fn), f"{module_path}:{func_name} is not callable"


class TestEventsMatchScripts:
    """EVENTS values in tracing/antigravity/constants.py must equal pyproject script keys."""

    def test_events_values_are_pyproject_keys(self, pyproject_scripts: dict[str, str]) -> None:
        from tracing.antigravity import constants as c

        script_keys = set(pyproject_scripts.keys())
        events_values = set(c.EVENTS.values())
        missing = events_values - script_keys
        assert not missing, f"EVENTS values missing from pyproject [project.scripts]: {sorted(missing)}"

    def test_events_keys_match_spec_event_names(self) -> None:
        """EVENTS must declare exactly the two Antigravity events we hook."""
        from tracing.antigravity import constants as c

        assert set(c.EVENTS) == {"PreInvocation", "Stop"}

    def test_pre_invocation_event_maps_to_pre_invocation_script(self) -> None:
        from tracing.antigravity import constants as c

        assert c.EVENTS["PreInvocation"] == "arize-hook-antigravity-pre-invocation"

    def test_stop_event_maps_to_stop_script(self) -> None:
        from tracing.antigravity import constants as c

        assert c.EVENTS["Stop"] == "arize-hook-antigravity-stop"


# ---------------------------------------------------------------------------
# install.sh — syntax + antigravity wiring
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def install_sh_text() -> str:
    return INSTALL_SH.read_text()


def test_install_sh_bash_syntax() -> None:
    """install.sh must parse cleanly under bash -n."""
    result = subprocess.run(
        ["bash", "-n", str(INSTALL_SH)],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, f"bash syntax error:\n{result.stderr}"


class TestInstallShAntigravity:
    """The harness router must know about the antigravity harness."""

    def test_harness_dir_maps_antigravity(self, install_sh_text: str) -> None:
        """harness_dir() must map ``antigravity`` -> ``tracing/antigravity``."""
        assert re.search(
            r'antigravity\)\s*echo\s*"tracing/antigravity"',
            install_sh_text,
        ), 'harness_dir() must contain an `antigravity) echo "tracing/antigravity"` clause'

    def test_harness_dir_antigravity_in_scope(self, install_sh_text: str) -> None:
        """The antigravity clause must live between an existing harness clause and the *) default."""
        # We use kiro as a stable predecessor (declared before antigravity per the task spec).
        kiro_pos = install_sh_text.find('kiro)    echo "tracing/kiro"')
        antigravity_pos = install_sh_text.find('antigravity) echo "tracing/antigravity"')
        default_pos = install_sh_text.find("*)       return 1")
        assert kiro_pos != -1, "kiro clause anchor not found"
        assert antigravity_pos != -1, "antigravity clause not found"
        assert default_pos != -1, "default *) clause not found"
        assert (
            kiro_pos < antigravity_pos < default_pos
        ), "antigravity clause must appear after kiro and before the *) default"

    def test_main_dispatch_includes_antigravity(self, install_sh_text: str) -> None:
        """The command-validation alternation must include ``antigravity``."""
        match = re.search(
            r"case\s+\"\$cmd\".*?\n\s*([a-zA-Z|]+)\)\s*\n\s*install_harness",
            install_sh_text,
            re.DOTALL,
        )
        assert match, "Could not locate command-validation alternation in main()"
        alternation = match.group(1).split("|")
        assert "antigravity" in alternation, f"main() dispatch alternation missing antigravity: {alternation}"

    def test_main_dispatch_alternation_contains_all_harnesses(self, install_sh_text: str) -> None:
        """Every harness with a directory mapping must also be valid in the dispatch alternation."""
        expected = {"claude", "codex", "copilot", "cursor", "gemini", "kiro", "antigravity"}
        match = re.search(
            r"case\s+\"\$cmd\".*?\n\s*([a-zA-Z|]+)\)\s*\n\s*install_harness",
            install_sh_text,
            re.DOTALL,
        )
        assert match
        alternation = set(match.group(1).split("|"))
        missing = expected - alternation
        assert not missing, f"main() dispatch alternation missing: {sorted(missing)}"

    def test_usage_mentions_antigravity(self, install_sh_text: str) -> None:
        """The usage() heredoc must list antigravity as a supported command."""
        # Lift the Commands section out of the heredoc.
        body = install_sh_text.split("Commands:", 1)[1].split("update", 1)[0]
        assert "antigravity" in body, "usage() Commands block must list antigravity"

    def test_usage_antigravity_description(self, install_sh_text: str) -> None:
        """The antigravity help line must describe it as the Google Antigravity CLI/IDE."""
        antigravity_lines = [
            line
            for line in install_sh_text.splitlines()
            if "antigravity" in line.lower() and "Antigravity" in line and "Install" in line
        ]
        assert antigravity_lines, "usage() must contain a line describing antigravity"


@pytest.mark.skipif(os.name == "nt", reason="bash not available on Windows")
def test_install_sh_help_output_lists_antigravity() -> None:
    """Running ``install.sh --help`` must mention antigravity."""
    result = subprocess.run(
        ["bash", str(INSTALL_SH), "--help"],
        capture_output=True,
        text=True,
        timeout=10,
        env={**os.environ, "NO_COLOR": "1"},
    )
    assert result.returncode == 0, f"--help failed: {result.stderr}"
    assert "antigravity" in result.stdout, "--help output must mention antigravity"


@pytest.mark.skipif(os.name == "nt", reason="bash not available on Windows")
def test_install_sh_antigravity_does_not_short_circuit_to_unknown_command(tmp_path: Path) -> None:
    """``./install.sh antigravity`` must be accepted by the router (i.e. not 'Unknown command').

    We can't actually run install_harness end-to-end here (it would clone the repo,
    bootstrap a venv, etc.), but we can confirm the router accepts the command word
    by pointing INSTALL_DIR/repo at an empty temp directory: the router will get
    past the dispatch and fail later for a different reason. The relevant signal
    is that ``Unknown command: antigravity`` is *not* printed.
    """
    env = {
        **os.environ,
        "NO_COLOR": "1",
        # Force the router away from a real install. We don't care if it fails
        # in setup_venv/install_repo; we only care that it didn't reject the
        # command word.
        "HOME": str(tmp_path),
        "ARIZE_INSTALL_BRANCH": "this-branch-does-not-exist-for-testing",
    }
    result = subprocess.run(
        ["bash", str(INSTALL_SH), "antigravity"],
        capture_output=True,
        text=True,
        timeout=15,
        env=env,
    )
    # We expect a non-zero exit (Python / network setup failures), but the
    # *router* should not have rejected the word "antigravity".
    assert "Unknown command: antigravity" not in result.stderr, f"Router rejected antigravity. stderr:\n{result.stderr}"


# ---------------------------------------------------------------------------
# Cross-task invariant: no token attributes are introduced in handlers
# ---------------------------------------------------------------------------


def test_handlers_do_not_set_token_count_attributes() -> None:
    """Sanity check tied to the project memory: tokens are deliberately withheld."""
    handlers_path = REPO_ROOT / "tracing" / "antigravity" / "hooks" / "handlers.py"
    source = handlers_path.read_text()
    assert (
        "llm.token_count" not in source
    ), "handlers.py must not set llm.token_count.* attributes (tokens are deliberately withheld)"
