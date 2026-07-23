"""Contract tests for the Claude JSONL -> typed event graph parser."""

import json
from pathlib import Path

from core.event_model import AgentEvent, EventStatus, ModelCallEvent, ToolEvent, TurnEvent
from tracing.claude_code.hooks.transcript import parse_claude_transcript

FIXTURE_DIR = Path(__file__).parent / "fixtures"


def _turn(session_id: str = "session-main-1") -> TurnEvent:
    return TurnEvent(
        event_id="turn-1",
        session_id=session_id,
        turn_id="turn-1",
        sequence=0,
        started_at_ms=1_767_268_800_000,
        ended_at_ms=None,
        status=EventStatus.RUNNING,
        input="synthetic prompt",
    )


def _agent() -> AgentEvent:
    return AgentEvent(
        event_id="agent:agent-1",
        parent_event_id="turn-1",
        session_id="session-agent-1",
        turn_id="turn-1",
        sequence=1,
        started_at_ms=1_767_272_401_000,
        ended_at_ms=None,
        status=EventStatus.RUNNING,
        agent_id="agent-1",
        input="read hello.py",
    )


def _typed(events, event_type):
    return [event for event in events if isinstance(event, event_type)]


def test_falsy_assistant_and_negative_tool_result_timestamps_are_diagnosed(tmp_path: Path):
    transcript = tmp_path / "invalid-timestamps.jsonl"
    rows = [
        {
            "type": "assistant",
            "uuid": "assistant-1",
            "timestamp": "",
            "message": {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "call-1", "name": "Read", "input": {}}],
            },
        },
        {
            "type": "user",
            "timestamp": -1,
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "call-1", "content": "ok"}],
            },
        },
    ]
    transcript.write_text("\n".join(json.dumps(row) for row in rows) + "\n")

    graph = parse_claude_transcript(transcript, _turn())

    diagnostics = [item for item in graph.diagnostics if item.code == "invalid_timestamp"]
    assert len(diagnostics) == 2
    assert {item.event_id for item in diagnostics} == {"assistant-1", "tool:call-1"}


def test_main_fixture_restores_each_model_call_and_exact_tool_parent():
    graph = parse_claude_transcript(FIXTURE_DIR / "main_tool_cycle.jsonl", _turn())

    models = _typed(graph.events, ModelCallEvent)
    tools = _typed(graph.events, ToolEvent)

    assert [model.event_id for model in models] == [
        "assistant-main-1",
        "assistant-main-2",
        "assistant-main-3",
    ]
    assert [model.parent_event_id for model in models] == ["turn-1"] * 3
    assert [tool.tool_call_id for tool in tools] == ["tool-read-1", "tool-bash-1"]
    assert [tool.parent_event_id for tool in tools] == ["assistant-main-1", "assistant-main-2"]
    assert [tool.tool_name for tool in tools] == ["Read", "Bash"]
    assert tools[0].output == 'def greeting():\n    return "SYNTHETIC_TOOL_OK"'
    assert tools[1].output == "SYNTHETIC_TOOL_OK"
    assert all(tool.status is EventStatus.COMPLETED for tool in tools)
    assert graph.validate() == []


def test_model_usage_and_text_are_kept_per_call():
    graph = parse_claude_transcript(FIXTURE_DIR / "main_tool_cycle.jsonl", _turn())
    models = _typed(graph.events, ModelCallEvent)

    assert models[0].model == "qwen3-coder-next"
    assert models[0].usage.input_tokens == 100
    assert models[0].usage.output_tokens == 20
    assert models[0].usage.cache_read_tokens == 10
    assert models[0].usage.cache_write_tokens == 5
    assert models[0].usage.total_tokens == 120
    assert models[0].output == ""
    assert models[2].output == "SYNTHETIC_TOOL_OK"
    assert models[2].status is EventStatus.COMPLETED


def test_split_message_id_records_coalesce_into_one_model_call(tmp_path: Path):
    """Claude Code v2 writes one assistant response (one message.id) across
    several records — thinking / text / each tool_use. They must fold into a
    single ModelCallEvent, not one LLM span per record, with un-inflated usage."""
    transcript = tmp_path / "split-message.jsonl"
    usage = {"input_tokens": 100, "output_tokens": 20, "cache_read_input_tokens": 10}
    rows = [
        {
            "type": "assistant",
            "uuid": "rec-1",
            "timestamp": "2026-01-01T00:00:00Z",
            "message": {
                "role": "assistant",
                "id": "msg-1",
                "model": "claude-x",
                "content": [{"type": "thinking", "thinking": "hmm"}],
                "usage": usage,
            },
        },
        {
            "type": "assistant",
            "uuid": "rec-2",
            "timestamp": "2026-01-01T00:00:01Z",
            "message": {
                "role": "assistant",
                "id": "msg-1",
                "model": "claude-x",
                "content": [{"type": "text", "text": "Hello"}],
                "usage": usage,
            },
        },
        {
            "type": "assistant",
            "uuid": "rec-3",
            "timestamp": "2026-01-01T00:00:02Z",
            "message": {
                "role": "assistant",
                "id": "msg-1",
                "content": [{"type": "tool_use", "id": "call-1", "name": "Read", "input": {"file_path": "x"}}],
                "usage": usage,
            },
        },
        {
            "type": "assistant",
            "uuid": "rec-4",
            "timestamp": "2026-01-01T00:00:03Z",
            "message": {
                "role": "assistant",
                "id": "msg-1",
                "content": [{"type": "tool_use", "id": "call-2", "name": "Bash", "input": {"command": "ls"}}],
                "usage": usage,
            },
        },
        {
            "type": "user",
            "timestamp": "2026-01-01T00:00:04Z",
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "call-1", "content": "ok1"}],
            },
        },
        {
            "type": "user",
            "timestamp": "2026-01-01T00:00:05Z",
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "call-2", "content": "ok2"}],
            },
        },
    ]
    transcript.write_text("\n".join(json.dumps(row) for row in rows) + "\n")

    graph = parse_claude_transcript(transcript, _turn())

    models = _typed(graph.events, ModelCallEvent)
    tools = _typed(graph.events, ToolEvent)

    # One real LLM call, not four.
    assert len(models) == 1
    assert models[0].event_id == "rec-1"
    assert models[0].source_id == "msg-1"
    assert models[0].model == "claude-x"
    assert "Hello" in (models[0].output or "")
    # Both tool_use blocks hang off the single coalesced call.
    assert [tool.tool_call_id for tool in tools] == ["call-1", "call-2"]
    assert [tool.parent_event_id for tool in tools] == ["rec-1", "rec-1"]
    # Usage is the message's usage, not summed across the split records.
    assert models[0].usage.input_tokens == 100
    assert models[0].usage.output_tokens == 20
    assert models[0].usage.cache_read_tokens == 10
    # Span covers the full response window.
    assert models[0].ended_at_ms > models[0].started_at_ms


def test_start_line_excludes_prior_model_and_tool_cycles():
    graph = parse_claude_transcript(FIXTURE_DIR / "main_tool_cycle.jsonl", _turn(), start_line=3)

    assert [event.event_id for event in _typed(graph.events, ModelCallEvent)] == [
        "assistant-main-2",
        "assistant-main-3",
    ]
    assert [event.tool_call_id for event in _typed(graph.events, ToolEvent)] == ["tool-bash-1"]


def test_subagent_transcript_uses_agent_as_root_and_keeps_agent_id():
    graph = parse_claude_transcript(FIXTURE_DIR / "subagent_agent.jsonl", _agent())

    models = _typed(graph.events, ModelCallEvent)
    tools = _typed(graph.events, ToolEvent)

    assert graph.events[0].event_id == "agent:agent-1"
    assert [model.parent_event_id for model in models] == ["agent:agent-1", "agent:agent-1"]
    assert all(model.agent_id == "agent-1" for model in models)
    assert tools[0].parent_event_id == "assistant-subagent-1"
    assert tools[0].agent_id == "agent-1"
    assert tools[0].output.startswith("def greeting")


def test_main_agent_tool_result_preserves_agent_correlation_metadata():
    root = _turn(session_id="session-agent-1")
    graph = parse_claude_transcript(FIXTURE_DIR / "subagent_main.jsonl", root)
    tool = _typed(graph.events, ToolEvent)[0]

    assert tool.tool_name == "Agent"
    assert tool.tool_call_id == "tool-agent-1"
    assert tool.input["subagent_type"] == "synthetic-explorer"
    assert tool.input["run_in_background"] is False
    assert tool.output["toolUseResult"]["agentId"] == "agent-1"
    assert tool.output["toolUseResult"]["agentType"] == "synthetic-explorer"


def test_malformed_unknown_and_partial_records_fail_soft(tmp_path: Path):
    transcript = tmp_path / "partial.jsonl"
    transcript.write_text(
        "not-json\n"
        + json.dumps({"type": "future-record", "payload": {"x": 1}})
        + "\n"
        + json.dumps(
            {
                "type": "assistant",
                "timestamp": "not-a-time",
                "message": {
                    "role": "assistant",
                    "model": "m",
                    "content": "legacy text",
                    "usage": {"input_tokens": "bad", "output_tokens": 2},
                },
            }
        )
        + "\n"
    )

    graph = parse_claude_transcript(transcript, _turn())
    models = _typed(graph.events, ModelCallEvent)

    assert len(models) == 1
    assert models[0].output == "legacy text"
    assert models[0].usage.input_tokens == 0
    assert models[0].usage.output_tokens == 2
    assert any(diagnostic.code == "malformed_json" for diagnostic in graph.diagnostics)
    assert any(diagnostic.code == "missing_source_id" for diagnostic in graph.diagnostics)
    assert any(diagnostic.code == "invalid_timestamp" for diagnostic in graph.diagnostics)


def test_error_tool_result_marks_failed_and_preserves_error(tmp_path: Path):
    transcript = tmp_path / "failed.jsonl"
    rows = [
        {
            "type": "assistant",
            "uuid": "assistant-failed-1",
            "timestamp": "2026-01-01T12:00:01.000Z",
            "sessionId": "session-main-1",
            "message": {
                "role": "assistant",
                "model": "m",
                "content": [{"type": "tool_use", "id": "tool-failed-1", "name": "Bash", "input": {}}],
                "usage": {},
            },
        },
        {
            "type": "user",
            "uuid": "result-failed-1",
            "timestamp": "2026-01-01T12:00:02.000Z",
            "sessionId": "session-main-1",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tool-failed-1",
                        "content": "command failed",
                        "is_error": True,
                    }
                ],
            },
        },
    ]
    transcript.write_text("\n".join(json.dumps(row) for row in rows) + "\n")

    graph = parse_claude_transcript(transcript, _turn())
    tool = _typed(graph.events, ToolEvent)[0]

    assert tool.status is EventStatus.FAILED
    assert tool.error == "command failed"
    assert tool.output == "command failed"


def test_invalid_numeric_timestamps_fail_soft(tmp_path: Path):
    transcript = tmp_path / "invalid-timestamps.jsonl"
    transcript.write_text(
        "\n".join(
            '{"type":"assistant","uuid":"assistant-%s","timestamp":%s,"message":{"role":"assistant","content":"ok"}}'
            % (index, value)
            for index, value in enumerate(("NaN", "Infinity", "1e309", "-1"), start=1)
        )
        + "\n"
    )

    graph = parse_claude_transcript(transcript, _turn())

    assert [event.started_at_ms for event in _typed(graph.events, ModelCallEvent)] == [None, None, None, None]
    assert sum(item.code == "invalid_timestamp" for item in graph.diagnostics) == 4


def test_parser_diagnostics_survive_revalidation(tmp_path: Path):
    transcript = tmp_path / "malformed.jsonl"
    transcript.write_text("not-json\n")
    graph = parse_claude_transcript(transcript, _turn())

    graph.validate()

    assert any(item.code == "malformed_json" for item in graph.diagnostics)


def test_duplicate_tool_call_id_keeps_first_use_for_result_correlation(tmp_path: Path):
    transcript = tmp_path / "duplicate-tools.jsonl"
    rows = [
        {
            "uuid": "assistant-1",
            "message": {"role": "assistant", "content": [{"type": "tool_use", "id": "shared", "name": "Read"}]},
        },
        {
            "uuid": "assistant-2",
            "message": {"role": "assistant", "content": [{"type": "tool_use", "id": "shared", "name": "Bash"}]},
        },
        {
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "shared", "content": "first"}],
            }
        },
    ]
    transcript.write_text("\n".join(json.dumps(row) for row in rows) + "\n")

    graph = parse_claude_transcript(transcript, _turn())
    tools = _typed(graph.events, ToolEvent)

    assert tools[0].output == "first"
    assert tools[0].status is EventStatus.COMPLETED
    assert tools[1].output is None
    assert tools[1].status is EventStatus.PENDING
    assert tools[0].event_id != tools[1].event_id
    assert any(item.code == "duplicate_tool_call_id" for item in graph.diagnostics)
