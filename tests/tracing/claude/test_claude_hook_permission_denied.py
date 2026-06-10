"""Tests for the PermissionDenied hook handler."""

from unittest import mock

import pytest

from core.common import StateManager
from tracing.claude_code.hooks.handlers import _handle_permission_denied


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


def _base_input():
    return {
        "session_id": "test-session-123",
        "transcript_path": "/tmp/transcript.jsonl",
        "cwd": "/home/user/project",
        "permission_mode": "auto",
        "tool_name": "Bash",
        "tool_input": {"command": "rm -rf /"},
    }


def _span_attrs(span):
    """Extract attributes from a captured span as a flat dict."""
    raw = span["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["attributes"]
    return {a["key"]: a["value"]["stringValue"] for a in raw}


def _span_meta(span):
    """Extract top-level span fields."""
    return span["resourceSpans"][0]["scopeSpans"][0]["spans"][0]


class TestPermissionDenied:
    def test_emits_chain_span(self, mock_resolve, captured_spans, state):
        state.set("current_trace_id", "trace-1")
        state.set("current_trace_span_id", "span-1")
        _handle_permission_denied(_base_input())
        assert len(captured_spans) == 1
        attrs = _span_attrs(captured_spans[0])
        assert attrs["openinference.span.kind"] == "CHAIN"
        assert _span_meta(captured_spans[0])["name"] == "Permission Denied"

    def test_sets_denied_attribute(self, mock_resolve, captured_spans, state):
        state.set("current_trace_id", "trace-1")
        state.set("current_trace_span_id", "span-1")
        _handle_permission_denied(_base_input())
        attrs = _span_attrs(captured_spans[0])
        assert attrs["permission.denied"] == "true"

    def test_sets_tool_attribute(self, mock_resolve, captured_spans, state):
        state.set("current_trace_id", "trace-1")
        state.set("current_trace_span_id", "span-1")
        _handle_permission_denied(_base_input())
        attrs = _span_attrs(captured_spans[0])
        assert attrs["permission.tool"] == "Bash"

    def test_sets_permission_type_when_present(self, mock_resolve, captured_spans, state):
        """When the payload carries a `permission` field, surface it as
        `permission.type` (mirrors _handle_permission_request)."""
        state.set("current_trace_id", "trace-1")
        state.set("current_trace_span_id", "span-1")
        payload = _base_input()
        payload["permission"] = "execute"
        _handle_permission_denied(payload)
        attrs = _span_attrs(captured_spans[0])
        assert attrs["permission.type"] == "execute"

    def test_sets_input_value_from_tool_input(self, mock_resolve, captured_spans, state):
        state.set("current_trace_id", "trace-1")
        state.set("current_trace_span_id", "span-1")
        _handle_permission_denied(_base_input())
        attrs = _span_attrs(captured_spans[0])
        assert '"command": "rm -rf /"' in attrs["input.value"]

    def test_redacts_tool_input_when_disabled(self, mock_resolve, captured_spans, state, monkeypatch):
        monkeypatch.setenv("ARIZE_LOG_TOOL_DETAILS", "false")
        state.set("current_trace_id", "trace-1")
        state.set("current_trace_span_id", "span-1")
        _handle_permission_denied(_base_input())
        attrs = _span_attrs(captured_spans[0])
        assert "<redacted" in attrs["input.value"]

    def test_no_trace_id_returns_early(self, mock_resolve, captured_spans, state):
        state.delete("current_trace_id")
        _handle_permission_denied(_base_input())
        assert len(captured_spans) == 0

    def test_attaches_to_current_turn(self, mock_resolve, captured_spans, state):
        state.set("current_trace_id", "trace-abc")
        state.set("current_trace_span_id", "span-def")
        _handle_permission_denied(_base_input())
        meta = _span_meta(captured_spans[0])
        assert meta["traceId"] == "trace-abc"
        assert meta["parentSpanId"] == "span-def"

    def test_includes_user_id_when_set(self, mock_resolve, captured_spans, state):
        state.set("current_trace_id", "trace-1")
        state.set("current_trace_span_id", "span-1")
        state.set("user_id", "u-42")
        _handle_permission_denied(_base_input())
        attrs = _span_attrs(captured_spans[0])
        assert attrs["user.id"] == "u-42"
