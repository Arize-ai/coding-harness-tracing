#!/usr/bin/env python3
"""Tests for tracing.opencode.hooks.handlers — the opencode snapshot reconciler.

opencode does NOT fire one process per hook event. The TypeScript plugin shim
collects a full session snapshot (via SDK) and pipes it to this Python entry
point as a single JSON payload of the form:

    { "type": "reconcile" | "close",
      "sessionID": "ses_xyz",
      "messages": [ { "info": <Message>, "parts": [ <Part>, ... ] }, ... ] }

The reconciler walks the snapshot, dedupes by message ID and tool callID, and
emits Turn / LLM / TOOL spans. The reconcile op only emits child spans; the
Turn CHAIN root is emitted only on `close` (session.idle).

Tests are modelled after tests/tracing/gemini/test_gemini_hook.py.
"""

from __future__ import annotations

import copy
import io
import json
import os
import sys
from pathlib import Path
from unittest import mock

import pytest

from core.common import FileLock, StateManager

# Force synchronous send_span path in any handler that spawns a fork.
os.environ["ARIZE_DISABLE_FORK"] = "true"

# Import handlers (this import is what fails first in pure-TDD: the module
# does not exist yet). The remaining tests then exercise its surface.
from tracing.opencode.hooks.handlers import (  # noqa: E402
    _handle_close,
    _handle_reconcile,
    _read_stdin,
    _send_span_async,
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
    return json.loads((FIXTURES / name).read_text())


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
    sm.set("session_id", "ses_test")
    sm.set("project_name", "test-opencode-project")
    sm.set("trace_count", "0")
    sm.set("tool_count", "0")
    sm.set("user_id", "test-user")
    sm.set("session_start_time", "1000")
    return sm


@pytest.fixture
def mock_resolve(state):
    with mock.patch("tracing.opencode.hooks.handlers.resolve_session", return_value=state) as m:
        yield m


@pytest.fixture
def mock_ensure():
    with mock.patch("tracing.opencode.hooks.handlers.ensure_session_initialized") as m:
        yield m


@pytest.fixture
def captured_spans():
    """Patch _send_span_async to capture emitted span payloads in order."""
    sent = []
    with mock.patch(
        "tracing.opencode.hooks.handlers._send_span_async",
        side_effect=lambda s: sent.append(s),
    ):
        yield sent


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
        with mock.patch.object(sys, "stdin", new=io.StringIO('{"type":"close","sessionID":"x"}')):
            assert _read_stdin() == {"type": "close", "sessionID": "x"}


# ---------------------------------------------------------------------------
# _send_span_async (test fork-disable)
# ---------------------------------------------------------------------------


class TestSendSpanAsync:
    def test_uses_sync_send_when_fork_disabled(self, monkeypatch):
        """ARIZE_DISABLE_FORK=true -> bypass fork, call send_span synchronously."""
        monkeypatch.setenv("ARIZE_DISABLE_FORK", "true")
        with mock.patch("tracing.opencode.hooks.handlers.send_span") as ss:
            _send_span_async({"resourceSpans": []})
        ss.assert_called_once_with({"resourceSpans": []})


# ---------------------------------------------------------------------------
# Basic reconcile flow (1 user + 1 assistant + 2 completed tools)
# ---------------------------------------------------------------------------


class TestReconcileBasic:
    def test_emits_one_llm_and_two_tool_spans(self, mock_resolve, mock_ensure, state, captured_spans):
        payload = _load_fixture("reconcile_basic.json")
        _handle_reconcile(payload)
        assert len(captured_spans) == 3
        kinds = sorted(_kind(s) for s in captured_spans)
        assert kinds == ["LLM", "TOOL", "TOOL"]

    def test_no_turn_chain_on_reconcile(self, mock_resolve, mock_ensure, state, captured_spans):
        """A reconcile by itself does NOT emit the Turn CHAIN root."""
        payload = _load_fixture("reconcile_basic.json")
        _handle_reconcile(payload)
        assert not _by_kind(captured_spans, "CHAIN")

    def test_opens_turn_state(self, mock_resolve, mock_ensure, state, captured_spans):
        """Reconcile opens a turn (sets current_trace_id/span_id/start/prompt)."""
        payload = _load_fixture("reconcile_basic.json")
        _handle_reconcile(payload)
        assert state.get("current_trace_id") is not None
        assert len(state.get("current_trace_id")) == 32
        assert state.get("current_trace_span_id") is not None
        assert len(state.get("current_trace_span_id")) == 16
        # The start time should be the user message's time.created (1000)
        assert state.get("current_trace_start_time") == "1000"
        # The prompt should be the concatenated user TextPart text
        assert state.get("current_trace_prompt") == "list files and edit main.py"
        # The user message id is tracked
        assert state.get("current_user_message_id") == "msg_user_1"

    def test_increments_trace_and_tool_counts(self, mock_resolve, mock_ensure, state, captured_spans):
        payload = _load_fixture("reconcile_basic.json")
        _handle_reconcile(payload)
        assert state.get("trace_count") == "1"
        assert state.get("tool_count") == "2"

    def test_llm_span_attrs(self, mock_resolve, mock_ensure, state, captured_spans):
        payload = _load_fixture("reconcile_basic.json")
        _handle_reconcile(payload)
        llm = _by_kind(captured_spans, "LLM")[0]
        attrs = _get_attrs(llm)
        assert attrs["llm.model_name"]["stringValue"] == "claude-sonnet-4"
        assert attrs["llm.provider"]["stringValue"] == "anthropic"
        # OpenInference: prompt is the total (uncached input 100 + cache_read 30
        # + cache_write 5 = 135); cache split reported via prompt_details.* subsets.
        assert attrs["llm.token_count.prompt"]["intValue"] == 135
        assert attrs["llm.token_count.completion"]["intValue"] == 50
        assert attrs["llm.token_count.total"]["intValue"] == 185
        assert attrs["llm.token_count.completion_details.reasoning"]["intValue"] == 7
        assert attrs["llm.token_count.prompt_details.cache_read"]["intValue"] == 30
        assert attrs["llm.token_count.prompt_details.cache_write"]["intValue"] == 5
        # cost (float)
        # OTLP encodes floats under "doubleValue" — assert numeric equality flexibly
        cost = attrs["llm.cost"]
        cost_val = cost.get("doubleValue") if isinstance(cost, dict) else None
        if cost_val is None:
            cost_val = float(next(iter(cost.values())))
        assert cost_val == pytest.approx(0.0125)
        # session / project / kind
        assert attrs["session.id"]["stringValue"] == "ses_test"
        assert attrs["project.name"]["stringValue"] == "test-opencode-project"
        assert attrs["openinference.span.kind"]["stringValue"] == "LLM"
        # input.value = turn prompt, output.value = assistant text
        assert attrs["input.value"]["stringValue"] == "list files and edit main.py"
        assert attrs["output.value"]["stringValue"] == "I'll list files then edit."

    def test_llm_span_name_includes_model(self, mock_resolve, mock_ensure, state, captured_spans):
        _handle_reconcile(_load_fixture("reconcile_basic.json"))
        llm = _by_kind(captured_spans, "LLM")[0]
        assert _name(llm) == "LLM: claude-sonnet-4"

    def test_llm_span_timing_from_message(self, mock_resolve, mock_ensure, state, captured_spans):
        """LLM span timestamps come from message.time.created / time.completed (ms)."""
        _handle_reconcile(_load_fixture("reconcile_basic.json"))
        llm = _by_kind(captured_spans, "LLM")[0]
        span = _get_span(llm)
        assert span["startTimeUnixNano"] == "2000000000"  # 2000 ms -> ns
        assert span["endTimeUnixNano"] == "5000000000"  # 5000 ms -> ns

    def test_llm_child_of_current_turn(self, mock_resolve, mock_ensure, state, captured_spans):
        _handle_reconcile(_load_fixture("reconcile_basic.json"))
        llm = _by_kind(captured_spans, "LLM")[0]
        span = _get_span(llm)
        assert span["traceId"] == state.get("current_trace_id")
        assert span["parentSpanId"] == state.get("current_trace_span_id")

    def test_tool_spans_use_state_input_and_output(self, mock_resolve, mock_ensure, state, captured_spans):
        _handle_reconcile(_load_fixture("reconcile_basic.json"))
        tools = _by_kind(captured_spans, "TOOL")
        bash = next(s for s in tools if _name(s) == "bash")
        edit = next(s for s in tools if _name(s) == "edit")
        bash_attrs = _get_attrs(bash)
        edit_attrs = _get_attrs(edit)
        assert bash_attrs["tool.name"]["stringValue"] == "bash"
        assert bash_attrs["output.value"]["stringValue"] == "total 4\nfile.py"
        assert bash_attrs["input.value"]["stringValue"] == json.dumps({"command": "ls -la"})
        assert edit_attrs["tool.name"]["stringValue"] == "edit"
        assert edit_attrs["output.value"]["stringValue"] == "edited main.py"

    def test_tool_spans_are_children_of_requesting_llm(self, mock_resolve, mock_ensure, state, captured_spans):
        """ToolPart.messageID authoritatively identifies the requesting assistant message."""
        _handle_reconcile(_load_fixture("reconcile_basic.json"))
        llm_span_id = _get_span(_by_kind(captured_spans, "LLM")[0])["spanId"]
        trace_id = state.get("current_trace_id")
        for tool in _by_kind(captured_spans, "TOOL"):
            span = _get_span(tool)
            assert span["parentSpanId"] == llm_span_id
            assert span["traceId"] == trace_id

    def test_late_tool_completion_keeps_requesting_llm_parent(self, mock_resolve, mock_ensure, state, captured_spans):
        """A later snapshot must recover the already-emitted LLM span ID from state."""
        first = _load_fixture("reconcile_basic.json")
        first["messages"][1]["parts"][1]["state"] = {
            "status": "running",
            "input": {"command": "ls -la"},
            "time": {"start": 2100},
        }
        first["messages"][1]["parts"] = first["messages"][1]["parts"][:2]
        _handle_reconcile(first)

        llm_span_id = _get_span(_by_kind(captured_spans, "LLM")[0])["spanId"]
        assert not _by_kind(captured_spans, "TOOL")

        second = _load_fixture("reconcile_basic.json")
        second["messages"][1]["parts"] = second["messages"][1]["parts"][:2]
        _handle_reconcile(second)

        tool = _get_span(_by_kind(captured_spans, "TOOL")[0])
        assert tool["parentSpanId"] == llm_span_id
        assert state.get("span_msg_msg_assist_1") == llm_span_id

    def test_mismatched_tool_message_id_falls_back_to_turn(self, mock_resolve, mock_ensure, state, captured_spans):
        payload = _load_fixture("reconcile_basic.json")
        payload["messages"][1]["parts"][1]["messageID"] = "msg_missing"
        payload["messages"][1]["parts"] = payload["messages"][1]["parts"][:2]

        _handle_reconcile(payload)

        tool = _by_kind(captured_spans, "TOOL")[0]
        span = _get_span(tool)
        attrs = _get_attrs(tool)
        assert span["parentSpanId"] == state.get("current_trace_span_id")
        assert attrs["tracing.parentage"]["stringValue"] == "turn_fallback"
        assert state.get("span_msg_msg_missing") is None

    def test_incomplete_assistant_tool_falls_back_to_turn(self, mock_resolve, mock_ensure, state, captured_spans):
        payload = _load_fixture("reconcile_basic.json")
        payload["messages"][1]["info"]["time"].pop("completed")
        payload["messages"][1]["parts"] = payload["messages"][1]["parts"][:2]

        _handle_reconcile(payload)

        assert _by_kind(captured_spans, "LLM") == []
        tool = _by_kind(captured_spans, "TOOL")[0]
        assert _get_span(tool)["parentSpanId"] == state.get("current_trace_span_id")
        assert _get_attrs(tool)["tracing.parentage"]["stringValue"] == "turn_fallback"


class TestSubagentSessionTopology:
    def test_task_child_session_emits_agent_subtree(self, mock_resolve, mock_ensure, state, captured_spans):
        payload = _load_fixture("subagent_session.json")
        _handle_close(payload)

        llms = _by_kind(captured_spans, "LLM")
        tools = _by_kind(captured_spans, "TOOL")
        agents = _by_kind(captured_spans, "AGENT")
        chains = _by_kind(captured_spans, "CHAIN")
        assert len(llms) == 3
        assert len(tools) == 1
        assert len(agents) == 1
        assert len(chains) == 1

        parent_llm = next(s for s in llms if _get_attrs(s)["llm.message_id"]["stringValue"] == "msg_parent_llm_1")
        child_llm = next(s for s in llms if _get_attrs(s)["llm.message_id"]["stringValue"] == "msg_child_llm")
        task = tools[0]
        agent = agents[0]

        assert _get_span(task)["parentSpanId"] == _get_span(parent_llm)["spanId"]
        assert _get_span(agent)["parentSpanId"] == _get_span(task)["spanId"]
        assert _get_span(child_llm)["parentSpanId"] == _get_span(agent)["spanId"]
        assert _get_attrs(agent)["session.id"]["stringValue"] == "ses_child"
        assert _get_attrs(agent)["agent.name"]["stringValue"] == "general"

    def test_incomplete_child_assistant_tool_falls_back_to_agent(
        self, mock_resolve, mock_ensure, state, captured_spans
    ):
        payload = _load_fixture("subagent_session.json")
        child_assistant = payload["childSessions"][0]["messages"][1]
        child_assistant["info"]["time"].pop("completed")
        child_assistant["parts"].append(
            {
                "id": "prt_child_tool_pending_llm",
                "sessionID": "ses_child",
                "messageID": "msg_child_llm",
                "type": "tool",
                "tool": "read",
                "callID": "call_child_pending_llm",
                "state": {
                    "status": "completed",
                    "input": {"filePath": "x"},
                    "output": "ok",
                    "time": {"start": 1800, "end": 1900},
                },
            }
        )

        _handle_close(payload)

        agent = _by_kind(captured_spans, "AGENT")[0]
        child_tool = next(
            span
            for span in _by_kind(captured_spans, "TOOL")
            if _get_attrs(span).get("tool.call_id", {}).get("stringValue") == "call_child_pending_llm"
        )
        assert _get_span(child_tool)["parentSpanId"] == _get_span(agent)["spanId"]
        assert _get_attrs(child_tool)["tracing.parentage"]["stringValue"] == "turn_fallback"

    def test_early_task_fallback_preserves_late_child_linkage(self, mock_resolve, mock_ensure, state, captured_spans):
        completed = _load_fixture("subagent_session.json")
        partial = copy.deepcopy(completed)
        partial["type"] = "reconcile"
        partial["messages"][1]["info"]["time"].pop("completed")
        partial["childSessions"] = []

        _handle_reconcile(partial)
        assert len(_by_kind(captured_spans, "TOOL")) == 1
        assert _by_kind(captured_spans, "AGENT") == []

        _handle_close(completed)

        assert len(_by_kind(captured_spans, "AGENT")) == 1
        assert any(
            _get_attrs(span).get("llm.message_id", {}).get("stringValue") == "msg_child_llm"
            for span in _by_kind(captured_spans, "LLM")
        )
        assert state.get("tool_session_call_task_1") == "ses_parent"

    def test_subagent_snapshot_replay_is_idempotent(self, mock_resolve, mock_ensure, state, captured_spans):
        payload = _load_fixture("subagent_session.json")
        _handle_close(payload)
        first_count = len(captured_spans)
        _handle_close(payload)
        assert len(captured_spans) == first_count

    def test_partial_child_reconcile_does_not_freeze_agent_span(self, mock_resolve, mock_ensure, state, captured_spans):
        completed = _load_fixture("subagent_session.json")
        partial = copy.deepcopy(completed)
        partial["type"] = "reconcile"
        child = partial["childSessions"][0]
        child["info"]["time"]["updated"] = 1800
        child["messages"] = child["messages"][:1]

        _handle_reconcile(partial)
        assert _by_kind(captured_spans, "AGENT") == []

        _handle_close(completed)
        agents = _by_kind(captured_spans, "AGENT")
        assert len(agents) == 1
        agent = agents[0]
        assert _get_attrs(agent)["output.value"]["stringValue"] == "[REDACTED]"
        assert _get_span(agent)["endTimeUnixNano"] == "2800000000"

    def test_close_recovers_agent_parent_when_child_fetch_is_missing(
        self, mock_resolve, mock_ensure, state, captured_spans
    ):
        reconcile = _load_fixture("subagent_session.json")
        reconcile["type"] = "reconcile"
        _handle_reconcile(reconcile)

        close = _load_fixture("subagent_session.json")
        close["childSessions"] = []
        _handle_close(close)

        agents = _by_kind(captured_spans, "AGENT")
        assert len(agents) == 1
        emitted_ids = {_get_span(span)["spanId"] for span in captured_spans}
        child_spans = [
            span
            for span in captured_spans
            if _get_attrs(span).get("session.id", {}).get("stringValue") == "ses_child"
            and _get_attrs(span).get("openinference.span.kind", {}).get("stringValue") != "AGENT"
        ]
        assert child_spans
        assert all(_get_span(span)["parentSpanId"] in emitted_ids for span in child_spans)

    def test_malformed_leaf_values_do_not_abort_terminal_close(self, mock_resolve, mock_ensure, state, captured_spans):
        payload = _load_fixture("reconcile_basic.json")
        payload["type"] = "close"
        payload["messages"][0]["parts"][0]["text"] = {"bad": "shape"}
        assistant = payload["messages"][1]
        assistant["info"]["time"]["created"] = {"bad": "timestamp"}
        assistant["info"]["tokens"]["input"] = {"bad": "counter"}
        assistant["parts"][0]["text"] = ["bad", "text"]
        tool = assistant["parts"][1]
        tool["tool"] = {"bad": "name"}
        tool["state"]["output"] = {"bad": "output"}
        tool["state"]["time"]["start"] = ["bad", "timestamp"]

        _handle_close(payload)

        assert len(_by_kind(captured_spans, "CHAIN")) == 1
        assert state.get("current_trace_id") is None
        assert state.get("closed_user_msg_user_1") == "1"

    @pytest.mark.parametrize("field", ["token", "cost"])
    def test_non_finite_numeric_leaf_does_not_abort_terminal_close(
        self, field, mock_resolve, mock_ensure, state, captured_spans
    ):
        payload = _load_fixture("reconcile_basic.json")
        payload["type"] = "close"
        if field == "token":
            payload["messages"][1]["info"]["tokens"]["input"] = json.loads("1e309")
        else:
            payload["messages"][1]["info"]["cost"] = json.loads("1e309")

        _handle_close(payload)

        assert len(_by_kind(captured_spans, "CHAIN")) == 1
        assert state.get("current_trace_id") is None
        assert state.get("closed_user_msg_user_1") == "1"

    def test_multi_turn_snapshot_keeps_child_subtree_in_task_trace(
        self, mock_resolve, mock_ensure, state, captured_spans
    ):
        payload = _load_fixture("subagent_session.json")
        payload["type"] = "reconcile"
        payload["childSessions"][0]["messages"][1]["parts"].append(
            {
                "id": "prt_child_tool",
                "sessionID": "ses_child",
                "messageID": "msg_child_llm",
                "type": "tool",
                "tool": "read",
                "callID": "call_child_read",
                "state": {
                    "status": "completed",
                    "input": {"filePath": "[REDACTED]"},
                    "output": "[REDACTED]",
                    "time": {"start": 1800, "end": 2200},
                },
            }
        )
        payload["messages"].extend(
            [
                {
                    "info": {
                        "id": "msg_user_later",
                        "sessionID": "ses_parent",
                        "role": "user",
                        "time": {"created": 5000},
                    },
                    "parts": [
                        {
                            "id": "prt_user_later",
                            "sessionID": "ses_parent",
                            "messageID": "msg_user_later",
                            "type": "text",
                            "text": "[REDACTED]",
                        }
                    ],
                },
                {
                    "info": {
                        "id": "msg_later_llm",
                        "sessionID": "ses_parent",
                        "role": "assistant",
                        "parentID": "msg_user_later",
                        "providerID": "opencode",
                        "modelID": "free-model",
                        "time": {"created": 5100, "completed": 5200},
                        "tokens": {"input": 1, "output": 1},
                        "finish": "stop",
                    },
                    "parts": [
                        {
                            "id": "prt_later_text",
                            "sessionID": "ses_parent",
                            "messageID": "msg_later_llm",
                            "type": "text",
                            "text": "[REDACTED]",
                        }
                    ],
                },
            ]
        )

        _handle_reconcile(payload)
        _handle_close(payload)

        task = next(
            span
            for span in _by_kind(captured_spans, "TOOL")
            if _get_attrs(span).get("tool.call_id", {}).get("stringValue") == "call_task_1"
        )
        task_trace = _get_span(task)["traceId"]
        agent = _by_kind(captured_spans, "AGENT")[0]
        child_llm = next(
            span
            for span in _by_kind(captured_spans, "LLM")
            if _get_attrs(span).get("session.id", {}).get("stringValue") == "ses_child"
        )
        child_tool = next(
            span
            for span in _by_kind(captured_spans, "TOOL")
            if _get_attrs(span).get("tool.call_id", {}).get("stringValue") == "call_child_read"
        )

        assert _get_span(agent)["traceId"] == task_trace
        assert _get_span(child_llm)["traceId"] == task_trace
        assert _get_span(child_tool)["traceId"] == task_trace
        assert _get_span(agent)["parentSpanId"] == _get_span(task)["spanId"]
        assert _get_span(child_llm)["parentSpanId"] == _get_span(agent)["spanId"]
        assert _get_span(child_tool)["parentSpanId"] == _get_span(child_llm)["spanId"]

    def test_mismatched_child_parent_session_is_not_linked(self, mock_resolve, mock_ensure, state, captured_spans):
        payload = _load_fixture("subagent_session.json")
        child = payload["childSessions"][0]
        child["parentSessionID"] = "ses_unrelated"
        child["info"]["parentID"] = "ses_unrelated"

        _handle_close(payload)

        assert _by_kind(captured_spans, "AGENT") == []
        assert all(_get_attrs(span)["session.id"]["stringValue"] != "ses_child" for span in captured_spans)

    def test_child_without_authoritative_parent_session_is_not_linked(
        self, mock_resolve, mock_ensure, state, captured_spans
    ):
        payload = _load_fixture("subagent_session.json")
        child = payload["childSessions"][0]
        child.pop("parentSessionID")
        child["info"].pop("parentID")

        _handle_close(payload)

        assert _by_kind(captured_spans, "AGENT") == []
        assert all(_get_attrs(span)["session.id"]["stringValue"] != "ses_child" for span in captured_spans)

    def test_foreign_child_messages_are_not_relabelled_into_child_trace(
        self, mock_resolve, mock_ensure, state, captured_spans
    ):
        payload = _load_fixture("subagent_session.json")
        child = payload["childSessions"][0]
        for message in child["messages"]:
            message["info"]["sessionID"] = "ses_foreign"
            for part in message["parts"]:
                part["sessionID"] = "ses_foreign"

        _handle_close(payload)

        assert _by_kind(captured_spans, "AGENT") == []
        assert all(_get_attrs(span)["session.id"]["stringValue"] != "ses_child" for span in captured_spans)

    def test_foreign_root_messages_are_not_emitted(self, mock_resolve, mock_ensure, state, captured_spans):
        payload = _load_fixture("subagent_session.json")
        payload["childSessions"] = []
        for message in payload["messages"]:
            message["info"]["sessionID"] = "ses_foreign"
            for part in message["parts"]:
                part["sessionID"] = "ses_foreign"

        _handle_close(payload)

        assert captured_spans == []
        assert state.get("current_trace_id") is None

    def test_malformed_child_info_does_not_abort_root_close(self, mock_resolve, mock_ensure, state, captured_spans):
        payload = _load_fixture("subagent_session.json")
        payload["childSessions"][0]["info"] = [1]

        _handle_close(payload)

        assert len(_by_kind(captured_spans, "CHAIN")) == 1
        assert _by_kind(captured_spans, "AGENT") == []
        assert state.get("current_trace_id") is None


class TestToolMetadataContinued:
    def test_tool_span_timing_from_state(self, mock_resolve, mock_ensure, state, captured_spans):
        """TOOL span timestamps come from state.time.start / state.time.end."""
        _handle_reconcile(_load_fixture("reconcile_basic.json"))
        tools = _by_kind(captured_spans, "TOOL")
        bash = next(s for s in tools if _name(s) == "bash")
        bash_span = _get_span(bash)
        assert bash_span["startTimeUnixNano"] == "2100000000"
        assert bash_span["endTimeUnixNano"] == "2400000000"

    def test_per_tool_bash_command(self, mock_resolve, mock_ensure, state, captured_spans):
        _handle_reconcile(_load_fixture("reconcile_basic.json"))
        bash = next(s for s in _by_kind(captured_spans, "TOOL") if _name(s) == "bash")
        attrs = _get_attrs(bash)
        assert attrs["tool.command"]["stringValue"] == "ls -la"

    def test_per_tool_edit_file_path(self, mock_resolve, mock_ensure, state, captured_spans):
        _handle_reconcile(_load_fixture("reconcile_basic.json"))
        edit = next(s for s in _by_kind(captured_spans, "TOOL") if _name(s) == "edit")
        attrs = _get_attrs(edit)
        assert attrs["tool.file_path"]["stringValue"] == "/home/user/myproj/main.py"

    def test_tool_description_uses_state_title(self, mock_resolve, mock_ensure, state, captured_spans):
        _handle_reconcile(_load_fixture("reconcile_basic.json"))
        edit = next(s for s in _by_kind(captured_spans, "TOOL") if _name(s) == "edit")
        attrs = _get_attrs(edit)
        # state.title == "main.py" for the edit tool fixture
        assert attrs["tool.description"]["stringValue"] == "main.py"

    def test_user_id_included(self, mock_resolve, mock_ensure, state, captured_spans):
        _handle_reconcile(_load_fixture("reconcile_basic.json"))
        for s in captured_spans:
            assert _get_attrs(s)["user.id"]["stringValue"] == "test-user"

    def test_user_id_omitted_when_empty(self, mock_resolve, mock_ensure, state, captured_spans):
        state.set("user_id", "")
        _handle_reconcile(_load_fixture("reconcile_basic.json"))
        for s in captured_spans:
            assert "user.id" not in _get_attrs(s)


# ---------------------------------------------------------------------------
# Dedup — running the SAME snapshot twice emits each span exactly once
# ---------------------------------------------------------------------------


class TestReconcileDedup:
    def test_second_reconcile_emits_nothing(self, mock_resolve, mock_ensure, state, captured_spans):
        payload = _load_fixture("reconcile_basic.json")
        _handle_reconcile(payload)
        first = len(captured_spans)
        assert first == 3

        # Second pass: identical snapshot
        _handle_reconcile(payload)
        # No new spans emitted
        assert len(captured_spans) == first

    def test_emitted_keys_persisted_in_state(self, mock_resolve, mock_ensure, state, captured_spans):
        _handle_reconcile(_load_fixture("reconcile_basic.json"))
        assert state.get("emitted_msg_msg_assist_1") is not None
        assert state.get("emitted_tool_call_bash_1") is not None
        assert state.get("emitted_tool_call_edit_1") is not None

    def test_idempotent_counters(self, mock_resolve, mock_ensure, state, captured_spans):
        """Running the same reconcile twice must not double-increment tool/trace counts."""
        payload = _load_fixture("reconcile_basic.json")
        _handle_reconcile(payload)
        assert state.get("trace_count") == "1"
        assert state.get("tool_count") == "2"
        _handle_reconcile(payload)
        # Second run: identical user message id, no new assistant or tools.
        assert state.get("trace_count") == "1"
        assert state.get("tool_count") == "2"


# ---------------------------------------------------------------------------
# Close emits Turn CHAIN root
# ---------------------------------------------------------------------------


class TestCloseEmitsTurn:
    def test_close_after_reconcile_emits_turn_root(self, mock_resolve, mock_ensure, state, captured_spans):
        payload = _load_fixture("reconcile_basic.json")
        _handle_reconcile(payload)
        assert len(captured_spans) == 3

        close_payload = dict(payload, type="close")
        _handle_close(close_payload)
        # One new span: the Turn CHAIN root
        new_spans = captured_spans[3:]
        assert len(new_spans) == 1
        turn = new_spans[0]
        attrs = _get_attrs(turn)
        assert attrs["openinference.span.kind"]["stringValue"] == "CHAIN"
        assert _name(turn) == "Turn"

    def test_close_does_not_re_emit_llm_or_tools(self, mock_resolve, mock_ensure, state, captured_spans):
        """Subsequent close pulls the same snapshot — dedup must prevent duplicates."""
        payload = _load_fixture("reconcile_basic.json")
        _handle_reconcile(payload)
        close_payload = dict(payload, type="close")
        _handle_close(close_payload)
        # 3 from reconcile + 1 Turn CHAIN = 4 total
        assert len(captured_spans) == 4
        assert len(_by_kind(captured_spans, "LLM")) == 1
        assert len(_by_kind(captured_spans, "TOOL")) == 2
        assert len(_by_kind(captured_spans, "CHAIN")) == 1

    def test_close_only_payload_emits_all_spans(self, mock_resolve, mock_ensure, state, captured_spans):
        """A close payload (no preceding reconcile) emits LLM + TOOL + CHAIN."""
        payload = dict(_load_fixture("reconcile_basic.json"), type="close")
        _handle_close(payload)
        assert len(_by_kind(captured_spans, "LLM")) == 1
        assert len(_by_kind(captured_spans, "TOOL")) == 2
        assert len(_by_kind(captured_spans, "CHAIN")) == 1

    def test_turn_root_uses_current_trace_ids(self, mock_resolve, mock_ensure, state, captured_spans):
        payload = _load_fixture("reconcile_basic.json")
        _handle_reconcile(payload)
        trace_id_before = state.get("current_trace_id")
        span_id_before = state.get("current_trace_span_id")
        _handle_close(dict(payload, type="close"))
        turn = _by_kind(captured_spans, "CHAIN")[0]
        span = _get_span(turn)
        assert span["traceId"] == trace_id_before
        assert span["spanId"] == span_id_before
        assert "parentSpanId" not in span

    def test_turn_root_uses_user_message_start_time(self, mock_resolve, mock_ensure, state, captured_spans):
        """Turn CHAIN startTime = user message.time.created (in ms)."""
        payload = dict(_load_fixture("reconcile_basic.json"), type="close")
        _handle_close(payload)
        turn = _by_kind(captured_spans, "CHAIN")[0]
        assert _get_span(turn)["startTimeUnixNano"] == "1000000000"  # 1000 ms

    def test_turn_root_output_is_final_assistant_text(self, mock_resolve, mock_ensure, state, captured_spans):
        payload = dict(_load_fixture("reconcile_basic.json"), type="close")
        _handle_close(payload)
        turn = _by_kind(captured_spans, "CHAIN")[0]
        attrs = _get_attrs(turn)
        assert attrs["output.value"]["stringValue"] == "I'll list files then edit."
        assert attrs["input.value"]["stringValue"] == "list files and edit main.py"

    def test_close_clears_current_trace_state(self, mock_resolve, mock_ensure, state, captured_spans):
        payload = dict(_load_fixture("reconcile_basic.json"), type="close")
        _handle_close(payload)
        assert state.get("current_trace_id") is None
        assert state.get("current_trace_span_id") is None
        assert state.get("current_trace_start_time") is None
        assert state.get("current_trace_prompt") is None
        assert state.get("current_user_message_id") is None


# ---------------------------------------------------------------------------
# Error tool span — status_code = 2 (ERROR)
# ---------------------------------------------------------------------------


class TestErrorTool:
    def test_close_with_error_tool_emits_three_spans(self, mock_resolve, mock_ensure, state, captured_spans):
        _handle_close(_load_fixture("close_with_tool_error.json"))
        # 1 LLM + 1 TOOL + 1 CHAIN
        assert len(captured_spans) == 3
        assert len(_by_kind(captured_spans, "LLM")) == 1
        assert len(_by_kind(captured_spans, "TOOL")) == 1
        assert len(_by_kind(captured_spans, "CHAIN")) == 1

    def test_error_tool_span_has_status_code_2(self, mock_resolve, mock_ensure, state, captured_spans):
        _handle_close(_load_fixture("close_with_tool_error.json"))
        tool = _by_kind(captured_spans, "TOOL")[0]
        span = _get_span(tool)
        assert span["status"]["code"] == 2

    def test_error_tool_span_has_status_message(self, mock_resolve, mock_ensure, state, captured_spans):
        _handle_close(_load_fixture("close_with_tool_error.json"))
        tool = _by_kind(captured_spans, "TOOL")[0]
        span = _get_span(tool)
        assert "ENOENT" in span["status"].get("message", "")

    def test_error_tool_output_uses_error_field(self, mock_resolve, mock_ensure, state, captured_spans):
        _handle_close(_load_fixture("close_with_tool_error.json"))
        tool = _by_kind(captured_spans, "TOOL")[0]
        attrs = _get_attrs(tool)
        assert "ENOENT" in attrs["output.value"]["stringValue"]

    def test_error_tool_input_uses_state_input(self, mock_resolve, mock_ensure, state, captured_spans):
        _handle_close(_load_fixture("close_with_tool_error.json"))
        tool = _by_kind(captured_spans, "TOOL")[0]
        attrs = _get_attrs(tool)
        assert attrs["input.value"]["stringValue"] == json.dumps({"filePath": "/missing.txt"})

    def test_error_tool_file_path_attr(self, mock_resolve, mock_ensure, state, captured_spans):
        """`read` tool with filePath gets tool.file_path attr."""
        _handle_close(_load_fixture("close_with_tool_error.json"))
        tool = _by_kind(captured_spans, "TOOL")[0]
        attrs = _get_attrs(tool)
        assert attrs["tool.file_path"]["stringValue"] == "/missing.txt"


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------


class TestRedaction:
    def test_redacts_prompt_and_response_when_log_prompts_false(
        self, mock_resolve, mock_ensure, state, captured_spans, monkeypatch
    ):
        monkeypatch.setenv("ARIZE_LOG_PROMPTS", "false")
        _handle_close(dict(_load_fixture("reconcile_basic.json"), type="close"))
        llm = _by_kind(captured_spans, "LLM")[0]
        attrs = _get_attrs(llm)
        assert "redacted" in attrs["input.value"]["stringValue"]
        assert "redacted" in attrs["output.value"]["stringValue"]

    def test_redacts_tool_content_when_log_tool_content_false(
        self, mock_resolve, mock_ensure, state, captured_spans, monkeypatch
    ):
        monkeypatch.setenv("ARIZE_LOG_TOOL_CONTENT", "false")
        _handle_close(dict(_load_fixture("reconcile_basic.json"), type="close"))
        tools = _by_kind(captured_spans, "TOOL")
        for t in tools:
            attrs = _get_attrs(t)
            assert "redacted" in attrs["input.value"]["stringValue"]
            assert "redacted" in attrs["output.value"]["stringValue"]

    def test_redacts_tool_details_when_log_tool_details_false(
        self, mock_resolve, mock_ensure, state, captured_spans, monkeypatch
    ):
        monkeypatch.setenv("ARIZE_LOG_TOOL_DETAILS", "false")
        _handle_close(dict(_load_fixture("reconcile_basic.json"), type="close"))
        bash = next(s for s in _by_kind(captured_spans, "TOOL") if _name(s) == "bash")
        attrs = _get_attrs(bash)
        # tool.command and tool.description are tool details
        assert "redacted" in attrs["tool.command"]["stringValue"]
        assert "redacted" in attrs["tool.description"]["stringValue"]

    def test_tool_name_not_redacted(self, mock_resolve, mock_ensure, state, captured_spans, monkeypatch):
        monkeypatch.setenv("ARIZE_LOG_TOOL_DETAILS", "false")
        monkeypatch.setenv("ARIZE_LOG_TOOL_CONTENT", "false")
        _handle_close(dict(_load_fixture("reconcile_basic.json"), type="close"))
        bash = next(s for s in _by_kind(captured_spans, "TOOL") if _name(s) == "bash")
        attrs = _get_attrs(bash)
        assert attrs["tool.name"]["stringValue"] == "bash"


# ---------------------------------------------------------------------------
# Per-tool specialized attribute mapping
# ---------------------------------------------------------------------------


def _make_payload_with_tool(tool_name, input_obj, ptype="close"):
    return {
        "type": ptype,
        "sessionID": "ses_t",
        "messages": [
            {
                "info": {
                    "id": "msg_user_x",
                    "sessionID": "ses_t",
                    "role": "user",
                    "time": {"created": 100},
                },
                "parts": [
                    {
                        "id": "p1",
                        "sessionID": "ses_t",
                        "messageID": "msg_user_x",
                        "type": "text",
                        "text": "do thing",
                    }
                ],
            },
            {
                "info": {
                    "id": "msg_assist_x",
                    "sessionID": "ses_t",
                    "role": "assistant",
                    "time": {"created": 200, "completed": 300},
                    "parentID": "msg_user_x",
                    "modelID": "claude-sonnet-4",
                    "providerID": "anthropic",
                    "mode": "build",
                    "path": {"cwd": "/x", "root": "/"},
                    "cost": 0,
                    "tokens": {"input": 1, "output": 1, "reasoning": 0, "cache": {"read": 0, "write": 0}},
                },
                "parts": [
                    {
                        "id": "p2",
                        "sessionID": "ses_t",
                        "messageID": "msg_assist_x",
                        "type": "text",
                        "text": "ok",
                    },
                    {
                        "id": "p3",
                        "sessionID": "ses_t",
                        "messageID": "msg_assist_x",
                        "type": "tool",
                        "callID": f"call_{tool_name}",
                        "tool": tool_name,
                        "state": {
                            "status": "completed",
                            "input": input_obj,
                            "output": "tool-out",
                            "title": "tool-title",
                            "metadata": {},
                            "time": {"start": 210, "end": 220},
                        },
                    },
                ],
            },
        ],
    }


class TestPerToolAttributeMapping:
    def test_read_sets_file_path(self, mock_resolve, mock_ensure, state, captured_spans):
        _handle_close(_make_payload_with_tool("read", {"filePath": "/a/b.py"}))
        tool = _by_kind(captured_spans, "TOOL")[0]
        assert _get_attrs(tool)["tool.file_path"]["stringValue"] == "/a/b.py"

    def test_write_sets_file_path(self, mock_resolve, mock_ensure, state, captured_spans):
        _handle_close(_make_payload_with_tool("write", {"filePath": "/a/c.py"}))
        tool = _by_kind(captured_spans, "TOOL")[0]
        assert _get_attrs(tool)["tool.file_path"]["stringValue"] == "/a/c.py"

    def test_grep_sets_query(self, mock_resolve, mock_ensure, state, captured_spans):
        _handle_close(_make_payload_with_tool("grep", {"pattern": "TODO"}))
        tool = _by_kind(captured_spans, "TOOL")[0]
        assert _get_attrs(tool)["tool.query"]["stringValue"] == "TODO"

    def test_glob_sets_query(self, mock_resolve, mock_ensure, state, captured_spans):
        _handle_close(_make_payload_with_tool("glob", {"pattern": "**/*.py"}))
        tool = _by_kind(captured_spans, "TOOL")[0]
        assert _get_attrs(tool)["tool.query"]["stringValue"] == "**/*.py"

    def test_webfetch_sets_url(self, mock_resolve, mock_ensure, state, captured_spans):
        _handle_close(_make_payload_with_tool("webfetch", {"url": "https://example.com"}))
        tool = _by_kind(captured_spans, "TOOL")[0]
        assert _get_attrs(tool)["tool.url"]["stringValue"] == "https://example.com"

    def test_unknown_tool_no_specialized_attrs(self, mock_resolve, mock_ensure, state, captured_spans):
        _handle_close(_make_payload_with_tool("custom_thing", {"foo": "bar"}))
        tool = _by_kind(captured_spans, "TOOL")[0]
        attrs = _get_attrs(tool)
        assert "tool.command" not in attrs
        assert "tool.file_path" not in attrs
        assert "tool.url" not in attrs
        assert "tool.query" not in attrs
        # description still set from state.title
        assert attrs["tool.description"]["stringValue"] == "tool-title"


# ---------------------------------------------------------------------------
# Token-detail omission rules
# ---------------------------------------------------------------------------


class TestTokenDetailOmissions:
    def test_zero_reasoning_omitted(self, mock_resolve, mock_ensure, state, captured_spans):
        """reasoning == 0 -> attr omitted."""
        payload = _make_payload_with_tool("read", {"filePath": "/x"})
        _handle_close(payload)
        llm = _by_kind(captured_spans, "LLM")[0]
        attrs = _get_attrs(llm)
        assert "llm.token_count.completion_details.reasoning" not in attrs

    def test_zero_cache_omitted(self, mock_resolve, mock_ensure, state, captured_spans):
        payload = _make_payload_with_tool("read", {"filePath": "/x"})
        _handle_close(payload)
        llm = _by_kind(captured_spans, "LLM")[0]
        attrs = _get_attrs(llm)
        assert "llm.token_count.prompt_details.cache_read" not in attrs
        assert "llm.token_count.prompt_details.cache_write" not in attrs

    def test_zero_cost_omitted(self, mock_resolve, mock_ensure, state, captured_spans):
        payload = _make_payload_with_tool("read", {"filePath": "/x"})
        _handle_close(payload)
        llm = _by_kind(captured_spans, "LLM")[0]
        attrs = _get_attrs(llm)
        assert "llm.cost" not in attrs


# ---------------------------------------------------------------------------
# Pending-turn fail-safe (a new user message arrives while a prior turn is still
# open, e.g. crashed before close fired)
# ---------------------------------------------------------------------------


class TestPendingTurnFailSafe:
    def test_new_user_message_closes_prior_turn(self, mock_resolve, mock_ensure, state, captured_spans):
        """A new user message id while old current_user_message_id is still set
        triggers a force-close of the pending turn (emits a CHAIN with the
        '(closed by reconcile fail-safe)' output)."""
        # Pre-set state as if a prior turn is mid-flight.
        state.set("current_trace_id", "p" * 32)
        state.set("current_trace_span_id", "q" * 16)
        state.set("current_trace_start_time", "500")
        state.set("current_trace_prompt", "prior prompt")
        state.set("current_user_message_id", "msg_user_prior")

        # Reconcile a snapshot with a NEW user message id.
        payload = _load_fixture("reconcile_basic.json")
        _handle_reconcile(payload)

        # The first captured span should be the fail-safe CHAIN for the prior turn.
        chains = _by_kind(captured_spans, "CHAIN")
        assert len(chains) >= 1
        attrs = _get_attrs(chains[0])
        assert "fail-safe" in attrs["output.value"]["stringValue"]
        span = _get_span(chains[0])
        assert span["traceId"] == "p" * 32
        assert span["spanId"] == "q" * 16
        assert "parentSpanId" not in span

        # New turn must have fresh ids
        assert state.get("current_trace_id") != "p" * 32
        assert state.get("current_user_message_id") == "msg_user_1"

    def test_same_user_message_id_does_not_force_close(self, mock_resolve, mock_ensure, state, captured_spans):
        """Re-receiving the same user message id is the normal dedup path —
        no fail-safe CHAIN should be emitted."""
        payload = _load_fixture("reconcile_basic.json")
        _handle_reconcile(payload)
        chains_first = _by_kind(captured_spans, "CHAIN")
        _handle_reconcile(payload)
        chains_after = _by_kind(captured_spans, "CHAIN")
        assert chains_after == chains_first  # no new CHAINs added


# ---------------------------------------------------------------------------
# Cross-turn (multi-turn) snapshot dedup — opencode's SDK returns ALL session
# messages on every snapshot pull, so prior-turn user messages reappear in
# subsequent reconciles. Re-opening closed turns would emit phantom CHAINs
# and inflate trace_count. This is the core invariant for opencode.
# ---------------------------------------------------------------------------


def _multi_turn_payload(ptype: str = "reconcile") -> dict:
    """Snapshot containing turn 1 (already processed) + new turn 2 messages."""
    base = _load_fixture("reconcile_basic.json")
    turn2 = [
        {
            "info": {
                "id": "msg_user_2",
                "sessionID": "ses_basic",
                "role": "user",
                "time": {"created": 10000},
                "agent": "build",
                "model": {"providerID": "anthropic", "modelID": "claude-sonnet-4"},
            },
            "parts": [
                {
                    "id": "prt_u_2",
                    "sessionID": "ses_basic",
                    "messageID": "msg_user_2",
                    "type": "text",
                    "text": "do something else",
                }
            ],
        },
        {
            "info": {
                "id": "msg_assist_2",
                "sessionID": "ses_basic",
                "role": "assistant",
                "time": {"created": 11000, "completed": 12000},
                "parentID": "msg_user_2",
                "modelID": "claude-sonnet-4",
                "providerID": "anthropic",
                "mode": "build",
                "path": {"cwd": "/home/user/myproj", "root": "/home/user"},
                "cost": 0,
                "tokens": {
                    "input": 5,
                    "output": 3,
                    "reasoning": 0,
                    "cache": {"read": 0, "write": 0},
                },
            },
            "parts": [
                {
                    "id": "prt_t_2",
                    "sessionID": "ses_basic",
                    "messageID": "msg_assist_2",
                    "type": "text",
                    "text": "done.",
                }
            ],
        },
    ]
    return {**base, "type": ptype, "messages": base["messages"] + turn2}


class TestMultiTurnSnapshotDedup:
    """Once a turn is closed, replaying its user message in a later snapshot
    must NOT re-open the turn or emit phantom CHAINs."""

    def test_replay_of_closed_snapshot_emits_nothing(self, mock_resolve, mock_ensure, state, captured_spans):
        """The shim is allowed to redeliver the same closed snapshot. Every
        replay after the first cycle must produce zero new spans."""
        _handle_reconcile(_load_fixture("reconcile_basic.json"))
        _handle_close(dict(_load_fixture("reconcile_basic.json"), type="close"))
        baseline = len(captured_spans)
        assert baseline == 4  # 1 LLM + 2 TOOL + 1 CHAIN
        for _ in range(3):
            _handle_reconcile(_load_fixture("reconcile_basic.json"))
        assert len(captured_spans) == baseline
        # And no fail-safe phantom CHAIN was emitted.
        for c in _by_kind(captured_spans, "CHAIN"):
            assert "fail-safe" not in _get_attrs(c)["output.value"]["stringValue"]

    def test_trace_count_not_inflated_by_replay(self, mock_resolve, mock_ensure, state, captured_spans):
        _handle_reconcile(_load_fixture("reconcile_basic.json"))
        _handle_close(dict(_load_fixture("reconcile_basic.json"), type="close"))
        assert state.get("trace_count") == "1"
        for _ in range(5):
            _handle_reconcile(_load_fixture("reconcile_basic.json"))
        assert state.get("trace_count") == "1"

    def test_multi_turn_reconcile_no_phantom_chain(self, mock_resolve, mock_ensure, state, captured_spans):
        """After turn 1 is closed, a snapshot containing turn 1 + turn 2 must:
        - NOT re-open turn 1 (no phantom CHAIN, no trace_count bump from replay)
        - open turn 2 fresh
        - emit only turn 2's NEW LLM (no tools in turn 2 fixture)
        - NOT emit any CHAIN yet (close hasn't fired)"""
        _handle_reconcile(_load_fixture("reconcile_basic.json"))
        _handle_close(dict(_load_fixture("reconcile_basic.json"), type="close"))
        spans_before = len(captured_spans)
        chains_before = len(_by_kind(captured_spans, "CHAIN"))

        _handle_reconcile(_multi_turn_payload("reconcile"))

        # No new CHAIN at all (no force-close phantom).
        assert len(_by_kind(captured_spans, "CHAIN")) == chains_before
        # Exactly one new span: the LLM for assistant 2.
        new_spans = captured_spans[spans_before:]
        assert len(new_spans) == 1
        assert _kind(new_spans[0]) == "LLM"
        # trace_count incremented to 2 (only the genuinely new turn opens).
        assert state.get("trace_count") == "2"
        # Current turn state points at turn 2.
        assert state.get("current_user_message_id") == "msg_user_2"
        # The closed marker for turn 1 is set.
        assert state.get("closed_user_msg_user_1") is not None

    def test_multi_turn_close_emits_only_one_new_chain(self, mock_resolve, mock_ensure, state, captured_spans):
        """Closing a multi-turn snapshot after turn 1 was already closed must
        emit EXACTLY one new CHAIN (for turn 2) with turn 2's input/output."""
        _handle_reconcile(_load_fixture("reconcile_basic.json"))
        _handle_close(dict(_load_fixture("reconcile_basic.json"), type="close"))
        chains_before = len(_by_kind(captured_spans, "CHAIN"))

        _handle_close(_multi_turn_payload("close"))

        chains_after = _by_kind(captured_spans, "CHAIN")
        assert len(chains_after) == chains_before + 1
        turn2_chain = chains_after[-1]
        attrs = _get_attrs(turn2_chain)
        # The new CHAIN uses turn 2's prompt + assistant text, NOT turn 1's
        # (this guards the "stale assistant poisons next CHAIN" bug).
        assert attrs["input.value"]["stringValue"] == "do something else"
        assert attrs["output.value"]["stringValue"] == "done."
        # No fail-safe phantom was emitted.
        for c in chains_after:
            assert "fail-safe" not in _get_attrs(c)["output.value"]["stringValue"]

    def test_full_two_turn_cycle_counts(self, mock_resolve, mock_ensure, state, captured_spans):
        """Full lifecycle: reconcile+close turn 1, then reconcile+close turn 2
        (via multi-turn snapshot). End state: 2 turns, 2 chains, 2 LLMs, 2 tools."""
        _handle_reconcile(_load_fixture("reconcile_basic.json"))
        _handle_close(dict(_load_fixture("reconcile_basic.json"), type="close"))
        _handle_reconcile(_multi_turn_payload("reconcile"))
        _handle_close(_multi_turn_payload("close"))

        assert state.get("trace_count") == "2"
        assert state.get("tool_count") == "2"  # only turn 1 had tools
        assert len(_by_kind(captured_spans, "CHAIN")) == 2
        assert len(_by_kind(captured_spans, "LLM")) == 2
        assert len(_by_kind(captured_spans, "TOOL")) == 2

    def test_closed_user_marker_blocks_reopen_after_failsafe(self, mock_resolve, mock_ensure, state, captured_spans):
        """A turn closed via fail-safe must also be marked closed, so a later
        replay of that user message doesn't open yet another phantom turn."""
        # Pre-set: a pending turn keyed on msg_user_prior.
        state.set("current_trace_id", "p" * 32)
        state.set("current_trace_span_id", "q" * 16)
        state.set("current_trace_start_time", "500")
        state.set("current_trace_prompt", "prior prompt")
        state.set("current_user_message_id", "msg_user_prior")

        # A snapshot whose user message id == msg_user_prior should now no-op.
        # (Simulate the SDK replaying the prior user message after fail-safe.)
        # First, trigger the fail-safe by reconciling a NEW user message.
        _handle_reconcile(_load_fixture("reconcile_basic.json"))
        assert state.get("closed_user_msg_user_prior") is not None

        # Now feed a synthetic snapshot whose user message id IS msg_user_prior.
        replay = {
            "type": "reconcile",
            "sessionID": "ses_basic",
            "messages": [
                {
                    "info": {
                        "id": "msg_user_prior",
                        "sessionID": "ses_basic",
                        "role": "user",
                        "time": {"created": 500},
                    },
                    "parts": [
                        {
                            "id": "prt_prior",
                            "sessionID": "ses_basic",
                            "messageID": "msg_user_prior",
                            "type": "text",
                            "text": "prior prompt",
                        }
                    ],
                }
            ],
        }
        chains_before = len(_by_kind(captured_spans, "CHAIN"))
        trace_count_before = state.get("trace_count")
        _handle_reconcile(replay)
        # No new CHAIN, no trace_count bump.
        assert len(_by_kind(captured_spans, "CHAIN")) == chains_before
        assert state.get("trace_count") == trace_count_before
        # The current turn (msg_user_1) is untouched.
        assert state.get("current_user_message_id") == "msg_user_1"


# ---------------------------------------------------------------------------
# main() entry point
# ---------------------------------------------------------------------------


class TestMainEntryPoint:
    def test_lock_timeout_fails_soft_without_force_acquire(self, state):
        payload = {"type": "reconcile", "sessionID": "ses_race", "messages": []}
        handler_lock = state.state_file.with_name(state.state_file.name + ".handler.lock")
        break_values = []

        def short_lock(path, timeout, *, break_on_timeout=None):
            break_values.append(break_on_timeout)
            kwargs = {} if break_on_timeout is None else {"break_on_timeout": break_on_timeout}
            return FileLock(path, timeout=0.01, **kwargs)

        with FileLock(handler_lock, timeout=1.0):
            with (
                mock.patch("tracing.opencode.hooks.handlers.check_requirements", return_value=True),
                mock.patch("tracing.opencode.hooks.handlers._read_stdin", return_value=payload),
                mock.patch("tracing.opencode.hooks.handlers.resolve_session", return_value=state),
                mock.patch("tracing.opencode.hooks.handlers.FileLock", side_effect=short_lock),
                mock.patch("tracing.opencode.hooks.handlers._handle_reconcile") as reconcile,
            ):
                main()

        assert break_values == [False]
        reconcile.assert_not_called()

    def test_serializes_same_session_invocations(self, monkeypatch, state):
        import threading
        import time

        payload = {"type": "reconcile", "sessionID": "ses_race", "messages": []}
        active = 0
        max_active = 0
        guard = threading.Lock()

        def slow_reconcile(_payload):
            nonlocal active, max_active
            with guard:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.1)
            with guard:
                active -= 1

        with (
            mock.patch("tracing.opencode.hooks.handlers.check_requirements", return_value=True),
            mock.patch("tracing.opencode.hooks.handlers._read_stdin", return_value=payload),
            mock.patch("tracing.opencode.hooks.handlers.resolve_session", return_value=state),
            mock.patch("tracing.opencode.hooks.handlers._handle_reconcile", side_effect=slow_reconcile),
        ):
            threads = [threading.Thread(target=main) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

        assert max_active == 1

    def test_initializes_new_state_only_after_handler_lock(self, tmp_path):
        payload = {"type": "reconcile", "sessionID": "ses_first", "messages": []}
        state = StateManager(
            state_dir=tmp_path,
            state_file=tmp_path / "state_ses_first.json",
            lock_path=tmp_path / ".lock_ses_first",
        )
        events = []
        inside_handler_lock = False
        original_init = state.init_state

        def resolve(_payload, *, initialize=True):
            events.append(f"resolve:{initialize}")
            if initialize:
                state.init_state()
            return state

        def init_state():
            assert inside_handler_lock, "state initialization must be serialized"
            events.append("init")
            original_init()

        class RecordingLock:
            def __init__(self, path, timeout, *, break_on_timeout=None):
                assert path == tmp_path / "state_ses_first.json.handler.lock"
                assert break_on_timeout is False

            def __enter__(self):
                nonlocal inside_handler_lock
                inside_handler_lock = True
                events.append("lock-enter")
                return self

            def __exit__(self, *_args):
                nonlocal inside_handler_lock
                events.append("lock-exit")
                inside_handler_lock = False

        def reconcile(_payload):
            events.append("handler")
            state.set("emitted_msg_x", "1")

        state.init_state = init_state
        with (
            mock.patch("tracing.opencode.hooks.handlers.check_requirements", return_value=True),
            mock.patch("tracing.opencode.hooks.handlers._read_stdin", return_value=payload),
            mock.patch("tracing.opencode.hooks.handlers.resolve_session", side_effect=resolve),
            mock.patch("tracing.opencode.hooks.handlers.FileLock", RecordingLock),
            mock.patch("tracing.opencode.hooks.handlers._handle_reconcile", side_effect=reconcile),
        ):
            main()

        assert events == ["resolve:False", "lock-enter", "init", "handler", "lock-exit"]
        assert state.get("emitted_msg_x") == "1"

    def test_dispatches_reconcile(self, monkeypatch):
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        payload = {"type": "reconcile", "sessionID": "ses_d", "messages": []}
        with (
            mock.patch("tracing.opencode.hooks.handlers.check_requirements", return_value=True),
            mock.patch.object(sys, "stdin", new=io.StringIO(json.dumps(payload))),
            mock.patch("tracing.opencode.hooks.handlers._handle_reconcile") as rh,
            mock.patch("tracing.opencode.hooks.handlers._handle_close") as ch,
        ):
            main()
        rh.assert_called_once_with(payload)
        ch.assert_not_called()

    def test_dispatches_close(self, monkeypatch):
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        payload = {"type": "close", "sessionID": "ses_d", "messages": []}
        with (
            mock.patch("tracing.opencode.hooks.handlers.check_requirements", return_value=True),
            mock.patch.object(sys, "stdin", new=io.StringIO(json.dumps(payload))),
            mock.patch("tracing.opencode.hooks.handlers._handle_reconcile") as rh,
            mock.patch("tracing.opencode.hooks.handlers._handle_close") as ch,
        ):
            main()
        ch.assert_called_once_with(payload)
        rh.assert_not_called()

    def test_unknown_type_does_not_dispatch(self, monkeypatch):
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        payload = {"type": "something-else", "sessionID": "ses_d"}
        with (
            mock.patch("tracing.opencode.hooks.handlers.check_requirements", return_value=True),
            mock.patch.object(sys, "stdin", new=io.StringIO(json.dumps(payload))),
            mock.patch("tracing.opencode.hooks.handlers._handle_reconcile") as rh,
            mock.patch("tracing.opencode.hooks.handlers._handle_close") as ch,
        ):
            main()  # must not raise
        rh.assert_not_called()
        ch.assert_not_called()

    def test_requirements_not_met_short_circuits(self, monkeypatch):
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "false")
        payload = {"type": "reconcile", "sessionID": "ses_d", "messages": []}
        with (
            mock.patch("tracing.opencode.hooks.handlers.check_requirements", return_value=False),
            mock.patch.object(sys, "stdin", new=io.StringIO(json.dumps(payload))),
            mock.patch("tracing.opencode.hooks.handlers._handle_reconcile") as rh,
            mock.patch("tracing.opencode.hooks.handlers._handle_close") as ch,
        ):
            main()
        rh.assert_not_called()
        ch.assert_not_called()

    def test_malformed_stdin_no_crash(self, monkeypatch):
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        with (
            mock.patch("tracing.opencode.hooks.handlers.check_requirements", return_value=True),
            mock.patch.object(sys, "stdin", new=io.StringIO("not-json")),
            mock.patch("tracing.opencode.hooks.handlers._handle_reconcile") as rh,
            mock.patch("tracing.opencode.hooks.handlers._handle_close") as ch,
        ):
            main()  # must not raise
        rh.assert_not_called()
        ch.assert_not_called()

    def test_handler_exception_is_caught(self, monkeypatch, capsys):
        """A raised exception in a handler must be caught — never escape main()."""
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        payload = {"type": "reconcile", "sessionID": "ses_d", "messages": []}
        with (
            mock.patch("tracing.opencode.hooks.handlers.check_requirements", return_value=True),
            mock.patch.object(sys, "stdin", new=io.StringIO(json.dumps(payload))),
            mock.patch(
                "tracing.opencode.hooks.handlers._handle_reconcile",
                side_effect=RuntimeError("boom"),
            ),
        ):
            main()  # must not raise

    def test_no_system_exit_on_unknown(self, monkeypatch):
        """main() must never raise SystemExit on unknown types."""
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        payload = {"type": "???"}
        with (
            mock.patch("tracing.opencode.hooks.handlers.check_requirements", return_value=True),
            mock.patch.object(sys, "stdin", new=io.StringIO(json.dumps(payload))),
        ):
            try:
                main()
            except SystemExit:
                pytest.fail("main() raised SystemExit on unknown type")


# ---------------------------------------------------------------------------
# Service / scope metadata
# ---------------------------------------------------------------------------


class TestSpanServiceMetadata:
    def test_service_name_opencode(self, mock_resolve, mock_ensure, state, captured_spans):
        _handle_close(dict(_load_fixture("reconcile_basic.json"), type="close"))
        s = captured_spans[0]
        svc = s["resourceSpans"][0]["resource"]["attributes"][0]
        assert svc["key"] == "service.name"
        assert svc["value"]["stringValue"] == "opencode"

    def test_scope_name_arize_opencode_plugin(self, mock_resolve, mock_ensure, state, captured_spans):
        _handle_close(dict(_load_fixture("reconcile_basic.json"), type="close"))
        s = captured_spans[0]
        scope = s["resourceSpans"][0]["scopeSpans"][0]["scope"]
        assert scope["name"] == "arize-opencode-plugin"
