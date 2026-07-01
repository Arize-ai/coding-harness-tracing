#!/usr/bin/env python3
"""Tests for tracing.copilot.hooks.handlers — Copilot hook handlers.

Tests cover response printing, session/prompt/tool/stop handlers,
and all CLI entry points.
"""

import io
import json
import sys
from unittest import mock

import pytest

from core.common import StateManager
from tracing.copilot.hooks.handlers import (
    _handle_post_tool_use,
    _handle_pre_tool_use,
    _handle_session_start,
    _handle_subagent_stop,
    _handle_user_prompt_submitted,
    _print_response,
    _read_stdin,
    post_tool_use,
    pre_tool_use,
    session_start,
    stop,
    subagent_stop,
    user_prompt_submitted,
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
    sm.set("session_id", "test-session-copilot")
    sm.set("project_name", "test-copilot-project")
    sm.set("trace_count", "0")
    sm.set("tool_count", "0")
    sm.set("user_id", "test-user")
    return sm


@pytest.fixture
def mock_resolve(state):
    """Mock resolve_session to return the test state fixture."""
    with mock.patch("tracing.copilot.hooks.handlers.resolve_session", return_value=state) as m:
        yield m


@pytest.fixture
def mock_ensure():
    """Mock ensure_session_initialized."""
    with mock.patch("tracing.copilot.hooks.handlers.ensure_session_initialized") as m:
        yield m


@pytest.fixture
def captured_spans():
    """Mock send_span and collect all payloads sent."""
    sent = []
    with mock.patch("tracing.copilot.hooks.handlers.send_span", side_effect=lambda s: sent.append(s)):
        yield sent


@pytest.fixture
def transcript_file(tmp_path):
    """Write a sample transcript to a temp file and return its path."""
    lines = [
        '{"type": "user", "message": {"role": "user", "content": "fix the bug"}}',
        '{"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "I found the issue."}], "model": "gpt-4o", "usage": {"input_tokens": 100, "output_tokens": 50, "cache_read_input_tokens": 10, "cache_creation_input_tokens": 5}}}',
    ]
    tf = tmp_path / "transcript.jsonl"
    tf.write_text("\n".join(lines) + "\n")
    return str(tf)


# ---------------------------------------------------------------------------
# _read_stdin tests
# ---------------------------------------------------------------------------


class TestReadStdin:

    def test_empty_stdin(self):
        with mock.patch.object(sys, "stdin", new=io.StringIO("")):
            assert _read_stdin("test") == {}

    def test_malformed_json(self):
        with mock.patch.object(sys, "stdin", new=io.StringIO("not json")):
            assert _read_stdin("test") == {}

    def test_valid_json(self):
        with mock.patch.object(sys, "stdin", new=io.StringIO('{"key": "val"}')):
            assert _read_stdin("test") == {"key": "val"}


# ---------------------------------------------------------------------------
# _print_response tests
# ---------------------------------------------------------------------------


class TestPrintResponse:

    def test_pre_tool_use_emits_permission_decision(self, capsys):
        _print_response("PreToolUse")
        out = capsys.readouterr().out.strip()
        payload = json.loads(out)
        assert payload == {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
            }
        }

    @pytest.mark.parametrize(
        "event",
        [
            "SessionStart",
            "UserPromptSubmit",
            "PostToolUse",
            "Stop",
            "SubagentStop",
        ],
    )
    def test_non_pre_tool_use_emits_continue(self, event, capsys):
        _print_response(event)
        out = capsys.readouterr().out.strip()
        payload = json.loads(out)
        assert payload == {"continue": True}


# ---------------------------------------------------------------------------
# session_start tests
# ---------------------------------------------------------------------------


class TestSessionStart:

    def test_initializes_state_from_snake_case_payload(self, tmp_path, monkeypatch):
        from tracing.copilot.hooks import adapter as _adapter

        monkeypatch.setattr(_adapter, "STATE_DIR", tmp_path)
        monkeypatch.delenv("ARIZE_PROJECT_NAME", raising=False)

        payload = {
            "cwd": "/some/repo",
            "hook_event_name": "SessionStart",
            "session_id": "sess-123",
            "initial_prompt": "kick off",
            "source": "new",
            "timestamp": "2026-05-04T00:00:00Z",
        }
        _handle_session_start(payload)

        from tracing.copilot.hooks.adapter import resolve_session

        state = resolve_session(payload)
        assert state.get("session_id") == "sess-123"
        assert state.get("project_name") == "repo"
        assert state.get("trace_count") == "0"


# ---------------------------------------------------------------------------
# user_prompt_submitted tests
# ---------------------------------------------------------------------------


class TestUserPromptSubmitted:

    def test_creates_trace_root(self, tmp_path, monkeypatch):
        from tracing.copilot.hooks import adapter as _adapter

        monkeypatch.setattr(_adapter, "STATE_DIR", tmp_path)

        payload = {
            "cwd": "/some/repo",
            "hook_event_name": "UserPromptSubmit",
            "session_id": "sess-abc",
            "prompt": "what is the capital of France?",
            "timestamp": "2026-05-04T00:00:00Z",
        }
        _handle_user_prompt_submitted(payload)

        from tracing.copilot.hooks.adapter import resolve_session

        state = resolve_session(payload)
        assert state.get("current_trace_id") not in (None, "")
        assert state.get("current_trace_span_id") not in (None, "")
        assert state.get("current_trace_prompt") == "what is the capital of France?"
        assert state.get("trace_count") == "1"
        assert state.get("tool_count") == "0"

    def test_returns_early_without_session_id(self, mock_resolve, mock_ensure, state, captured_spans):
        """Returns early when session_id is None."""
        state.delete("session_id")
        inp = {"cwd": "/tmp/project", "hook_event_name": "UserPromptSubmit", "prompt": "hello"}
        _handle_user_prompt_submitted(inp)
        assert state.get("current_trace_id") is None


# ---------------------------------------------------------------------------
# pre_tool_use tests
# ---------------------------------------------------------------------------


class TestPreToolUse:

    def test_records_tool_start_by_tool_use_id(self, mock_resolve, state):
        """Records tool_{tool_use_id}_start when tool_use_id is present."""
        inp = {
            "cwd": "/repo",
            "hook_event_name": "PreToolUse",
            "session_id": "sess-1",
            "tool_use_id": "tool-42",
            "tool_name": "bash",
            "tool_input": {"command": "ls"},
        }
        _handle_pre_tool_use(inp)
        val = state.get("tool_tool-42_start")
        assert val is not None
        assert int(val) > 0

    def test_records_tool_start_by_tool_name(self, mock_resolve, state):
        """Falls back to tool_name when tool_use_id is absent."""
        inp = {
            "cwd": "/repo",
            "hook_event_name": "PreToolUse",
            "session_id": "sess-1",
            "tool_name": "bash",
            "tool_input": {"command": "ls"},
        }
        _handle_pre_tool_use(inp)
        val = state.get("tool_bash_start")
        assert val is not None
        assert int(val) > 0

    def test_missing_tool_id_generates_fallback(self, mock_resolve, state):
        """Missing tool_use_id and tool_name generates a fallback ID."""
        with mock.patch("tracing.copilot.hooks.handlers.generate_trace_id", return_value="gen-id-123"):
            inp = {"cwd": "/repo", "hook_event_name": "PreToolUse", "session_id": "sess-1"}
            _handle_pre_tool_use(inp)
        val = state.get("tool_gen-id-123_start")
        assert val is not None


# ---------------------------------------------------------------------------
# post_tool_use tests
# ---------------------------------------------------------------------------


class TestPostToolUse:

    def test_emits_bash_tool_span(self, mock_resolve, state, captured_spans):
        """Builds a TOOL span for bash with command enrichment."""
        state.set("current_trace_id", "trace-abc")
        state.set("current_trace_span_id", "span-parent")
        inp = {
            "session_id": "sess-1",
            "hook_event_name": "PostToolUse",
            "cwd": "/repo",
            "tool_name": "bash",
            "tool_input": {"command": "ls -la", "description": "list"},
            "tool_result": {"result_type": "success", "text_result_for_llm": "out"},
        }
        _handle_post_tool_use(inp)
        assert len(captured_spans) == 1
        attrs = _get_span_attrs(captured_spans[0])
        assert attrs["openinference.span.kind"]["stringValue"] == "TOOL"
        assert attrs["tool.name"]["stringValue"] == "bash"
        assert "ls -la" in attrs["input.value"]["stringValue"]
        assert attrs["output.value"]["stringValue"] == "out"
        assert attrs["tool.command"]["stringValue"] == "ls -la"
        assert attrs["tool.description"]["stringValue"] == "ls -la"
        assert attrs["tool.result_type"]["stringValue"] == "success"

    def test_emits_span_for_unknown_tool(self, mock_resolve, state, captured_spans):
        """Builds a TOOL span for an unrecognized tool name."""
        state.set("current_trace_id", "trace-abc")
        state.set("current_trace_span_id", "span-parent")
        inp = {
            "session_id": "sess-1",
            "hook_event_name": "PostToolUse",
            "cwd": "/repo",
            "tool_name": "report_intent",
            "tool_input": {"intent": "checking copilot"},
            "tool_result": {"result_type": "success", "text_result_for_llm": "ack"},
        }
        _handle_post_tool_use(inp)
        assert len(captured_spans) == 1
        attrs = _get_span_attrs(captured_spans[0])
        assert attrs["tool.name"]["stringValue"] == "report_intent"
        assert attrs["output.value"]["stringValue"] == "ack"
        assert attrs["tool.description"]["stringValue"]  # non-empty

    def test_extracts_text_result_for_llm(self, mock_resolve, state, captured_spans):
        """Extracts text_result_for_llm from tool_result."""
        state.set("current_trace_id", "trace-abc")
        state.set("current_trace_span_id", "span-parent")
        inp = {
            "session_id": "sess-1",
            "hook_event_name": "PostToolUse",
            "cwd": "/repo",
            "tool_name": "read",
            "tool_input": {"file_path": "/foo.py"},
            "tool_result": {"result_type": "success", "text_result_for_llm": "file contents here"},
        }
        _handle_post_tool_use(inp)
        attrs = _get_span_attrs(captured_spans[0])
        assert attrs["output.value"]["stringValue"] == "file contents here"

    def test_result_type_attribute(self, mock_resolve, state, captured_spans):
        """Sets tool.result_type from tool_result.result_type."""
        state.set("current_trace_id", "trace-abc")
        state.set("current_trace_span_id", "span-parent")
        inp = {
            "session_id": "sess-1",
            "hook_event_name": "PostToolUse",
            "cwd": "/repo",
            "tool_name": "edit",
            "tool_input": {},
            "tool_result": {"result_type": "failure", "text_result_for_llm": "error"},
        }
        _handle_post_tool_use(inp)
        attrs = _get_span_attrs(captured_spans[0])
        assert attrs["tool.result_type"]["stringValue"] == "failure"

    def test_no_result_type_when_empty(self, mock_resolve, state, captured_spans):
        """Does not set tool.result_type when result_type is empty."""
        state.set("current_trace_id", "trace-abc")
        state.set("current_trace_span_id", "span-parent")
        inp = {
            "session_id": "sess-1",
            "hook_event_name": "PostToolUse",
            "cwd": "/repo",
            "tool_name": "bash",
            "tool_input": {"command": "echo hi"},
            "tool_result": {"text_result_for_llm": "hi"},
        }
        _handle_post_tool_use(inp)
        attrs = _get_span_attrs(captured_spans[0])
        assert "tool.result_type" not in attrs

    def test_case_insensitive_bash_enrichment(self, mock_resolve, state, captured_spans):
        """Bash tool enrichment works regardless of tool name casing."""
        state.set("current_trace_id", "trace-abc")
        state.set("current_trace_span_id", "span-parent")
        inp = {
            "session_id": "sess-1",
            "hook_event_name": "PostToolUse",
            "cwd": "/repo",
            "tool_name": "Bash",
            "tool_input": {"command": "git status"},
            "tool_result": {"result_type": "success", "text_result_for_llm": "clean"},
        }
        _handle_post_tool_use(inp)
        attrs = _get_span_attrs(captured_spans[0])
        assert attrs["tool.command"]["stringValue"] == "git status"
        assert attrs["tool.description"]["stringValue"] == "git status"

    def test_no_session_id_returns_early(self, state, captured_spans):
        """If session_id is None, returns without sending span."""
        state.delete("session_id")
        with mock.patch("tracing.copilot.hooks.handlers.resolve_session", return_value=state):
            _handle_post_tool_use({"tool_name": "bash", "tool_input": {"command": "ls"}})
        assert len(captured_spans) == 0

    def test_uses_pre_tool_start_time(self, mock_resolve, state, captured_spans):
        """Timing uses pre_tool_use start time if available in state."""
        state.set("current_trace_id", "trace-abc")
        state.set("current_trace_span_id", "span-parent")
        state.set("tool_read_start", "1000000")
        inp = {
            "session_id": "sess-1",
            "hook_event_name": "PostToolUse",
            "cwd": "/repo",
            "tool_name": "read",
            "tool_input": {"file_path": "/a.py"},
            "tool_result": {"text_result_for_llm": "content"},
        }
        _handle_post_tool_use(inp)
        span = _get_span(captured_spans[0])
        assert span["startTimeUnixNano"] == "1000000000000"
        assert state.get("tool_read_start") is None

    def test_grep_tool_enrichment(self, mock_resolve, state, captured_spans):
        """Grep tool sets query, file_path, and description."""
        state.set("current_trace_id", "trace-abc")
        state.set("current_trace_span_id", "span-parent")
        inp = {
            "session_id": "sess-1",
            "hook_event_name": "PostToolUse",
            "cwd": "/repo",
            "tool_name": "grep",
            "tool_input": {"pattern": "TODO", "path": "/src"},
            "tool_result": {"text_result_for_llm": "matches"},
        }
        _handle_post_tool_use(inp)
        attrs = _get_span_attrs(captured_spans[0])
        assert attrs["tool.query"]["stringValue"] == "TODO"
        assert attrs["tool.file_path"]["stringValue"] == "/src"
        assert attrs["tool.description"]["stringValue"].startswith("grep: ")

    def test_webfetch_tool_enrichment(self, mock_resolve, state, captured_spans):
        """WebFetch tool sets url (case-insensitive match)."""
        state.set("current_trace_id", "trace-abc")
        state.set("current_trace_span_id", "span-parent")
        inp = {
            "session_id": "sess-1",
            "hook_event_name": "PostToolUse",
            "cwd": "/repo",
            "tool_name": "WebFetch",
            "tool_input": {"url": "https://example.com"},
            "tool_result": {"text_result_for_llm": "page"},
        }
        _handle_post_tool_use(inp)
        attrs = _get_span_attrs(captured_spans[0])
        assert attrs["tool.url"]["stringValue"] == "https://example.com"

    def test_read_tool_file_path_enrichment(self, mock_resolve, state, captured_spans):
        """Read tool sets file_path and description."""
        state.set("current_trace_id", "trace-abc")
        state.set("current_trace_span_id", "span-parent")
        inp = {
            "session_id": "sess-1",
            "hook_event_name": "PostToolUse",
            "cwd": "/repo",
            "tool_name": "Read",
            "tool_input": {"file_path": "/foo/bar.py"},
            "tool_result": {"result_type": "success", "text_result_for_llm": "file content"},
        }
        _handle_post_tool_use(inp)
        attrs = _get_span_attrs(captured_spans[0])
        assert attrs["tool.name"]["stringValue"] == "Read"
        assert attrs["tool.file_path"]["stringValue"] == "/foo/bar.py"
        assert attrs["output.value"]["stringValue"] == "file content"


# ---------------------------------------------------------------------------
# stop tests
# ---------------------------------------------------------------------------


class TestHandleStop:

    def _seed_trace(self, tmp_path, monkeypatch, prompt="hi"):
        from tracing.copilot.hooks import adapter as _adapter

        monkeypatch.setattr(_adapter, "STATE_DIR", tmp_path)
        from tracing.copilot.hooks.handlers import _handle_user_prompt_submitted

        _handle_user_prompt_submitted(
            {
                "session_id": "sess-1",
                "hook_event_name": "UserPromptSubmit",
                "cwd": "/repo",
                "prompt": prompt,
            }
        )

    def test_emits_llm_span_with_model_from_transcript(self, tmp_path, monkeypatch):
        self._seed_trace(tmp_path, monkeypatch, prompt="why?")
        tpath = tmp_path / "events.jsonl"
        tpath.write_text(
            json.dumps({"type": "session.model_change", "data": {"newModel": "gpt-5-mini"}}) + "\n",
            encoding="utf-8",
        )

        sent = []
        from tracing.copilot.hooks import handlers as _handlers

        monkeypatch.setattr(_handlers, "send_span", lambda s: sent.append(s))

        _handlers._handle_stop(
            {
                "session_id": "sess-1",
                "hook_event_name": "Stop",
                "cwd": "/repo",
                "stop_reason": "end_turn",
                "transcript_path": str(tpath),
            }
        )

        assert len(sent) == 1
        attrs = _get_span_attrs(sent[0])
        assert attrs["llm.model_name"]["stringValue"] == "gpt-5-mini"
        assert attrs["input.value"]["stringValue"] == "why?"
        meta = json.loads(attrs["metadata"]["stringValue"])
        assert meta["stop_reason"] == "end_turn"

    def test_emits_llm_span_without_transcript(self, tmp_path, monkeypatch):
        self._seed_trace(tmp_path, monkeypatch, prompt="ping")
        sent = []
        from tracing.copilot.hooks import handlers as _handlers

        monkeypatch.setattr(_handlers, "send_span", lambda s: sent.append(s))

        _handlers._handle_stop(
            {
                "session_id": "sess-1",
                "hook_event_name": "Stop",
                "cwd": "/repo",
                "stop_reason": "end_turn",
            }
        )

        assert len(sent) == 1
        attrs = _get_span_attrs(sent[0])
        assert attrs["input.value"]["stringValue"] == "ping"
        assert "llm.model_name" not in attrs

    def test_clears_trace_state_after_emit(self, tmp_path, monkeypatch):
        self._seed_trace(tmp_path, monkeypatch)
        from tracing.copilot.hooks import handlers as _handlers

        monkeypatch.setattr(_handlers, "send_span", lambda s: None)
        from tracing.copilot.hooks.adapter import resolve_session

        payload = {
            "session_id": "sess-1",
            "hook_event_name": "Stop",
            "cwd": "/repo",
            "stop_reason": "end_turn",
        }
        _handlers._handle_stop(payload)

        state = resolve_session(payload)
        assert state.get("current_trace_id") is None
        assert state.get("current_trace_span_id") is None
        assert state.get("current_trace_prompt") is None


# ---------------------------------------------------------------------------
# subagent_stop tests
# ---------------------------------------------------------------------------


class TestHandleSubagentStop:

    def test_emits_chain_span_with_agent_metadata(self, tmp_path, monkeypatch):
        from tracing.copilot.hooks import adapter as _adapter

        monkeypatch.setattr(_adapter, "STATE_DIR", tmp_path)
        from tracing.copilot.hooks.handlers import _handle_user_prompt_submitted

        _handle_user_prompt_submitted(
            {
                "session_id": "sess-X",
                "hook_event_name": "UserPromptSubmit",
                "cwd": "/repo",
                "prompt": "p",
            }
        )

        sent = []
        from tracing.copilot.hooks import handlers as _handlers

        monkeypatch.setattr(_handlers, "send_span", lambda s: sent.append(s))

        _handlers._handle_subagent_stop(
            {
                "session_id": "sess-X",
                "hook_event_name": "SubagentStop",
                "cwd": "/repo",
                "agent_id": "ag-7",
                "agent_type": "research",
            }
        )

        assert len(sent) == 1
        attrs = _get_span_attrs(sent[0])
        assert attrs["openinference.span.kind"]["stringValue"] == "CHAIN"
        meta = json.loads(attrs["metadata"]["stringValue"])
        assert meta["agent_id"] == "ag-7"
        assert meta["agent_type"] == "research"


# ---------------------------------------------------------------------------
# Consecutive prompts open fresh traces
# ---------------------------------------------------------------------------


class TestConsecutivePrompts:

    def test_consecutive_prompts_each_open_fresh_trace(self, mock_resolve, mock_ensure, state, captured_spans):
        """Each user_prompt_submitted opens a fresh trace without sending spans."""
        _handle_user_prompt_submitted({"cwd": "/tmp/project", "prompt": "first"})
        first_trace = state.get("current_trace_id")
        _handle_user_prompt_submitted({"cwd": "/tmp/project", "prompt": "second"})
        second_trace = state.get("current_trace_id")
        assert first_trace != second_trace
        assert state.get("current_trace_prompt") == "second"
        assert state.get("trace_count") == "2"
        assert len(captured_spans) == 0


# ---------------------------------------------------------------------------
# Error handling tests
# ---------------------------------------------------------------------------


class TestErrorHandling:

    def test_entry_point_catches_exception(self, monkeypatch, capsys):
        """Exception in handler → entry point catches, calls error()."""
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        with (
            mock.patch("tracing.copilot.hooks.handlers._read_stdin", return_value={}),
            mock.patch("tracing.copilot.hooks.handlers.check_requirements", return_value=True),
            mock.patch("tracing.copilot.hooks.handlers._handle_session_start", side_effect=RuntimeError("boom")),
        ):
            session_start()
        captured = capsys.readouterr()
        assert "boom" in captured.err

    def test_malformed_stdin_no_crash(self, monkeypatch):
        """Malformed stdin JSON doesn't crash entry point."""
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        with (
            mock.patch("tracing.copilot.hooks.handlers.check_requirements", return_value=True),
            mock.patch.object(sys, "stdin", new=io.StringIO("not json")),
            mock.patch("tracing.copilot.hooks.handlers.resolve_session") as rs,
            mock.patch("tracing.copilot.hooks.handlers.ensure_session_initialized"),
        ):
            session_start()
        rs.assert_called_once_with({})


# ---------------------------------------------------------------------------
# Entry point tests (all 6 CLI wrappers)
# ---------------------------------------------------------------------------

ENTRY_POINTS = [
    ("session_start", session_start, "_handle_session_start", "SessionStart"),
    ("user_prompt_submitted", user_prompt_submitted, "_handle_user_prompt_submitted", "UserPromptSubmit"),
    ("pre_tool_use", pre_tool_use, "_handle_pre_tool_use", "PreToolUse"),
    ("post_tool_use", post_tool_use, "_handle_post_tool_use", "PostToolUse"),
    ("stop", stop, "_handle_stop", "Stop"),
    ("subagent_stop", subagent_stop, "_handle_subagent_stop", "SubagentStop"),
]


class TestEntryPoints:

    @pytest.mark.parametrize("name,entry_fn,handler_name,event", ENTRY_POINTS)
    def test_happy_path_calls_handler(self, name, entry_fn, handler_name, event):
        """Entry point calls the corresponding _handle_* with parsed stdin JSON."""
        input_data = {"session_id": "s1", "hook_event_name": event}
        with (
            mock.patch("tracing.copilot.hooks.handlers.check_requirements", return_value=True),
            mock.patch("tracing.copilot.hooks.handlers._read_stdin", return_value=input_data),
            mock.patch(f"tracing.copilot.hooks.handlers.{handler_name}") as handler_mock,
            mock.patch("tracing.copilot.hooks.handlers._print_response"),
        ):
            entry_fn()
        handler_mock.assert_called_once_with(input_data)

    @pytest.mark.parametrize("name,entry_fn,handler_name,event", ENTRY_POINTS)
    def test_requirements_not_met_skips_handler(self, name, entry_fn, handler_name, event):
        """When check_requirements returns False, handler is NOT called but response is still printed."""
        with (
            mock.patch("tracing.copilot.hooks.handlers.check_requirements", return_value=False),
            mock.patch("tracing.copilot.hooks.handlers._read_stdin", return_value={}),
            mock.patch(f"tracing.copilot.hooks.handlers.{handler_name}") as handler_mock,
            mock.patch("tracing.copilot.hooks.handlers._print_response") as pr_mock,
        ):
            entry_fn()
        handler_mock.assert_not_called()
        pr_mock.assert_called_once_with(event)

    @pytest.mark.parametrize("name,entry_fn,handler_name,event", ENTRY_POINTS)
    def test_exception_caught_and_logged(self, name, entry_fn, handler_name, event, capsys):
        """Handler exception is caught; error is logged to stderr, response still printed."""
        with (
            mock.patch("tracing.copilot.hooks.handlers.check_requirements", return_value=True),
            mock.patch("tracing.copilot.hooks.handlers._read_stdin", return_value={}),
            mock.patch(f"tracing.copilot.hooks.handlers.{handler_name}", side_effect=RuntimeError("test-boom")),
            mock.patch("tracing.copilot.hooks.handlers._print_response") as pr_mock,
        ):
            entry_fn()  # should not raise
        captured = capsys.readouterr()
        assert "test-boom" in captured.err
        pr_mock.assert_called_once_with(event)

    @pytest.mark.parametrize("name,entry_fn,handler_name,event", ENTRY_POINTS)
    def test_prints_response(self, name, entry_fn, handler_name, event):
        """Entry point calls _print_response with correct event name."""
        input_data = {"session_id": "s1", "hook_event_name": event}
        with (
            mock.patch("tracing.copilot.hooks.handlers.check_requirements", return_value=True),
            mock.patch("tracing.copilot.hooks.handlers._read_stdin", return_value=input_data),
            mock.patch(f"tracing.copilot.hooks.handlers.{handler_name}"),
            mock.patch("tracing.copilot.hooks.handlers._print_response") as pr_mock,
        ):
            entry_fn()
        pr_mock.assert_called_once_with(event)

    def test_pre_tool_use_prints_permission_on_exception(self, capsys):
        """pre_tool_use MUST print permission response even when handler crashes."""
        with (
            mock.patch("tracing.copilot.hooks.handlers.check_requirements", return_value=True),
            mock.patch("tracing.copilot.hooks.handlers._read_stdin", return_value={}),
            mock.patch("tracing.copilot.hooks.handlers._handle_pre_tool_use", side_effect=RuntimeError("boom")),
        ):
            pre_tool_use()
        out = capsys.readouterr().out.strip()
        payload = json.loads(out)
        assert payload["hookSpecificOutput"]["permissionDecision"] == "allow"

    def test_pre_tool_use_prints_permission_when_disabled(self, capsys):
        """pre_tool_use MUST print permission response even when tracing disabled."""
        with (
            mock.patch("tracing.copilot.hooks.handlers.check_requirements", return_value=False),
            mock.patch("tracing.copilot.hooks.handlers._read_stdin", return_value={}),
        ):
            pre_tool_use()
        out = capsys.readouterr().out.strip()
        payload = json.loads(out)
        assert payload["hookSpecificOutput"]["permissionDecision"] == "allow"


# ---------------------------------------------------------------------------
# project.name attribute tests
# ---------------------------------------------------------------------------


class TestProjectNameOnAllSpans:

    def test_tool_span_has_project_name(self, mock_resolve, state, captured_spans):
        """TOOL spans include project.name."""
        state.set("current_trace_id", "trace-abc")
        state.set("current_trace_span_id", "span-parent")
        inp = {
            "session_id": "sess-1",
            "hook_event_name": "PostToolUse",
            "cwd": "/repo",
            "tool_name": "bash",
            "tool_input": {"command": "ls"},
            "tool_result": {"text_result_for_llm": "output"},
        }
        _handle_post_tool_use(inp)
        attrs = _get_span_attrs(captured_spans[0])
        assert attrs["project.name"]["stringValue"] == "test-copilot-project"

    def test_subagent_span_has_project_name(self, mock_resolve, state, captured_spans):
        """Subagent CHAIN spans include project.name."""
        inp = {"session_id": "sess-1", "hook_event_name": "SubagentStop", "agent_type": "test-agent", "agent_id": "a1"}
        _handle_subagent_stop(inp)
        attrs = _get_span_attrs(captured_spans[0])
        assert attrs["project.name"]["stringValue"] == "test-copilot-project"

    def test_user_prompt_sets_trace_state(self, mock_resolve, mock_ensure, state, captured_spans):
        """user_prompt_submitted sets trace state with project context."""
        inp = {"cwd": "/tmp/project", "prompt": "new prompt"}
        _handle_user_prompt_submitted(inp)
        assert state.get("current_trace_id") is not None
        assert state.get("current_trace_prompt") == "new prompt"
        assert state.get("trace_count") == "1"
        assert state.get("tool_count") == "0"
