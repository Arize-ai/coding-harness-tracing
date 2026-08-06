"""Tests for the durable Claude hook tool-observation buffer."""

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from core.common import StateManager
from tracing.claude_code.hooks.tool_buffer import ToolBuffer, ToolObservation


@pytest.fixture
def state(tmp_path):
    manager = StateManager(
        state_dir=tmp_path,
        state_file=tmp_path / "state_session.json",
        lock_path=tmp_path / ".lock_session",
    )
    manager.init_state()
    return manager


@pytest.fixture
def buffer(state):
    return ToolBuffer(state)


def test_record_start_persists_a_json_safe_observation(buffer, state):
    observation = buffer.record_start(
        "tool-1",
        tool_name="Read",
        tool_input={"path": Path("/tmp/input"), "lines": (1, 2)},
        started_at_ms=100,
        hook_event_metadata={"hook_event_name": "PreToolUse"},
    )

    assert observation == ToolObservation(
        tool_use_id="tool-1",
        tool_name="Read",
        tool_input={"path": "/tmp/input", "lines": [1, 2]},
        started_at_ms=100,
        status="pending",
        hook_event_metadata={"hook_event_name": "PreToolUse"},
    )
    persisted = json.loads(state.get(ToolBuffer.STATE_KEY))
    assert list(persisted) == ["tool-1"]
    assert persisted["tool-1"] == observation.to_dict()


def test_record_result_merges_with_start_and_survives_new_buffer_instance(state):
    first = ToolBuffer(state)
    first.record_start("tool-1", tool_name="Bash", tool_input={"command": "pwd"}, started_at_ms=100)
    first.record_result(
        "tool-1",
        status="success",
        tool_response={"stdout": "/repo"},
        ended_at_ms=125,
        hook_event_metadata={"hook_event_name": "PostToolUse"},
    )

    observation = ToolBuffer(state).get("tool-1")
    assert observation == ToolObservation(
        tool_use_id="tool-1",
        tool_name="Bash",
        tool_input={"command": "pwd"},
        tool_response={"stdout": "/repo"},
        started_at_ms=100,
        ended_at_ms=125,
        status="success",
        hook_event_metadata={"hook_event_name": "PostToolUse"},
    )


def test_result_before_start_is_retained_and_merged_later(buffer):
    buffer.record_result(
        "tool-late-start",
        status="error",
        error={"message": "permission denied"},
        ended_at_ms=220,
    )

    observation = buffer.record_start(
        "tool-late-start",
        tool_name="Write",
        tool_input={"file_path": "/protected"},
        started_at_ms=200,
    )

    assert observation.tool_name == "Write"
    assert observation.tool_input == {"file_path": "/protected"}
    assert observation.started_at_ms == 200
    assert observation.ended_at_ms == 220
    assert observation.status == "error"
    assert observation.error == {"message": "permission denied"}


def test_repeated_hook_delivery_with_same_values_is_idempotent(buffer, state):
    start_kwargs = {
        "tool_name": "Read",
        "tool_input": {"path": "/tmp/a"},
        "started_at_ms": 100,
        "hook_event_metadata": {"event": "pre"},
    }
    result_kwargs = {
        "status": "success",
        "tool_response": {"content": "hello"},
        "ended_at_ms": 110,
        "hook_event_metadata": {"event": "post"},
    }
    buffer.record_start("tool-1", **start_kwargs)
    buffer.record_result("tool-1", **result_kwargs)
    stored_once = state.get(ToolBuffer.STATE_KEY)

    buffer.record_start("tool-1", **start_kwargs)
    buffer.record_result("tool-1", **result_kwargs)

    assert state.get(ToolBuffer.STATE_KEY) == stored_once
    assert len(buffer.all()) == 1


@pytest.mark.parametrize("tool_use_id", [None, "", "   ", 123])
def test_invalid_tool_use_id_is_rejected(buffer, tool_use_id):
    with pytest.raises(ValueError, match="tool_use_id"):
        buffer.record_start(tool_use_id, tool_name="Read", tool_input={}, started_at_ms=1)
    with pytest.raises(ValueError, match="tool_use_id"):
        buffer.record_result(tool_use_id, status="success", ended_at_ms=2)
    with pytest.raises(ValueError, match="tool_use_id"):
        buffer.get(tool_use_id)
    with pytest.raises(ValueError, match="tool_use_id"):
        buffer.remove(tool_use_id)


def test_multiple_concurrent_tool_ids_remain_isolated(state):
    def record(index):
        local_buffer = ToolBuffer(state)
        tool_id = f"tool-{index}"
        local_buffer.record_start(
            tool_id,
            tool_name="Task",
            tool_input={"index": index},
            started_at_ms=index,
        )
        local_buffer.record_result(
            tool_id,
            status="success",
            tool_response={"result": index},
            ended_at_ms=index + 100,
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(record, range(24)))

    observations = {item.tool_use_id: item for item in ToolBuffer(state).all()}
    assert len(observations) == 24
    for index in range(24):
        item = observations[f"tool-{index}"]
        assert item.tool_input == {"index": index}
        assert item.tool_response == {"result": index}
        assert item.started_at_ms == index
        assert item.ended_at_ms == index + 100


def test_remove_many_only_acknowledges_unchanged_snapshot(buffer):
    buffer.record_start("updated", tool_name="Read", started_at_ms=1)
    buffer.record_start("exported", tool_name="Read", started_at_ms=2)
    snapshot = buffer.all()

    buffer.record_result("updated", status="success", tool_response="new", ended_at_ms=3)
    buffer.record_start("new", tool_name="Bash", started_at_ms=4)

    assert buffer.remove_many(snapshot) == {"exported"}
    assert {item.tool_use_id for item in buffer.all()} == {"updated", "new"}


def test_remove_and_clear_update_the_single_persisted_namespace(buffer, state):
    buffer.record_start("tool-1", tool_name="Read", tool_input={}, started_at_ms=1)
    buffer.record_start("tool-2", tool_name="Write", tool_input={}, started_at_ms=2)

    assert buffer.remove("tool-1") is True
    assert buffer.remove("tool-1") is False
    assert [item.tool_use_id for item in buffer.all()] == ["tool-2"]

    buffer.clear()
    assert buffer.all() == []
    raw_state = json.loads(state.state_file.read_text(encoding="utf-8"))
    assert set(raw_state) == {ToolBuffer.STATE_KEY}
    assert json.loads(raw_state[ToolBuffer.STATE_KEY]) == {}


def test_malformed_stored_json_fails_soft_to_empty_buffer(state):
    state.set(ToolBuffer.STATE_KEY, "{not valid json")

    buffer = ToolBuffer(state)

    assert buffer.get("missing") is None
    assert buffer.all() == []
    assert buffer.remove("missing") is False


def test_invalid_result_status_is_rejected(buffer):
    with pytest.raises(ValueError, match="status"):
        buffer.record_result("tool-1", status="pending", ended_at_ms=2)
