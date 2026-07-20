#!/usr/bin/env python3
"""End-to-end packaging tests for the Cursor marketplace plugin.

Validates that the manifests, hook configuration, packaging metadata, core
symlink, and bootstrap script all cohere as described in the design spec
(`docs/superpowers/specs/2026-06-08-cursor-marketplace-plugin-design.md`).
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover - fallback for older Python
    tomllib = None  # type: ignore[assignment]

REPO_ROOT = Path(__file__).parent.parent.parent.parent

# Sanity-check the path traversal — this file lives at
# tests/tracing/cursor/test_cursor_plugin.py, so four parents up is the repo
# root.
assert (REPO_ROOT / "pyproject.toml").exists(), f"REPO_ROOT misresolved: {REPO_ROOT} has no pyproject.toml"


PLUGIN_DIR = REPO_ROOT / "tracing" / "cursor"
MARKETPLACE_JSON = REPO_ROOT / ".cursor-plugin" / "marketplace.json"
PLUGIN_JSON = PLUGIN_DIR / ".cursor-plugin" / "plugin.json"
HOOKS_JSON = PLUGIN_DIR / "hooks" / "hooks.json"
PYPROJECT = PLUGIN_DIR / "pyproject.toml"
CORE_SYMLINK = PLUGIN_DIR / "core"
RUN_HOOK = PLUGIN_DIR / "scripts" / "run-hook"


# --- marketplace.json ---


class TestMarketplaceJson:
    """Registry entry at .cursor-plugin/marketplace.json."""

    @pytest.fixture
    def data(self):
        with open(MARKETPLACE_JSON) as f:
            return json.load(f)

    def test_is_valid_json(self, data):
        assert isinstance(data, dict)

    def test_has_required_fields(self, data):
        assert "name" in data
        assert "plugins" in data
        assert isinstance(data["plugins"], list)
        assert data["plugins"], "plugins[] must not be empty"

    def test_first_plugin_source(self, data):
        assert data["plugins"][0]["source"] == "./tracing/cursor"

    def test_first_plugin_name(self, data):
        assert data["plugins"][0]["name"] == "cursor-tracing"


# --- plugin.json ---


class TestPluginJson:
    """Plugin manifest at tracing/cursor/.cursor-plugin/plugin.json."""

    @pytest.fixture
    def data(self):
        with open(PLUGIN_JSON) as f:
            return json.load(f)

    def test_is_valid_json(self, data):
        assert isinstance(data, dict)

    def test_has_required_fields(self, data):
        assert "name" in data
        assert "description" in data
        assert "version" in data

    def test_name_matches_distribution(self, data):
        assert data["name"] == "cursor-tracing"

    def test_hooks_path(self, data):
        assert data["hooks"] == "hooks/hooks.json"

    def test_hooks_file_referenced_by_manifest_exists(self, data):
        hooks_path = (PLUGIN_DIR / data["hooks"]).resolve()
        assert hooks_path.exists(), f"hooks file from plugin.json missing: {hooks_path}"


# --- hooks.json ---


class TestHooksJson:
    """Cursor hook map at tracing/cursor/hooks/hooks.json."""

    @pytest.fixture
    def data(self):
        with open(HOOKS_JSON) as f:
            return json.load(f)

    def test_is_valid_json(self, data):
        assert isinstance(data, dict)
        assert "hooks" in data

    def test_event_keys_match_constants(self, data):
        from tracing.cursor.constants import HOOK_EVENTS

        assert set(data["hooks"].keys()) == set(HOOK_EVENTS)

    def _iter_commands(self, data):
        for event, hook_list in data["hooks"].items():
            for entry in hook_list:
                yield event, entry["command"]

    def test_every_command_uses_run_hook(self, data):
        # Commands must be anchored to the plugin install dir via
        # ${CURSOR_PLUGIN_ROOT}: Cursor runs plugin hook commands from the
        # opened *project* folder, not the plugin dir, so a relative
        # "./scripts/run-hook" resolves to <project>/scripts/run-hook and
        # silently no-ops. The script still self-resolves PLUGIN_ROOT from $0
        # as a fallback once it is found.
        expected = "${CURSOR_PLUGIN_ROOT}/scripts/run-hook"
        for event, cmd in self._iter_commands(data):
            assert cmd == expected, f"{event}: expected {expected!r}, got {cmd!r}"

    def test_no_forbidden_tokens_in_commands(self, data):
        for event, cmd in self._iter_commands(data):
            assert ".sh" not in cmd, f"{event}: command contains '.sh': {cmd}"
            assert "bash" not in cmd, f"{event}: command contains 'bash': {cmd}"
            assert "~/.arize" not in cmd, f"{event}: command contains '~/.arize': {cmd}"


# --- pyproject.toml ---


class TestPyproject:
    """Plugin-local pyproject at tracing/cursor/pyproject.toml."""

    def test_is_real_file_not_symlink(self):
        assert PYPROJECT.exists()
        assert not PYPROJECT.is_symlink(), (
            "tracing/cursor/pyproject.toml must be a real file, not a symlink — "
            "a symlinked pyproject materializes to repo-root content whose "
            "package layout references source outside the plugin root, "
            "breaking marketplace installs."
        )

    @pytest.fixture
    def parsed(self):
        if tomllib is None:
            pytest.skip("tomllib not available (Python < 3.11)")
        with open(PYPROJECT, "rb") as f:
            return tomllib.load(f)

    def test_distribution_name_is_distinct(self, parsed):
        # Must differ from repo-root dist name to avoid two distributions
        # owning the same files in one venv (silent overwrite).
        assert parsed["project"]["name"] == "cursor-tracing"

    def test_entry_point_for_cursor(self, parsed):
        scripts = parsed["project"]["scripts"]
        assert scripts["arize-hook-cursor"] == "tracing.cursor.hooks.handlers:main"

    def test_setuptools_packages_exact_set(self, parsed):
        packages = set(parsed["tool"]["setuptools"]["packages"])
        # Exact set: guards against the #41 vscode_bridge trap where listing
        # a core.* dir that only has gitignored files breaks marketplace
        # installs.
        assert packages == {
            "tracing.cursor",
            "tracing.cursor.hooks",
            "core",
            "core.setup",
        }


# --- core symlink ---


class TestCoreSymlink:
    """The tracing/cursor/core path must be a symlink to ../../core."""

    def test_is_symlink(self):
        assert CORE_SYMLINK.is_symlink(), f"{CORE_SYMLINK} must be a symlink to ../../core"

    def test_symlink_target(self):
        target = os.readlink(CORE_SYMLINK)
        assert target == "../../core", f"unexpected symlink target: {target!r}"

    def test_symlink_resolves(self):
        assert (REPO_ROOT / "tracing/cursor/core/common.py").exists()


# --- run-hook script ---


class TestRunHook:
    """Bootstrap script at tracing/cursor/scripts/run-hook."""

    def test_exists(self):
        assert RUN_HOOK.is_file()

    def test_is_executable(self):
        assert os.access(RUN_HOOK, os.X_OK)

    def test_has_sh_shebang(self):
        first_line = RUN_HOOK.read_text().splitlines()[0]
        assert first_line.startswith("#!/bin/sh"), f"expected POSIX sh shebang, got: {first_line!r}"

    def test_references_cursor_entry_point(self):
        assert "arize-hook-cursor" in RUN_HOOK.read_text()

    def test_uses_dedicated_venv_path(self):
        # Must be plugin-dedicated, NOT the shared bare harness/venv used by
        # install.sh — otherwise a marketplace install would clobber an
        # install.sh-managed venv (and vice versa).
        text = RUN_HOOK.read_text()
        assert "cursor-plugin-venv" in text

    def test_resolves_plugin_root_from_script(self):
        # Cursor exposes no plugin-root env var; the script must derive its
        # own location.
        assert "dirname" in RUN_HOOK.read_text()

    def test_does_not_reference_claude_plugin_vars(self):
        text = RUN_HOOK.read_text()
        assert "CLAUDE_PLUGIN_ROOT" not in text
        assert "CLAUDE_PLUGIN_DATA" not in text

    def test_sh_syntax_check_passes(self):
        if not shutil.which("sh"):
            pytest.skip("sh not available")
        result = subprocess.run(
            ["sh", "-n", str(RUN_HOOK)],
            capture_output=True,
        )
        assert result.returncode == 0, f"sh -n syntax check failed: {result.stderr.decode(errors='replace')}"

    def test_fails_open_with_empty_stdout_when_bootstrap_fails(self):
        """If no Python is available, run-hook must exit 0 with empty stdout.

        Cursor interprets stdout as control JSON, so a tracing hook must
        never emit anything there even when its bootstrap can't proceed.
        """
        if not shutil.which("sh"):
            pytest.skip("sh not available")

        with tempfile.TemporaryDirectory() as tmp:
            # PATH set so no python/python3/py can be found; HOME redirected
            # so any cached venv from a prior run isn't picked up.
            env = {
                "HOME": tmp,
                "PATH": tmp,  # empty dir → no python on PATH
            }
            result = subprocess.run(
                [str(RUN_HOOK)],
                env=env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=30,
            )
        assert result.returncode == 0, (
            f"run-hook must fail open (exit 0); got {result.returncode}. "
            f"stderr: {result.stderr.decode(errors='replace')}"
        )
        assert result.stdout == b"", f"run-hook must write nothing to stdout; got {result.stdout!r}"


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-v"]))
