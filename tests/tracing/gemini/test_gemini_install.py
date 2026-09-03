"""Tests for Gemini entry points in pyproject.toml.

Mirrors tests/test_copilot_install.py — verifies that all required
Gemini hook entry points are declared in pyproject.toml [project.scripts].
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"


# ---------------------------------------------------------------------------
# pyproject.toml entry point tests
# ---------------------------------------------------------------------------


class TestGeminiEntryPoints:
    """Verify all 8 Gemini hook entry points in pyproject.toml."""

    @pytest.fixture(autouse=True)
    def _load_pyproject(self):
        self.text = PYPROJECT.read_text()

    def test_session_start_entry_point(self):
        assert 'arize-hook-gemini-session-start = "tracing.gemini.hooks.handlers:session_start"' in self.text

    def test_session_end_entry_point(self):
        assert 'arize-hook-gemini-session-end = "tracing.gemini.hooks.handlers:session_end"' in self.text

    def test_before_agent_entry_point(self):
        assert 'arize-hook-gemini-before-agent = "tracing.gemini.hooks.handlers:before_agent"' in self.text

    def test_after_agent_entry_point(self):
        assert 'arize-hook-gemini-after-agent = "tracing.gemini.hooks.handlers:after_agent"' in self.text

    def test_before_model_entry_point(self):
        assert 'arize-hook-gemini-before-model = "tracing.gemini.hooks.handlers:before_model"' in self.text

    def test_after_model_entry_point(self):
        assert 'arize-hook-gemini-after-model = "tracing.gemini.hooks.handlers:after_model"' in self.text

    def test_before_tool_entry_point(self):
        assert 'arize-hook-gemini-before-tool = "tracing.gemini.hooks.handlers:before_tool"' in self.text

    def test_after_tool_entry_point(self):
        assert 'arize-hook-gemini-after-tool = "tracing.gemini.hooks.handlers:after_tool"' in self.text

    def test_exactly_8_hook_entry_points(self):
        """There should be exactly 8 gemini hook entry points."""
        count = self.text.count("arize-hook-gemini-")
        assert count == 8, f"Expected 8 gemini hook entries, got {count}"

    def test_entry_points_importable(self):
        """All referenced handler functions should be importable."""
        from tracing.gemini.hooks.handlers import (
            after_agent,
            after_model,
            after_tool,
            before_agent,
            before_model,
            before_tool,
            session_end,
            session_start,
        )

        for fn in [
            session_start,
            session_end,
            before_agent,
            after_agent,
            before_model,
            after_model,
            before_tool,
            after_tool,
        ]:
            assert callable(fn)
