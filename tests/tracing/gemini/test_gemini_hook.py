#!/usr/bin/env python3
"""Tests for tracing.gemini.hooks.handlers — the 8 Gemini hook handlers.

Mirrors tests/test_copilot_hook.py structure but adapted for Gemini's
single-mode adapter (no VS Code / CLI dual-mode) and Gemini-specific
event names (BeforeAgent/AfterAgent, BeforeModel/AfterModel, etc.).
"""
from __future__ import annotations

import io
import json
import sys
from unittest import mock

import pytest

from core.common import StateManager
from tracing.gemini.hooks import handlers as handlers_mod
from tracing.gemini.hooks.handlers import (
    _extract_text,
    _extract_tokens,
    _flush_pending_model_call,
    _handle_after_agent,
    _handle_after_model,
    _handle_after_tool,
    _handle_before_agent,
    _handle_before_model,
    _handle_before_tool,
    _handle_session_end,
    _handle_session_start,
    _print_response,
    _read_stdin,
    _send_span_async,
    after_agent,
    after_model,
    after_tool,
    before_agent,
    before_model,
    before_tool,
    main,
    session_end,
    session_start,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_span_attrs(span_payload):
    """Extract attributes dict from OTLP span payload."""
    span = span_payload["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    return {a["key"]: a["value"] for a in span["attributes"]}


def _get_span(span_payload):
    """Extract span object from OTLP span payload."""
    return span_payload["resourceSpans"][0]["scopeSpans"][0]["spans"][0]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def state(tmp_path):
    """Create a StateManager with a temp state file, pre-initialized."""
    sf = tmp_path / "state_test.json"
    lp = tmp_path / ".lock_test"
    sm = StateManager(state_dir=tmp_path, state_file=sf, lock_path=lp)
    sm.init_state()
    sm.set("session_id", "test-session-gemini")
    sm.set("project_name", "test-gemini-project")
    sm.set("trace_count", "0")
    sm.set("tool_count", "0")
    sm.set("user_id", "test-user")
    return sm


@pytest.fixture
def mock_resolve(state):
    """Mock resolve_session to return the test state fixture."""
    with mock.patch("tracing.gemini.hooks.handlers.resolve_session", return_value=state) as m:
        yield m


@pytest.fixture
def mock_ensure():
    """Mock ensure_session_initialized."""
    with mock.patch("tracing.gemini.hooks.handlers.ensure_session_initialized") as m:
        yield m


@pytest.fixture
def captured_spans():
    """Mock _send_span_async and collect all payloads emitted by handlers.

    Patching _send_span_async (rather than send_span) lets tests run
    synchronously without forking, regardless of the ARIZE_DISABLE_FORK env.
    """
    sent = []
    with mock.patch("tracing.gemini.hooks.handlers._send_span_async", side_effect=lambda s: sent.append(s)):
        yield sent


# ---------------------------------------------------------------------------
# _read_stdin tests
# ---------------------------------------------------------------------------


class TestReadStdin:
    def test_empty_stdin(self):
        with mock.patch.object(sys, "stdin", new=io.StringIO("")):
            assert _read_stdin() == {}

    def test_malformed_json(self):
        with mock.patch.object(sys, "stdin", new=io.StringIO("not json")):
            assert _read_stdin() == {}

    def test_valid_json(self):
        with mock.patch.object(sys, "stdin", new=io.StringIO('{"key": "val"}')):
            assert _read_stdin() == {"key": "val"}


# ---------------------------------------------------------------------------
# _print_response tests
# ---------------------------------------------------------------------------


class TestPrintResponse:
    def test_prints_empty_json(self, capsys):
        """Gemini _print_response always prints {} for all events."""
        _print_response()
        out = json.loads(capsys.readouterr().out.strip())
        assert out == {}

    def test_prints_trailing_newline(self, capsys):
        """print() adds a trailing newline (intentional for Gemini)."""
        _print_response()
        raw = capsys.readouterr().out
        assert raw.endswith("\n")

    def test_no_continue_field(self, capsys):
        """Response must NOT contain 'continue' field (unlike copilot)."""
        _print_response()
        out = json.loads(capsys.readouterr().out.strip())
        assert "continue" not in out


# ---------------------------------------------------------------------------
# session_start tests
# ---------------------------------------------------------------------------


class TestSessionStart:
    def test_calls_resolve_and_ensure(self, mock_resolve, mock_ensure, state):
        """session_start calls resolve_session and ensure_session_initialized."""
        _handle_session_start({"prompt": "hello"})
        mock_resolve.assert_called_once()
        mock_ensure.assert_called_once()

    def test_logs_session_id(self, mock_resolve, mock_ensure, state):
        """session_start logs the session ID."""
        with mock.patch("tracing.gemini.hooks.handlers.log") as log_mock:
            _handle_session_start({})
        calls = [c[0][0] for c in log_mock.call_args_list]
        assert any("test-session-gemini" in c for c in calls)


# ---------------------------------------------------------------------------
# session_end tests
# ---------------------------------------------------------------------------


class TestSessionEnd:
    def test_no_session_id_returns_early(self, state):
        """Returns early when session_id is None."""
        state.delete("session_id")
        with (
            mock.patch("tracing.gemini.hooks.handlers.resolve_session", return_value=state),
            mock.patch("tracing.gemini.hooks.handlers.log") as log_mock,
            mock.patch("tracing.gemini.hooks.handlers.gc_stale_state_files") as gc_mock,
        ):
            _handle_session_end({})
        log_mock.assert_not_called()
        gc_mock.assert_not_called()

    def test_logs_session_summary(self, mock_resolve, state):
        """Logs session summary with trace_count and tool_count."""
        state.set("trace_count", "10")
        state.set("tool_count", "25")
        with (
            mock.patch("tracing.gemini.hooks.handlers.log") as log_mock,
            mock.patch("tracing.gemini.hooks.handlers.gc_stale_state_files"),
        ):
            _handle_session_end({})
        calls = [c[0][0] for c in log_mock.call_args_list]
        assert any("10" in c and "25" in c for c in calls)

    def test_removes_state_file(self, mock_resolve, state):
        """Removes state file on session end."""
        assert state.state_file.exists()
        with (
            mock.patch("tracing.gemini.hooks.handlers.log"),
            mock.patch("tracing.gemini.hooks.handlers.gc_stale_state_files"),
        ):
            _handle_session_end({})
        assert not state.state_file.exists()

    def test_calls_gc(self, mock_resolve, state):
        """Calls gc_stale_state_files."""
        with (
            mock.patch("tracing.gemini.hooks.handlers.log"),
            mock.patch("tracing.gemini.hooks.handlers.gc_stale_state_files") as gc_mock,
        ):
            _handle_session_end({})
        gc_mock.assert_called_once()

    def test_failsafe_closes_pending_turn(self, mock_resolve, state, captured_spans):
        """If trace state is still set, session_end closes it as a CHAIN root span."""
        state.set("current_trace_id", "t" * 32)
        state.set("current_trace_span_id", "s" * 16)
        state.set("current_trace_start_time", "1000")
        with (
            mock.patch("tracing.gemini.hooks.handlers.log"),
            mock.patch("tracing.gemini.hooks.handlers.gc_stale_state_files"),
        ):
            _handle_session_end({})
        assert len(captured_spans) >= 1
        attrs = _get_span_attrs(captured_spans[0])
        assert attrs["openinference.span.kind"]["stringValue"] == "CHAIN"
        assert "closed by SessionEnd fail-safe" in attrs.get("output.value", {}).get("stringValue", "")
        span = _get_span(captured_spans[0])
        assert span["traceId"] == "t" * 32
        assert span["spanId"] == "s" * 16
        assert "parentSpanId" not in span
        # State must be cleared so SessionEnd's own bookkeeping doesn't re-emit
        assert state.get("current_trace_id") is None
        assert state.get("current_trace_span_id") is None


# ---------------------------------------------------------------------------
# before_agent tests
# ---------------------------------------------------------------------------


class TestBeforeAgent:
    def test_sets_trace_state(self, mock_resolve, state):
        """before_agent sets current_trace_id, span_id, start_time, prompt."""
        _handle_before_agent({"messages": [{"role": "user", "content": "explain this code"}]})
        assert state.get("current_trace_id") is not None
        assert len(state.get("current_trace_id")) == 32
        assert state.get("current_trace_span_id") is not None
        assert len(state.get("current_trace_span_id")) == 16
        assert state.get("current_trace_start_time") is not None
        assert state.get("trace_count") == "1"
        assert state.get("current_trace_prompt") == "explain this code"

    def test_does_not_redact_at_save_time(self, mock_resolve, state, monkeypatch):
        """Prompt is NOT redacted at save time even when log_prompts is False."""
        monkeypatch.setenv("ARIZE_LOG_PROMPTS", "false")
        _handle_before_agent({"messages": [{"role": "user", "content": "secret prompt"}]})
        saved = state.get("current_trace_prompt")
        assert saved == "secret prompt"

    def test_saves_prompt_when_allowed(self, mock_resolve, state, monkeypatch):
        """Prompt is saved as-is when log_prompts is True."""
        monkeypatch.setenv("ARIZE_LOG_PROMPTS", "true")
        _handle_before_agent({"messages": [{"role": "user", "content": "visible prompt"}]})
        assert state.get("current_trace_prompt") == "visible prompt"

    def test_empty_prompt(self, mock_resolve, state):
        """Handles missing prompt gracefully."""
        _handle_before_agent({})
        assert state.get("current_trace_prompt") == ""

    def test_robust_prompt_extraction(self, mock_resolve, state):
        """Extracts prompt from flat 'prompt' field using _extract_text."""
        _handle_before_agent({"prompt": {"parts": [{"text": "robust prompt"}]}})
        assert state.get("current_trace_prompt") == "robust prompt"

    def test_failsafe_closes_prior_pending_turn(self, mock_resolve, state, captured_spans):
        """If a prior turn's trace state is still set (no AfterAgent fired), BeforeAgent
        emits a CHAIN closure span for it before starting the new turn — so child spans
        from the prior turn aren't orphaned."""
        # Simulate a turn that started but never closed
        state.set("current_trace_id", "p" * 32)
        state.set("current_trace_span_id", "q" * 16)
        state.set("current_trace_start_time", "1000")
        state.set("current_trace_prompt", "prior prompt")

        _handle_before_agent({"prompt": "new prompt"})

        # One closure span for the prior turn must have been sent
        assert len(captured_spans) == 1
        attrs = _get_span_attrs(captured_spans[0])
        assert attrs["openinference.span.kind"]["stringValue"] == "CHAIN"
        assert "closed by BeforeAgent fail-safe" in attrs["output.value"]["stringValue"]
        span = _get_span(captured_spans[0])
        assert span["traceId"] == "p" * 32
        assert span["spanId"] == "q" * 16
        assert "parentSpanId" not in span

        # New turn's IDs must be different (fresh trace)
        new_trace = state.get("current_trace_id")
        new_span = state.get("current_trace_span_id")
        assert new_trace != "p" * 32
        assert new_span != "q" * 16
        assert state.get("current_trace_prompt") == "new prompt"


# ---------------------------------------------------------------------------
# after_agent tests
# ---------------------------------------------------------------------------


class TestAfterAgent:
    def test_builds_chain_span(self, mock_resolve, state, captured_spans):
        """after_agent builds a root CHAIN span for the completed turn."""
        state.set("current_trace_id", "a" * 32)
        state.set("current_trace_span_id", "b" * 16)
        state.set("current_trace_start_time", "1000")
        state.set("current_trace_prompt", "explain this")
        _handle_after_agent({"response": {"content": "Here is the explanation."}})
        assert len(captured_spans) == 1
        attrs = _get_span_attrs(captured_spans[0])
        assert attrs["openinference.span.kind"]["stringValue"] == "CHAIN"
        assert attrs["session.id"]["stringValue"] == "test-session-gemini"
        assert attrs["project.name"]["stringValue"] == "test-gemini-project"
        assert attrs["input.value"]["stringValue"] == "explain this"
        assert attrs["output.value"]["stringValue"] == "Here is the explanation."

    def test_robust_response_extraction(self, mock_resolve, state, captured_spans):
        """Extracts response from various fields using _get_robust and _extract_text."""
        state.set("current_trace_id", "a" * 32)
        state.set("current_trace_span_id", "b" * 16)
        state.set("current_trace_prompt", "test")

        # Test prompt_response
        _handle_after_agent({"prompt_response": "resp 1"})
        assert _get_span_attrs(captured_spans[0])["output.value"]["stringValue"] == "resp 1"

        # Test llm_response with candidates
        state.set("current_trace_id", "a" * 32)
        state.set("current_trace_span_id", "b" * 16)
        _handle_after_agent({"llm_response": {"candidates": [{"content": {"parts": [{"text": "resp 2"}]}}]}})
        assert _get_span_attrs(captured_spans[1])["output.value"]["stringValue"] == "resp 2"

    def test_redacts_response(self, mock_resolve, state, captured_spans, monkeypatch):
        """Response is redacted when log_prompts is False."""
        monkeypatch.setenv("ARIZE_LOG_PROMPTS", "false")
        state.set("current_trace_id", "a" * 32)
        state.set("current_trace_span_id", "b" * 16)
        state.set("current_trace_start_time", "1000")
        state.set("current_trace_prompt", "<redacted (13 chars)>")
        _handle_after_agent({"response": {"content": "secret output"}})
        assert len(captured_spans) == 1
        attrs = _get_span_attrs(captured_spans[0])
        assert "redacted" in attrs["output.value"]["stringValue"]

    def test_includes_user_id(self, mock_resolve, state, captured_spans):
        """user.id is included when non-empty."""
        state.set("current_trace_id", "a" * 32)
        state.set("current_trace_span_id", "b" * 16)
        state.set("current_trace_start_time", "1000")
        state.set("current_trace_prompt", "test")
        _handle_after_agent({"response": {"content": "ok"}})
        attrs = _get_span_attrs(captured_spans[0])
        assert attrs["user.id"]["stringValue"] == "test-user"

    def test_no_user_id_when_empty(self, mock_resolve, state, captured_spans):
        """user.id is NOT included when empty."""
        state.set("user_id", "")
        state.set("current_trace_id", "a" * 32)
        state.set("current_trace_span_id", "b" * 16)
        state.set("current_trace_start_time", "1000")
        state.set("current_trace_prompt", "test")
        _handle_after_agent({"response": {"content": "ok"}})
        attrs = _get_span_attrs(captured_spans[0])
        assert "user.id" not in attrs

    def test_clears_trace_state(self, mock_resolve, state, captured_spans):
        """Clears current_trace_* state keys after sending."""
        state.set("current_trace_id", "a" * 32)
        state.set("current_trace_span_id", "b" * 16)
        state.set("current_trace_start_time", "1000")
        state.set("current_trace_prompt", "test")
        _handle_after_agent({"response": {"content": "ok"}})
        assert state.get("current_trace_id") is None
        assert state.get("current_trace_span_id") is None
        assert state.get("current_trace_start_time") is None
        assert state.get("current_trace_prompt") is None

    def test_span_is_root(self, mock_resolve, state, captured_spans):
        """The CHAIN span is a root span (no parentSpanId)."""
        state.set("current_trace_id", "a" * 32)
        state.set("current_trace_span_id", "b" * 16)
        state.set("current_trace_start_time", "1000")
        state.set("current_trace_prompt", "test")
        _handle_after_agent({"response": {"content": "ok"}})
        span = _get_span(captured_spans[0])
        assert "parentSpanId" not in span


# ---------------------------------------------------------------------------
# before_model tests
# ---------------------------------------------------------------------------


class TestBeforeModel:
    def test_records_model_call_start(self, mock_resolve, state):
        """Stashes model call start time keyed by model_call_id."""
        _handle_before_model({"model_call_id": "mc-1"})
        val = state.get("model_mc-1_start")
        assert val is not None
        assert int(val) > 0

    def test_sets_current_model_call_id(self, mock_resolve, state):
        """Sets current_model_call_id for AfterModel to find."""
        _handle_before_model({"model_call_id": "mc-2"})
        assert state.get("current_model_call_id") == "mc-2"

    def test_generates_id_when_missing(self, mock_resolve, state):
        """Generates a span ID as model_call_id when not in payload."""
        _handle_before_model({})
        call_id = state.get("current_model_call_id")
        assert call_id is not None
        assert len(call_id) == 16  # generate_span_id returns 16 hex chars

    def test_works_without_trace_context(self, mock_resolve, state):
        """Records start time even without current_trace_id (no crash)."""
        # No current_trace_id set
        _handle_before_model({"model_call_id": "mc-3"})
        assert state.get("model_mc-3_start") is not None


# ---------------------------------------------------------------------------
# after_model tests
# ---------------------------------------------------------------------------


def _final_chunk(extra: dict = None, p: int = 10, c: int = 5) -> dict:
    """Build an AfterModel payload that includes usage tokens, marking it as
    the final streaming chunk so _handle_after_model flushes immediately."""
    payload = {"usageMetadata": {"promptTokenCount": p, "candidatesTokenCount": c}}
    if extra:
        payload.update(extra)
    return payload


class TestAfterModel:
    """AfterModel accumulates streaming chunks; a span is emitted only when the
    final chunk arrives (token counts present) or on explicit flush."""

    def test_builds_llm_span(self, mock_resolve, state, captured_spans):
        """A final chunk (with tokens) emits a single LLM span using the
        accumulated prompt and response."""
        state.set("current_trace_id", "a" * 32)
        state.set("current_trace_span_id", "b" * 16)
        state.set("current_model_call_id", "mc-1")
        state.set("model_mc-1_start", "1000")
        state.set("model_mc-1_prompt", json.dumps([{"role": "user", "content": "what is 2+2?"}]))
        _handle_after_model(
            {
                "model": "gemini-2.5-pro",
                "llm_response": {"candidates": [{"content": {"parts": ["4"]}}]},
                "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5},
                "model_call_id": "mc-1",
            }
        )
        assert len(captured_spans) == 1
        attrs = _get_span_attrs(captured_spans[0])
        assert attrs["openinference.span.kind"]["stringValue"] == "LLM"
        assert attrs["llm.model_name"]["stringValue"] == "gemini-2.5-pro"
        assert attrs["llm.token_count.prompt"]["intValue"] == 10
        assert attrs["llm.token_count.completion"]["intValue"] == 5
        assert attrs["llm.token_count.total"]["intValue"] == 15
        assert attrs["input.value"]["stringValue"] == json.dumps([{"role": "user", "content": "what is 2+2?"}])
        assert attrs["output.value"]["stringValue"] == "4"

    def test_streaming_chunks_coalesce_into_one_span(self, mock_resolve, state, captured_spans):
        """Multiple AfterModel events for one call concatenate into a single span."""
        state.set("current_trace_id", "a" * 32)
        state.set("current_trace_span_id", "b" * 16)
        state.set("current_model_call_id", "mc-1")
        state.set("model_mc-1_start", "1000")

        # Chunks 1-3: text only, no tokens — accumulate, don't emit
        _handle_after_model({"model": "gemini-3-flash", "llm_response": {"text": "Hello"}, "model_call_id": "mc-1"})
        _handle_after_model({"llm_response": {"text": " "}, "model_call_id": "mc-1"})
        _handle_after_model({"llm_response": {"text": "world"}, "model_call_id": "mc-1"})
        assert captured_spans == []

        # Chunk 4: final chunk with tokens — flushes the accumulated span
        _handle_after_model(
            {
                "llm_response": {"text": ""},
                "usageMetadata": {"promptTokenCount": 100, "candidatesTokenCount": 3},
                "model_call_id": "mc-1",
            }
        )
        assert len(captured_spans) == 1
        attrs = _get_span_attrs(captured_spans[0])
        assert attrs["output.value"]["stringValue"] == "Hello world"
        assert attrs["llm.model_name"]["stringValue"] == "gemini-3-flash"
        assert attrs["llm.token_count.completion"]["intValue"] == 3

    def test_non_final_chunk_does_not_emit(self, mock_resolve, state, captured_spans):
        """AfterModel with no tokens accumulates into state without emitting."""
        state.set("current_trace_id", "a" * 32)
        state.set("current_trace_span_id", "b" * 16)
        state.set("current_model_call_id", "mc-1")
        state.set("model_mc-1_start", "1000")
        _handle_after_model({"model": "gemini-2.5-pro", "model_call_id": "mc-1"})
        assert captured_spans == []
        # current_model_call_id is preserved so the next chunk continues the same span
        assert state.get("current_model_call_id") == "mc-1"

    def test_span_name_includes_model(self, mock_resolve, state, captured_spans):
        """Span name is 'LLM: {model_name}' when model is provided."""
        state.set("current_trace_id", "a" * 32)
        state.set("current_trace_span_id", "b" * 16)
        state.set("current_model_call_id", "mc-1")
        state.set("model_mc-1_start", "1000")
        _handle_after_model(_final_chunk({"model": "gemini-2.5-pro", "model_call_id": "mc-1"}))
        span = _get_span(captured_spans[0])
        assert span["name"] == "LLM: gemini-2.5-pro"

    def test_span_name_plain_when_no_model(self, mock_resolve, state, captured_spans):
        """Span name is 'LLM' when model is not provided."""
        state.set("current_trace_id", "a" * 32)
        state.set("current_trace_span_id", "b" * 16)
        state.set("current_model_call_id", "mc-1")
        state.set("model_mc-1_start", "1000")
        _handle_after_model(_final_chunk({"model_call_id": "mc-1"}))
        span = _get_span(captured_spans[0])
        assert span["name"] == "LLM"

    def test_child_of_current_turn(self, mock_resolve, state, captured_spans):
        """LLM span is a child of the current turn's span."""
        state.set("current_trace_id", "a" * 32)
        state.set("current_trace_span_id", "b" * 16)
        state.set("current_model_call_id", "mc-1")
        state.set("model_mc-1_start", "1000")
        _handle_after_model(_final_chunk({"model": "gemini-2.5-pro", "model_call_id": "mc-1"}))
        span = _get_span(captured_spans[0])
        assert span["traceId"] == "a" * 32
        assert span["parentSpanId"] == "b" * 16

    def test_no_trace_context_returns_early(self, mock_resolve, state, captured_spans):
        """Returns without sending when no current_trace_id in state."""
        state.set("current_model_call_id", "mc-1")
        state.set("model_mc-1_start", "1000")
        _handle_after_model(_final_chunk({"model": "gemini-2.5-pro", "model_call_id": "mc-1"}))
        assert len(captured_spans) == 0

    def test_redacts_prompt_and_response(self, mock_resolve, state, captured_spans, monkeypatch):
        """Redacts input/output when log_prompts is False."""
        monkeypatch.setenv("ARIZE_LOG_PROMPTS", "false")
        state.set("current_trace_id", "a" * 32)
        state.set("current_trace_span_id", "b" * 16)
        state.set("current_model_call_id", "mc-1")
        state.set("model_mc-1_start", "1000")
        state.set("model_mc-1_prompt", "secret prompt")
        _handle_after_model(
            _final_chunk(
                {
                    "model": "gemini-2.5-pro",
                    "llm_response": {"candidates": [{"content": {"parts": ["secret response"]}}]},
                    "model_call_id": "mc-1",
                }
            )
        )
        attrs = _get_span_attrs(captured_spans[0])
        assert "redacted" in attrs["input.value"]["stringValue"]
        assert "redacted" in attrs["output.value"]["stringValue"]

    def test_cleans_up_model_state(self, mock_resolve, state, captured_spans):
        """Cleans up model accumulator keys and current_model_call_id after flush."""
        state.set("current_trace_id", "a" * 32)
        state.set("current_trace_span_id", "b" * 16)
        state.set("current_model_call_id", "mc-1")
        state.set("model_mc-1_start", "1000")
        _handle_after_model(_final_chunk({"model": "gemini-2.5-pro", "model_call_id": "mc-1"}))
        assert state.get("model_mc-1_start") is None
        assert state.get("model_mc-1_response") is None
        assert state.get("model_mc-1_model") is None
        assert state.get("model_mc-1_prompt") is None
        assert state.get("current_model_call_id") is None

    def test_uses_current_model_call_id_from_state(self, mock_resolve, state, captured_spans):
        """Real Gemini AfterModel payloads don't carry model_call_id, so the
        accumulator key is always read from state.current_model_call_id. A
        final chunk flushes the span using that id."""
        state.set("current_trace_id", "a" * 32)
        state.set("current_trace_span_id", "b" * 16)
        state.set("current_model_call_id", "fallback-mc")
        state.set("model_fallback-mc_start", "1000")
        _handle_after_model(_final_chunk({"model": "gemini-2.5-pro"}))
        assert len(captured_spans) == 1
        assert state.get("model_fallback-mc_start") is None
        assert state.get("current_model_call_id") is None

    def test_after_agent_flushes_pending_call_with_zero_tokens(self, mock_resolve, state, captured_spans):
        """A model call that never received a final chunk is flushed by AfterAgent
        with whatever it accumulated -- including zero token counts."""
        from tracing.gemini.hooks.handlers import _handle_after_agent

        state.set("current_trace_id", "a" * 32)
        state.set("current_trace_span_id", "b" * 16)
        state.set("current_trace_start_time", "1000")
        state.set("current_trace_prompt", "test")
        state.set("current_model_call_id", "mc-1")
        state.set("model_mc-1_start", "1000")
        state.set("model_mc-1_model", "gemini-2.5-pro")
        # No final chunk arrives
        _handle_after_model({"model": "gemini-2.5-pro", "model_call_id": "mc-1"})
        assert captured_spans == []

        _handle_after_agent({"prompt_response": "ok"})
        # Two spans: the flushed LLM span and the CHAIN root
        assert len(captured_spans) == 2
        llm_attrs = _get_span_attrs(captured_spans[0])
        assert llm_attrs["openinference.span.kind"]["stringValue"] == "LLM"
        assert llm_attrs["llm.token_count.prompt"]["intValue"] == 0
        assert llm_attrs["llm.token_count.completion"]["intValue"] == 0

    def test_accumulates_text_from_candidates(self, mock_resolve, state, captured_spans):
        """Extracts text from nested candidates structure across chunks."""
        state.set("current_trace_id", "a" * 32)
        state.set("current_trace_span_id", "b" * 16)
        state.set("current_model_call_id", "mc-1")
        state.set("model_mc-1_start", "1000")
        _handle_after_model(
            _final_chunk(
                {
                    "model": "gemini-2.5-pro",
                    "llm_response": {"candidates": [{"content": {"parts": [{"text": "Hello"}, {"text": " world"}]}}]},
                    "model_call_id": "mc-1",
                }
            )
        )
        attrs = _get_span_attrs(captured_spans[0])
        assert attrs["output.value"]["stringValue"] == "Hello\n world"

    def test_real_gemini_payload_empty_text_with_candidates(self, mock_resolve, state, captured_spans):
        """Real Gemini llm_response has text='' but content in candidates — must not short-circuit."""
        state.set("current_trace_id", "a" * 32)
        state.set("current_trace_span_id", "b" * 16)
        state.set("current_model_call_id", "mc-1")
        state.set("model_mc-1_start", "1000")
        _handle_after_model(
            {
                "model": "gemini-3-flash-preview",
                "llm_response": {
                    "candidates": [{"content": {"parts": ["Actual response content"], "role": "model"}}],
                    "text": "",
                    "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 1},
                },
                "model_call_id": "mc-1",
            }
        )
        attrs = _get_span_attrs(captured_spans[0])
        assert attrs["output.value"]["stringValue"] == "Actual response content"

    def test_handles_structured_prompt(self, mock_resolve, state, captured_spans, monkeypatch):
        """JSON-encodes structured prompt before redaction."""
        monkeypatch.setenv("ARIZE_LOG_PROMPTS", "true")
        state.set("current_trace_id", "a" * 32)
        state.set("current_trace_span_id", "b" * 16)
        state.set("current_model_call_id", "mc-1")
        state.set("model_mc-1_start", "1000")
        structured = [{"role": "user", "content": "hello"}]
        state.set("model_mc-1_prompt", json.dumps(structured))
        _handle_after_model(_final_chunk({"model": "gemini-2.5-pro", "model_call_id": "mc-1"}))
        attrs = _get_span_attrs(captured_spans[0])
        assert attrs["input.value"]["stringValue"] == json.dumps(structured)

    def test_next_before_model_flushes_prior_call(self, mock_resolve, state, captured_spans):
        """If a prior model call never received its final chunk, the next BeforeModel
        flushes it as its own LLM span before opening the new accumulator."""
        from tracing.gemini.hooks.handlers import _handle_before_model

        state.set("current_trace_id", "a" * 32)
        state.set("current_trace_span_id", "b" * 16)
        state.set("current_model_call_id", "prior")
        state.set("model_prior_start", "1000")
        state.set("model_prior_response", "partial response")
        state.set("model_prior_model", "gemini-2.5-pro")

        _handle_before_model(
            {"llm_request": {"model": "gemini-2.5-pro", "messages": [{"role": "user", "content": "x"}]}}
        )
        # Prior call gets flushed
        assert len(captured_spans) == 1
        attrs = _get_span_attrs(captured_spans[0])
        assert attrs["output.value"]["stringValue"] == "partial response"
        # Prior accumulator is cleared; new model_call_id is set
        assert state.get("model_prior_response") is None
        new_id = state.get("current_model_call_id")
        assert new_id is not None and new_id != "prior"

    def test_includes_user_id(self, mock_resolve, state, captured_spans):
        """user.id is included when non-empty."""
        state.set("current_trace_id", "a" * 32)
        state.set("current_trace_span_id", "b" * 16)
        state.set("current_model_call_id", "mc-1")
        state.set("model_mc-1_start", "1000")
        _handle_after_model(_final_chunk({"model": "gemini-2.5-pro", "model_call_id": "mc-1"}))
        attrs = _get_span_attrs(captured_spans[0])
        assert attrs["user.id"]["stringValue"] == "test-user"


# ---------------------------------------------------------------------------
# before_tool tests
# ---------------------------------------------------------------------------


class TestBeforeTool:
    def test_records_tool_start_by_id(self, mock_resolve, state):
        """Records tool start time keyed by tool_call_id."""
        _handle_before_tool({"tool_call_id": "tc-1", "tool_name": "read_file"})
        val = state.get("tool_tc-1_start")
        assert val is not None
        assert int(val) > 0

    def test_falls_back_to_tool_name(self, mock_resolve, state):
        """Falls back to tool_name when tool_call_id is missing."""
        _handle_before_tool({"tool_name": "run_shell_command"})
        val = state.get("tool_run_shell_command_start")
        assert val is not None
        assert int(val) > 0

    def test_falls_back_to_unknown(self, mock_resolve, state):
        """Falls back to 'unknown' when both tool_call_id and tool_name are missing."""
        _handle_before_tool({})
        val = state.get("tool_unknown_start")
        assert val is not None


# ---------------------------------------------------------------------------
# after_tool tests
# ---------------------------------------------------------------------------


class TestAfterTool:
    def test_builds_tool_span(self, mock_resolve, state, captured_spans):
        """after_tool builds a TOOL span with correct attributes."""
        state.set("current_trace_id", "a" * 32)
        state.set("current_trace_span_id", "b" * 16)
        inp = {
            "tool_name": "read_file",
            "tool_call_id": "tc-1",
            "tool_args": {"file_path": "/foo/bar.py"},
            "tool_result": "file content",
        }
        _handle_after_tool(inp)
        assert len(captured_spans) == 1
        attrs = _get_span_attrs(captured_spans[0])
        assert attrs["openinference.span.kind"]["stringValue"] == "TOOL"
        assert attrs["tool.name"]["stringValue"] == "read_file"
        assert attrs["tool.file_path"]["stringValue"] == "/foo/bar.py"
        assert attrs["output.value"]["stringValue"] == "file content"

    def test_real_gemini_tool_response_extracts_llmcontent(self, mock_resolve, state, captured_spans):
        """Real Gemini tool_response is {llmContent: ..., returnDisplay: ...} — must extract llmContent."""
        state.set("current_trace_id", "a" * 32)
        state.set("current_trace_span_id", "b" * 16)
        _handle_after_tool(
            {
                "tool_name": "list_directory",
                "tool_input": {"dir_path": "."},
                "tool_response": {
                    "llmContent": "Directory listing for /tmp:\n  [DIR] foo\n  bar.txt\n",
                    "returnDisplay": {"files": ["foo", "bar.txt"], "summary": "2 items"},
                },
            }
        )
        attrs = _get_span_attrs(captured_spans[0])
        assert attrs["output.value"]["stringValue"] == "Directory listing for /tmp:\n  [DIR] foo\n  bar.txt\n"

    def test_no_trace_context_skips(self, mock_resolve, state, captured_spans):
        """Skips when no current_trace_id (no active turn)."""
        _handle_after_tool({"tool_name": "read_file", "tool_result": "content"})
        assert len(captured_spans) == 0

    def test_increments_tool_count(self, mock_resolve, state, captured_spans):
        """Increments tool_count in state."""
        state.set("current_trace_id", "a" * 32)
        state.set("current_trace_span_id", "b" * 16)
        _handle_after_tool({"tool_name": "read_file", "tool_result": "content"})
        assert state.get("tool_count") == "1"

    def test_child_of_current_turn(self, mock_resolve, state, captured_spans):
        """TOOL span is a child of the current turn."""
        state.set("current_trace_id", "a" * 32)
        state.set("current_trace_span_id", "b" * 16)
        _handle_after_tool({"tool_name": "read_file", "tool_result": "content"})
        span = _get_span(captured_spans[0])
        assert span["traceId"] == "a" * 32
        assert span["parentSpanId"] == "b" * 16

    def test_uses_before_tool_start_time(self, mock_resolve, state, captured_spans):
        """Uses tool start time recorded by before_tool."""
        state.set("current_trace_id", "a" * 32)
        state.set("current_trace_span_id", "b" * 16)
        state.set("tool_tc-1_start", "1000000")
        _handle_after_tool(
            {
                "tool_name": "read_file",
                "tool_call_id": "tc-1",
                "tool_result": "content",
            }
        )
        span = _get_span(captured_spans[0])
        assert span["startTimeUnixNano"] == "1000000000000"
        # Start time key should be cleaned up
        assert state.get("tool_tc-1_start") is None

    def test_session_and_project_attrs(self, mock_resolve, state, captured_spans):
        """TOOL span includes session.id and project.name."""
        state.set("current_trace_id", "a" * 32)
        state.set("current_trace_span_id", "b" * 16)
        _handle_after_tool({"tool_name": "read_file", "tool_result": "content"})
        attrs = _get_span_attrs(captured_spans[0])
        assert attrs["session.id"]["stringValue"] == "test-session-gemini"
        assert attrs["project.name"]["stringValue"] == "test-gemini-project"

    # -- Tool argument enrichment tests --

    def test_run_shell_command_enrichment(self, mock_resolve, state, captured_spans):
        """run_shell_command sets tool.command and tool.description."""
        state.set("current_trace_id", "a" * 32)
        state.set("current_trace_span_id", "b" * 16)
        _handle_after_tool(
            {
                "tool_name": "run_shell_command",
                "tool_args": {"command": "git status"},
                "tool_result": "clean",
            }
        )
        attrs = _get_span_attrs(captured_spans[0])
        assert attrs["tool.command"]["stringValue"] == "git status"
        assert attrs["tool.description"]["stringValue"] == "git status"

    def test_read_file_enrichment(self, mock_resolve, state, captured_spans):
        """read_file sets tool.file_path and tool.description."""
        state.set("current_trace_id", "a" * 32)
        state.set("current_trace_span_id", "b" * 16)
        _handle_after_tool(
            {
                "tool_name": "read_file",
                "tool_args": {"file_path": "/src/main.py"},
                "tool_result": "content",
            }
        )
        attrs = _get_span_attrs(captured_spans[0])
        assert attrs["tool.file_path"]["stringValue"] == "/src/main.py"
        assert attrs["tool.description"]["stringValue"] == "/src/main.py"

    def test_write_file_enrichment(self, mock_resolve, state, captured_spans):
        """write_file sets tool.file_path."""
        state.set("current_trace_id", "a" * 32)
        state.set("current_trace_span_id", "b" * 16)
        _handle_after_tool(
            {
                "tool_name": "write_file",
                "tool_args": {"file_path": "/src/out.py"},
                "tool_result": "ok",
            }
        )
        attrs = _get_span_attrs(captured_spans[0])
        assert attrs["tool.file_path"]["stringValue"] == "/src/out.py"

    def test_edit_enrichment(self, mock_resolve, state, captured_spans):
        """edit sets tool.file_path."""
        state.set("current_trace_id", "a" * 32)
        state.set("current_trace_span_id", "b" * 16)
        _handle_after_tool(
            {
                "tool_name": "edit",
                "tool_args": {"absolute_path": "/src/app.py"},
                "tool_result": "ok",
            }
        )
        attrs = _get_span_attrs(captured_spans[0])
        assert attrs["tool.file_path"]["stringValue"] == "/src/app.py"

    def test_replace_enrichment(self, mock_resolve, state, captured_spans):
        """replace sets tool.file_path."""
        state.set("current_trace_id", "a" * 32)
        state.set("current_trace_span_id", "b" * 16)
        _handle_after_tool(
            {
                "tool_name": "replace",
                "tool_args": {"file_path": "/src/fix.py"},
                "tool_result": "ok",
            }
        )
        attrs = _get_span_attrs(captured_spans[0])
        assert attrs["tool.file_path"]["stringValue"] == "/src/fix.py"

    def test_glob_enrichment(self, mock_resolve, state, captured_spans):
        """glob sets tool.query, tool.file_path, and description."""
        state.set("current_trace_id", "a" * 32)
        state.set("current_trace_span_id", "b" * 16)
        _handle_after_tool(
            {
                "tool_name": "glob",
                "tool_args": {"pattern": "**/*.py", "path": "/src"},
                "tool_result": "matches",
            }
        )
        attrs = _get_span_attrs(captured_spans[0])
        assert attrs["tool.query"]["stringValue"] == "**/*.py"
        assert attrs["tool.file_path"]["stringValue"] == "/src"
        assert attrs["tool.description"]["stringValue"] == "**/*.py"

    def test_search_file_content_enrichment(self, mock_resolve, state, captured_spans):
        """search_file_content sets tool.query and description with grep: prefix."""
        state.set("current_trace_id", "a" * 32)
        state.set("current_trace_span_id", "b" * 16)
        _handle_after_tool(
            {
                "tool_name": "search_file_content",
                "tool_args": {"pattern": "TODO", "path": "/src"},
                "tool_result": "matches",
            }
        )
        attrs = _get_span_attrs(captured_spans[0])
        assert attrs["tool.query"]["stringValue"] == "TODO"
        assert attrs["tool.file_path"]["stringValue"] == "/src"
        assert attrs["tool.description"]["stringValue"].startswith("grep: ")

    def test_grep_enrichment(self, mock_resolve, state, captured_spans):
        """grep (alias) sets tool.query and description with grep: prefix."""
        state.set("current_trace_id", "a" * 32)
        state.set("current_trace_span_id", "b" * 16)
        _handle_after_tool(
            {
                "tool_name": "grep",
                "tool_args": {"pattern": "FIXME", "path": "/lib"},
                "tool_result": "matches",
            }
        )
        attrs = _get_span_attrs(captured_spans[0])
        assert attrs["tool.query"]["stringValue"] == "FIXME"
        assert attrs["tool.description"]["stringValue"].startswith("grep: ")

    def test_web_fetch_enrichment(self, mock_resolve, state, captured_spans):
        """web_fetch sets tool.url and description."""
        state.set("current_trace_id", "a" * 32)
        state.set("current_trace_span_id", "b" * 16)
        _handle_after_tool(
            {
                "tool_name": "web_fetch",
                "tool_args": {"url": "https://example.com"},
                "tool_result": "page content",
            }
        )
        attrs = _get_span_attrs(captured_spans[0])
        assert attrs["tool.url"]["stringValue"] == "https://example.com"
        assert attrs["tool.description"]["stringValue"] == "https://example.com"

    def test_google_web_search_enrichment(self, mock_resolve, state, captured_spans):
        """google_web_search sets tool.query and description."""
        state.set("current_trace_id", "a" * 32)
        state.set("current_trace_span_id", "b" * 16)
        _handle_after_tool(
            {
                "tool_name": "google_web_search",
                "tool_args": {"query": "python async"},
                "tool_result": "results",
            }
        )
        attrs = _get_span_attrs(captured_spans[0])
        assert attrs["tool.query"]["stringValue"] == "python async"
        assert attrs["tool.description"]["stringValue"] == "python async"

    def test_web_search_enrichment(self, mock_resolve, state, captured_spans):
        """web_search (alias) sets tool.query and description."""
        state.set("current_trace_id", "a" * 32)
        state.set("current_trace_span_id", "b" * 16)
        _handle_after_tool(
            {
                "tool_name": "web_search",
                "tool_args": {"query": "rust tutorial"},
                "tool_result": "results",
            }
        )
        attrs = _get_span_attrs(captured_spans[0])
        assert attrs["tool.query"]["stringValue"] == "rust tutorial"

    def test_unknown_tool_description(self, mock_resolve, state, captured_spans):
        """Unknown tool gets description from input_value[:200]."""
        state.set("current_trace_id", "a" * 32)
        state.set("current_trace_span_id", "b" * 16)
        _handle_after_tool(
            {
                "tool_name": "custom_tool",
                "tool_args": {"key": "value"},
                "tool_result": "result",
            }
        )
        attrs = _get_span_attrs(captured_spans[0])
        assert "tool.description" in attrs
        # Should not have tool-specific attributes
        assert "tool.command" not in attrs
        assert "tool.file_path" not in attrs
        assert "tool.url" not in attrs
        assert "tool.query" not in attrs

    # -- Redaction tests --

    def test_redacts_tool_content(self, mock_resolve, state, captured_spans, monkeypatch):
        """tool input and output are redacted when log_tool_content is False."""
        monkeypatch.setenv("ARIZE_LOG_TOOL_CONTENT", "false")
        state.set("current_trace_id", "a" * 32)
        state.set("current_trace_span_id", "b" * 16)
        _handle_after_tool(
            {
                "tool_name": "read_file",
                "tool_args": {"file_path": "/secret.py"},
                "tool_result": "secret file content",
            }
        )
        attrs = _get_span_attrs(captured_spans[0])
        assert "redacted" in attrs["input.value"]["stringValue"]
        assert "redacted" in attrs["output.value"]["stringValue"]

    def test_redacts_tool_details(self, mock_resolve, state, captured_spans, monkeypatch):
        """tool description/command/file_path/url/query redacted when log_tool_details is False."""
        monkeypatch.setenv("ARIZE_LOG_TOOL_DETAILS", "false")
        state.set("current_trace_id", "a" * 32)
        state.set("current_trace_span_id", "b" * 16)
        _handle_after_tool(
            {
                "tool_name": "run_shell_command",
                "tool_args": {"command": "rm -rf /"},
                "tool_result": "output",
            }
        )
        attrs = _get_span_attrs(captured_spans[0])
        assert "redacted" in attrs["tool.description"]["stringValue"]
        assert "redacted" in attrs["tool.command"]["stringValue"]

    def test_tool_name_not_redacted(self, mock_resolve, state, captured_spans, monkeypatch):
        """tool.name is NOT redacted (non-sensitive metadata)."""
        monkeypatch.setenv("ARIZE_LOG_TOOL_DETAILS", "false")
        monkeypatch.setenv("ARIZE_LOG_TOOL_CONTENT", "false")
        state.set("current_trace_id", "a" * 32)
        state.set("current_trace_span_id", "b" * 16)
        _handle_after_tool(
            {
                "tool_name": "read_file",
                "tool_args": {"file_path": "/secret.py"},
                "tool_result": "secret",
            }
        )
        attrs = _get_span_attrs(captured_spans[0])
        assert attrs["tool.name"]["stringValue"] == "read_file"

    def test_conditional_redaction_only_nonempty(self, mock_resolve, state, captured_spans, monkeypatch):
        """Only non-empty tool details are redacted (empty strings are not set)."""
        monkeypatch.setenv("ARIZE_LOG_TOOL_DETAILS", "false")
        state.set("current_trace_id", "a" * 32)
        state.set("current_trace_span_id", "b" * 16)
        _handle_after_tool(
            {
                "tool_name": "read_file",
                "tool_args": {"file_path": "/foo.py"},
                "tool_result": "content",
            }
        )
        attrs = _get_span_attrs(captured_spans[0])
        # tool.file_path should be set and redacted
        assert "tool.file_path" in attrs
        # tool.command should NOT be set (empty for read_file)
        assert "tool.command" not in attrs
        # tool.url should NOT be set
        assert "tool.url" not in attrs

    def test_empty_tool_args(self, mock_resolve, state, captured_spans):
        """Handles None/missing tool_args gracefully."""
        state.set("current_trace_id", "a" * 32)
        state.set("current_trace_span_id", "b" * 16)
        _handle_after_tool(
            {
                "tool_name": "custom_tool",
                "tool_result": "result",
            }
        )
        assert len(captured_spans) == 1
        attrs = _get_span_attrs(captured_spans[0])
        assert attrs["tool.name"]["stringValue"] == "custom_tool"

    def test_description_truncated_to_200(self, mock_resolve, state, captured_spans):
        """tool.description is truncated to 200 chars."""
        state.set("current_trace_id", "a" * 32)
        state.set("current_trace_span_id", "b" * 16)
        long_command = "x" * 300
        _handle_after_tool(
            {
                "tool_name": "run_shell_command",
                "tool_args": {"command": long_command},
                "tool_result": "output",
            }
        )
        attrs = _get_span_attrs(captured_spans[0])
        assert len(attrs["tool.description"]["stringValue"]) <= 200


# ---------------------------------------------------------------------------
# Error handling tests
# ---------------------------------------------------------------------------


class TestErrorHandling:
    def test_entry_point_catches_exception(self, monkeypatch, capsys):
        """Exception in handler -> entry point catches, calls error()."""
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        with (
            mock.patch("tracing.gemini.hooks.handlers._read_stdin", return_value={}),
            mock.patch("tracing.gemini.hooks.handlers.check_requirements", return_value=True),
            mock.patch("tracing.gemini.hooks.handlers._handle_session_start", side_effect=RuntimeError("boom")),
        ):
            session_start()
        captured = capsys.readouterr()
        assert "boom" in captured.err

    def test_malformed_stdin_no_crash(self, monkeypatch):
        """Malformed stdin JSON doesn't crash entry point."""
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        with (
            mock.patch("tracing.gemini.hooks.handlers.check_requirements", return_value=True),
            mock.patch.object(sys, "stdin", new=io.StringIO("not json")),
            mock.patch("tracing.gemini.hooks.handlers.resolve_session") as rs,
            mock.patch("tracing.gemini.hooks.handlers.ensure_session_initialized"),
        ):
            session_start()
        rs.assert_called_once_with({})

    def test_response_printed_on_exception(self, capsys):
        """Response is printed even when handler raises."""
        with (
            mock.patch("tracing.gemini.hooks.handlers._read_stdin", return_value={}),
            mock.patch("tracing.gemini.hooks.handlers.check_requirements", return_value=True),
            mock.patch("tracing.gemini.hooks.handlers._handle_session_start", side_effect=RuntimeError("boom")),
        ):
            session_start()
        out = json.loads(capsys.readouterr().out.strip())
        assert out == {}

    def test_no_sys_exit(self, capsys):
        """Entry points never call sys.exit()."""
        with (
            mock.patch("tracing.gemini.hooks.handlers._read_stdin", return_value={}),
            mock.patch("tracing.gemini.hooks.handlers.check_requirements", return_value=True),
            mock.patch("tracing.gemini.hooks.handlers._handle_session_start", side_effect=RuntimeError("boom")),
        ):
            # This should NOT raise SystemExit
            session_start()


# ---------------------------------------------------------------------------
# Entry point tests (all 8 CLI wrappers)
# ---------------------------------------------------------------------------

ENTRY_POINTS = [
    ("session_start", session_start, "_handle_session_start"),
    ("session_end", session_end, "_handle_session_end"),
    ("before_agent", before_agent, "_handle_before_agent"),
    ("after_agent", after_agent, "_handle_after_agent"),
    ("before_model", before_model, "_handle_before_model"),
    ("after_model", after_model, "_handle_after_model"),
    ("before_tool", before_tool, "_handle_before_tool"),
    ("after_tool", after_tool, "_handle_after_tool"),
]


class TestEntryPoints:
    @pytest.mark.parametrize("name,entry_fn,handler_name", ENTRY_POINTS)
    def test_happy_path_calls_handler(self, name, entry_fn, handler_name):
        """Entry point calls the corresponding _handle_* with parsed stdin JSON."""
        input_data = {"prompt": "test"}
        with (
            mock.patch("tracing.gemini.hooks.handlers.check_requirements", return_value=True),
            mock.patch("tracing.gemini.hooks.handlers._read_stdin", return_value=input_data),
            mock.patch(f"tracing.gemini.hooks.handlers.{handler_name}") as handler_mock,
            mock.patch("tracing.gemini.hooks.handlers._print_response"),
        ):
            entry_fn()
        handler_mock.assert_called_once_with(input_data)

    @pytest.mark.parametrize("name,entry_fn,handler_name", ENTRY_POINTS)
    def test_requirements_not_met_skips_handler(self, name, entry_fn, handler_name):
        """When check_requirements returns False, handler is NOT called but response is still printed."""
        with (
            mock.patch("tracing.gemini.hooks.handlers.check_requirements", return_value=False),
            mock.patch("tracing.gemini.hooks.handlers._read_stdin", return_value={}),
            mock.patch(f"tracing.gemini.hooks.handlers.{handler_name}") as handler_mock,
            mock.patch("tracing.gemini.hooks.handlers._print_response") as pr_mock,
        ):
            entry_fn()
        handler_mock.assert_not_called()
        pr_mock.assert_called_once()

    @pytest.mark.parametrize("name,entry_fn,handler_name", ENTRY_POINTS)
    def test_exception_caught_and_logged(self, name, entry_fn, handler_name, capsys):
        """Handler exception is caught; error is logged to stderr, response still printed."""
        with (
            mock.patch("tracing.gemini.hooks.handlers.check_requirements", return_value=True),
            mock.patch("tracing.gemini.hooks.handlers._read_stdin", return_value={}),
            mock.patch(f"tracing.gemini.hooks.handlers.{handler_name}", side_effect=RuntimeError("test-boom")),
            mock.patch("tracing.gemini.hooks.handlers._print_response") as pr_mock,
        ):
            entry_fn()  # should not raise
        captured = capsys.readouterr()
        assert "test-boom" in captured.err
        pr_mock.assert_called_once()

    @pytest.mark.parametrize("name,entry_fn,handler_name", ENTRY_POINTS)
    def test_always_prints_empty_json_response(self, name, entry_fn, handler_name, capsys):
        """Entry point always prints {} to stdout."""
        with (
            mock.patch("tracing.gemini.hooks.handlers.check_requirements", return_value=True),
            mock.patch("tracing.gemini.hooks.handlers._read_stdin", return_value={}),
            mock.patch(f"tracing.gemini.hooks.handlers.{handler_name}"),
        ):
            entry_fn()
        out = json.loads(capsys.readouterr().out.strip())
        assert out == {}


# ---------------------------------------------------------------------------
# Integration: before_agent + after_agent flow
# ---------------------------------------------------------------------------


class TestTurnFlow:
    def test_before_after_agent_produces_chain_span(self, mock_resolve, state, captured_spans):
        """BeforeAgent followed by AfterAgent produces a CHAIN span."""
        _handle_before_agent({"messages": [{"role": "user", "content": "explain X"}]})
        trace_id = state.get("current_trace_id")
        span_id = state.get("current_trace_span_id")
        assert trace_id is not None
        assert span_id is not None

        _handle_after_agent({"response": {"content": "X is..."}})
        assert len(captured_spans) == 1
        attrs = _get_span_attrs(captured_spans[0])
        assert attrs["openinference.span.kind"]["stringValue"] == "CHAIN"
        span = _get_span(captured_spans[0])
        assert span["traceId"] == trace_id
        assert span["spanId"] == span_id

    def test_model_span_nested_in_turn(self, mock_resolve, state, captured_spans):
        """BeforeModel/AfterModel within a turn produces a child LLM span."""
        # Start turn
        _handle_before_agent({"messages": [{"role": "user", "content": "test"}]})
        trace_id = state.get("current_trace_id")
        parent_span_id = state.get("current_trace_span_id")

        # Model call within turn
        _handle_before_model({"model_call_id": "mc-1", "messages": [{"role": "user", "content": "test"}]})
        _handle_after_model(
            {
                "model": "gemini-2.5-pro",
                "response": {"content": "answer", "usage": {"prompt_tokens": 5, "candidates_tokens": 3}},
                "model_call_id": "mc-1",
            }
        )
        assert len(captured_spans) == 1
        span = _get_span(captured_spans[0])
        assert span["traceId"] == trace_id
        assert span["parentSpanId"] == parent_span_id

    def test_tool_span_nested_in_turn(self, mock_resolve, state, captured_spans):
        """BeforeTool/AfterTool within a turn produces a child TOOL span."""
        # Start turn
        _handle_before_agent({"messages": [{"role": "user", "content": "test"}]})
        trace_id = state.get("current_trace_id")
        parent_span_id = state.get("current_trace_span_id")

        # Tool call within turn
        _handle_before_tool({"tool_call_id": "tc-1", "tool_name": "read_file"})
        _handle_after_tool(
            {
                "tool_name": "read_file",
                "tool_call_id": "tc-1",
                "tool_args": {"file_path": "/foo.py"},
                "tool_result": "content",
            }
        )
        assert len(captured_spans) == 1
        span = _get_span(captured_spans[0])
        assert span["traceId"] == trace_id
        assert span["parentSpanId"] == parent_span_id


# ---------------------------------------------------------------------------
# project.name attribute tests
# ---------------------------------------------------------------------------


class TestProjectNameOnAllSpans:
    def test_chain_span_has_project_name(self, mock_resolve, state, captured_spans):
        """CHAIN spans include project.name."""
        state.set("current_trace_id", "a" * 32)
        state.set("current_trace_span_id", "b" * 16)
        state.set("current_trace_start_time", "1000")
        state.set("current_trace_prompt", "test")
        _handle_after_agent({"response": {"content": "ok"}})
        attrs = _get_span_attrs(captured_spans[0])
        assert attrs["project.name"]["stringValue"] == "test-gemini-project"

    def test_llm_span_has_project_name(self, mock_resolve, state, captured_spans):
        """LLM spans include project.name."""
        state.set("current_trace_id", "a" * 32)
        state.set("current_trace_span_id", "b" * 16)
        state.set("current_model_call_id", "mc-1")
        state.set("model_mc-1_start", "1000")
        _handle_after_model(_final_chunk({"model": "gemini-2.5-pro", "model_call_id": "mc-1"}))
        attrs = _get_span_attrs(captured_spans[0])
        assert attrs["project.name"]["stringValue"] == "test-gemini-project"

    def test_tool_span_has_project_name(self, mock_resolve, state, captured_spans):
        """TOOL spans include project.name."""
        state.set("current_trace_id", "a" * 32)
        state.set("current_trace_span_id", "b" * 16)
        _handle_after_tool(
            {
                "tool_name": "read_file",
                "tool_args": {"file_path": "/foo.py"},
                "tool_result": "content",
            }
        )
        attrs = _get_span_attrs(captured_spans[0])
        assert attrs["project.name"]["stringValue"] == "test-gemini-project"


# ---------------------------------------------------------------------------
# Integration tests: session initialization via actual adapter (not mocked)
# ---------------------------------------------------------------------------


class TestSessionStartIntegration:
    """Integration tests that exercise the real adapter without mocking resolve_session."""

    @pytest.fixture
    def gemini_state_dir(self, tmp_harness_dir, monkeypatch):
        """Point adapter STATE_DIR to a temp directory."""
        from tracing.gemini.hooks import adapter as _adapter

        state_dir = tmp_harness_dir / "state" / "gemini"
        state_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(_adapter, "STATE_DIR", state_dir)
        return state_dir

    @pytest.fixture
    def captured_spans_real(self):
        """Mock _send_span_async and collect all payloads emitted by handlers."""
        sent = []
        with mock.patch("tracing.gemini.hooks.handlers._send_span_async", side_effect=lambda s: sent.append(s)):
            yield sent

    def test_session_start_initializes_state(self, tmp_harness_dir, gemini_state_dir, monkeypatch, captured_spans_real):
        """Feed session_id/cwd payload to session_start. State file exists with correct keys."""
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        monkeypatch.delenv("GEMINI_SESSION_ID", raising=False)
        monkeypatch.delenv("ARIZE_PROJECT_NAME", raising=False)
        monkeypatch.delenv("ARIZE_USER_ID", raising=False)

        _handle_session_start({"session_id": "sess-123", "cwd": "/tmp/proj"})

        # State file is keyed by the payload session_id (resolve_session key).
        state_file = gemini_state_dir / "state_sess-123.json"
        assert state_file.exists()

        data = json.loads(state_file.read_text())
        # session.id reuses the payload session_id so Arize spans correlate
        # back to the same Gemini session.
        assert data["session_id"] == "sess-123"
        assert data["trace_count"] == "0"
        assert data["project_name"] == "proj"

    def test_session_id_from_env_when_payload_missing(
        self, tmp_harness_dir, gemini_state_dir, monkeypatch, captured_spans_real
    ):
        """GEMINI_SESSION_ID env is used when payload has no session_id."""
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        monkeypatch.setenv("GEMINI_SESSION_ID", "env-sid")
        monkeypatch.delenv("ARIZE_PROJECT_NAME", raising=False)
        monkeypatch.delenv("ARIZE_USER_ID", raising=False)

        _handle_session_start({})

        state_file = gemini_state_dir / "state_env-sid.json"
        assert state_file.exists()

    def test_pid_fallback_when_both_missing(self, tmp_harness_dir, gemini_state_dir, monkeypatch, captured_spans_real):
        """When no env var and no payload session_id, the resolve key falls back
        to the grandparent PID (a positive-integer string)."""
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        monkeypatch.delenv("GEMINI_SESSION_ID", raising=False)
        monkeypatch.delenv("ARIZE_PROJECT_NAME", raising=False)
        monkeypatch.delenv("ARIZE_USER_ID", raising=False)

        _handle_session_start({})

        state_files = list(gemini_state_dir.glob("state_*.json"))
        assert len(state_files) == 1
        key = state_files[0].stem.replace("state_", "", 1)
        assert key.isdigit()
        assert int(key) > 0


# ---------------------------------------------------------------------------
# Pure-function helper coverage
# ---------------------------------------------------------------------------


class TestExtractTextEdgeCases:
    def test_returns_empty_for_none(self):
        assert _extract_text(None) == ""

    def test_returndisplay_used_when_no_llmcontent(self):
        """returnDisplay is the UI fallback when llmContent is absent."""
        assert _extract_text({"returnDisplay": "ui only"}) == "ui only"

    def test_unknown_dict_returns_str_repr(self):
        """Unknown shapes fall through to str(obj) (last-resort behavior)."""
        out = _extract_text({"weird": "shape"})
        assert "weird" in out

    def test_list_joins_with_newline(self):
        assert _extract_text(["a", "b", "c"]) == "a\nb\nc"


class TestExtractTokensEdgeCases:
    def test_non_numeric_tokens_default_to_zero(self):
        """Non-int token values fall back to (0, 0) rather than raising."""
        payload = {
            "llm_response": {
                "usageMetadata": {
                    "promptTokenCount": "not-a-number",
                    "candidatesTokenCount": [1, 2],
                }
            }
        }
        assert _extract_tokens(payload) == (0, 0)

    def test_pulls_from_top_level_usage_metadata(self):
        """Usage can also live at the top level of the payload."""
        payload = {"usage_metadata": {"prompt_token_count": 7, "candidates_token_count": 3}}
        assert _extract_tokens(payload) == (7, 3)


# ---------------------------------------------------------------------------
# _send_span_async fallback paths
# ---------------------------------------------------------------------------


class TestSendSpanAsync:
    def test_disable_fork_env_uses_sync_send(self, monkeypatch):
        """ARIZE_DISABLE_FORK=true short-circuits to synchronous send_span."""
        monkeypatch.setenv("ARIZE_DISABLE_FORK", "true")
        with mock.patch("tracing.gemini.hooks.handlers.send_span") as send_mock:
            _send_span_async({"x": 1})
        send_mock.assert_called_once_with({"x": 1})

    def test_no_fork_attr_uses_sync_send(self, monkeypatch):
        """If os.fork is absent (Windows-like), fall back to sync send."""
        monkeypatch.setenv("ARIZE_DISABLE_FORK", "false")
        # Simulate Windows by removing os.fork from the module's view.
        import tracing.gemini.hooks.handlers as h

        real_fork = getattr(h.os, "fork", None)
        try:
            if real_fork is not None:
                monkeypatch.delattr(h.os, "fork", raising=False)
            with mock.patch("tracing.gemini.hooks.handlers.send_span") as send_mock:
                _send_span_async({"x": 2})
            send_mock.assert_called_once_with({"x": 2})
        finally:
            # monkeypatch.delattr restores automatically at teardown
            pass

    def test_fork_oserror_uses_sync_send(self, monkeypatch):
        """If os.fork() itself raises OSError, fall back to sync send."""
        monkeypatch.setenv("ARIZE_DISABLE_FORK", "false")

        def boom():
            raise OSError("EAGAIN")

        with (
            mock.patch("tracing.gemini.hooks.handlers.os.fork", side_effect=boom),
            mock.patch("tracing.gemini.hooks.handlers.send_span") as send_mock,
        ):
            _send_span_async({"x": 3})
        send_mock.assert_called_once_with({"x": 3})


# ---------------------------------------------------------------------------
# _flush_pending_model_call edge cases
# ---------------------------------------------------------------------------


class TestFlushPendingModelCall:
    def test_no_current_model_call_id_is_noop(self, tmp_path):
        """No-op when state has no current_model_call_id."""
        sm = StateManager(state_dir=tmp_path, state_file=tmp_path / "s.json", lock_path=tmp_path / ".l")
        sm.init_state()
        with mock.patch("tracing.gemini.hooks.handlers._send_span_async") as send_mock:
            _flush_pending_model_call(sm)
        send_mock.assert_not_called()

    def test_no_active_trace_drops_accumulators_silently(self, tmp_path):
        """If a model call is pending but no trace is active, accumulators are
        cleared without emitting a dangling LLM span."""
        sm = StateManager(state_dir=tmp_path, state_file=tmp_path / "s.json", lock_path=tmp_path / ".l")
        sm.init_state()
        sm.set("current_model_call_id", "orphan")
        sm.set("model_orphan_response", "partial")
        with mock.patch("tracing.gemini.hooks.handlers._send_span_async") as send_mock:
            _flush_pending_model_call(sm)
        send_mock.assert_not_called()
        assert sm.get("model_orphan_response") is None
        assert sm.get("current_model_call_id") is None


# ---------------------------------------------------------------------------
# Edge cases in main handlers
# ---------------------------------------------------------------------------


class TestSessionEndEdgeCases:
    def test_no_session_id_returns_silently(self, tmp_path, monkeypatch):
        """SessionEnd with an uninitialized state returns without raising or
        calling gc (covers the early-return guard)."""
        sm = StateManager(state_dir=tmp_path, state_file=tmp_path / "s.json", lock_path=tmp_path / ".l")
        sm.init_state()
        with (
            mock.patch("tracing.gemini.hooks.handlers.resolve_session", return_value=sm),
            mock.patch("tracing.gemini.hooks.handlers.gc_stale_state_files") as gc_mock,
        ):
            _handle_session_end({})
        gc_mock.assert_not_called()

    def test_lock_file_unlinked_on_session_end(self, tmp_path):
        """A regular file at lock_path is unlinked during session_end cleanup."""
        sf = tmp_path / "state_x.json"
        lp = tmp_path / ".lock_x"
        # Write state directly so we don't acquire any lock that would race
        # with the file-vs-dir we install at lock_path below.
        sf.write_text(json.dumps({"session_id": "x"}, indent=2))
        lp.write_text("")  # fcntl-style file lock
        sm = StateManager(state_dir=tmp_path, state_file=sf, lock_path=lp)
        with (
            mock.patch("tracing.gemini.hooks.handlers.resolve_session", return_value=sm),
            mock.patch("tracing.gemini.hooks.handlers.log"),
            mock.patch("tracing.gemini.hooks.handlers.gc_stale_state_files"),
        ):
            _handle_session_end({})
        assert not lp.exists()

    def test_lock_dir_rmdir_on_session_end(self, tmp_path):
        """A directory at lock_path is rmdir'd during session_end cleanup."""
        sf = tmp_path / "state_y.json"
        lp = tmp_path / ".lock_y"
        sf.write_text(json.dumps({"session_id": "y"}, indent=2))
        lp.mkdir()  # mkdir-fallback lock
        sm = StateManager(state_dir=tmp_path, state_file=sf, lock_path=lp)
        with (
            mock.patch("tracing.gemini.hooks.handlers.resolve_session", return_value=sm),
            mock.patch("tracing.gemini.hooks.handlers.log"),
            mock.patch("tracing.gemini.hooks.handlers.gc_stale_state_files"),
        ):
            _handle_session_end({})
        assert not lp.exists()


class TestAfterAgentEdgeCases:
    def test_no_trace_state_returns_without_sending(self, mock_resolve, state, captured_spans):
        """AfterAgent without an active turn returns silently (covers the guard)."""
        # Ensure no current_trace_id is set
        assert state.get("current_trace_id") is None
        _handle_after_agent({"prompt_response": "ok"})
        assert captured_spans == []


class TestAfterModelEdgeCases:
    def test_orphan_chunk_creates_accumulator(self, mock_resolve, state, captured_spans):
        """AfterModel with no preceding BeforeModel synthesizes an accumulator
        so the chunk's content isn't lost."""
        state.set("current_trace_id", "a" * 32)
        state.set("current_trace_span_id", "b" * 16)
        # No current_model_call_id set
        _handle_after_model({"llm_response": {"text": "stray"}})
        # Span isn't emitted yet (no tokens), but accumulator state exists
        new_id = state.get("current_model_call_id")
        assert new_id is not None
        assert state.get(f"model_{new_id}_response") == "stray"


class TestAfterToolEdgeCases:
    def test_non_dict_tool_args_uses_str_description(self, mock_resolve, state, captured_spans):
        """Non-dict tool_args are stringified into the tool description."""
        state.set("current_trace_id", "a" * 32)
        state.set("current_trace_span_id", "b" * 16)
        _handle_after_tool(
            {
                "tool_name": "raw_string_tool",
                "tool_input": "not-a-dict-arg",
                "tool_response": "result",
            }
        )
        attrs = _get_span_attrs(captured_spans[0])
        assert attrs["tool.description"]["stringValue"] == "not-a-dict-arg"


# ---------------------------------------------------------------------------
# main() dispatcher
# ---------------------------------------------------------------------------


class TestMainDispatcher:
    def test_no_args_prints_usage_and_exits(self, capsys, monkeypatch):
        """main() with no handler argument prints usage and exits with code 1."""
        monkeypatch.setattr(sys, "argv", ["arize-hook"])
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "usage" in err.lower()

    def test_unknown_handler_exits_with_error(self, capsys, monkeypatch):
        """main() rejects an unknown handler name with exit code 1."""
        monkeypatch.setattr(sys, "argv", ["arize-hook", "not_a_real_handler"])
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "unknown handler" in err.lower()

    def test_dispatches_to_named_handler(self, monkeypatch):
        """main() routes argv[1] to the matching entry-point function."""
        monkeypatch.setattr(sys, "argv", ["arize-hook", "session_start"])
        with mock.patch.object(handlers_mod, "session_start") as ss_mock:
            main()
        ss_mock.assert_called_once()
