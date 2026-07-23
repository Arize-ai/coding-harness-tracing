"""Contract tests for the harness-neutral coding-agent event model."""

import json

from core.event_model import (
    AgentEvent,
    EventGraph,
    EventStatus,
    GraphDiagnostic,
    ModelCallEvent,
    ToolEvent,
    TurnEvent,
    Usage,
)


def _turn(event_id="turn-1", **overrides):
    values = {
        "event_id": event_id,
        "session_id": "session-1",
        "turn_id": "turn-1",
        "sequence": 1,
        "started_at_ms": 100,
        "ended_at_ms": 200,
        "status": EventStatus.COMPLETED,
        "input": {"prompt": "fix it"},
        "output": {"text": "done"},
    }
    values.update(overrides)
    return TurnEvent(**values)


def test_usage_total_excludes_cache_counts_by_default():
    usage = Usage(
        input_tokens=100,
        output_tokens=25,
        cache_read_tokens=80,
        cache_write_tokens=10,
    )

    assert usage.total_tokens == 125


def test_usage_can_preserve_an_explicit_source_total():
    usage = Usage(input_tokens=100, output_tokens=25, reported_total_tokens=215)

    assert usage.total_tokens == 215


def test_typed_events_represent_turn_agent_model_and_tool_relationships():
    turn = _turn()
    agent = AgentEvent(
        event_id="agent-1",
        parent_event_id=turn.event_id,
        session_id=turn.session_id,
        turn_id=turn.turn_id,
        agent_id="researcher",
        source_id="claude-subagent-7",
        sequence=2,
        started_at_ms=110,
        ended_at_ms=190,
        status=EventStatus.COMPLETED,
        input={"task": "inspect"},
        output={"summary": "ok"},
    )
    model = ModelCallEvent(
        event_id="model-1",
        parent_event_id=agent.event_id,
        session_id=turn.session_id,
        turn_id=turn.turn_id,
        agent_id=agent.agent_id,
        sequence=3,
        started_at_ms=120,
        ended_at_ms=150,
        status=EventStatus.COMPLETED,
        model="claude-test",
        input={"messages": [{"role": "user", "content": "inspect"}]},
        output={"content": [{"type": "text", "text": "checking"}]},
        usage=Usage(input_tokens=12, output_tokens=4, cache_read_tokens=3),
    )
    tool = ToolEvent(
        event_id="tool-1",
        parent_event_id=model.event_id,
        session_id=turn.session_id,
        turn_id=turn.turn_id,
        agent_id=agent.agent_id,
        tool_call_id="call-1",
        sequence=4,
        started_at_ms=130,
        ended_at_ms=140,
        status=EventStatus.FAILED,
        error="command failed",
        tool_name="shell",
        input={"command": "false"},
        output={"exit_code": 1},
    )

    graph = EventGraph([turn, agent, model, tool])

    assert graph.validate() == []
    assert graph.events == [turn, agent, model, tool]
    assert model.usage.total_tokens == 16
    assert tool.error == "command failed"


def test_graph_reports_duplicate_ids_without_dropping_events():
    first = _turn("duplicate")
    second = _turn("duplicate", sequence=2)
    graph = EventGraph([first, second])

    diagnostics = graph.validate()

    assert len(graph.events) == 2
    assert any(item.code == "duplicate_event_id" and item.event_id == "duplicate" for item in diagnostics)
    assert graph.diagnostics == diagnostics


def test_revalidation_preserves_equal_external_diagnostic_by_identity():
    graph = EventGraph([_turn("duplicate"), _turn("duplicate", sequence=2)])
    external = GraphDiagnostic(
        code="duplicate_event_id",
        message="event_id is used by more than one event",
        event_id="duplicate",
        related_event_id="duplicate",
    )
    graph.diagnostics.append(external)

    graph.validate()
    graph.validate()

    matching = [item for item in graph.diagnostics if item == external]
    assert len(matching) == 2
    assert matching[0] is external


def test_graph_reports_missing_parents():
    child = _turn("child", parent_event_id="absent")

    diagnostics = EventGraph([child]).validate()

    assert any(
        item.code == "missing_parent" and item.event_id == "child" and item.related_event_id == "absent"
        for item in diagnostics
    )


def test_graph_reports_self_parent_and_multi_event_cycles():
    self_parent = _turn("self", parent_event_id="self")
    first = _turn("first", parent_event_id="second")
    second = _turn("second", parent_event_id="first")

    diagnostics = EventGraph([self_parent, first, second]).validate()

    cyclic_ids = {item.event_id for item in diagnostics if item.code == "parent_cycle"}
    assert cyclic_ids == {"self", "first", "second"}


def test_graph_reports_tool_call_ids_reused_by_different_tools():
    common = {
        "session_id": "session-1",
        "turn_id": "turn-1",
        "tool_call_id": "call-shared",
        "started_at_ms": 100,
        "ended_at_ms": 110,
        "status": EventStatus.COMPLETED,
    }
    shell = ToolEvent(event_id="shell", sequence=1, tool_name="shell", **common)
    editor = ToolEvent(event_id="editor", sequence=2, tool_name="editor", **common)

    diagnostics = EventGraph([shell, editor]).validate()

    assert any(
        item.code == "duplicate_tool_call_id" and item.event_id == "editor" and item.related_event_id == "shell"
        for item in diagnostics
    )


def test_graph_reports_tool_call_id_reuse_for_same_tool_name():
    common = {
        "session_id": "session-1",
        "turn_id": "turn-1",
        "tool_call_id": "call-shared",
        "tool_name": "Read",
        "started_at_ms": 100,
        "ended_at_ms": 110,
        "status": EventStatus.COMPLETED,
    }
    first = ToolEvent(event_id="first", sequence=1, parent_event_id="model-1", **common)
    second = ToolEvent(event_id="second", sequence=2, parent_event_id="model-2", **common)

    diagnostics = EventGraph([first, second]).validate()

    assert any(item.code == "duplicate_tool_call_id" and item.event_id == "second" for item in diagnostics)


def test_graph_reports_multiple_roots():
    diagnostics = EventGraph([_turn("first"), _turn("second", sequence=2)]).validate()

    assert any(
        item.code == "multiple_roots" and item.event_id == "second" and item.related_event_id == "first"
        for item in diagnostics
    )


def test_graph_reports_invalid_timestamp_ranges_without_crashing():
    reversed_range = _turn("reversed", started_at_ms=200, ended_at_ms=100)
    malformed_range = _turn("malformed", started_at_ms="not-a-time", ended_at_ms=100)

    diagnostics = EventGraph([reversed_range, malformed_range]).validate()

    invalid_ids = {item.event_id for item in diagnostics if item.code == "invalid_timestamp_range"}
    assert invalid_ids == {"reversed", "malformed"}


def test_graph_and_events_are_json_serializable():
    graph = EventGraph(
        [
            _turn(
                status=EventStatus.FAILED,
                input={"nested": (EventStatus.RUNNING,)},
                output=None,
                error="stopped",
            )
        ]
    )

    payload = graph.to_dict()
    encoded = json.dumps(payload)

    assert payload["events"][0]["event_type"] == "turn"
    assert payload["events"][0]["status"] == "failed"
    assert payload["events"][0]["input"] == {"nested": ["running"]}
    assert '"diagnostics": []' in encoded
