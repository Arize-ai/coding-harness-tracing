"""Tests for Copilot entry points in pyproject.toml.

Shell/batch install-flow tests that previously lived here have been removed;
equivalent coverage now lives in tests/test_install_copilot.py (Wave 2) which
tests the Python install()/uninstall() functions against a fake home.

The embedded Python JSON-generation functional tests were also removed — the
logic they tested (VS Code + CLI hooks JSON merging) now lives in
tracing.copilot/install.py and is exercised by test_install_copilot.py.
"""

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"


# ---------------------------------------------------------------------------
# pyproject.toml entry point tests
# ---------------------------------------------------------------------------


class TestCopilotEntryPoints:
    """Verify all 6 Copilot hook entry points in pyproject.toml."""

    @pytest.fixture(autouse=True)
    def _load_pyproject(self):
        self.text = PYPROJECT.read_text()

    def test_session_start_entry_point(self):
        assert 'arize-hook-copilot-session-start = "tracing.copilot.hooks.handlers:session_start"' in self.text

    def test_user_prompt_entry_point(self):
        assert 'arize-hook-copilot-user-prompt = "tracing.copilot.hooks.handlers:user_prompt_submitted"' in self.text

    def test_pre_tool_entry_point(self):
        assert 'arize-hook-copilot-pre-tool = "tracing.copilot.hooks.handlers:pre_tool_use"' in self.text

    def test_post_tool_entry_point(self):
        assert 'arize-hook-copilot-post-tool = "tracing.copilot.hooks.handlers:post_tool_use"' in self.text

    def test_stop_entry_point(self):
        assert 'arize-hook-copilot-stop = "tracing.copilot.hooks.handlers:stop"' in self.text

    def test_subagent_stop_entry_point(self):
        assert 'arize-hook-copilot-subagent-stop = "tracing.copilot.hooks.handlers:subagent_stop"' in self.text

    def test_exactly_6_hook_entry_points(self):
        """There should be exactly 6 copilot hook entry points."""
        count = self.text.count("arize-hook-copilot-")
        assert count == 6, f"Expected 6 copilot hook entries, got {count}"

    def test_entry_points_importable(self):
        """All referenced handler functions should be importable."""
        from tracing.copilot.hooks.handlers import (
            post_tool_use,
            pre_tool_use,
            session_start,
            stop,
            subagent_stop,
            user_prompt_submitted,
        )

        for fn in [
            session_start,
            user_prompt_submitted,
            pre_tool_use,
            post_tool_use,
            stop,
            subagent_stop,
        ]:
            assert callable(fn)
