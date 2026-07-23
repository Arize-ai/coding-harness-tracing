#!/usr/bin/env python3
"""Tests for SubagentStart handler and its interaction with SubagentStop."""

import json
from pathlib import Path
from unittest import mock

import pytest

from core.common import StateManager
from tracing.claude_code.hooks.handlers import _handle_subagent_start, _handle_subagent_stop

HOOKS_JSON = Path(__file__).parents[3] / "tracing" / "claude_code" / "hooks" / "hooks.json"

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _enable_logging(monkeypatch):
    """Existing assertions expect raw content in spans; opt in to all logging."""
    monkeypatch.setenv("ARIZE_LOG_PROMPTS", "true")
    monkeypatch.setenv("ARIZE_LOG_TOOL_DETAILS", "true")
    monkeypatch.setenv("ARIZE_LOG_TOOL_CONTENT", "true")


@pytest.fixture
def state(tmp_path):
    """Create a StateManager with a temp state file, pre-initialized."""
    sf = tmp_path / "state_test.json"
    lp = tmp_path / ".lock_test"
    sm = StateManager(state_dir=tmp_path, state_file=sf, lock_path=lp)
    sm.init_state()
    sm.set("session_id", "test-session-123")
    sm.set("project_name", "test-project")
    sm.set("trace_count", "0")
    sm.set("tool_count", "0")
    sm.set("user_id", "test-user")
    return sm


@pytest.fixture
def mock_resolve(state):
    """Mock resolve_session to return the test state fixture."""
    with mock.patch("tracing.claude_code.hooks.handlers.resolve_session", return_value=state) as m:
        yield m


@pytest.fixture
def captured_spans():
    """Mock send_span and collect all payloads sent."""
    sent = []
    with mock.patch("tracing.claude_code.hooks.handlers.send_span", side_effect=lambda s: sent.append(s)):
        yield sent


# ---------------------------------------------------------------------------
# SubagentStart tests
# ---------------------------------------------------------------------------


def test_hooks_json_registers_subagent_start():
    hooks = json.loads(HOOKS_JSON.read_text(encoding="utf-8"))["hooks"]
    commands = [entry["command"] for matcher in hooks["SubagentStart"] for entry in matcher["hooks"]]
    assert any("arize-hook-subagent-start" in command for command in commands)


class TestSubagentStart:

    def test_records_start_time_in_state(self, mock_resolve, state, captured_spans):
        """SubagentStart stores a timestamp string keyed by agent_id."""
        _handle_subagent_start({"agent_id": "agent-1", "agent_type": "general"})
        stored = state.get("subagent_agent-1_start_time")
        assert stored is not None
        assert stored != ""
        assert int(stored) > 0

    def test_records_prompt_when_present(self, mock_resolve, state, captured_spans):
        """SubagentStart stores the prompt when provided."""
        _handle_subagent_start({"agent_id": "agent-1", "agent_type": "general", "prompt": "do the thing"})
        assert state.get("subagent_agent-1_prompt") == "do the thing"

    def test_no_agent_id_returns_early(self, mock_resolve, state, captured_spans):
        """Missing agent_id causes early return with no state writes or spans."""
        _handle_subagent_start({"agent_type": "general"})
        assert len(captured_spans) == 0
        # No state keys should be written with empty agent_id prefix
        assert state.get("subagent__start_time") is None

    def test_no_span_emitted_at_start(self, mock_resolve, state, captured_spans):
        """SubagentStart never emits a span."""
        _handle_subagent_start({"agent_id": "agent-1", "agent_type": "general", "prompt": "hello"})
        assert len(captured_spans) == 0


# ---------------------------------------------------------------------------
# SubagentStop interaction tests
# ---------------------------------------------------------------------------


class TestSubagentStopWithStoredState:

    def test_subagent_stop_uses_stored_start_time(self, mock_resolve, state, captured_spans):
        """When state has a stored start time, SubagentStop uses it."""
        state.set("current_trace_id", "t" * 32)
        state.set("current_trace_span_id", "s" * 16)
        state.set("subagent_a1_start_time", "1000")
        with mock.patch("tracing.claude_code.hooks.handlers.resolve_transcript_path", return_value=None):
            _handle_subagent_stop({"agent_id": "a1", "agent_type": "general"})
        assert len(captured_spans) == 1
        span = captured_spans[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        assert span["startTimeUnixNano"] == "1000000000"

    def test_subagent_stop_falls_back_when_state_missing(self, mock_resolve, state, captured_spans, transcript_file):
        """Without stored start time, falls back to transcript birth time."""
        state.set("current_trace_id", "t" * 32)
        state.set("current_trace_span_id", "s" * 16)

        real_stat = Path(transcript_file).stat()
        mock_stat = mock.MagicMock(wraps=real_stat)
        mock_stat.st_birthtime = 1700000.0
        mock_stat.st_mode = real_stat.st_mode

        with mock.patch(
            "tracing.claude_code.hooks.handlers.resolve_transcript_path", return_value=Path(transcript_file)
        ):
            with mock.patch.object(Path, "stat", return_value=mock_stat):
                _handle_subagent_stop(
                    {"agent_type": "explorer", "agent_id": "a2", "agent_transcript_path": transcript_file}
                )

        span = captured_spans[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        assert span["startTimeUnixNano"] == "1700000000000000"

    def test_subagent_stop_cleans_up_state(self, mock_resolve, state, captured_spans):
        """After SubagentStop, per-agent state keys are removed."""
        state.set("current_trace_id", "t" * 32)
        state.set("current_trace_span_id", "s" * 16)
        state.set("subagent_a1_start_time", "5000")
        state.set("subagent_a1_prompt", "do work")
        with mock.patch("tracing.claude_code.hooks.handlers.resolve_transcript_path", return_value=None):
            _handle_subagent_stop({"agent_id": "a1", "agent_type": "general"})
        assert state.get("subagent_a1_start_time") is None
        assert state.get("subagent_a1_prompt") is None

    def test_subagent_stop_uses_stored_prompt_as_input(self, mock_resolve, state, captured_spans):
        """Stored prompt appears as input.value on the CHAIN span."""
        state.set("current_trace_id", "t" * 32)
        state.set("current_trace_span_id", "s" * 16)
        state.set("subagent_a1_prompt", "do work")
        with mock.patch("tracing.claude_code.hooks.handlers.resolve_transcript_path", return_value=None):
            _handle_subagent_stop({"agent_id": "a1", "agent_type": "general"})
        assert len(captured_spans) == 1
        span = captured_spans[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        attrs = {a["key"]: a["value"] for a in span["attributes"]}
        assert attrs["input.value"]["stringValue"] == "do work"
