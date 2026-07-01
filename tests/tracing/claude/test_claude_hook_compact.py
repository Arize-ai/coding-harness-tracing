#!/usr/bin/env python3
"""Tests for PreCompact / PostCompact hook handlers."""

from unittest import mock

import pytest

from core.common import StateManager
from tracing.claude_code.hooks.handlers import _handle_post_compact, _handle_pre_compact

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _enable_logging(monkeypatch):
    """Opt in to all logging so assertions see raw content."""
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


def _extract_span(captured_spans, idx=0):
    """Extract the inner span dict from the OTLP envelope."""
    return captured_spans[idx]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]


def _extract_attrs(span):
    """Flatten OTLP attributes to {key: value} with string/int extraction."""
    result = {}
    for a in span["attributes"]:
        v = a["value"]
        result[a["key"]] = v.get("stringValue", v.get("intValue"))
    return result


# ---------------------------------------------------------------------------
# PreCompact tests
# ---------------------------------------------------------------------------


class TestPreCompact:
    def test_pre_compact_records_start_time(self, mock_resolve, captured_spans, state):
        _handle_pre_compact({"trigger": "manual"})
        value = state.get("compact_start_time")
        assert value is not None
        assert value.isdigit()

    def test_pre_compact_records_trigger(self, mock_resolve, captured_spans, state):
        _handle_pre_compact({"trigger": "manual"})
        assert state.get("compact_trigger") == "manual"

    def test_pre_compact_emits_no_span(self, mock_resolve, captured_spans, state):
        _handle_pre_compact({"trigger": "auto"})
        assert len(captured_spans) == 0


# ---------------------------------------------------------------------------
# PostCompact tests
# ---------------------------------------------------------------------------


class TestPostCompact:
    def test_post_compact_emits_chain_span(self, mock_resolve, captured_spans, state):
        state.set("current_trace_id", "t" * 32)
        state.set("compact_start_time", "5000")
        state.set("compact_trigger", "manual")
        _handle_post_compact({})
        assert len(captured_spans) == 1
        span = _extract_span(captured_spans)
        attrs = _extract_attrs(span)
        assert attrs["openinference.span.kind"] == "CHAIN"
        assert attrs["compact.trigger"] == "manual"
        assert span["name"] == "Compact (manual)"

    def test_post_compact_uses_recorded_start_time(self, mock_resolve, captured_spans, state):
        state.set("current_trace_id", "t" * 32)
        state.set("compact_start_time", "5000")
        state.set("compact_trigger", "manual")
        _handle_post_compact({})
        span = _extract_span(captured_spans)
        assert span["startTimeUnixNano"] == "5000000000"

    def test_post_compact_attaches_to_current_turn(self, mock_resolve, captured_spans, state):
        state.set("current_trace_id", "t" * 32)
        state.set("current_trace_span_id", "s" * 16)
        state.set("compact_start_time", "5000")
        _handle_post_compact({})
        span = _extract_span(captured_spans)
        assert span["traceId"] == "t" * 32
        assert span["parentSpanId"] == "s" * 16

    def test_post_compact_skips_when_no_active_turn(self, mock_resolve, captured_spans, state):
        """No current_trace_id ⇒ no span (avoids orphan compact traces).

        Pending compact state must still be cleaned up so a subsequent turn
        doesn't see stale compact_start_time / compact_trigger.
        """
        state.set("compact_start_time", "5000")
        state.set("compact_trigger", "auto")
        # current_trace_id is unset
        _handle_post_compact({})
        assert len(captured_spans) == 0
        assert state.get("compact_start_time") is None
        assert state.get("compact_trigger") is None

    def test_post_compact_cleans_up_state(self, mock_resolve, captured_spans, state):
        state.set("current_trace_id", "t" * 32)
        state.set("compact_start_time", "5000")
        state.set("compact_trigger", "auto")
        _handle_post_compact({})
        assert state.get("compact_start_time") is None
        assert state.get("compact_trigger") is None

    def test_post_compact_no_session_id_returns_early(self, mock_resolve, captured_spans, state):
        state.delete("session_id")
        _handle_post_compact({})
        assert len(captured_spans) == 0

    def test_post_compact_falls_back_to_payload_trigger(self, mock_resolve, captured_spans, state):
        state.set("current_trace_id", "t" * 32)
        state.set("compact_start_time", "5000")
        _handle_post_compact({"trigger": "auto"})
        span = _extract_span(captured_spans)
        attrs = _extract_attrs(span)
        assert attrs["compact.trigger"] == "auto"
