#!/usr/bin/env python3
"""Tests for UserPromptExpansion hook handler and its integration with Stop/StopFailure."""

from unittest import mock

import pytest

from core.common import StateManager
from tracing.claude_code.hooks.handlers import _handle_stop, _handle_stop_failure, _handle_user_prompt_expansion

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
# Tests
# ---------------------------------------------------------------------------


class TestUserPromptExpansion:
    def test_records_command_metadata_in_state(self, mock_resolve, captured_spans, state):
        payload = {
            "session_id": "test-session-123",
            "expansion_type": "slash_command",
            "command_name": "review",
            "command_args": "PR-42",
            "command_source": "user",
        }
        _handle_user_prompt_expansion(payload)
        assert state.get("pending_expansion_type") == "slash_command"
        assert state.get("pending_command_name") == "review"
        assert state.get("pending_command_args") == "PR-42"
        assert state.get("pending_command_source") == "user"

    def test_records_only_present_fields(self, mock_resolve, captured_spans, state):
        payload = {
            "session_id": "test-session-123",
            "command_name": "commit",
        }
        _handle_user_prompt_expansion(payload)
        assert state.get("pending_command_name") == "commit"
        assert state.get("pending_expansion_type") is None
        assert state.get("pending_command_args") is None
        assert state.get("pending_command_source") is None

    def test_no_span_emitted(self, mock_resolve, captured_spans, state):
        payload = {
            "session_id": "test-session-123",
            "expansion_type": "slash_command",
            "command_name": "review",
            "command_args": "PR-42",
            "command_source": "user",
        }
        _handle_user_prompt_expansion(payload)
        assert len(captured_spans) == 0


class TestStopWithCommandMetadata:
    def _setup_trace_state(self, state):
        state.set("current_trace_id", "trace-aaa")
        state.set("current_trace_span_id", "span-bbb")
        state.set("current_trace_start_time", "1000000")
        state.set("current_trace_prompt", "expanded prompt text")
        state.set("trace_count", "1")

    def test_stop_attaches_command_metadata_to_turn_span(self, mock_resolve, captured_spans, state):
        self._setup_trace_state(state)
        state.set("pending_expansion_type", "slash_command")
        state.set("pending_command_name", "review")
        state.set("pending_command_args", "PR-42")
        state.set("pending_command_source", "user")

        with mock.patch("tracing.claude_code.hooks.handlers.resolve_transcript_path", return_value=None):
            _handle_stop({"session_id": "test-session-123"})

        assert len(captured_spans) == 1
        span = captured_spans[0]
        attrs = {a["key"]: a["value"] for a in span["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["attributes"]}
        assert attrs["command.expansion_type"]["stringValue"] == "slash_command"
        assert attrs["command.name"]["stringValue"] == "review"
        assert attrs["command.args"]["stringValue"] == "PR-42"
        assert attrs["command.source"]["stringValue"] == "user"

    def test_stop_clears_pending_after_emit(self, mock_resolve, captured_spans, state):
        self._setup_trace_state(state)
        state.set("pending_expansion_type", "slash_command")
        state.set("pending_command_name", "review")
        state.set("pending_command_args", "PR-42")
        state.set("pending_command_source", "user")

        with mock.patch("tracing.claude_code.hooks.handlers.resolve_transcript_path", return_value=None):
            _handle_stop({"session_id": "test-session-123"})

        assert state.get("pending_expansion_type") is None
        assert state.get("pending_command_name") is None
        assert state.get("pending_command_args") is None
        assert state.get("pending_command_source") is None

    def test_stop_failure_clears_pending(self, mock_resolve, captured_spans, state):
        self._setup_trace_state(state)
        state.set("pending_command_name", "review")

        _handle_stop_failure(
            {
                "session_id": "test-session-123",
                "error": "context_overflow",
                "error_details": "too long",
            }
        )

        assert state.get("pending_command_name") is None

    def test_stop_omits_command_attrs_when_absent(self, mock_resolve, captured_spans, state):
        self._setup_trace_state(state)

        with mock.patch("tracing.claude_code.hooks.handlers.resolve_transcript_path", return_value=None):
            _handle_stop({"session_id": "test-session-123"})

        assert len(captured_spans) == 1
        span = captured_spans[0]
        attr_keys = {a["key"] for a in span["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["attributes"]}
        assert "command.expansion_type" not in attr_keys
        assert "command.name" not in attr_keys
        assert "command.args" not in attr_keys
        assert "command.source" not in attr_keys

    def test_command_args_redacted(self, monkeypatch, mock_resolve, captured_spans, state):
        monkeypatch.setenv("ARIZE_LOG_PROMPTS", "false")
        self._setup_trace_state(state)
        state.set("pending_expansion_type", "slash_command")
        state.set("pending_command_name", "review")
        state.set("pending_command_args", "PR-42")
        state.set("pending_command_source", "user")

        with mock.patch("tracing.claude_code.hooks.handlers.resolve_transcript_path", return_value=None):
            _handle_stop({"session_id": "test-session-123"})

        assert len(captured_spans) == 1
        span = captured_spans[0]
        attrs = {a["key"]: a["value"] for a in span["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["attributes"]}
        assert "<redacted" in attrs["command.args"]["stringValue"]
