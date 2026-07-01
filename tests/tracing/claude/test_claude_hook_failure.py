#!/usr/bin/env python3
"""Tests for _handle_post_tool_use_failure in tracing.claude_code.hooks.handlers."""

from unittest import mock

import pytest

from core.common import StateManager
from tracing.claude_code.hooks.handlers import _handle_post_tool_use_failure

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


def test_emits_tool_span_with_error_attrs(mock_resolve, captured_spans):
    payload = {
        "session_id": "test-session-123",
        "tool_name": "Bash",
        "tool_input": {"command": "false"},
        "tool_response": "",
        "error": "exit code 1",
    }
    _handle_post_tool_use_failure(payload)

    assert len(captured_spans) == 1
    span = captured_spans[0]
    attrs = {a["key"]: a["value"] for a in span["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["attributes"]}
    assert attrs["openinference.span.kind"]["stringValue"] == "TOOL"
    assert attrs["tool.name"]["stringValue"] == "Bash"
    assert attrs["error.type"]["stringValue"] == "tool_failure"
    assert attrs["error.message"]["stringValue"] == "exit code 1"
    assert attrs["output.value"]["stringValue"] == "exit code 1"


def test_falls_back_to_error_when_response_empty(mock_resolve, captured_spans):
    payload = {
        "session_id": "test-session-123",
        "tool_name": "Bash",
        "tool_input": {"command": "false"},
        "tool_response": "",
        "error": "boom",
    }
    _handle_post_tool_use_failure(payload)

    span = captured_spans[0]
    attrs = {a["key"]: a["value"] for a in span["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["attributes"]}
    assert attrs["output.value"]["stringValue"] == "boom"


def test_uses_response_when_present(mock_resolve, captured_spans):
    payload = {
        "session_id": "test-session-123",
        "tool_name": "Bash",
        "tool_input": {"command": "false"},
        "tool_response": "partial output",
        "error": "boom",
    }
    _handle_post_tool_use_failure(payload)

    span = captured_spans[0]
    attrs = {a["key"]: a["value"] for a in span["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["attributes"]}
    assert attrs["output.value"]["stringValue"] == "partial output"
    assert attrs["error.message"]["stringValue"] == "boom"


def test_span_name_marked_failed(mock_resolve, captured_spans):
    payload = {
        "session_id": "test-session-123",
        "tool_name": "Bash",
        "tool_input": {"command": "false"},
        "error": "fail",
    }
    _handle_post_tool_use_failure(payload)

    span_data = captured_spans[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    assert span_data["name"] == "Bash (failed)"


def test_increments_tool_count(mock_resolve, captured_spans, state):
    payload = {
        "session_id": "test-session-123",
        "tool_name": "Bash",
        "tool_input": {"command": "false"},
        "error": "fail",
    }
    _handle_post_tool_use_failure(payload)

    assert state.get("tool_count") == "1"


def test_no_session_id_returns_early(mock_resolve, captured_spans, state):
    state.delete("session_id")
    payload = {
        "session_id": "test-session-123",
        "tool_name": "Bash",
        "tool_input": {"command": "false"},
        "error": "fail",
    }
    _handle_post_tool_use_failure(payload)

    assert len(captured_spans) == 0


def test_uses_pre_tool_start_time_when_present(mock_resolve, captured_spans, state):
    state.set("tool_xxx_start", "1500")
    payload = {
        "session_id": "test-session-123",
        "tool_use_id": "xxx",
        "tool_name": "Bash",
        "tool_input": {"command": "false"},
        "error": "fail",
    }
    _handle_post_tool_use_failure(payload)

    span_data = captured_spans[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    assert span_data["startTimeUnixNano"] == "1500000000"


def test_redacts_when_logging_disabled(mock_resolve, captured_spans, monkeypatch):
    monkeypatch.setenv("ARIZE_LOG_TOOL_CONTENT", "false")
    payload = {
        "session_id": "test-session-123",
        "tool_name": "Bash",
        "tool_input": {"command": "false"},
        "tool_response": "",
        "error": "secret error details",
    }
    _handle_post_tool_use_failure(payload)

    span = captured_spans[0]
    attrs = {a["key"]: a["value"] for a in span["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["attributes"]}
    assert "redacted" in attrs["output.value"]["stringValue"].lower()
    assert "redacted" in attrs["error.message"]["stringValue"].lower()
