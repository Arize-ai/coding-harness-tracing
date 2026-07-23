"""High-fidelity Claude hook integration contract."""

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from core.common import StateManager
from core.event_model import EventGraph, EventStatus, ToolEvent, TurnEvent
from tracing.claude_code.hooks.handlers import (
    _buffer_subagent,
    _cleanup_pending_subagents,
    _handle_post_tool_use,
    _handle_post_tool_use_failure,
    _handle_pre_tool_use,
    _handle_stop,
    _handle_subagent_start,
    _handle_subagent_stop,
    _merge_pending_subagents,
    _merge_tool_observations,
    _pending_subagents,
)
from tracing.claude_code.hooks.tool_buffer import ToolBuffer, ToolObservation
from tracing.claude_code.hooks.transcript import parse_claude_transcript

FIXTURE = Path(__file__).parent / "fixtures" / "main_tool_cycle.jsonl"
SUBAGENT_MAIN_FIXTURE = Path(__file__).parent / "fixtures" / "subagent_main.jsonl"
SUBAGENT_AGENT_FIXTURE = Path(__file__).parent / "fixtures" / "subagent_agent.jsonl"


def _spans(payload):
    return payload["resourceSpans"][0]["scopeSpans"][0]["spans"]


def _attrs(span):
    return {attribute["key"]: next(iter(attribute["value"].values())) for attribute in span["attributes"]}


def test_malformed_persisted_tool_timestamp_fails_soft_with_diagnostic():
    root = TurnEvent(
        event_id="turn",
        session_id="session",
        turn_id="1",
        sequence=0,
        started_at_ms=1,
        ended_at_ms=2,
        status=EventStatus.COMPLETED,
    )
    tool = ToolEvent(
        event_id="tool:call",
        parent_event_id="turn",
        session_id="session",
        turn_id="1",
        sequence=1,
        started_at_ms=1,
        ended_at_ms=None,
        status=EventStatus.PENDING,
        tool_call_id="call",
    )
    graph = EventGraph([root, tool])
    observation = ToolObservation.from_dict({"tool_use_id": "call", "ended_at_ms": "not-a-time", "status": "success"})

    assert _merge_tool_observations(graph, [observation]) == [observation]
    assert tool.ended_at_ms is None
    assert any(item.code == "invalid_timestamp" and item.event_id == tool.event_id for item in graph.diagnostics)


def test_hook_output_overlay_preserves_transcript_agent_correlation():
    root = TurnEvent(
        event_id="turn:test",
        session_id="session-agent-1",
        turn_id="1",
        sequence=0,
        started_at_ms=None,
        ended_at_ms=None,
        status=EventStatus.RUNNING,
    )
    graph = parse_claude_transcript(SUBAGENT_MAIN_FIXTURE, root)
    _merge_tool_observations(
        graph,
        [
            ToolObservation(
                tool_use_id="tool-agent-1",
                tool_response="hook-only agent result",
                status="success",
            )
        ],
    )

    agent_tool = next(event for event in graph.events if getattr(event, "tool_call_id", None) == "tool-agent-1")
    assert agent_tool.output["content"] == "hook-only agent result"
    assert agent_tool.output["toolUseResult"]["agentId"] == "agent-1"


def test_hook_overlay_is_first_wins_for_duplicate_tool_call_ids():
    tools = [
        ToolEvent(
            event_id=f"tool-{index}",
            parent_event_id=f"model-{index}",
            session_id="session",
            turn_id="1",
            sequence=index,
            started_at_ms=None,
            ended_at_ms=None,
            status=EventStatus.PENDING,
            tool_call_id="duplicate",
            tool_name="Read",
        )
        for index in (1, 2)
    ]
    observation = ToolObservation(tool_use_id="duplicate", tool_response="hook-result", status="success")

    matched = _merge_tool_observations(EventGraph(tools), [observation])

    assert matched == [observation]
    assert tools[0].status is EventStatus.COMPLETED
    assert tools[0].output == "hook-result"
    assert tools[1].status is EventStatus.PENDING
    assert tools[1].output is None


def test_unmatched_snapshots_are_not_acknowledged(tmp_path: Path):
    state = StateManager(tmp_path, state_file=tmp_path / "state.json", lock_path=tmp_path / "state.lock")
    state.init_state()
    descriptor = {
        "agent_id": "unmatched",
        "agent_type": "general-purpose",
        "transcript_path": str(SUBAGENT_AGENT_FIXTURE),
    }
    state.set("pending_subagents", json.dumps({"unmatched": descriptor}))
    root = TurnEvent(
        event_id="turn",
        session_id="session",
        turn_id="1",
        sequence=0,
        started_at_ms=None,
        ended_at_ms=None,
        status=EventStatus.COMPLETED,
    )

    matched = _merge_pending_subagents(EventGraph([root]), _pending_subagents(state))
    _cleanup_pending_subagents(state, matched)

    assert matched == {}
    assert "unmatched" in _pending_subagents(state)


def test_parallel_subagent_buffering_is_atomic(tmp_path: Path):
    state = StateManager(tmp_path, state_file=tmp_path / "state.json", lock_path=tmp_path / "state.lock")
    state.init_state()
    count = 24
    for index in range(count):
        state.set(f"subagent_agent-{index}_start_time", str(index))

    def add(index: int) -> None:
        _buffer_subagent(
            state,
            {
                "agent_id": f"agent-{index}",
                "agent_type": "general-purpose",
                "transcript_path": str(SUBAGENT_MAIN_FIXTURE),
                "agent_transcript_path": str(SUBAGENT_AGENT_FIXTURE),
            },
            ended_at_ms=100 + index,
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(add, range(count)))

    assert set(_pending_subagents(state)) == {f"agent-{index}" for index in range(count)}


def test_pending_subagent_cleanup_only_acknowledges_unchanged_snapshot(tmp_path: Path):
    state = StateManager(tmp_path, state_file=tmp_path / "state.json", lock_path=tmp_path / "state.lock")
    state.init_state()
    state.set("subagent_old_start_time", "1")
    _buffer_subagent(
        state,
        {
            "agent_id": "old",
            "agent_type": "general-purpose",
            "transcript_path": str(SUBAGENT_MAIN_FIXTURE),
            "agent_transcript_path": str(SUBAGENT_AGENT_FIXTURE),
        },
        ended_at_ms=10,
    )
    snapshot = _pending_subagents(state)

    state.set("subagent_new_start_time", "2")
    _buffer_subagent(
        state,
        {
            "agent_id": "new",
            "agent_type": "general-purpose",
            "transcript_path": str(SUBAGENT_MAIN_FIXTURE),
            "agent_transcript_path": str(SUBAGENT_AGENT_FIXTURE),
        },
        ended_at_ms=20,
    )

    _cleanup_pending_subagents(state, snapshot)

    assert list(_pending_subagents(state)) == ["new"]
    assert state.get("subagent_new_start_time") == "2"


def test_real_v2_transcript_is_exported_once_as_correlated_tree(tmp_path: Path):
    state_file = tmp_path / "state.json"
    state = StateManager(tmp_path, state_file=state_file, lock_path=tmp_path / "state.lock")
    state.init_state()
    state.set("session_id", "session-main-1")
    state.set("current_trace_id", "a" * 32)
    state.set("current_trace_span_id", "b" * 16)
    state.set("current_trace_start_time", "1767268800000")
    state.set("current_trace_prompt", "synthetic prompt")
    state.set("trace_count", "1")
    state.set("trace_start_line", "0")
    state.set("project_name", "synthetic-project")
    sent = []

    def send_success(payload):
        sent.append(payload)
        return True

    common_input = {"session_id": "session-main-1", "transcript_path": str(FIXTURE)}
    with (
        mock.patch("tracing.claude_code.hooks.handlers.resolve_session", return_value=state),
        mock.patch("tracing.claude_code.hooks.handlers.resolve_transcript_path", return_value=FIXTURE),
        mock.patch(
            "tracing.claude_code.hooks.handlers.get_timestamp_ms",
            side_effect=[1767268801000, 1767268802000, 1767268803000, 1767268804000, 1767268805000],
        ),
        mock.patch("tracing.claude_code.hooks.handlers.send_span", side_effect=send_success),
    ):
        _handle_pre_tool_use(
            {
                **common_input,
                "tool_name": "Read",
                "tool_use_id": "tool-read-1",
                "tool_input": {"file_path": "/synthetic/project/hello.py"},
            }
        )
        _handle_post_tool_use(
            {
                **common_input,
                "tool_name": "Read",
                "tool_use_id": "tool-read-1",
                "tool_input": {"file_path": "/synthetic/project/hello.py"},
                "tool_response": "hook read output",
            }
        )
        _handle_stop({**common_input, "last_assistant_message": "SYNTHETIC_TOOL_OK"})

    assert len(sent) == 1
    spans = _spans(sent[0])
    assert [span["name"] for span in spans] == [
        "Turn 1",
        "LLM call 1: qwen3-coder-next",
        "Read",
        "LLM call 2: qwen3-coder-next",
        "Bash",
        "LLM call 3: qwen3-coder-next",
    ]
    assert spans[0]["spanId"] == "b" * 16
    assert "parentSpanId" not in spans[0]
    assert spans[2]["parentSpanId"] == spans[1]["spanId"]
    assert spans[4]["parentSpanId"] == spans[3]["spanId"]
    assert _attrs(spans[0])["openinference.span.kind"] == "CHAIN"
    assert "llm.token_count.total" not in _attrs(spans[0])
    assert _attrs(spans[2])["output.value"] == "hook read output"
    assert _attrs(spans[2])["tool.call.id"] == "tool-read-1"
    assert ToolBuffer(state).all() == []
    assert state.get("current_trace_id") is None


def test_legacy_post_tool_without_transcript_keeps_immediate_fallback(tmp_path: Path):
    state_file = tmp_path / "state.json"
    state = StateManager(tmp_path, state_file=state_file, lock_path=tmp_path / "state.lock")
    state.init_state()
    state.set("session_id", "legacy-session")
    state.set("current_trace_id", "trace-legacy")
    state.set("current_trace_span_id", "span-legacy")
    sent = []

    with (
        mock.patch("tracing.claude_code.hooks.handlers.resolve_session", return_value=state),
        mock.patch("tracing.claude_code.hooks.handlers.send_span", side_effect=sent.append),
    ):
        _handle_post_tool_use(
            {
                "tool_name": "Read",
                "tool_use_id": "legacy-tool",
                "tool_input": {"file_path": "/legacy.py"},
                "tool_response": "legacy output",
            }
        )

    assert len(sent) == 1
    assert _spans(sent[0])[0]["name"] == "Read"


def test_failed_export_retains_buffer_and_reuses_span_ids_on_retry(tmp_path: Path):
    state = StateManager(tmp_path, state_file=tmp_path / "state.json", lock_path=tmp_path / "state.lock")
    state.init_state()
    for key, value in {
        "session_id": "session-main-1",
        "current_trace_id": "a" * 32,
        "current_trace_span_id": "b" * 16,
        "current_trace_start_time": "1767268800000",
        "current_trace_prompt": "synthetic prompt",
        "trace_count": "1",
        "trace_start_line": "0",
        "project_name": "synthetic-project",
    }.items():
        state.set(key, value)

    common_input = {"session_id": "session-main-1", "transcript_path": str(FIXTURE)}
    attempts = []

    def send_with_retry(payload):
        attempts.append(payload)
        return len(attempts) == 2

    with (
        mock.patch("tracing.claude_code.hooks.handlers.resolve_session", return_value=state),
        mock.patch("tracing.claude_code.hooks.handlers.resolve_transcript_path", return_value=FIXTURE),
        mock.patch(
            "tracing.claude_code.hooks.handlers.get_timestamp_ms",
            side_effect=[1767268801000, 1767268802000, 1767268803000, 1767268804000],
        ),
        mock.patch("tracing.claude_code.hooks.handlers.send_span", side_effect=send_with_retry),
    ):
        _handle_pre_tool_use(
            {
                **common_input,
                "tool_name": "Read",
                "tool_use_id": "tool-read-1",
                "tool_input": {"file_path": "/synthetic/project/hello.py"},
            }
        )
        _handle_post_tool_use(
            {
                **common_input,
                "tool_name": "Read",
                "tool_use_id": "tool-read-1",
                "tool_response": "hook read output",
            }
        )
        _handle_stop({**common_input, "last_assistant_message": "SYNTHETIC_TOOL_OK"})

        assert state.get("current_trace_id") == "a" * 32
        assert len(ToolBuffer(state).all()) == 1
        assert state.get("high_fidelity_span_ids") is not None

        _handle_stop({**common_input, "last_assistant_message": "SYNTHETIC_TOOL_OK"})

    assert len(attempts) == 2
    assert [span["spanId"] for span in _spans(attempts[0])] == [span["spanId"] for span in _spans(attempts[1])]
    assert ToolBuffer(state).all() == []
    assert state.get("current_trace_id") is None
    assert state.get("high_fidelity_span_ids") is None


def test_foreground_subagent_is_buffered_and_exported_inside_main_tree(tmp_path: Path):
    state = StateManager(tmp_path, state_file=tmp_path / "state.json", lock_path=tmp_path / "state.lock")
    state.init_state()
    for key, value in {
        "session_id": "session-agent-1",
        "current_trace_id": "c" * 32,
        "current_trace_span_id": "d" * 16,
        "current_trace_start_time": "1767272400000",
        "current_trace_prompt": "Delegate one read-only subagent.",
        "trace_count": "1",
        "trace_start_line": "0",
        "project_name": "synthetic-project",
    }.items():
        state.set(key, value)

    sent = []
    export_results = iter([False, True])

    def send_with_retry(payload):
        sent.append(payload)
        return next(export_results)

    with (
        mock.patch("tracing.claude_code.hooks.handlers.resolve_session", return_value=state),
        mock.patch("tracing.claude_code.hooks.handlers.resolve_transcript_path", return_value=SUBAGENT_MAIN_FIXTURE),
        mock.patch(
            "tracing.claude_code.hooks.handlers.get_timestamp_ms",
            side_effect=[1767272401000, 1767272403000, 1767272404000, 1767272405000],
        ),
        mock.patch("tracing.claude_code.hooks.handlers.send_span", side_effect=send_with_retry),
    ):
        _handle_subagent_start(
            {
                "session_id": "session-agent-1",
                "transcript_path": str(SUBAGENT_MAIN_FIXTURE),
                "agent_id": "agent-1",
                "agent_type": "synthetic-explorer",
                "prompt_id": "prompt-agent-1",
            }
        )
        _handle_subagent_stop(
            {
                "session_id": "session-agent-1",
                "transcript_path": str(SUBAGENT_MAIN_FIXTURE),
                "agent_transcript_path": str(SUBAGENT_AGENT_FIXTURE),
                "agent_id": "agent-1",
                "agent_type": "synthetic-explorer",
                "last_assistant_message": "Function: greeting; return: SYNTHETIC_TOOL_OK",
            }
        )

        assert sent == []
        assert state.get("pending_subagents") is not None

        _handle_stop(
            {
                "session_id": "session-agent-1",
                "transcript_path": str(SUBAGENT_MAIN_FIXTURE),
                "last_assistant_message": "SUBAGENT_SCHEMA_OK",
            }
        )
        assert len(sent) == 1
        assert state.get("pending_subagents") is not None

        _handle_stop(
            {
                "session_id": "session-agent-1",
                "transcript_path": str(SUBAGENT_MAIN_FIXTURE),
                "last_assistant_message": "SUBAGENT_SCHEMA_OK",
            }
        )

    assert len(sent) == 2
    assert [span["spanId"] for span in _spans(sent[0])] == [span["spanId"] for span in _spans(sent[1])]
    spans = _spans(sent[1])
    assert [span["name"] for span in spans] == [
        "Turn 1",
        "LLM call 1: qwen3-coder-next",
        "Agent",
        "Subagent: synthetic-explorer",
        "LLM call 2: qwen3-coder-next",
        "Read",
        "LLM call 3: qwen3-coder-next",
        "LLM call 4: qwen3-coder-next",
    ]
    assert spans[2]["parentSpanId"] == spans[1]["spanId"]
    assert spans[3]["parentSpanId"] == spans[2]["spanId"]
    assert spans[4]["parentSpanId"] == spans[3]["spanId"]
    assert spans[5]["parentSpanId"] == spans[4]["spanId"]
    assert spans[6]["parentSpanId"] == spans[3]["spanId"]
    assert spans[7]["parentSpanId"] == spans[0]["spanId"]
    assert _attrs(spans[3])["openinference.span.kind"] == "AGENT"
    assert _attrs(spans[3])["subagent.id"] == "agent-1"
    assert _attrs(spans[3])["subagent.type"] == "synthetic-explorer"
    assert state.get("pending_subagents") is None
    assert state.get("subagent_agent-1_start_time") is None


def test_failed_tool_hook_is_correlated_and_marks_tool_span_error(tmp_path: Path):
    state = StateManager(tmp_path, state_file=tmp_path / "state.json", lock_path=tmp_path / "state.lock")
    state.init_state()
    for key, value in {
        "session_id": "session-main-1",
        "current_trace_id": "e" * 32,
        "current_trace_span_id": "f" * 16,
        "current_trace_start_time": "1767268800000",
        "current_trace_prompt": "synthetic prompt",
        "trace_count": "1",
        "trace_start_line": "0",
    }.items():
        state.set(key, value)

    sent = []

    def send_success(payload):
        sent.append(payload)
        return True

    common_input = {"session_id": "session-main-1", "transcript_path": str(FIXTURE)}
    with (
        mock.patch("tracing.claude_code.hooks.handlers.resolve_session", return_value=state),
        mock.patch("tracing.claude_code.hooks.handlers.resolve_transcript_path", return_value=FIXTURE),
        mock.patch(
            "tracing.claude_code.hooks.handlers.get_timestamp_ms",
            side_effect=[1767268801000, 1767268802000, 1767268803000],
        ),
        mock.patch("tracing.claude_code.hooks.handlers.send_span", side_effect=send_success),
    ):
        _handle_pre_tool_use(
            {
                **common_input,
                "tool_name": "Read",
                "tool_use_id": "tool-read-1",
                "tool_input": {"file_path": "/synthetic/project/missing.py"},
            }
        )
        _handle_post_tool_use_failure(
            {
                **common_input,
                "tool_name": "Read",
                "tool_use_id": "tool-read-1",
                "tool_input": {"file_path": "/synthetic/project/missing.py"},
                "error": "synthetic read failure",
            }
        )
        assert sent == []
        _handle_stop({**common_input, "last_assistant_message": "handled"})

    tool = next(span for span in _spans(sent[0]) if _attrs(span).get("tool.call.id") == "tool-read-1")
    assert tool["status"]["code"] == 2
    assert _attrs(tool)["error.message"] == "synthetic read failure"
    assert ToolBuffer(state).all() == []
