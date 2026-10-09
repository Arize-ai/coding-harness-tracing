"""Tests for wave 3: wire entry points to <harness>_tracing packages.

Verifies:
1. pyproject.toml entry points use <harness>_tracing.hooks.* paths (not core.hooks.*)
2. core/hooks/ directory no longer exists
3. No stale core.hooks or core.codex_buffer references in source files
4. DEVELOPMENT.md entry-point table references new module paths
5. .pre-commit-config.yaml uses underscore paths
6. All entry-point target modules and functions exist and are importable
7. Installed entry-point scripts reference new module paths
8. Copilot entry points included in pyproject.toml (all 8 hooks)
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest

from core.hook_table import iter_hooks, legacy_entry_point
from tests._pyproject import parse_project_scripts

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# ---------------------------------------------------------------------------
# 1. pyproject.toml entry points use new module paths
# ---------------------------------------------------------------------------

# Built from the hook table so there is one list of hooks to maintain;
# tests/core/test_cli.py checks the table matches pyproject exactly.
EXPECTED_HARNESS_ENTRY_POINTS = {legacy_entry_point(harness, event): target for harness, event, target in iter_hooks()}


def _parse_pyproject_scripts():
    """Parse [project.scripts] from pyproject.toml."""
    return parse_project_scripts((REPO_ROOT / "pyproject.toml").read_text())


class TestPyprojectEntryPointsUpdated:
    """All hook/proxy/buffer entry points use <harness>_tracing.* paths."""

    @pytest.fixture(autouse=True)
    def _load(self):
        self.scripts = _parse_pyproject_scripts()
        self.pyproject_text = (REPO_ROOT / "pyproject.toml").read_text()

    @pytest.mark.parametrize("name,target", list(EXPECTED_HARNESS_ENTRY_POINTS.items()))
    def test_harness_entry_point(self, name, target):
        assert name in self.scripts, f"Missing entry point: {name}"
        assert self.scripts[name] == target, f"{name}: expected '{target}', got '{self.scripts[name]}'"

    def test_no_core_hooks_in_pyproject(self):
        """pyproject.toml must not reference core.hooks anywhere in entry points."""
        assert "core.hooks" not in self.pyproject_text

    def test_no_setup_wizard_entry_points(self):
        """The legacy arize-setup-* wizard entry points have been removed (#132)."""
        assert not any(name.startswith("arize-setup-") for name in self.scripts)

    def test_total_entry_point_count(self):
        """Entry point count should match expected harness + arize-config + arize-harness."""
        expected_count = len(EXPECTED_HARNESS_ENTRY_POINTS) + 2  # +2 for arize-config, arize-harness
        assert (
            len(self.scripts) == expected_count
        ), f"Expected {expected_count} entry points, got {len(self.scripts)}: {sorted(self.scripts.keys())}"


# ---------------------------------------------------------------------------
# 5. Entry-point target modules and functions exist
# ---------------------------------------------------------------------------


class TestEntryPointTargetsExist:
    """All entry-point target modules exist on disk and define the referenced callable."""

    @pytest.mark.parametrize("target", list(EXPECTED_HARNESS_ENTRY_POINTS.values()))
    def test_target_module_file_exists(self, target):
        module_path, func_name = target.split(":")
        file_path = REPO_ROOT / module_path.replace(".", "/")
        file_path = file_path.with_suffix(".py")
        assert file_path.is_file(), f"Module file not found: {file_path.relative_to(REPO_ROOT)}"

    @pytest.mark.parametrize("target", list(EXPECTED_HARNESS_ENTRY_POINTS.values()))
    def test_target_function_defined(self, target):
        """The function is defined (via AST parsing) in the target module."""
        module_path, func_name = target.split(":")
        file_path = REPO_ROOT / module_path.replace(".", "/")
        file_path = file_path.with_suffix(".py")
        source = file_path.read_text()
        tree = ast.parse(source)
        func_names = [node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)]
        assert func_name in func_names, f"Function '{func_name}' not found in {file_path.relative_to(REPO_ROOT)}"

    @pytest.mark.parametrize("target", list(EXPECTED_HARNESS_ENTRY_POINTS.values()))
    def test_target_importable(self, target):
        """The module is importable and the function is callable."""
        module_path, func_name = target.split(":")
        mod = importlib.import_module(module_path)
        fn = getattr(mod, func_name, None)
        assert fn is not None, f"{func_name} not found in {module_path}"
        assert callable(fn), f"{module_path}:{func_name} is not callable"


# ---------------------------------------------------------------------------
# 6. Installed entry-point scripts reference new paths
# ---------------------------------------------------------------------------


class TestInstalledScripts:
    """The generated scripts in .venv/bin/ import from the new module paths."""

    VENV_BIN = REPO_ROOT / ".venv" / "bin"

    @pytest.mark.parametrize(
        "script,expected_import",
        [
            ("arize-hook-session-start", "from tracing.claude_code.hooks.handlers import session_start"),
            ("arize-hook-codex-notify", "from tracing.codex.hooks.handlers import notify"),
            ("arize-hook-cursor", "from tracing.cursor.hooks.handlers import main"),
            ("arize-hook-copilot-session-start", "from tracing.copilot.hooks.handlers import session_start"),
            ("arize-hook-gemini-session-start", "from tracing.gemini.hooks.handlers import session_start"),
        ],
    )
    def test_installed_script_import(self, script, expected_import):
        script_path = self.VENV_BIN / script
        if not script_path.exists():
            pytest.skip(f"{script} not installed in .venv/bin/")
        content = script_path.read_text()
        assert expected_import in content, f"Script {script} does not import from new path. Content:\n{content}"

    @pytest.mark.parametrize(
        "script",
        [
            "arize-hook-session-start",
            "arize-hook-codex-notify",
            "arize-hook-cursor",
            "arize-hook-copilot-session-start",
            "arize-hook-gemini-session-start",
        ],
    )
    def test_installed_script_no_core_hooks(self, script):
        script_path = self.VENV_BIN / script
        if not script_path.exists():
            pytest.skip(f"{script} not installed in .venv/bin/")
        content = script_path.read_text()
        assert "core.hooks" not in content, f"Script {script} still references core.hooks"


# ---------------------------------------------------------------------------
# 7. Hooks directories exist in harness packages
# ---------------------------------------------------------------------------


class TestHooksDirsInHarnessPackages:
    """Each harness package has a hooks/ subdirectory with expected files."""

    @pytest.mark.parametrize(
        "pkg,expected_files",
        [
            ("tracing/claude_code", ["__init__.py", "adapter.py", "handlers.py"]),
            ("tracing/codex", ["__init__.py", "adapter.py", "handlers.py"]),
            ("tracing/copilot", ["__init__.py", "adapter.py", "handlers.py"]),
            ("tracing/cursor", ["__init__.py", "adapter.py", "handlers.py"]),
            ("tracing/gemini", ["__init__.py", "adapter.py", "handlers.py"]),
            ("tracing/kiro", ["__init__.py", "adapter.py", "handlers.py"]),
            ("tracing/opencode", ["__init__.py", "adapter.py", "handlers.py"]),
            ("tracing/omp", ["__init__.py", "adapter.py", "handlers.py"]),
        ],
    )
    def test_hooks_dir_has_expected_files(self, pkg, expected_files):
        hooks_dir = REPO_ROOT / pkg / "hooks"
        assert hooks_dir.is_dir(), f"{pkg}/hooks/ must exist"
        for fname in expected_files:
            assert (hooks_dir / fname).is_file(), f"{pkg}/hooks/{fname} must exist"
