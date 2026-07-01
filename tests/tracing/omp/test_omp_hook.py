#!/usr/bin/env python3
r"""Tests for tracing.omp.hooks.handlers — the omp stateful event handler.

omp is in-process (Bun runtime): a tiny TypeScript shim spawns this Python
entry point once per lifecycle event, piping the event payload to stdin as a
single JSON object of the form:

    { "type": "before_agent_start" | "turn_end" | "agent_end" | "session_shutdown",
      "sessionId": "abc123",
      "...": "type-specific fields" }

Unlike opencode, omp's lifecycle events fire EXACTLY ONCE each and already
carry final, structured data (the prompt, the completed AssistantMessage with
inline usage + model, and that turn's toolResults). So this handler is a small
state machine keyed by session id — structurally like the gemini handler — and
needs NO message-id / callID dedup.

Span tree (one trace per agent run):
    Turn (CHAIN, root)             <- before_agent_start opens it, agent_end closes it
      |- LLM: <model> (LLM)        <- one per turn_end
      \- <tool> (TOOL)             <- one per ToolResultMessage, child of the Turn root

Tests are modelled after tests/tracing/opencode/test_opencode_hook.py.
"""
from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path
from unittest import mock

import pytest

from core.common import StateManager

# Force synchronous send_span path in any handler that spawns a fork.
os.environ["ARIZE_DISABLE_FORK"] = "true"

# Import handlers (this import is what fails first in pure-TDD: the module does
# not exist yet). The remaining tests then exercise its surface.
from tracing.omp.hooks.handlers import (  # noqa: E402
    _assistant_text,
    _handle_agent_end,
    _handle_before_agent_start,
    _handle_session_shutdown,
    _handle_turn_end,
    _read_stdin,
    _send_span_async,
    _text_of_content,
    _tool_calls,
    _user_prompt,
    main,
)

FIXTURES = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_span(span_payload):
    return span_payload["resourceSpans"][0]["scopeSpans"][0]["spans"][0]


def _get_attrs(span_payload):
    span = _get_span(span_payload)
    return {a["key"]: a["value"] for a in span["attributes"]}


def _kind(payload):
    return _get_attrs(payload)["openinference.span.kind"]["stringValue"]


def _name(payload):
    return _get_span(payload)["name"]


def _load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _by_kind(spans, kind):
    return [s for s in spans if _kind(s) == kind]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def state(tmp_path):
    """StateManager with pre-initialized session keys."""
    sf = tmp_path / "state_test.json"
    lp = tmp_path / ".lock_test"
    sm = StateManager(state_dir=tmp_path, state_file=sf, lock_path=lp)
    sm.init_state()
    sm.set("session_id", "omp_sess_1")
    sm.set("project_name", "test-omp-project")
    sm.set("trace_count", "0")
    sm.set("tool_count", "0")
    sm.set("user_id", "test-user")
    sm.set("session_start_time", "1000")
    return sm


@pytest.fixture
def mock_resolve(state):
    with mock.patch("tracing.omp.hooks.handlers.resolve_session", return_value=state) as m:
        yield m


@pytest.fixture
def mock_ensure():
    with mock.patch("tracing.omp.hooks.handlers.ensure_session_initialized") as m:
        yield m


@pytest.fixture
def captured_spans():
    """Patch _send_span_async to capture emitted span payloads in order."""
    sent = []
    with mock.patch(
        "tracing.omp.hooks.handlers._send_span_async",
        side_effect=lambda s: sent.append(s),
    ):
        yield sent


def _run_basic_turn(state):
    """Drive before_agent_start + turn_end_basic, returning (trace_id, span_id)
    captured BEFORE agent_end clears them."""
    _handle_before_agent_start(_load_fixture("before_agent_start.json"))
    _handle_turn_end(_load_fixture("turn_end_basic.json"))
    return state.get("current_trace_id"), state.get("current_trace_span_id")


# ---------------------------------------------------------------------------
# Pure helper functions
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_assistant_text_joins_text_items_only(self):
        message = {
            "content": [
                {"type": "text", "text": "Hello "},
                {"type": "thinking", "text": "SECRET"},
                {"type": "toolCall", "id": "c1", "name": "bash", "arguments": {}},
                {"type": "text", "text": "world"},
            ]
        }
        assert _assistant_text(message) == "Hello world"

    def test_assistant_text_empty_when_no_text(self):
        message = {"content": [{"type": "toolCall", "id": "c1", "name": "x", "arguments": {}}]}
        assert _assistant_text(message) == ""

    def test_assistant_text_handles_missing_content(self):
        assert _assistant_text({}) == ""
        assert _assistant_text({"content": None}) == ""

    def test_user_prompt_returns_string_as_is(self):
        assert _user_prompt("do the thing") == "do the thing"

    def test_user_prompt_coerces_non_string_to_empty(self):
        assert _user_prompt(None) == ""
        assert _user_prompt(["a", "b"]) == ""
        assert _user_prompt({"x": 1}) == ""
        assert _user_prompt(1) == ""

    def test_tool_calls_maps_id_to_name_and_args(self):
        message = {
            "content": [
                {"type": "text", "text": "ok"},
                {"type": "toolCall", "id": "c1", "name": "bash", "arguments": {"command": "ls"}},
                {"type": "toolCall", "id": "c2", "name": "edit", "arguments": {"filePath": "/a"}},
            ]
        }
        calls = _tool_calls(message)
        assert set(calls.keys()) == {"c1", "c2"}
        assert calls["c1"]["name"] == "bash"
        assert calls["c1"]["arguments"] == {"command": "ls"}
        assert calls["c2"]["arguments"] == {"filePath": "/a"}

    def test_tool_calls_empty_when_no_tool_calls(self):
        assert _tool_calls({"content": [{"type": "text", "text": "hi"}]}) == {}
        assert _tool_calls({}) == {}

    def test_text_of_content_joins_text_parts(self):
        content = [
            {"type": "text", "text": "a"},
            {"type": "image"},
            {"type": "text", "text": "b"},
        ]
        assert _text_of_content(content) == "ab"

    def test_text_of_content_handles_empty(self):
        assert _text_of_content([]) == ""
        assert _text_of_content(None) == ""


# ---------------------------------------------------------------------------
# _read_stdin
# ---------------------------------------------------------------------------


class TestReadStdin:
    def test_empty_stdin(self):
        with mock.patch.object(sys, "stdin", new=io.StringIO("")):
            assert _read_stdin() == {}

    def test_malformed_json(self):
        with mock.patch.object(sys, "stdin", new=io.StringIO("not json")):
            assert _read_stdin() == {}

    def test_valid_json(self):
        with mock.patch.object(sys, "stdin", new=io.StringIO('{"type":"agent_end","sessionId":"x"}')):
            assert _read_stdin() == {"type": "agent_end", "sessionId": "x"}


# ---------------------------------------------------------------------------
# _send_span_async (test fork-disable)
# ---------------------------------------------------------------------------


class TestSendSpanAsync:
    def test_uses_sync_send_when_fork_disabled(self, monkeypatch):
        """ARIZE_DISABLE_FORK=true -> bypass fork, call send_span synchronously."""
        monkeypatch.setenv("ARIZE_DISABLE_FORK", "true")
        with mock.patch("tracing.omp.hooks.handlers.send_span") as ss:
            _send_span_async({"resourceSpans": []})
        ss.assert_called_once_with({"resourceSpans": []})


# ---------------------------------------------------------------------------
# before_agent_start — opens a trace (one per agent run)
# ---------------------------------------------------------------------------


class TestBeforeAgentStart:
    def test_opens_trace_state(self, mock_resolve, mock_ensure, state, captured_spans):
        _handle_before_agent_start(_load_fixture("before_agent_start.json"))
        assert state.get("current_trace_id") is not None
        assert len(state.get("current_trace_id")) == 32
        assert state.get("current_trace_span_id") is not None
        assert len(state.get("current_trace_span_id")) == 16
        assert state.get("current_trace_prompt") == "list files and edit main.py"
        assert state.get("current_trace_start_time") is not None
        # current_final_output initialized to empty string.
        assert state.get("current_final_output") == ""

    def test_emits_no_span_on_open(self, mock_resolve, mock_ensure, state, captured_spans):
        """Opening a trace does NOT emit a span (the Turn root is emitted on agent_end)."""
        _handle_before_agent_start(_load_fixture("before_agent_start.json"))
        assert captured_spans == []

    def test_increments_trace_count(self, mock_resolve, mock_ensure, state, captured_spans):
        _handle_before_agent_start(_load_fixture("before_agent_start.json"))
        assert state.get("trace_count") == "1"

    def test_force_closes_prior_open_trace(self, mock_resolve, mock_ensure, state, captured_spans):
        """A still-open prior trace is force-closed (CHAIN emitted) before the new one opens."""
        state.set("current_trace_id", "p" * 32)
        state.set("current_trace_span_id", "q" * 16)
        state.set("current_trace_start_time", "500")
        state.set("current_trace_prompt", "prior prompt")
        state.set("current_final_output", "")

        _handle_before_agent_start(_load_fixture("before_agent_start.json"))

        chains = _by_kind(captured_spans, "CHAIN")
        assert len(chains) == 1
        span = _get_span(chains[0])
        assert span["traceId"] == "p" * 32
        assert span["spanId"] == "q" * 16
        assert "parentSpanId" not in span
        # New trace has fresh ids and the new prompt.
        assert state.get("current_trace_id") != "p" * 32
        assert state.get("current_trace_prompt") == "list files and edit main.py"


# ---------------------------------------------------------------------------
# turn_end — emits LLM + TOOL children of the open Turn root
# ---------------------------------------------------------------------------


class TestTurnEndBasic:
    def test_emits_one_llm_and_two_tool_spans(self, mock_resolve, mock_ensure, state, captured_spans):
        _run_basic_turn(state)
        assert len(captured_spans) == 3
        kinds = sorted(_kind(s) for s in captured_spans)
        assert kinds == ["LLM", "TOOL", "TOOL"]

    def test_no_chain_on_turn_end(self, mock_resolve, mock_ensure, state, captured_spans):
        """A turn_end emits child spans only — never the Turn CHAIN root."""
        _run_basic_turn(state)
        assert not _by_kind(captured_spans, "CHAIN")

    def test_causal_order_llm_then_tools(self, mock_resolve, mock_ensure, state, captured_spans):
        _run_basic_turn(state)
        assert _kind(captured_spans[0]) == "LLM"
        assert _kind(captured_spans[1]) == "TOOL"
        assert _kind(captured_spans[2]) == "TOOL"

    def test_increments_tool_count(self, mock_resolve, mock_ensure, state, captured_spans):
        _run_basic_turn(state)
        assert state.get("tool_count") == "2"

    def test_lazily_opens_trace_when_none(self, mock_resolve, mock_ensure, state, captured_spans):
        """A turn_end with no open trace lazily opens one (empty prompt) so spans
        always get a root to hang from."""
        _handle_turn_end(_load_fixture("turn_end_basic.json"))
        assert state.get("current_trace_id") is not None
        assert state.get("current_trace_span_id") is not None
        # Lazily opened trace has an empty prompt.
        assert state.get("current_trace_prompt") == ""
        # The child spans still got emitted under the lazily-opened root.
        for s in captured_spans:
            assert _get_span(s)["traceId"] == state.get("current_trace_id")
            assert _get_span(s)["parentSpanId"] == state.get("current_trace_span_id")


class TestTurnEndLLMSpan:
    def test_llm_span_name_includes_model(self, mock_resolve, mock_ensure, state, captured_spans):
        _run_basic_turn(state)
        llm = _by_kind(captured_spans, "LLM")[0]
        assert _name(llm) == "LLM: claude-sonnet-4"

    def test_llm_token_counts(self, mock_resolve, mock_ensure, state, captured_spans):
        _run_basic_turn(state)
        attrs = _get_attrs(_by_kind(captured_spans, "LLM")[0])
        # OpenInference: prompt is the inclusive total
        # = input(100) + cacheRead(30) + cacheWrite(5) = 135
        assert attrs["llm.token_count.prompt"]["intValue"] == 135
        assert attrs["llm.token_count.completion"]["intValue"] == 50
        assert attrs["llm.token_count.total"]["intValue"] == 185
        assert attrs["llm.token_count.completion_details.reasoning"]["intValue"] == 7
        assert attrs["llm.token_count.prompt_details.cache_read"]["intValue"] == 30
        assert attrs["llm.token_count.prompt_details.cache_write"]["intValue"] == 5

    def test_llm_model_and_provider(self, mock_resolve, mock_ensure, state, captured_spans):
        _run_basic_turn(state)
        attrs = _get_attrs(_by_kind(captured_spans, "LLM")[0])
        assert attrs["llm.model_name"]["stringValue"] == "claude-sonnet-4"
        assert attrs["llm.provider"]["stringValue"] == "anthropic"

    def test_llm_cost(self, mock_resolve, mock_ensure, state, captured_spans):
        _run_basic_turn(state)
        attrs = _get_attrs(_by_kind(captured_spans, "LLM")[0])
        cost = attrs["llm.cost"]
        cost_val = cost.get("doubleValue") if isinstance(cost, dict) else None
        if cost_val is None:
            cost_val = float(next(iter(cost.values())))
        assert cost_val == pytest.approx(0.0125)

    def test_llm_input_output_values(self, mock_resolve, mock_ensure, state, captured_spans):
        _run_basic_turn(state)
        attrs = _get_attrs(_by_kind(captured_spans, "LLM")[0])
        # input.value = the turn prompt (from before_agent_start)
        assert attrs["input.value"]["stringValue"] == "list files and edit main.py"
        # output.value = the assistant text (thinking/toolCall items skipped)
        assert attrs["output.value"]["stringValue"] == "I'll list files then edit."

    def test_llm_common_attrs(self, mock_resolve, mock_ensure, state, captured_spans):
        _run_basic_turn(state)
        attrs = _get_attrs(_by_kind(captured_spans, "LLM")[0])
        assert attrs["session.id"]["stringValue"] == "omp_sess_1"
        assert attrs["project.name"]["stringValue"] == "test-omp-project"
        assert attrs["openinference.span.kind"]["stringValue"] == "LLM"

    def test_llm_timing_uses_timestamp_and_duration(self, mock_resolve, mock_ensure, state, captured_spans):
        """end = message.timestamp (5000ms); start = end - duration (3000ms) = 2000ms."""
        _run_basic_turn(state)
        span = _get_span(_by_kind(captured_spans, "LLM")[0])
        assert span["endTimeUnixNano"] == "5000000000"
        assert span["startTimeUnixNano"] == "2000000000"

    def test_llm_child_of_turn_root(self, mock_resolve, mock_ensure, state, captured_spans):
        trace_id, span_id = _run_basic_turn(state)
        span = _get_span(_by_kind(captured_spans, "LLM")[0])
        assert span["traceId"] == trace_id
        assert span["parentSpanId"] == span_id

    def test_llm_timing_no_duration_uses_timestamp_for_both(self, mock_resolve, mock_ensure, state, captured_spans):
        """A turn_end whose message has no duration uses timestamp for both ends."""
        _handle_before_agent_start(_load_fixture("before_agent_start.json"))
        _handle_turn_end(_load_fixture("turn_end_tool_error.json"))
        span = _get_span(_by_kind(captured_spans, "LLM")[0])
        assert span["startTimeUnixNano"] == "3000000000"
        assert span["endTimeUnixNano"] == "3000000000"

    def test_sets_current_final_output(self, mock_resolve, mock_ensure, state, captured_spans):
        """The last turn's assistant text is stashed so agent_end can report it."""
        _run_basic_turn(state)
        assert state.get("current_final_output") == "I'll list files then edit."


class TestTurnEndToolSpans:
    def test_tool_spans_pair_args_with_results(self, mock_resolve, mock_ensure, state, captured_spans):
        _run_basic_turn(state)
        tools = _by_kind(captured_spans, "TOOL")
        bash = next(s for s in tools if _name(s) == "bash")
        edit = next(s for s in tools if _name(s) == "edit")
        bash_attrs = _get_attrs(bash)
        edit_attrs = _get_attrs(edit)
        # input.value = json.dumps of the matched ToolCall.arguments
        assert bash_attrs["input.value"]["stringValue"] == json.dumps({"command": "ls -la"})
        assert edit_attrs["input.value"]["stringValue"] == json.dumps({"filePath": "/tmp/main.py"})
        # output.value = joined text of the ToolResultMessage.content
        assert bash_attrs["output.value"]["stringValue"] == "total 4\nfile.py"
        assert edit_attrs["output.value"]["stringValue"] == "edited main.py"

    def test_tool_name_attr(self, mock_resolve, mock_ensure, state, captured_spans):
        _run_basic_turn(state)
        bash = next(s for s in _by_kind(captured_spans, "TOOL") if _name(s) == "bash")
        assert _get_attrs(bash)["tool.name"]["stringValue"] == "bash"

    def test_tools_are_children_of_turn_root_not_llm(self, mock_resolve, mock_ensure, state, captured_spans):
        """TOOL spans hang off the Turn root span id, NOT the LLM span."""
        trace_id, span_id = _run_basic_turn(state)
        llm_span_id = _get_span(_by_kind(captured_spans, "LLM")[0])["spanId"]
        for tool in _by_kind(captured_spans, "TOOL"):
            span = _get_span(tool)
            assert span["traceId"] == trace_id
            assert span["parentSpanId"] == span_id
            assert span["parentSpanId"] != llm_span_id

    def test_tool_timing_from_result_timestamp(self, mock_resolve, mock_ensure, state, captured_spans):
        """TOOL span end uses ToolResultMessage.timestamp (bash result = 5100ms)."""
        _run_basic_turn(state)
        bash = next(s for s in _by_kind(captured_spans, "TOOL") if _name(s) == "bash")
        assert _get_span(bash)["endTimeUnixNano"] == "5100000000"

    def test_per_tool_bash_command(self, mock_resolve, mock_ensure, state, captured_spans):
        _run_basic_turn(state)
        bash = next(s for s in _by_kind(captured_spans, "TOOL") if _name(s) == "bash")
        assert _get_attrs(bash)["tool.command"]["stringValue"] == "ls -la"

    def test_per_tool_edit_file_path(self, mock_resolve, mock_ensure, state, captured_spans):
        _run_basic_turn(state)
        edit = next(s for s in _by_kind(captured_spans, "TOOL") if _name(s) == "edit")
        assert _get_attrs(edit)["tool.file_path"]["stringValue"] == "/tmp/main.py"

    def test_user_id_included(self, mock_resolve, mock_ensure, state, captured_spans):
        _run_basic_turn(state)
        for s in captured_spans:
            assert _get_attrs(s)["user.id"]["stringValue"] == "test-user"

    def test_user_id_omitted_when_empty(self, mock_resolve, mock_ensure, state, captured_spans):
        state.set("user_id", "")
        _run_basic_turn(state)
        for s in captured_spans:
            assert "user.id" not in _get_attrs(s)

    def test_result_without_matching_call_emits_args_unknown(self, mock_resolve, mock_ensure, state, captured_spans):
        """A toolResult with no matching ToolCall in this payload still emits a
        TOOL span — with empty args (args unknown)."""
        payload = {
            "type": "turn_end",
            "sessionId": "omp_sess_1",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "ok"}],
                "model": "claude-sonnet-4",
                "provider": "anthropic",
                "usage": {"input": 1, "output": 1, "cacheRead": 0, "cacheWrite": 0},
                "timestamp": 100,
            },
            "toolResults": [
                {
                    "role": "toolResult",
                    "toolCallId": "orphan_call",
                    "toolName": "bash",
                    "content": [{"type": "text", "text": "out"}],
                    "isError": False,
                    "timestamp": 120,
                }
            ],
        }
        _handle_before_agent_start(_load_fixture("before_agent_start.json"))
        _handle_turn_end(payload)
        tool = _by_kind(captured_spans, "TOOL")[0]
        attrs = _get_attrs(tool)
        assert attrs["tool.name"]["stringValue"] == "bash"
        assert attrs["input.value"]["stringValue"] == json.dumps({})
        assert attrs["output.value"]["stringValue"] == "out"


# ---------------------------------------------------------------------------
# Multiple turn_ends between one before_agent_start and one agent_end
# ---------------------------------------------------------------------------


class TestMultipleTurnEnds:
    def test_two_turn_ends_emit_two_llm_under_same_root(self, mock_resolve, mock_ensure, state, captured_spans):
        _handle_before_agent_start(_load_fixture("before_agent_start.json"))
        trace_id = state.get("current_trace_id")
        span_id = state.get("current_trace_span_id")

        _handle_turn_end(_load_fixture("turn_end_basic.json"))
        _handle_turn_end(_load_fixture("turn_end_tool_error.json"))

        llms = _by_kind(captured_spans, "LLM")
        assert len(llms) == 2
        for llm in llms:
            span = _get_span(llm)
            assert span["traceId"] == trace_id
            assert span["parentSpanId"] == span_id
        # trace_count stays at 1 (one agent run), tools accumulate (2 + 1).
        assert state.get("trace_count") == "1"
        assert state.get("tool_count") == "3"

    def test_final_output_reflects_last_turn(self, mock_resolve, mock_ensure, state, captured_spans):
        _handle_before_agent_start(_load_fixture("before_agent_start.json"))
        _handle_turn_end(_load_fixture("turn_end_basic.json"))
        _handle_turn_end(_load_fixture("turn_end_tool_error.json"))
        assert state.get("current_final_output") == "reading the file"


# ---------------------------------------------------------------------------
# Error tool span — status_code = 2 (ERROR)
# ---------------------------------------------------------------------------


class TestErrorTool:
    def test_error_tool_status_code_2(self, mock_resolve, mock_ensure, state, captured_spans):
        _handle_before_agent_start(_load_fixture("before_agent_start.json"))
        _handle_turn_end(_load_fixture("turn_end_tool_error.json"))
        tool = _by_kind(captured_spans, "TOOL")[0]
        assert _get_span(tool)["status"]["code"] == 2

    def test_error_tool_status_message(self, mock_resolve, mock_ensure, state, captured_spans):
        _handle_before_agent_start(_load_fixture("before_agent_start.json"))
        _handle_turn_end(_load_fixture("turn_end_tool_error.json"))
        tool = _by_kind(captured_spans, "TOOL")[0]
        assert "ENOENT" in _get_span(tool)["status"].get("message", "")

    def test_error_tool_output_is_error_text(self, mock_resolve, mock_ensure, state, captured_spans):
        _handle_before_agent_start(_load_fixture("before_agent_start.json"))
        _handle_turn_end(_load_fixture("turn_end_tool_error.json"))
        tool = _by_kind(captured_spans, "TOOL")[0]
        assert "ENOENT" in _get_attrs(tool)["output.value"]["stringValue"]

    def test_error_tool_file_path_attr(self, mock_resolve, mock_ensure, state, captured_spans):
        _handle_before_agent_start(_load_fixture("before_agent_start.json"))
        _handle_turn_end(_load_fixture("turn_end_tool_error.json"))
        tool = _by_kind(captured_spans, "TOOL")[0]
        assert _get_attrs(tool)["tool.file_path"]["stringValue"] == "/missing.txt"

    def test_error_tool_increments_tool_count(self, mock_resolve, mock_ensure, state, captured_spans):
        _handle_before_agent_start(_load_fixture("before_agent_start.json"))
        _handle_turn_end(_load_fixture("turn_end_tool_error.json"))
        assert state.get("tool_count") == "1"


# ---------------------------------------------------------------------------
# agent_end — emits the Turn CHAIN root and clears state
# ---------------------------------------------------------------------------


class TestAgentEnd:
    def test_full_flow_emits_llm_tools_and_chain(self, mock_resolve, mock_ensure, state, captured_spans):
        """before_agent_start + turn_end (1 LLM + 2 TOOL) + agent_end => 4 spans,
        the CHAIN emitted last (causal order)."""
        _run_basic_turn(state)
        _handle_agent_end(_load_fixture("agent_end.json"))
        assert len(captured_spans) == 4
        assert len(_by_kind(captured_spans, "LLM")) == 1
        assert len(_by_kind(captured_spans, "TOOL")) == 2
        assert len(_by_kind(captured_spans, "CHAIN")) == 1
        # CHAIN is emitted last.
        assert _kind(captured_spans[-1]) == "CHAIN"

    def test_chain_name_and_kind(self, mock_resolve, mock_ensure, state, captured_spans):
        _run_basic_turn(state)
        _handle_agent_end(_load_fixture("agent_end.json"))
        chain = _by_kind(captured_spans, "CHAIN")[0]
        assert _name(chain) == "Turn"
        assert _kind(chain) == "CHAIN"

    def test_chain_uses_turn_root_ids_no_parent(self, mock_resolve, mock_ensure, state, captured_spans):
        trace_id, span_id = _run_basic_turn(state)
        _handle_agent_end(_load_fixture("agent_end.json"))
        chain = _by_kind(captured_spans, "CHAIN")[0]
        span = _get_span(chain)
        assert span["traceId"] == trace_id
        assert span["spanId"] == span_id
        assert "parentSpanId" not in span

    def test_chain_input_and_output(self, mock_resolve, mock_ensure, state, captured_spans):
        _run_basic_turn(state)
        _handle_agent_end(_load_fixture("agent_end.json"))
        attrs = _get_attrs(_by_kind(captured_spans, "CHAIN")[0])
        assert attrs["input.value"]["stringValue"] == "list files and edit main.py"
        # output = the last assistant text in agent_end.messages (authoritative
        # and race-free), NOT the racing current_final_output accumulator.
        assert attrs["output.value"]["stringValue"] == "fallback final answer"

    def test_chain_start_time_from_trace_start(self, mock_resolve, mock_ensure, state, captured_spans):
        _handle_before_agent_start(_load_fixture("before_agent_start.json"))
        start_time = state.get("current_trace_start_time")
        _handle_turn_end(_load_fixture("turn_end_basic.json"))
        _handle_agent_end(_load_fixture("agent_end.json"))
        chain = _by_kind(captured_spans, "CHAIN")[0]
        assert _get_span(chain)["startTimeUnixNano"] == f"{int(start_time)}000000"

    def test_output_prefers_messages_over_stale_accumulator(self, mock_resolve, mock_ensure, state, captured_spans):
        """The final answer fires its own turn_end AND lands in agent_end.messages.
        Because each event runs in a separate detached process racing on shared
        state, agent_end must read its own messages payload, not the accumulator."""
        _handle_before_agent_start(_load_fixture("before_agent_start.json"))
        # Simulate a stale accumulator: a prior turn's text is what a racing
        # turn_end would have left behind when agent_end's process reads state.
        state.set("current_final_output", "intermediate turn text")
        _handle_agent_end(_load_fixture("agent_end.json"))
        attrs = _get_attrs(_by_kind(captured_spans, "CHAIN")[0])
        assert attrs["output.value"]["stringValue"] == "fallback final answer"

    def test_output_fallback_to_accumulator_when_messages_lack_assistant_text(
        self, mock_resolve, mock_ensure, state, captured_spans
    ):
        """When agent_end.messages carries no assistant text (e.g. only a tool
        result), the Turn output falls back to current_final_output."""
        _handle_before_agent_start(_load_fixture("before_agent_start.json"))
        state.set("current_final_output", "accumulated final text")
        _handle_agent_end(
            {
                "type": "agent_end",
                "sessionId": "omp_sess_1",
                "messages": [
                    {"role": "toolResult", "content": [{"type": "text", "text": "tool output"}]},
                ],
            }
        )
        attrs = _get_attrs(_by_kind(captured_spans, "CHAIN")[0])
        assert attrs["output.value"]["stringValue"] == "accumulated final text"

    def test_clears_current_trace_state(self, mock_resolve, mock_ensure, state, captured_spans):
        _run_basic_turn(state)
        _handle_agent_end(_load_fixture("agent_end.json"))
        assert state.get("current_trace_id") is None
        assert state.get("current_trace_span_id") is None
        assert state.get("current_trace_start_time") is None
        assert state.get("current_trace_prompt") is None
        assert state.get("current_final_output") is None

    def test_agent_end_without_open_trace_is_noop(self, mock_resolve, mock_ensure, state, captured_spans):
        """agent_end with no open trace emits nothing (and does not raise)."""
        _handle_agent_end(_load_fixture("agent_end.json"))
        assert captured_spans == []


# ---------------------------------------------------------------------------
# session_shutdown — fail-safe close of a still-open trace
# ---------------------------------------------------------------------------


class TestSessionShutdown:
    def test_open_trace_emits_chain_failsafe(self, mock_resolve, mock_ensure, state, captured_spans):
        """A still-open trace at session_shutdown emits its Turn CHAIN root."""
        _handle_before_agent_start(_load_fixture("before_agent_start.json"))
        trace_id = state.get("current_trace_id")
        span_id = state.get("current_trace_span_id")

        _handle_session_shutdown({"type": "session_shutdown", "sessionId": "omp_sess_1"})

        chains = _by_kind(captured_spans, "CHAIN")
        assert len(chains) == 1
        span = _get_span(chains[0])
        assert span["traceId"] == trace_id
        assert span["spanId"] == span_id
        assert "parentSpanId" not in span

    def test_failsafe_output_uses_final_output_when_present(self, mock_resolve, mock_ensure, state, captured_spans):
        _handle_before_agent_start(_load_fixture("before_agent_start.json"))
        _handle_turn_end(_load_fixture("turn_end_basic.json"))
        _handle_session_shutdown({"type": "session_shutdown", "sessionId": "omp_sess_1"})
        chain = _by_kind(captured_spans, "CHAIN")[0]
        assert _get_attrs(chain)["output.value"]["stringValue"] == "I'll list files then edit."

    def test_failsafe_output_uses_reason_when_no_final_output(self, mock_resolve, mock_ensure, state, captured_spans):
        """Shutdown right after before_agent_start (no turn) closes with a reason marker."""
        _handle_before_agent_start(_load_fixture("before_agent_start.json"))
        _handle_session_shutdown({"type": "session_shutdown", "sessionId": "omp_sess_1"})
        chain = _by_kind(captured_spans, "CHAIN")[0]
        out = _get_attrs(chain)["output.value"]["stringValue"]
        assert "closed" in out.lower()

    def test_clears_state(self, mock_resolve, mock_ensure, state, captured_spans):
        _handle_before_agent_start(_load_fixture("before_agent_start.json"))
        _handle_session_shutdown({"type": "session_shutdown", "sessionId": "omp_sess_1"})
        assert state.get("current_trace_id") is None
        assert state.get("current_trace_span_id") is None

    def test_no_open_trace_is_noop(self, mock_resolve, mock_ensure, state, captured_spans):
        """session_shutdown with no open trace emits nothing and does not raise."""
        _handle_session_shutdown({"type": "session_shutdown", "sessionId": "omp_sess_1"})
        assert captured_spans == []


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------


class TestRedaction:
    def test_redacts_prompt_and_response_when_log_prompts_false(
        self, mock_resolve, mock_ensure, state, captured_spans, monkeypatch
    ):
        monkeypatch.setenv("ARIZE_LOG_PROMPTS", "false")
        _run_basic_turn(state)
        _handle_agent_end(_load_fixture("agent_end.json"))
        llm = _by_kind(captured_spans, "LLM")[0]
        attrs = _get_attrs(llm)
        assert "redacted" in attrs["input.value"]["stringValue"]
        assert "redacted" in attrs["output.value"]["stringValue"]
        # The CHAIN root prompt/output are also redacted.
        chain = _by_kind(captured_spans, "CHAIN")[0]
        cattrs = _get_attrs(chain)
        assert "redacted" in cattrs["input.value"]["stringValue"]
        assert "redacted" in cattrs["output.value"]["stringValue"]

    def test_redacts_tool_content_when_log_tool_content_false(
        self, mock_resolve, mock_ensure, state, captured_spans, monkeypatch
    ):
        monkeypatch.setenv("ARIZE_LOG_TOOL_CONTENT", "false")
        _run_basic_turn(state)
        for t in _by_kind(captured_spans, "TOOL"):
            attrs = _get_attrs(t)
            assert "redacted" in attrs["input.value"]["stringValue"]
            assert "redacted" in attrs["output.value"]["stringValue"]

    def test_redacts_tool_details_when_log_tool_details_false(
        self, mock_resolve, mock_ensure, state, captured_spans, monkeypatch
    ):
        monkeypatch.setenv("ARIZE_LOG_TOOL_DETAILS", "false")
        _run_basic_turn(state)
        bash = next(s for s in _by_kind(captured_spans, "TOOL") if _name(s) == "bash")
        assert "redacted" in _get_attrs(bash)["tool.command"]["stringValue"]

    def test_tool_name_not_redacted(self, mock_resolve, mock_ensure, state, captured_spans, monkeypatch):
        monkeypatch.setenv("ARIZE_LOG_TOOL_DETAILS", "false")
        monkeypatch.setenv("ARIZE_LOG_TOOL_CONTENT", "false")
        _run_basic_turn(state)
        bash = next(s for s in _by_kind(captured_spans, "TOOL") if _name(s) == "bash")
        assert _get_attrs(bash)["tool.name"]["stringValue"] == "bash"


# ---------------------------------------------------------------------------
# Per-tool specialized attribute mapping
# ---------------------------------------------------------------------------


def _make_turn_end_with_tool(tool_name, arguments):
    """A turn_end payload with a single tool call+result for the given tool."""
    return {
        "type": "turn_end",
        "sessionId": "omp_sess_1",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "ok"},
                {"type": "toolCall", "id": "call_x", "name": tool_name, "arguments": arguments},
            ],
            "model": "claude-sonnet-4",
            "provider": "anthropic",
            "usage": {"input": 1, "output": 1, "cacheRead": 0, "cacheWrite": 0, "cost": {"total": 0}},
            "timestamp": 200,
        },
        "toolResults": [
            {
                "role": "toolResult",
                "toolCallId": "call_x",
                "toolName": tool_name,
                "content": [{"type": "text", "text": "tool-out"}],
                "isError": False,
                "timestamp": 220,
            }
        ],
    }


class TestPerToolAttributeMapping:
    def test_read_sets_file_path(self, mock_resolve, mock_ensure, state, captured_spans):
        _handle_before_agent_start(_load_fixture("before_agent_start.json"))
        _handle_turn_end(_make_turn_end_with_tool("read", {"filePath": "/a/b.py"}))
        tool = _by_kind(captured_spans, "TOOL")[0]
        assert _get_attrs(tool)["tool.file_path"]["stringValue"] == "/a/b.py"

    def test_write_sets_file_path(self, mock_resolve, mock_ensure, state, captured_spans):
        _handle_before_agent_start(_load_fixture("before_agent_start.json"))
        _handle_turn_end(_make_turn_end_with_tool("write", {"filePath": "/a/c.py"}))
        tool = _by_kind(captured_spans, "TOOL")[0]
        assert _get_attrs(tool)["tool.file_path"]["stringValue"] == "/a/c.py"

    def test_file_path_falls_back_to_path_key(self, mock_resolve, mock_ensure, state, captured_spans):
        """When `filePath` is absent the handler falls back to `path`."""
        _handle_before_agent_start(_load_fixture("before_agent_start.json"))
        _handle_turn_end(_make_turn_end_with_tool("read", {"path": "/a/d.py"}))
        tool = _by_kind(captured_spans, "TOOL")[0]
        assert _get_attrs(tool)["tool.file_path"]["stringValue"] == "/a/d.py"

    def test_grep_sets_query(self, mock_resolve, mock_ensure, state, captured_spans):
        _handle_before_agent_start(_load_fixture("before_agent_start.json"))
        _handle_turn_end(_make_turn_end_with_tool("grep", {"pattern": "TODO"}))
        tool = _by_kind(captured_spans, "TOOL")[0]
        assert _get_attrs(tool)["tool.query"]["stringValue"] == "TODO"

    def test_glob_sets_query(self, mock_resolve, mock_ensure, state, captured_spans):
        _handle_before_agent_start(_load_fixture("before_agent_start.json"))
        _handle_turn_end(_make_turn_end_with_tool("glob", {"pattern": "**/*.py"}))
        tool = _by_kind(captured_spans, "TOOL")[0]
        assert _get_attrs(tool)["tool.query"]["stringValue"] == "**/*.py"

    def test_unknown_tool_no_specialized_attrs(self, mock_resolve, mock_ensure, state, captured_spans):
        _handle_before_agent_start(_load_fixture("before_agent_start.json"))
        _handle_turn_end(_make_turn_end_with_tool("custom_thing", {"foo": "bar"}))
        tool = _by_kind(captured_spans, "TOOL")[0]
        attrs = _get_attrs(tool)
        assert "tool.command" not in attrs
        assert "tool.file_path" not in attrs
        assert "tool.url" not in attrs
        assert "tool.query" not in attrs
        # tool.name is still present.
        assert attrs["tool.name"]["stringValue"] == "custom_thing"


# ---------------------------------------------------------------------------
# Token-detail omission rules
# ---------------------------------------------------------------------------


class TestTokenDetailOmissions:
    def test_zero_reasoning_omitted(self, mock_resolve, mock_ensure, state, captured_spans):
        _handle_before_agent_start(_load_fixture("before_agent_start.json"))
        _handle_turn_end(_make_turn_end_with_tool("read", {"filePath": "/x"}))
        attrs = _get_attrs(_by_kind(captured_spans, "LLM")[0])
        assert "llm.token_count.completion_details.reasoning" not in attrs

    def test_zero_cache_omitted(self, mock_resolve, mock_ensure, state, captured_spans):
        _handle_before_agent_start(_load_fixture("before_agent_start.json"))
        _handle_turn_end(_make_turn_end_with_tool("read", {"filePath": "/x"}))
        attrs = _get_attrs(_by_kind(captured_spans, "LLM")[0])
        assert "llm.token_count.prompt_details.cache_read" not in attrs
        assert "llm.token_count.prompt_details.cache_write" not in attrs

    def test_zero_cost_omitted(self, mock_resolve, mock_ensure, state, captured_spans):
        _handle_before_agent_start(_load_fixture("before_agent_start.json"))
        _handle_turn_end(_make_turn_end_with_tool("read", {"filePath": "/x"}))
        attrs = _get_attrs(_by_kind(captured_spans, "LLM")[0])
        assert "llm.cost" not in attrs


# ---------------------------------------------------------------------------
# main() entry point dispatch
# ---------------------------------------------------------------------------


class TestMainEntryPoint:
    def _run_main(self, payload):
        with (
            mock.patch("tracing.omp.hooks.handlers.check_requirements", return_value=True),
            mock.patch.object(sys, "stdin", new=io.StringIO(json.dumps(payload))),
            mock.patch("tracing.omp.hooks.handlers._handle_before_agent_start") as bas,
            mock.patch("tracing.omp.hooks.handlers._handle_turn_end") as te,
            mock.patch("tracing.omp.hooks.handlers._handle_agent_end") as ae,
            mock.patch("tracing.omp.hooks.handlers._handle_session_shutdown") as ss,
        ):
            main()
        return bas, te, ae, ss

    def test_dispatches_before_agent_start(self, monkeypatch):
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        payload = {"type": "before_agent_start", "sessionId": "x", "prompt": "hi"}
        bas, te, ae, ss = self._run_main(payload)
        bas.assert_called_once_with(payload)
        te.assert_not_called()
        ae.assert_not_called()
        ss.assert_not_called()

    def test_dispatches_turn_end(self, monkeypatch):
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        payload = {"type": "turn_end", "sessionId": "x", "message": {}, "toolResults": []}
        bas, te, ae, ss = self._run_main(payload)
        te.assert_called_once_with(payload)
        bas.assert_not_called()

    def test_dispatches_agent_end(self, monkeypatch):
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        payload = {"type": "agent_end", "sessionId": "x", "messages": []}
        bas, te, ae, ss = self._run_main(payload)
        ae.assert_called_once_with(payload)

    def test_dispatches_session_shutdown(self, monkeypatch):
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        payload = {"type": "session_shutdown", "sessionId": "x"}
        bas, te, ae, ss = self._run_main(payload)
        ss.assert_called_once_with(payload)

    def test_unknown_type_does_not_dispatch(self, monkeypatch):
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        payload = {"type": "something-else", "sessionId": "x"}
        bas, te, ae, ss = self._run_main(payload)
        for m in (bas, te, ae, ss):
            m.assert_not_called()

    def test_requirements_not_met_short_circuits(self, monkeypatch):
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "false")
        payload = {"type": "turn_end", "sessionId": "x"}
        with (
            mock.patch("tracing.omp.hooks.handlers.check_requirements", return_value=False),
            mock.patch.object(sys, "stdin", new=io.StringIO(json.dumps(payload))),
            mock.patch("tracing.omp.hooks.handlers._handle_turn_end") as te,
        ):
            main()
        te.assert_not_called()

    def test_malformed_stdin_no_crash(self, monkeypatch):
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        with (
            mock.patch("tracing.omp.hooks.handlers.check_requirements", return_value=True),
            mock.patch.object(sys, "stdin", new=io.StringIO("not-json")),
            mock.patch("tracing.omp.hooks.handlers._handle_turn_end") as te,
        ):
            main()  # must not raise
        te.assert_not_called()

    def test_handler_exception_is_caught(self, monkeypatch):
        """A raised exception in a handler must be caught — never escape main()."""
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        payload = {"type": "turn_end", "sessionId": "x"}
        with (
            mock.patch("tracing.omp.hooks.handlers.check_requirements", return_value=True),
            mock.patch.object(sys, "stdin", new=io.StringIO(json.dumps(payload))),
            mock.patch(
                "tracing.omp.hooks.handlers._handle_turn_end",
                side_effect=RuntimeError("boom"),
            ),
        ):
            main()  # must not raise

    def test_no_system_exit_on_unknown(self, monkeypatch):
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        payload = {"type": "???"}
        with (
            mock.patch("tracing.omp.hooks.handlers.check_requirements", return_value=True),
            mock.patch.object(sys, "stdin", new=io.StringIO(json.dumps(payload))),
        ):
            try:
                main()
            except SystemExit:
                pytest.fail("main() raised SystemExit on unknown type")

    def test_empty_stdin_no_crash(self, monkeypatch):
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        with (
            mock.patch("tracing.omp.hooks.handlers.check_requirements", return_value=True),
            mock.patch.object(sys, "stdin", new=io.StringIO("")),
            mock.patch("tracing.omp.hooks.handlers._handle_turn_end") as te,
        ):
            main()  # must not raise
        te.assert_not_called()


# ---------------------------------------------------------------------------
# Service / scope metadata
# ---------------------------------------------------------------------------


class TestSpanServiceMetadata:
    def test_service_name_omp(self, mock_resolve, mock_ensure, state, captured_spans):
        _run_basic_turn(state)
        _handle_agent_end(_load_fixture("agent_end.json"))
        s = captured_spans[0]
        svc = s["resourceSpans"][0]["resource"]["attributes"][0]
        assert svc["key"] == "service.name"
        assert svc["value"]["stringValue"] == "omp"

    def test_scope_name_arize_omp_plugin(self, mock_resolve, mock_ensure, state, captured_spans):
        _run_basic_turn(state)
        _handle_agent_end(_load_fixture("agent_end.json"))
        s = captured_spans[0]
        scope = s["resourceSpans"][0]["scopeSpans"][0]["scope"]
        assert scope["name"] == "arize-omp-plugin"
