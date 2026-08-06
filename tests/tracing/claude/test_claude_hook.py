#!/usr/bin/env python3
"""Tests for tracing.claude_code.hooks.handlers — the 10 Claude Code hook handlers."""

import json
import sys
from pathlib import Path
from unittest import mock

import pytest

from core.common import StateManager
from tracing.claude_code.hooks.handlers import (
    _handle_notification,
    _handle_permission_request,
    _handle_post_tool_use,
    _handle_pre_tool_use,
    _handle_session_end,
    _handle_session_start,
    _handle_stop,
    _handle_stop_failure,
    _handle_subagent_stop,
    _handle_user_prompt_submit,
    _read_stdin,
    _scan_transcript_for_usage,
    notification,
    permission_request,
    post_tool_use,
    pre_tool_use,
    session_end,
    session_start,
    stop,
    stop_failure,
    subagent_stop,
    user_prompt_submit,
)
from tracing.claude_code.hooks.tool_buffer import ToolBuffer

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


# transcript_file fixture is provided by conftest.py


# ---------------------------------------------------------------------------
# _read_stdin tests
# ---------------------------------------------------------------------------


class TestReadStdin:

    def test_empty_stdin(self):
        """Empty stdin returns {}."""
        with mock.patch.object(sys, "stdin", new=__import__("io").StringIO("")):
            assert _read_stdin() == {}

    def test_malformed_json(self):
        """Malformed JSON returns {}."""
        with mock.patch.object(sys, "stdin", new=__import__("io").StringIO("not json")):
            assert _read_stdin() == {}

    def test_valid_json(self):
        """Valid JSON is parsed."""
        with mock.patch.object(sys, "stdin", new=__import__("io").StringIO('{"a": 1}')):
            assert _read_stdin() == {"a": 1}


# ---------------------------------------------------------------------------
# session_start tests
# ---------------------------------------------------------------------------


class TestSessionStart:

    def test_calls_resolve_and_init(self, state, captured_spans):
        """session_start calls resolve_session and ensure_session_initialized."""
        with (
            mock.patch("tracing.claude_code.hooks.handlers.resolve_session", return_value=state) as rs,
            mock.patch("tracing.claude_code.hooks.handlers.ensure_session_initialized") as esi,
        ):
            inp = {"session_id": "s1"}
            _handle_session_start(inp)
            rs.assert_called_once_with(inp)
            esi.assert_called_once_with(state, inp)


# ---------------------------------------------------------------------------
# pre_tool_use tests
# ---------------------------------------------------------------------------


class TestPreToolUse:

    def test_sets_tool_start_time(self, mock_resolve, state):
        """pre_tool_use stores tool_{id}_start in state."""
        _handle_pre_tool_use({"tool_use_id": "tool-42"})
        val = state.get("tool_tool-42_start")
        assert val is not None
        assert int(val) > 0

    def test_missing_tool_use_id_generates_one(self, mock_resolve, state):
        """Missing tool_use_id generates a fallback id and still sets start time."""
        with mock.patch("tracing.claude_code.hooks.handlers.generate_trace_id", return_value="gen-id-123"):
            _handle_pre_tool_use({})
        val = state.get("tool_gen-id-123_start")
        assert val is not None
        assert int(val) > 0


# ---------------------------------------------------------------------------
# post_tool_use tests
# ---------------------------------------------------------------------------


class TestPostToolUse:

    def test_builds_tool_span(self, mock_resolve, state, captured_spans):
        """post_tool_use builds a TOOL span with correct attributes."""
        state.set("current_trace_id", "trace-abc")
        state.set("current_trace_span_id", "span-parent")
        _handle_post_tool_use(
            {
                "tool_name": "Read",
                "tool_use_id": "t1",
                "tool_input": {"file_path": "/foo/bar.py"},
                "tool_response": "file content",
            }
        )
        assert len(captured_spans) == 1
        span = captured_spans[0]
        attrs = {a["key"]: a["value"] for a in span["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["attributes"]}
        assert attrs["openinference.span.kind"]["stringValue"] == "TOOL"
        assert attrs["tool.name"]["stringValue"] == "Read"
        assert attrs["tool.file_path"]["stringValue"] == "/foo/bar.py"

    def test_bash_tool_sets_command(self, mock_resolve, state, captured_spans):
        """Bash tool sets tool.command attr and description is command[:200]."""
        state.set("current_trace_id", "trace-abc")
        state.set("current_trace_span_id", "span-parent")
        _handle_post_tool_use(
            {
                "tool_name": "Bash",
                "tool_use_id": "t2",
                "tool_input": {"command": "ls -la /tmp"},
                "tool_response": "output",
            }
        )
        assert len(captured_spans) == 1
        span = captured_spans[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        attrs = {a["key"]: a["value"] for a in span["attributes"]}
        assert attrs["tool.command"]["stringValue"] == "ls -la /tmp"
        assert attrs["tool.description"]["stringValue"] == "ls -la /tmp"

    def test_grep_tool_sets_query_and_path(self, mock_resolve, state, captured_spans):
        """Grep tool sets both tool.query and tool.file_path, description prefixed 'grep: '."""
        state.set("current_trace_id", "trace-abc")
        state.set("current_trace_span_id", "span-parent")
        _handle_post_tool_use(
            {
                "tool_name": "Grep",
                "tool_use_id": "t3",
                "tool_input": {"pattern": "TODO", "path": "/src"},
                "tool_response": "matches",
            }
        )
        span = captured_spans[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        attrs = {a["key"]: a["value"] for a in span["attributes"]}
        assert attrs["tool.query"]["stringValue"] == "TODO"
        assert attrs["tool.file_path"]["stringValue"] == "/src"
        assert attrs["tool.description"]["stringValue"].startswith("grep: ")

    def test_webfetch_tool_sets_url(self, mock_resolve, state, captured_spans):
        """WebFetch tool sets tool.url attr."""
        state.set("current_trace_id", "trace-abc")
        state.set("current_trace_span_id", "span-parent")
        _handle_post_tool_use(
            {
                "tool_name": "WebFetch",
                "tool_use_id": "t4",
                "tool_input": {"url": "https://example.com"},
                "tool_response": "page",
            }
        )
        span = captured_spans[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        attrs = {a["key"]: a["value"] for a in span["attributes"]}
        assert attrs["tool.url"]["stringValue"] == "https://example.com"

    def test_unknown_tool_description_is_input(self, mock_resolve, state, captured_spans):
        """Unknown tool_name → description is first 200 chars of input."""
        state.set("current_trace_id", "trace-abc")
        state.set("current_trace_span_id", "span-parent")
        _handle_post_tool_use(
            {
                "tool_name": "CustomTool",
                "tool_use_id": "t5",
                "tool_input": {"data": "hello"},
                "tool_response": "result",
            }
        )
        span = captured_spans[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        attrs = {a["key"]: a["value"] for a in span["attributes"]}
        desc = attrs["tool.description"]["stringValue"]
        assert len(desc) <= 200
        # Description is the JSON serialization of tool_input, truncated
        assert "hello" in desc

    def test_uses_pre_tool_start_time(self, mock_resolve, state, captured_spans):
        """Timing uses pre_tool_use start time if available in state."""
        state.set("current_trace_id", "trace-abc")
        state.set("current_trace_span_id", "span-parent")
        state.set("tool_t7_start", "1000000")
        _handle_post_tool_use(
            {
                "tool_name": "Read",
                "tool_use_id": "t7",
                "tool_input": {"file_path": "/a.py"},
                "tool_response": "content",
            }
        )
        span = captured_spans[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        assert span["startTimeUnixNano"] == "1000000000000"  # 1000000 ms -> ns
        # Verify cleanup
        assert state.get("tool_t7_start") is None

    def test_no_session_id_returns_early(self, state, captured_spans):
        """If session_id is None, returns without sending span."""
        state.delete("session_id")
        with mock.patch("tracing.claude_code.hooks.handlers.resolve_session", return_value=state):
            _handle_post_tool_use({"tool_name": "Bash", "tool_use_id": "t8"})
        assert len(captured_spans) == 0


# ---------------------------------------------------------------------------
# user_prompt_submit tests
# ---------------------------------------------------------------------------


class TestUserPromptSubmit:

    def test_sets_trace_state(self, mock_resolve, state, captured_spans):
        """user_prompt_submit sets current_trace_id, span_id, start_time, prompt."""
        with mock.patch("tracing.claude_code.hooks.handlers.ensure_session_initialized"):
            _handle_user_prompt_submit({"prompt": "hello world"})
        assert state.get("current_trace_id") is not None
        assert len(state.get("current_trace_id")) == 32
        assert state.get("current_trace_span_id") is not None
        assert len(state.get("current_trace_span_id")) == 16
        assert state.get("current_trace_start_time") is not None
        assert state.get("current_trace_prompt") == "hello world"

    def test_increments_trace_count(self, mock_resolve, state, captured_spans):
        """user_prompt_submit increments trace_count."""
        with mock.patch("tracing.claude_code.hooks.handlers.ensure_session_initialized"):
            _handle_user_prompt_submit({"prompt": "test"})
        assert state.get("trace_count") == "1"

    def test_records_trace_start_line(self, mock_resolve, state, captured_spans, transcript_file):
        """Records trace_start_line from transcript file line count."""
        with mock.patch("tracing.claude_code.hooks.handlers.ensure_session_initialized"):
            _handle_user_prompt_submit(
                {
                    "prompt": "test",
                    "transcript_path": transcript_file,
                }
            )
        # sample_transcript.jsonl has 3 lines
        assert state.get("trace_start_line") == "3"

    def test_no_transcript_sets_zero(self, mock_resolve, state, captured_spans):
        """Missing transcript sets trace_start_line to 0."""
        with mock.patch("tracing.claude_code.hooks.handlers.ensure_session_initialized"):
            _handle_user_prompt_submit({"prompt": "test"})
        assert state.get("trace_start_line") == "0"

    def test_failsafe_closes_orphan(self, mock_resolve, state, captured_spans):
        """If current_trace_id already in state, sends fail-safe LLM span."""
        state.set("current_trace_id", "old-trace-id-00000000000000000000")
        state.set("current_trace_span_id", "old-span-1234567")
        state.set("current_trace_start_time", "999000")
        state.set("current_trace_prompt", "old prompt")
        with mock.patch("tracing.claude_code.hooks.handlers.ensure_session_initialized"):
            _handle_user_prompt_submit({"prompt": "new prompt"})
        # Should have sent a fail-safe span
        assert len(captured_spans) == 1
        span = captured_spans[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        attrs = {a["key"]: a["value"] for a in span["attributes"]}
        assert attrs["openinference.span.kind"]["stringValue"] == "LLM"
        assert "fail-safe" in attrs["output.value"]["stringValue"]
        # New trace should be set up
        assert state.get("current_trace_id") != "old-trace-id-00000000000000000000"

    def test_failed_orphan_export_retains_old_turn_and_does_not_start_new(self, mock_resolve, state):
        state.set("current_trace_id", "old-trace-id-00000000000000000000")
        state.set("current_trace_span_id", "old-span-1234567")
        state.set("current_trace_start_time", "999000")
        state.set("current_trace_prompt", "old prompt")
        state.set("trace_count", "7")

        with (
            mock.patch("tracing.claude_code.hooks.handlers.ensure_session_initialized"),
            mock.patch("tracing.claude_code.hooks.handlers.send_span", return_value=False),
        ):
            _handle_user_prompt_submit({"prompt": "new prompt"})

        assert state.get("current_trace_id") == "old-trace-id-00000000000000000000"
        assert state.get("current_trace_span_id") == "old-span-1234567"
        assert state.get("current_trace_prompt") == "old prompt"
        assert state.get("trace_count") == "7"


# ---------------------------------------------------------------------------
# stop tests
# ---------------------------------------------------------------------------


class TestStop:

    def test_parses_transcript_array_content(self, mock_resolve, state, captured_spans, transcript_file):
        """Parses transcript, extracts text from content array format."""
        state.set("current_trace_id", "t" * 32)
        state.set("current_trace_span_id", "s" * 16)
        state.set("current_trace_start_time", "1000")
        state.set("current_trace_prompt", "fix the bug")
        state.set("trace_start_line", "0")
        with mock.patch(
            "tracing.claude_code.hooks.handlers.resolve_transcript_path", return_value=Path(transcript_file)
        ):
            _handle_stop({})
        assert len(captured_spans) == 1
        span = captured_spans[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        attrs = {a["key"]: a["value"] for a in span["attributes"]}
        assert "I found the issue." in attrs["output.value"]["stringValue"]

    def test_stop_cleanup_does_not_erase_a_concurrent_newer_turn(self, mock_resolve, state, transcript_file):
        state.set("current_trace_id", "o" * 32)
        state.set("current_trace_span_id", "p" * 16)
        state.set("current_trace_start_time", "1000")
        state.set("current_trace_prompt", "old prompt")
        state.set("trace_start_line", "0")

        def replace_with_newer_turn(_payload):
            state.set("current_trace_id", "n" * 32)
            state.set("current_trace_span_id", "q" * 16)
            state.set("current_trace_start_time", "2000")
            state.set("current_trace_prompt", "new prompt")
            state.set("trace_start_line", "9")
            state.set("high_fidelity_span_ids", '{"turn:new":"r"}')
            return True

        with (
            mock.patch(
                "tracing.claude_code.hooks.handlers.resolve_transcript_path", return_value=Path(transcript_file)
            ),
            mock.patch(
                "tracing.claude_code.hooks.handlers.send_span",
                side_effect=replace_with_newer_turn,
            ),
        ):
            _handle_stop({})

        assert state.get("current_trace_id") == "n" * 32
        assert state.get("current_trace_span_id") == "q" * 16
        assert state.get("current_trace_start_time") == "2000"
        assert state.get("current_trace_prompt") == "new prompt"
        assert state.get("trace_start_line") == "9"
        assert state.get("high_fidelity_span_ids") == '{"turn:new":"r"}'

    def test_parses_transcript_string_content(self, mock_resolve, state, captured_spans, tmp_path):
        """Parses transcript with string content format."""
        tf = tmp_path / "transcript_str.jsonl"
        entry = {
            "message": {
                "role": "assistant",
                "content": "Hello from string format",
                "model": "claude-test",
                "usage": {"input_tokens": 10, "output_tokens": 5},
            }
        }
        tf.write_text(json.dumps(entry) + "\n")

        state.set("current_trace_id", "t" * 32)
        state.set("current_trace_span_id", "s" * 16)
        state.set("current_trace_start_time", "1000")
        state.set("current_trace_prompt", "test")
        state.set("trace_start_line", "0")
        with mock.patch("tracing.claude_code.hooks.handlers.resolve_transcript_path", return_value=tf):
            _handle_stop({})
        span = captured_spans[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        attrs = {a["key"]: a["value"] for a in span["attributes"]}
        assert attrs["output.value"]["stringValue"] == "Hello from string format"

    def test_accumulates_tokens(self, mock_resolve, state, captured_spans, transcript_file):
        """Accumulates tokens: input + cache_read + cache_creation → prompt."""
        state.set("current_trace_id", "t" * 32)
        state.set("current_trace_span_id", "s" * 16)
        state.set("current_trace_start_time", "1000")
        state.set("current_trace_prompt", "fix the bug")
        state.set("trace_start_line", "0")
        with mock.patch(
            "tracing.claude_code.hooks.handlers.resolve_transcript_path", return_value=Path(transcript_file)
        ):
            _handle_stop({})
        span = captured_spans[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        attrs = {a["key"]: a["value"] for a in span["attributes"]}
        # input_tokens=100, cache_read=10, cache_creation=5 → 115 prompt (total, incl. cache)
        assert attrs["llm.token_count.prompt"]["intValue"] == 115
        # output_tokens=50
        assert attrs["llm.token_count.completion"]["intValue"] == 50
        # total
        assert attrs["llm.token_count.total"]["intValue"] == 165
        # Cache breakdown surfaced as prompt_details so cost is priced at cache rates
        assert attrs["llm.token_count.prompt_details.cache_read"]["intValue"] == 10
        assert attrs["llm.token_count.prompt_details.cache_write"]["intValue"] == 5

    def test_omits_cache_details_when_uncached(self, mock_resolve, state, captured_spans, tmp_path):
        """No prompt_details.cache_* attributes when there are no cache tokens."""
        tf = tmp_path / "uncached.jsonl"
        entry = {
            "message": {
                "role": "assistant",
                "content": "no caching here",
                "model": "claude-test",
                "usage": {"input_tokens": 42, "output_tokens": 7},
            }
        }
        tf.write_text(json.dumps(entry) + "\n")
        state.set("current_trace_id", "t" * 32)
        state.set("current_trace_span_id", "s" * 16)
        state.set("current_trace_start_time", "1000")
        state.set("current_trace_prompt", "test")
        state.set("trace_start_line", "0")
        with mock.patch("tracing.claude_code.hooks.handlers.resolve_transcript_path", return_value=tf):
            _handle_stop({})
        span = captured_spans[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        attrs = {a["key"]: a["value"] for a in span["attributes"]}
        assert attrs["llm.token_count.prompt"]["intValue"] == 42
        assert "llm.token_count.prompt_details.cache_read" not in attrs
        assert "llm.token_count.prompt_details.cache_write" not in attrs

    def test_skips_lines_before_trace_start_line(self, mock_resolve, state, captured_spans, transcript_file):
        """Skips lines before trace_start_line."""
        state.set("current_trace_id", "t" * 32)
        state.set("current_trace_span_id", "s" * 16)
        state.set("current_trace_start_time", "1000")
        state.set("current_trace_prompt", "test")
        # Set start_line past all entries (3 lines in fixture)
        state.set("trace_start_line", "3")
        with mock.patch(
            "tracing.claude_code.hooks.handlers.resolve_transcript_path", return_value=Path(transcript_file)
        ):
            _handle_stop({})
        span = captured_spans[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        attrs = {a["key"]: a["value"] for a in span["attributes"]}
        assert attrs["output.value"]["stringValue"] == "(No response)"

    def test_no_transcript_file(self, mock_resolve, state, captured_spans):
        """No transcript file and no last_assistant_message → output is '(No response)'."""
        state.set("current_trace_id", "t" * 32)
        state.set("current_trace_span_id", "s" * 16)
        state.set("current_trace_start_time", "1000")
        state.set("current_trace_prompt", "test")
        state.set("trace_start_line", "0")
        with mock.patch("tracing.claude_code.hooks.handlers.resolve_transcript_path", return_value=None):
            _handle_stop({})
        span = captured_spans[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        attrs = {a["key"]: a["value"] for a in span["attributes"]}
        assert attrs["output.value"]["stringValue"] == "(No response)"

    def test_cleans_up_trace_state(self, mock_resolve, state, captured_spans, transcript_file):
        """Cleans up current_trace_* state keys after sending."""
        state.set("current_trace_id", "t" * 32)
        state.set("current_trace_span_id", "s" * 16)
        state.set("current_trace_start_time", "1000")
        state.set("current_trace_prompt", "fix the bug")
        state.set("trace_start_line", "0")
        with mock.patch(
            "tracing.claude_code.hooks.handlers.resolve_transcript_path", return_value=Path(transcript_file)
        ):
            _handle_stop({})
        assert state.get("current_trace_id") is None
        assert state.get("current_trace_span_id") is None
        assert state.get("current_trace_start_time") is None
        assert state.get("current_trace_prompt") is None
        assert state.get("trace_start_line") is None

    def test_legacy_failed_export_retains_turn_buffers_for_retry(self, mock_resolve, state):
        for key, value in {
            "current_trace_id": "t" * 32,
            "current_trace_span_id": "s" * 16,
            "current_trace_start_time": "1000",
            "current_trace_prompt": "retry me",
            "trace_start_line": "0",
            "high_fidelity_span_ids": '{"turn:x": "abc"}',
            "pending_subagents": '{"agent-1": {"agent_id": "agent-1"}}',
        }.items():
            state.set(key, value)
        ToolBuffer(state).record_start("tool-1", tool_name="Read")

        with (
            mock.patch("tracing.claude_code.hooks.handlers.resolve_transcript_path", return_value=None),
            mock.patch("tracing.claude_code.hooks.handlers.send_span", return_value=False),
        ):
            _handle_stop({})

        assert state.get("current_trace_id") == "t" * 32
        assert state.get("pending_subagents") is not None
        assert state.get("high_fidelity_span_ids") is not None
        assert [item.tool_use_id for item in ToolBuffer(state).all()] == ["tool-1"]

    def test_legacy_success_preserves_unexported_child_snapshots(self, mock_resolve, state, captured_spans):
        for key, value in {
            "current_trace_id": "t" * 32,
            "current_trace_span_id": "s" * 16,
            "current_trace_start_time": "1000",
            "current_trace_prompt": "legacy",
            "trace_start_line": "0",
            "pending_subagents": '{"agent-1": {"agent_id": "agent-1"}}',
        }.items():
            state.set(key, value)
        ToolBuffer(state).record_start("tool-1", tool_name="Read")

        with mock.patch("tracing.claude_code.hooks.handlers.resolve_transcript_path", return_value=None):
            _handle_stop({})

        assert state.get("current_trace_id") is None
        assert [item.tool_use_id for item in ToolBuffer(state).all()] == ["tool-1"]
        assert state.get("pending_subagents") is not None

    def test_gc_every_5_turns(self, mock_resolve, state, captured_spans):
        """GC runs every 5 turns."""
        state.set("current_trace_id", "t" * 32)
        state.set("current_trace_span_id", "s" * 16)
        state.set("current_trace_start_time", "1000")
        state.set("current_trace_prompt", "test")
        state.set("trace_count", "5")
        state.set("trace_start_line", "0")
        with mock.patch("tracing.claude_code.hooks.handlers.gc_stale_state_files") as gc_mock:
            _handle_stop({})
            gc_mock.assert_called_once()

    def test_gc_not_called_off_cycle(self, mock_resolve, state, captured_spans):
        """GC not called when trace_count is not multiple of 5."""
        state.set("current_trace_id", "t" * 32)
        state.set("current_trace_span_id", "s" * 16)
        state.set("current_trace_start_time", "1000")
        state.set("current_trace_prompt", "test")
        state.set("trace_count", "3")
        state.set("trace_start_line", "0")
        with mock.patch("tracing.claude_code.hooks.handlers.gc_stale_state_files") as gc_mock:
            _handle_stop({})
            gc_mock.assert_not_called()

    def test_no_trace_id_returns_early(self, mock_resolve, state, captured_spans):
        """No current_trace_id → returns without sending."""
        _handle_stop({})
        assert len(captured_spans) == 0

    def test_golden_transcript(self, mock_resolve, state, captured_spans, transcript_file):
        """Golden test: exact token counts and output from sample_transcript.jsonl."""
        state.set("current_trace_id", "t" * 32)
        state.set("current_trace_span_id", "s" * 16)
        state.set("current_trace_start_time", "1000")
        state.set("current_trace_prompt", "fix the bug")
        state.set("trace_start_line", "0")
        with mock.patch(
            "tracing.claude_code.hooks.handlers.resolve_transcript_path", return_value=Path(transcript_file)
        ):
            _handle_stop({})

        assert len(captured_spans) == 1
        span = captured_spans[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        attrs = {a["key"]: a["value"] for a in span["attributes"]}

        # Token counts: input=100 + cache_read=10 + cache_creation=5 = 115 prompt
        assert attrs["llm.token_count.prompt"]["intValue"] == 115
        assert attrs["llm.token_count.completion"]["intValue"] == 50
        assert attrs["llm.token_count.total"]["intValue"] == 165
        assert attrs["llm.token_count.prompt_details.cache_read"]["intValue"] == 10
        assert attrs["llm.token_count.prompt_details.cache_write"]["intValue"] == 5
        # Output text
        assert attrs["output.value"]["stringValue"] == "I found the issue."
        # Model
        assert attrs["llm.model_name"]["stringValue"] == "claude-sonnet-4-20250514"

    def test_stop_uses_last_assistant_message(self, mock_resolve, state, captured_spans):
        """Stop prefers last_assistant_message from input when present."""
        state.set("current_trace_id", "t" * 32)
        state.set("current_trace_span_id", "s" * 16)
        state.set("current_trace_start_time", "1000")
        state.set("current_trace_prompt", "test")
        with mock.patch("tracing.claude_code.hooks.handlers.resolve_transcript_path", return_value=None):
            _handle_stop({"last_assistant_message": "hello world"})
        assert len(captured_spans) == 1
        span = captured_spans[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        attrs = {a["key"]: a["value"] for a in span["attributes"]}
        assert attrs["output.value"]["stringValue"] == "hello world"

    def test_stop_falls_back_to_no_response_when_no_input(self, mock_resolve, state, captured_spans):
        """No last_assistant_message and no transcript → (No response)."""
        state.set("current_trace_id", "t" * 32)
        state.set("current_trace_span_id", "s" * 16)
        state.set("current_trace_start_time", "1000")
        state.set("current_trace_prompt", "test")
        with mock.patch("tracing.claude_code.hooks.handlers.resolve_transcript_path", return_value=None):
            _handle_stop({})
        span = captured_spans[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        attrs = {a["key"]: a["value"] for a in span["attributes"]}
        assert attrs["output.value"]["stringValue"] == "(No response)"

    def test_stop_extracts_tokens_from_transcript_with_last_msg(
        self, mock_resolve, state, captured_spans, transcript_file
    ):
        """Token counts come from transcript even when last_assistant_message is set."""
        state.set("current_trace_id", "t" * 32)
        state.set("current_trace_span_id", "s" * 16)
        state.set("current_trace_start_time", "1000")
        state.set("current_trace_prompt", "test")
        state.set("trace_start_line", "0")
        with mock.patch(
            "tracing.claude_code.hooks.handlers.resolve_transcript_path", return_value=Path(transcript_file)
        ):
            _handle_stop({"last_assistant_message": "overridden text"})
        span = captured_spans[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        attrs = {a["key"]: a["value"] for a in span["attributes"]}
        # Output comes from last_assistant_message
        assert attrs["output.value"]["stringValue"] == "overridden text"
        # Token counts still come from the transcript
        assert attrs["llm.token_count.prompt"]["intValue"] == 115
        assert attrs["llm.token_count.completion"]["intValue"] == 50

    def test_stop_uses_transcript_text_when_last_assistant_message_missing(
        self, mock_resolve, state, captured_spans, transcript_file
    ):
        """When last_assistant_message is absent, output comes from transcript scan."""
        state.set("current_trace_id", "t" * 32)
        state.set("current_trace_span_id", "s" * 16)
        state.set("current_trace_start_time", "1000")
        state.set("current_trace_prompt", "test")
        state.set("trace_start_line", "0")
        with mock.patch(
            "tracing.claude_code.hooks.handlers.resolve_transcript_path", return_value=Path(transcript_file)
        ):
            _handle_stop({})
        span = captured_spans[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        attrs = {a["key"]: a["value"] for a in span["attributes"]}
        assert attrs["output.value"]["stringValue"] == "I found the issue."


# ---------------------------------------------------------------------------
# _scan_transcript_for_usage tests
# ---------------------------------------------------------------------------


class TestScanTranscriptForUsage:

    def test_basic_extraction(self, transcript_file):
        """Extracts text, tokens, and model from standard transcript."""
        output, usage, model = _scan_transcript_for_usage(Path(transcript_file), 0)
        assert output == "I found the issue."
        assert usage.prompt == 115  # 100 + 10 + 5
        assert usage.completion == 50
        assert usage.cache_read == 10
        assert usage.cache_write == 5
        assert model == "claude-sonnet-4-20250514"

    def test_skips_lines_before_start(self, transcript_file):
        """Lines before start_line are skipped entirely."""
        output, usage, model = _scan_transcript_for_usage(Path(transcript_file), 3)
        assert output == ""
        assert usage.prompt == 0
        assert usage.completion == 0
        assert usage.cache_read == 0
        assert usage.cache_write == 0
        assert model == ""

    def test_handles_string_content(self, tmp_path):
        """Handles content as a plain string (not a list)."""
        tf = tmp_path / "str.jsonl"
        entry = {
            "message": {
                "role": "assistant",
                "content": "plain text",
                "model": "m1",
                "usage": {"input_tokens": 1, "output_tokens": 2},
            }
        }
        tf.write_text(json.dumps(entry) + "\n")
        output, usage, model = _scan_transcript_for_usage(tf, 0)
        assert output == "plain text"
        assert usage.prompt == 1
        assert usage.completion == 2
        assert model == "m1"

    def test_handles_malformed_lines(self, tmp_path):
        """Skips malformed JSONL lines without crashing."""
        tf = tmp_path / "bad.jsonl"
        lines = [
            "not json at all",
            json.dumps(
                {"message": {"role": "assistant", "content": "good", "model": "m", "usage": {"output_tokens": 5}}}
            ),
            "",
            "{invalid json",
        ]
        tf.write_text("\n".join(lines) + "\n")
        output, usage, model = _scan_transcript_for_usage(tf, 0)
        assert output == "good"
        assert usage.completion == 5

    def test_skips_non_assistant_messages(self, tmp_path):
        """Only processes assistant messages, skips user/system."""
        tf = tmp_path / "mixed.jsonl"
        lines = [
            json.dumps({"message": {"role": "user", "content": "user msg"}}),
            json.dumps(
                {
                    "message": {
                        "role": "assistant",
                        "content": "assistant msg",
                        "model": "m",
                        "usage": {"output_tokens": 3},
                    }
                }
            ),
            json.dumps({"message": {"role": "system", "content": "system msg"}}),
        ]
        tf.write_text("\n".join(lines) + "\n")
        output, usage, model = _scan_transcript_for_usage(tf, 0)
        assert output == "assistant msg"
        assert usage.completion == 3

    def test_concatenates_multiple_assistant_messages(self, tmp_path):
        """Multiple assistant messages are concatenated with newlines."""
        tf = tmp_path / "multi.jsonl"
        lines = [
            json.dumps(
                {"message": {"role": "assistant", "content": "first", "model": "m1", "usage": {"output_tokens": 1}}}
            ),
            json.dumps(
                {"message": {"role": "assistant", "content": "second", "model": "m2", "usage": {"output_tokens": 2}}}
            ),
        ]
        tf.write_text("\n".join(lines) + "\n")
        output, usage, model = _scan_transcript_for_usage(tf, 0)
        assert output == "first\nsecond"
        assert usage.completion == 3
        # Model should be the last non-empty one
        assert model == "m2"

    def test_empty_content_skipped(self, tmp_path):
        """Assistant messages with empty content don't contribute to output."""
        tf = tmp_path / "empty.jsonl"
        lines = [
            json.dumps(
                {
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": ""}],
                        "model": "m",
                        "usage": {"output_tokens": 1},
                    }
                }
            ),
        ]
        tf.write_text("\n".join(lines) + "\n")
        output, usage, model = _scan_transcript_for_usage(tf, 0)
        assert output == ""
        assert usage.completion == 1

    def test_accumulates_all_token_types(self, tmp_path):
        """Prompt total sums input + cache_read + cache_creation; cache buckets tracked separately."""
        tf = tmp_path / "tokens.jsonl"
        entry = {
            "message": {
                "role": "assistant",
                "content": "x",
                "model": "m",
                "usage": {
                    "input_tokens": 10,
                    "cache_read_input_tokens": 20,
                    "cache_creation_input_tokens": 30,
                    "output_tokens": 40,
                },
            }
        }
        tf.write_text(json.dumps(entry) + "\n")
        output, usage, model = _scan_transcript_for_usage(tf, 0)
        assert usage.prompt == 60  # 10 + 20 + 30
        assert usage.completion == 40
        assert usage.cache_read == 20
        assert usage.cache_write == 30

    def test_duplicate_request_records_counted_once(self, tmp_path):
        """A streamed response writes one record per content block, each
        repeating the same usage snapshot — one API call must be billed once,
        while the text of every content block is still concatenated."""
        tf = tmp_path / "dup.jsonl"
        usage_snapshot = {
            "input_tokens": 10,
            "cache_read_input_tokens": 20,
            "cache_creation_input_tokens": 30,
            "output_tokens": 40,
        }
        lines = [
            json.dumps(
                {
                    "requestId": "req_1",
                    "message": {
                        "id": "msg_1",
                        "role": "assistant",
                        "content": text,
                        "model": "m",
                        "usage": usage_snapshot,
                    },
                }
            )
            for text in ("first block", "second block", "third block")
        ]
        tf.write_text("\n".join(lines) + "\n")
        output, usage, model = _scan_transcript_for_usage(tf, 0)
        assert output == "first block\nsecond block\nthird block"
        assert usage.prompt == 60  # 10 + 20 + 30, once — not three times
        assert usage.completion == 40
        assert usage.cache_read == 20
        assert usage.cache_write == 30

    def test_partial_then_final_snapshot_last_record_wins(self, tmp_path):
        """Some harness versions write partial usage snapshots before the
        final one; the last record for a request is authoritative."""
        tf = tmp_path / "partial.jsonl"
        lines = [
            json.dumps(
                {
                    "requestId": "req_1",
                    "message": {
                        "id": "msg_1",
                        "role": "assistant",
                        "content": "x",
                        "model": "m",
                        "usage": {"input_tokens": 10, "output_tokens": 2},
                    },
                }
            ),
            json.dumps(
                {
                    "requestId": "req_1",
                    "message": {
                        "id": "msg_1",
                        "role": "assistant",
                        "content": "y",
                        "model": "m",
                        "usage": {"input_tokens": 10, "output_tokens": 31979},
                    },
                }
            ),
        ]
        tf.write_text("\n".join(lines) + "\n")
        output, usage, model = _scan_transcript_for_usage(tf, 0)
        assert usage.completion == 31979
        assert usage.prompt == 10

    def test_distinct_requests_are_summed(self, tmp_path):
        """Different API calls (distinct requestIds) still accumulate."""
        tf = tmp_path / "distinct.jsonl"
        lines = [
            json.dumps(
                {
                    "requestId": f"req_{n}",
                    "message": {
                        "id": f"msg_{n}",
                        "role": "assistant",
                        "content": "x",
                        "model": "m",
                        "usage": {"input_tokens": 10, "output_tokens": n},
                    },
                }
            )
            for n in (1, 2)
        ]
        tf.write_text("\n".join(lines) + "\n")
        output, usage, model = _scan_transcript_for_usage(tf, 0)
        assert usage.completion == 3
        assert usage.prompt == 20

    def test_records_without_identity_counted_individually(self, tmp_path):
        """Records with neither requestId nor message.id can't be correlated,
        so each one counts (matches the pre-dedup behavior for such records)."""
        tf = tmp_path / "noid.jsonl"
        lines = [
            json.dumps({"message": {"role": "assistant", "content": "a", "model": "m", "usage": {"output_tokens": 1}}}),
            json.dumps({"message": {"role": "assistant", "content": "b", "model": "m", "usage": {"output_tokens": 2}}}),
        ]
        tf.write_text("\n".join(lines) + "\n")
        output, usage, model = _scan_transcript_for_usage(tf, 0)
        assert usage.completion == 3


# ---------------------------------------------------------------------------
# subagent_stop tests
# ---------------------------------------------------------------------------


class TestSubagentStop:

    def test_skips_empty_agent_type(self, mock_resolve, state, captured_spans):
        """Skips when agent_type is empty."""
        state.set("current_trace_id", "t" * 32)
        _handle_subagent_stop({"agent_type": ""})
        assert len(captured_spans) == 0

    def test_skips_unknown_agent_type(self, mock_resolve, state, captured_spans):
        """Skips when agent_type is 'unknown'."""
        state.set("current_trace_id", "t" * 32)
        _handle_subagent_stop({"agent_type": "unknown"})
        assert len(captured_spans) == 0

    def test_parses_subagent_transcript(self, mock_resolve, state, captured_spans, transcript_file):
        """Parses subagent transcript same as stop."""
        state.set("current_trace_id", "t" * 32)
        state.set("current_trace_span_id", "s" * 16)
        with mock.patch(
            "tracing.claude_code.hooks.handlers.resolve_transcript_path", return_value=Path(transcript_file)
        ):
            _handle_subagent_stop(
                {
                    "agent_type": "code-review",
                    "agent_id": "agent-1",
                    "agent_transcript_path": transcript_file,
                }
            )
        assert len(captured_spans) == 1
        span = captured_spans[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        attrs = {a["key"]: a["value"] for a in span["attributes"]}
        assert attrs["openinference.span.kind"]["stringValue"] == "CHAIN"
        assert attrs["subagent.type"]["stringValue"] == "code-review"
        assert "I found the issue." in attrs["output.value"]["stringValue"]

    def test_uses_file_creation_time(self, mock_resolve, state, captured_spans, transcript_file):
        """Uses file creation time (st_birthtime) for start_time."""
        state.set("current_trace_id", "t" * 32)
        state.set("current_trace_span_id", "s" * 16)

        real_stat = Path(transcript_file).stat()
        mock_stat = mock.MagicMock(wraps=real_stat)
        mock_stat.st_birthtime = 1700000.0  # seconds → 1700000000 ms
        mock_stat.st_mode = real_stat.st_mode

        with mock.patch(
            "tracing.claude_code.hooks.handlers.resolve_transcript_path", return_value=Path(transcript_file)
        ):
            with mock.patch.object(Path, "stat", return_value=mock_stat):
                _handle_subagent_stop(
                    {
                        "agent_type": "explorer",
                        "agent_id": "a2",
                        "agent_transcript_path": transcript_file,
                    }
                )

        span = captured_spans[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        assert span["startTimeUnixNano"] == "1700000000000000"  # 1700000000 ms -> ns

    def test_falls_back_to_ctime(self, mock_resolve, state, captured_spans, transcript_file):
        """Falls back to st_ctime when st_birthtime unavailable."""
        state.set("current_trace_id", "t" * 32)
        state.set("current_trace_span_id", "s" * 16)

        real_stat = Path(transcript_file).stat()

        class FakeStat:
            """Stat result without st_birthtime."""

            st_mode = real_stat.st_mode
            st_size = real_stat.st_size
            st_ctime = 1600000.0

        with mock.patch(
            "tracing.claude_code.hooks.handlers.resolve_transcript_path", return_value=Path(transcript_file)
        ):
            with mock.patch.object(Path, "stat", return_value=FakeStat()):
                _handle_subagent_stop(
                    {
                        "agent_type": "explorer",
                        "agent_id": "a3",
                        "agent_transcript_path": transcript_file,
                    }
                )

        span = captured_spans[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        assert span["startTimeUnixNano"] == "1600000000000000"

    def test_no_trace_id_returns_early(self, mock_resolve, state, captured_spans):
        """No current_trace_id → returns without sending."""
        _handle_subagent_stop({"agent_type": "explorer"})
        assert len(captured_spans) == 0

    def test_subagent_stop_always_sets_output_value(self, mock_resolve, state, captured_spans):
        """Regression: output.value is always set, even when empty (was conditional)."""
        state.set("current_trace_id", "t" * 32)
        state.set("current_trace_span_id", "s" * 16)
        with mock.patch("tracing.claude_code.hooks.handlers.resolve_transcript_path", return_value=None):
            _handle_subagent_stop(
                {
                    "agent_type": "explorer",
                    "agent_id": "a4",
                    "last_assistant_message": "",
                }
            )
        assert len(captured_spans) == 1
        span = captured_spans[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        attrs = {a["key"]: a["value"] for a in span["attributes"]}
        assert "output.value" in attrs
        assert attrs["output.value"]["stringValue"] == "(No response)"

    def test_subagent_stop_prefers_agent_transcript_path(self, mock_resolve, state, captured_spans, tmp_path):
        """When both transcript_path and agent_transcript_path are in input, resolver picks agent_transcript_path."""
        # Create two transcripts with distinct token counts
        main_tf = tmp_path / "main_transcript.jsonl"
        main_entry = {
            "message": {
                "role": "assistant",
                "content": "main output",
                "model": "claude-main",
                "usage": {"input_tokens": 999, "output_tokens": 999},
            }
        }
        main_tf.write_text(json.dumps(main_entry) + "\n")

        agent_tf = tmp_path / "agent_transcript.jsonl"
        agent_entry = {
            "message": {
                "role": "assistant",
                "content": "agent output",
                "model": "claude-test",
                "usage": {"input_tokens": 77, "output_tokens": 33},
            }
        }
        agent_tf.write_text(json.dumps(agent_entry) + "\n")

        state.set("current_trace_id", "t" * 32)
        state.set("current_trace_span_id", "s" * 16)
        # Don't mock the resolver — let it exercise the real priority logic
        _handle_subagent_stop(
            {
                "agent_type": "explorer",
                "agent_id": "a5",
                "agent_transcript_path": str(agent_tf),
                "transcript_path": str(main_tf),
            }
        )
        span = captured_spans[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        attrs = {a["key"]: a["value"] for a in span["attributes"]}
        # Token counts must come from the agent transcript, not the main one
        assert attrs["llm.token_count.prompt"]["intValue"] == 77
        assert attrs["llm.token_count.completion"]["intValue"] == 33

    def test_subagent_stop_uses_last_assistant_message(self, mock_resolve, state, captured_spans):
        """SubagentStop prefers last_assistant_message from input when present."""
        state.set("current_trace_id", "t" * 32)
        state.set("current_trace_span_id", "s" * 16)
        with mock.patch("tracing.claude_code.hooks.handlers.resolve_transcript_path", return_value=None):
            _handle_subagent_stop(
                {
                    "agent_type": "explorer",
                    "agent_id": "a6",
                    "last_assistant_message": "Found the file at line 42",
                }
            )
        assert len(captured_spans) == 1
        span = captured_spans[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        attrs = {a["key"]: a["value"] for a in span["attributes"]}
        assert attrs["output.value"]["stringValue"] == "Found the file at line 42"

    def test_subagent_stop_tokens_from_transcript_with_last_msg(
        self, mock_resolve, state, captured_spans, transcript_file
    ):
        """Token counts come from transcript even when last_assistant_message is set."""
        state.set("current_trace_id", "t" * 32)
        state.set("current_trace_span_id", "s" * 16)
        with mock.patch(
            "tracing.claude_code.hooks.handlers.resolve_transcript_path", return_value=Path(transcript_file)
        ):
            _handle_subagent_stop(
                {
                    "agent_type": "explorer",
                    "agent_id": "a7",
                    "last_assistant_message": "overridden subagent text",
                }
            )
        span = captured_spans[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        attrs = {a["key"]: a["value"] for a in span["attributes"]}
        assert attrs["output.value"]["stringValue"] == "overridden subagent text"
        assert attrs["llm.token_count.prompt"]["intValue"] == 115
        assert attrs["llm.token_count.completion"]["intValue"] == 50


# ---------------------------------------------------------------------------
# stop_failure tests
# ---------------------------------------------------------------------------


class TestStopFailure:

    def test_stop_failure_sends_error_span(self, mock_resolve, state, captured_spans):
        """StopFailure sends a span with error attributes."""
        state.set("current_trace_id", "t" * 32)
        state.set("current_trace_span_id", "s" * 16)
        state.set("current_trace_start_time", "1000")
        state.set("current_trace_prompt", "test")
        _handle_stop_failure(
            {
                "error": "rate_limit",
                "error_details": "429",
                "last_assistant_message": "API Error: Rate limit reached",
            }
        )
        assert len(captured_spans) == 1
        span = captured_spans[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        attrs = {a["key"]: a["value"] for a in span["attributes"]}
        assert attrs["error.type"]["stringValue"] == "rate_limit"
        assert attrs["error.message"]["stringValue"] == "429"
        assert attrs["output.value"]["stringValue"] == "API Error: Rate limit reached"
        assert "(failed)" in span["name"]

    def test_stop_failure_redacts_error_details_when_prompt_logging_is_disabled(
        self, monkeypatch, mock_resolve, state, captured_spans
    ):
        monkeypatch.setenv("ARIZE_LOG_PROMPTS", "false")
        state.set("current_trace_id", "t" * 32)
        state.set("current_trace_span_id", "s" * 16)
        state.set("current_trace_start_time", "1000")
        state.set("current_trace_prompt", "test")

        _handle_stop_failure({"error": "crash", "error_details": "secret-token"})

        span = captured_spans[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        attrs = {a["key"]: a["value"] for a in span["attributes"]}
        assert attrs["error.message"]["stringValue"] == "<redacted (12 chars)>"

    def test_stop_failure_success_preserves_unexported_tool_and_subagent_buffers(
        self, mock_resolve, state, captured_spans
    ):
        state.set("current_trace_id", "t" * 32)
        state.set("current_trace_span_id", "s" * 16)
        state.set("current_trace_start_time", "1000")
        state.set("current_trace_prompt", "test")
        state.set("high_fidelity_span_ids", "{}")
        state.set("pending_subagents", '{"agent-1": {"agent_id": "agent-1"}}')
        ToolBuffer(state).record_start("tool-1", tool_name="Read")

        _handle_stop_failure({"error": "crash", "error_details": "segfault"})

        assert [item.tool_use_id for item in ToolBuffer(state).all()] == ["tool-1"]
        assert state.get("pending_subagents") is not None
        assert state.get("high_fidelity_span_ids") is None

    def test_stop_failure_failed_export_retains_turn_buffers(self, mock_resolve, state):
        state.set("current_trace_id", "t" * 32)
        state.set("current_trace_span_id", "s" * 16)
        state.set("current_trace_start_time", "1000")
        state.set("current_trace_prompt", "test")
        state.set("high_fidelity_span_ids", "{}")
        state.set("pending_subagents", '{"agent-1": {"agent_id": "agent-1"}}')
        ToolBuffer(state).record_start("tool-1", tool_name="Read")

        with mock.patch("tracing.claude_code.hooks.handlers.send_span", return_value=False):
            _handle_stop_failure({"error": "crash", "error_details": "segfault"})

        assert state.get("current_trace_id") == "t" * 32
        assert state.get("pending_subagents") is not None
        assert state.get("high_fidelity_span_ids") is not None
        assert [item.tool_use_id for item in ToolBuffer(state).all()] == ["tool-1"]

    def test_stop_failure_returns_early_without_trace_state(self, mock_resolve, state, captured_spans):
        """No current_trace_id → returns without sending."""
        _handle_stop_failure(
            {
                "error": "rate_limit",
                "error_details": "429",
                "last_assistant_message": "API Error",
            }
        )
        assert len(captured_spans) == 0

    def test_stop_failure_fallback_output(self, mock_resolve, state, captured_spans):
        """When last_assistant_message is empty, output falls back to error description."""
        state.set("current_trace_id", "t" * 32)
        state.set("current_trace_span_id", "s" * 16)
        state.set("current_trace_start_time", "1000")
        state.set("current_trace_prompt", "test")
        _handle_stop_failure({"error": "timeout", "error_details": "timed out"})
        span = captured_spans[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        attrs = {a["key"]: a["value"] for a in span["attributes"]}
        assert attrs["output.value"]["stringValue"] == "(Stop failed: timeout)"

    def test_stop_failure_cleans_up_state(self, mock_resolve, state, captured_spans):
        """StopFailure cleans up trace state like Stop does."""
        state.set("current_trace_id", "t" * 32)
        state.set("current_trace_span_id", "s" * 16)
        state.set("current_trace_start_time", "1000")
        state.set("current_trace_prompt", "test")
        state.set("trace_start_line", "0")
        _handle_stop_failure({"error": "crash", "error_details": "segfault"})
        assert state.get("current_trace_id") is None
        assert state.get("current_trace_span_id") is None
        assert state.get("current_trace_start_time") is None
        assert state.get("current_trace_prompt") is None
        assert state.get("trace_start_line") is None


# ---------------------------------------------------------------------------
# notification tests
# ---------------------------------------------------------------------------


class TestNotification:

    def test_builds_chain_span(self, mock_resolve, state, captured_spans):
        """Builds CHAIN span with notification attributes."""
        state.set("current_trace_id", "t" * 32)
        state.set("current_trace_span_id", "s" * 16)
        _handle_notification(
            {
                "message": "Build succeeded",
                "title": "CI",
                "type": "success",
            }
        )
        assert len(captured_spans) == 1
        span = captured_spans[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        attrs = {a["key"]: a["value"] for a in span["attributes"]}
        assert attrs["openinference.span.kind"]["stringValue"] == "CHAIN"
        assert attrs["notification.message"]["stringValue"] == "Build succeeded"
        assert attrs["notification.title"]["stringValue"] == "CI"
        assert attrs["notification.type"]["stringValue"] == "success"

    def test_default_notification_type(self, mock_resolve, state, captured_spans):
        """Default notification_type is 'info'."""
        state.set("current_trace_id", "t" * 32)
        _handle_notification({"message": "hello"})
        span = captured_spans[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        attrs = {a["key"]: a["value"] for a in span["attributes"]}
        assert attrs["notification.type"]["stringValue"] == "info"

    def test_no_trace_id_returns_early(self, mock_resolve, state, captured_spans):
        """No current_trace_id → no span sent."""
        _handle_notification({"message": "test"})
        assert len(captured_spans) == 0


# ---------------------------------------------------------------------------
# permission_request tests
# ---------------------------------------------------------------------------


class TestPermissionRequest:

    def test_builds_chain_span(self, mock_resolve, state, captured_spans):
        """Builds CHAIN span with permission attributes."""
        state.set("current_trace_id", "t" * 32)
        state.set("current_trace_span_id", "s" * 16)
        _handle_permission_request(
            {
                "permission": "allow",
                "tool_name": "Bash",
                "tool_input": {"command": "rm -rf /"},
            }
        )
        assert len(captured_spans) == 1
        span = captured_spans[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        attrs = {a["key"]: a["value"] for a in span["attributes"]}
        assert attrs["openinference.span.kind"]["stringValue"] == "CHAIN"
        assert attrs["permission.type"]["stringValue"] == "allow"
        assert attrs["permission.tool"]["stringValue"] == "Bash"
        assert "command" in attrs["input.value"]["stringValue"]

    def test_logs_debug_input(self, mock_resolve, state, captured_spans):
        """Logs debug input via log()."""
        state.set("current_trace_id", "t" * 32)
        with mock.patch("tracing.claude_code.hooks.handlers.log") as log_mock:
            _handle_permission_request({"permission": "deny", "tool_name": "Edit"})
        log_mock.assert_called_once()
        assert "permission_request" in log_mock.call_args[0][0]

    def test_no_trace_id_returns_early(self, mock_resolve, state, captured_spans):
        """No current_trace_id → no span sent."""
        _handle_permission_request({"permission": "allow"})
        assert len(captured_spans) == 0


# ---------------------------------------------------------------------------
# session_end tests
# ---------------------------------------------------------------------------


class TestSessionEnd:

    def test_logs_session_summary(self, mock_resolve, state):
        """Logs session summary via error()."""
        state.set("trace_count", "10")
        state.set("tool_count", "25")
        with (
            mock.patch("tracing.claude_code.hooks.handlers.error") as err_mock,
            mock.patch("tracing.claude_code.hooks.handlers.gc_stale_state_files"),
        ):
            _handle_session_end({})
        calls = [c[0][0] for c in err_mock.call_args_list]
        assert any("10 traces" in c for c in calls)
        assert any("25 tools" in c for c in calls)

    def test_removes_state_file(self, mock_resolve, state, tmp_path):
        """Removes state file and lock dir."""
        assert state.state_file.exists()
        with (
            mock.patch("tracing.claude_code.hooks.handlers.error"),
            mock.patch("tracing.claude_code.hooks.handlers.gc_stale_state_files"),
        ):
            _handle_session_end({})
        assert not state.state_file.exists()

    def test_calls_gc(self, mock_resolve, state):
        """Calls gc_stale_state_files."""
        with (
            mock.patch("tracing.claude_code.hooks.handlers.error"),
            mock.patch("tracing.claude_code.hooks.handlers.gc_stale_state_files") as gc_mock,
        ):
            _handle_session_end({})
        gc_mock.assert_called_once()

    def test_graceful_when_session_id_none(self, state):
        """Returns early when session_id is None."""
        state.delete("session_id")
        with (
            mock.patch("tracing.claude_code.hooks.handlers.resolve_session", return_value=state),
            mock.patch("tracing.claude_code.hooks.handlers.error") as err_mock,
            mock.patch("tracing.claude_code.hooks.handlers.gc_stale_state_files") as gc_mock,
        ):
            _handle_session_end({})
        err_mock.assert_not_called()
        gc_mock.assert_not_called()


# ---------------------------------------------------------------------------
# Error handling tests
# ---------------------------------------------------------------------------


class TestErrorHandling:

    def test_exception_caught_by_entry_point(self, monkeypatch, capsys):
        """Exception in _handle_session_start → entry point catches, calls error()."""
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        with (
            mock.patch("tracing.claude_code.hooks.handlers._read_stdin", return_value={}),
            mock.patch("tracing.claude_code.hooks.handlers.check_requirements", return_value=True),
            mock.patch("tracing.claude_code.hooks.handlers._handle_session_start", side_effect=RuntimeError("boom")),
        ):
            session_start()
        captured = capsys.readouterr()
        assert "boom" in captured.err

    def test_malformed_stdin_no_crash(self, monkeypatch, capsys):
        """Malformed stdin JSON in entry point doesn't crash."""
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        with (
            mock.patch("tracing.claude_code.hooks.handlers.check_requirements", return_value=True),
            mock.patch.object(sys, "stdin", new=__import__("io").StringIO("not valid json")),
            mock.patch("tracing.claude_code.hooks.handlers.resolve_session") as rs,
            mock.patch("tracing.claude_code.hooks.handlers.ensure_session_initialized"),
        ):
            session_start()
        # _read_stdin returns {} on invalid JSON, so resolve_session is called with {}
        rs.assert_called_once_with({})


# ---------------------------------------------------------------------------
# Entry point tests (all 9 CLI wrappers)
# ---------------------------------------------------------------------------

ENTRY_POINTS = [
    ("session_start", session_start, "_handle_session_start"),
    ("pre_tool_use", pre_tool_use, "_handle_pre_tool_use"),
    ("post_tool_use", post_tool_use, "_handle_post_tool_use"),
    ("user_prompt_submit", user_prompt_submit, "_handle_user_prompt_submit"),
    ("stop", stop, "_handle_stop"),
    ("subagent_stop", subagent_stop, "_handle_subagent_stop"),
    ("stop_failure", stop_failure, "_handle_stop_failure"),
    ("notification", notification, "_handle_notification"),
    ("permission_request", permission_request, "_handle_permission_request"),
    ("session_end", session_end, "_handle_session_end"),
]


class TestEntryPoints:

    @pytest.mark.parametrize("name,entry_fn,handler_name", ENTRY_POINTS)
    def test_happy_path_calls_handler(self, name, entry_fn, handler_name):
        """Entry point calls the corresponding _handle_* with parsed stdin JSON."""
        input_data = {"session_id": "s1"}
        with (
            mock.patch("tracing.claude_code.hooks.handlers.check_requirements", return_value=True),
            mock.patch("tracing.claude_code.hooks.handlers._read_stdin", return_value=input_data),
            mock.patch(f"tracing.claude_code.hooks.handlers.{handler_name}") as handler_mock,
        ):
            entry_fn()
        handler_mock.assert_called_once_with(input_data)

    @pytest.mark.parametrize("name,entry_fn,handler_name", ENTRY_POINTS)
    def test_requirements_not_met_skips_handler(self, name, entry_fn, handler_name):
        """When check_requirements returns False, handler is NOT called."""
        with (
            mock.patch("tracing.claude_code.hooks.handlers.check_requirements", return_value=False),
            mock.patch(f"tracing.claude_code.hooks.handlers.{handler_name}") as handler_mock,
        ):
            entry_fn()
        handler_mock.assert_not_called()

    @pytest.mark.parametrize("name,entry_fn,handler_name", ENTRY_POINTS)
    def test_exception_caught_and_logged(self, name, entry_fn, handler_name, capsys):
        """Handler exception is caught; error is logged to stderr, no raise."""
        with (
            mock.patch("tracing.claude_code.hooks.handlers.check_requirements", return_value=True),
            mock.patch("tracing.claude_code.hooks.handlers._read_stdin", return_value={}),
            mock.patch(f"tracing.claude_code.hooks.handlers.{handler_name}", side_effect=RuntimeError("test-boom")),
        ):
            entry_fn()  # should not raise
        captured = capsys.readouterr()
        assert "test-boom" in captured.err


# ---------------------------------------------------------------------------
# Content redaction (security flags)
# ---------------------------------------------------------------------------


def _attrs(span):
    """Helper: extract a span payload's attributes as a {key: value-dict} mapping."""
    return {a["key"]: a["value"] for a in span["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["attributes"]}


class TestContentRedaction:
    """Verify ARIZE_LOG_PROMPTS / TOOL_DETAILS / TOOL_CONTENT control span content."""

    @pytest.fixture(autouse=True)
    def _redaction_defaults(self, monkeypatch):
        # Override the module-level _enable_logging fixture: turn everything off
        # except prompts (which defaults on per the design).
        monkeypatch.setenv("ARIZE_LOG_PROMPTS", "true")
        monkeypatch.setenv("ARIZE_LOG_TOOL_DETAILS", "false")
        monkeypatch.setenv("ARIZE_LOG_TOOL_CONTENT", "false")

    def test_post_tool_use_redacts_content_and_details_by_default(self, mock_resolve, state, captured_spans):
        state.set("current_trace_id", "trace-abc")
        state.set("current_trace_span_id", "span-parent")
        _handle_post_tool_use(
            {
                "tool_name": "Read",
                "tool_use_id": "t1",
                "tool_input": {"file_path": "/secret/path.py"},
                "tool_response": "secret content",
            }
        )
        attrs = _attrs(captured_spans[0])
        assert attrs["input.value"]["stringValue"].startswith("<redacted (")
        assert attrs["output.value"]["stringValue"].startswith("<redacted (")
        assert attrs["tool.file_path"]["stringValue"].startswith("<redacted (")
        assert attrs["tool.description"]["stringValue"].startswith("<redacted (")

    def test_post_tool_use_no_zero_redacted_for_empty_optional_fields(self, mock_resolve, state, captured_spans):
        """Non-Bash tools should not emit a `<redacted (0 chars)>` tool.command attr."""
        state.set("current_trace_id", "trace-abc")
        state.set("current_trace_span_id", "span-parent")
        _handle_post_tool_use(
            {
                "tool_name": "Read",
                "tool_use_id": "t1",
                "tool_input": {"file_path": "/foo.py"},
                "tool_response": "x",
            }
        )
        attrs = _attrs(captured_spans[0])
        assert "tool.command" not in attrs
        assert "tool.url" not in attrs
        assert "tool.query" not in attrs

    def test_user_prompt_redacted_at_span_emit_when_flag_off(self, mock_resolve, state, captured_spans, monkeypatch):
        """Prompts are stored RAW in state and redacted only when the span is built."""
        monkeypatch.setenv("ARIZE_LOG_PROMPTS", "false")
        _handle_user_prompt_submit({"prompt": "secret prompt", "session_id": "s1"})
        # State holds the raw value
        assert state.get("current_trace_prompt") == "secret prompt"
        # Build the Stop span and confirm input.value comes out redacted
        _handle_stop({"session_id": "s1"})
        attrs = _attrs(captured_spans[0])
        assert attrs["input.value"]["stringValue"].startswith("<redacted (")

    def test_user_prompt_kept_when_flag_on(self, mock_resolve, state):
        _handle_user_prompt_submit({"prompt": "hello", "session_id": "s1"})
        assert state.get("current_trace_prompt") == "hello"

    def test_permission_request_redacts_tool_input(self, mock_resolve, state, captured_spans):
        state.set("current_trace_id", "trace-abc")
        state.set("current_trace_span_id", "span-parent")
        _handle_permission_request({"permission": "ask", "tool_name": "Bash", "tool_input": {"command": "rm -rf /"}})
        attrs = _attrs(captured_spans[0])
        assert attrs["input.value"]["stringValue"].startswith("<redacted (")

    def test_notification_redacts_message_when_prompts_off(self, mock_resolve, state, captured_spans, monkeypatch):
        monkeypatch.setenv("ARIZE_LOG_PROMPTS", "false")
        state.set("current_trace_id", "trace-abc")
        state.set("current_trace_span_id", "span-parent")
        _handle_notification({"message": "hi", "title": "alert", "type": "info"})
        attrs = _attrs(captured_spans[0])
        assert attrs["notification.message"]["stringValue"].startswith("<redacted (")
        assert attrs["notification.title"]["stringValue"].startswith("<redacted (")
