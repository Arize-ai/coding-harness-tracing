"""Async (background) subagent export, task-notification turns, and Stop settle wait."""

import itertools
import json
from pathlib import Path
from unittest import mock

from core.common import StateManager
from tracing.claude_code.hooks import async_subagents, transcript_settle
from tracing.claude_code.hooks.handlers import (
    _handle_stop,
    _handle_subagent_start,
    _handle_subagent_stop,
    _handle_user_prompt_submit,
)

FIXTURES = Path(__file__).parent / "fixtures"
MAIN_FIXTURE = FIXTURES / "subagent_main.jsonl"
AGENT_FIXTURE = FIXTURES / "subagent_agent.jsonl"
SESSION = "session-async-1"
TRACE_ID = "c" * 32
TASK_NOTIFICATION = (
    "<task-notification>\n<task-id>agent-1</task-id>\n<status>completed</status>\n"
    "<summary>Background agent done</summary>\n</task-notification>"
)


def _spans(payload):
    return payload["resourceSpans"][0]["scopeSpans"][0]["spans"]


def _attrs(span):
    return {attribute["key"]: next(iter(attribute["value"].values())) for attribute in span["attributes"]}


def _state(tmp_path: Path, **overrides) -> StateManager:
    state = StateManager(tmp_path, state_file=tmp_path / "state.json", lock_path=tmp_path / "state.lock")
    state.init_state()
    values = {
        "session_id": SESSION,
        "current_trace_id": TRACE_ID,
        "current_trace_span_id": "d" * 16,
        "current_trace_start_time": "1767272400000",
        "current_trace_prompt": "Delegate one read-only subagent.",
        "trace_count": "1",
        "trace_start_line": "0",
        "project_name": "synthetic-project",
        **overrides,
    }
    for key, value in values.items():
        state.set(key, value)
    return state


def _patches(state, sent, transcript=MAIN_FIXTURE):
    return (
        mock.patch("tracing.claude_code.hooks.handlers.resolve_session", return_value=state),
        mock.patch("tracing.claude_code.hooks.handlers.resolve_transcript_path", return_value=transcript),
        mock.patch("tracing.claude_code.hooks.handlers.get_timestamp_ms", side_effect=itertools.count(1767272401000, 1000)),
        mock.patch("tracing.claude_code.hooks.handlers.send_span", side_effect=lambda payload: sent.append(payload) or True),
    )


def _subagent_start():
    return {"session_id": SESSION, "transcript_path": str(MAIN_FIXTURE), "agent_id": "agent-1", "agent_type": "synthetic-explorer"}


def _subagent_stop(**overrides):
    return {
        "session_id": SESSION,
        "transcript_path": str(MAIN_FIXTURE),
        "agent_transcript_path": str(AGENT_FIXTURE),
        "agent_id": "agent-1",
        "agent_type": "synthetic-explorer",
        "last_assistant_message": "Function: greeting; return: SYNTHETIC_TOOL_OK",
        **overrides,
    }


def _stop():
    return {"session_id": SESSION, "transcript_path": str(MAIN_FIXTURE), "last_assistant_message": "SUBAGENT_SCHEMA_OK"}


def test_async_subagent_is_exported_under_the_agent_tool_span_of_the_finished_turn(tmp_path: Path):
    state = _state(tmp_path)
    sent = []
    p1, p2, p3, p4 = _patches(state, sent)
    with p1, p2, p3, p4:
        _handle_subagent_start(_subagent_start())
        _handle_stop(_stop())  # Claude Code 2.1.2xx: the turn ends before the background agent does
        assert len(sent) == 1
        recorded = async_subagents.decode_parents(state.get(async_subagents.STATE_KEY))
        assert set(recorded) == {"agent-1"}
        assert state.get("current_trace_id") is None

        _handle_subagent_stop(_subagent_stop())

    assert len(sent) == 2
    turn_spans = _spans(sent[0])
    agent_tool = next(span for span in turn_spans if span["name"] == "Agent")
    subagent_spans = _spans(sent[1])
    assert [span["name"] for span in subagent_spans] == [
        "Subagent: synthetic-explorer",
        "LLM call 1: qwen3-coder-next",
        "Read",
        "LLM call 2: qwen3-coder-next",
    ]
    root = subagent_spans[0]
    assert root["traceId"] == TRACE_ID
    assert root["parentSpanId"] == agent_tool["spanId"]
    assert {span["traceId"] for span in subagent_spans} == {TRACE_ID}
    assert all(span["parentSpanId"] for span in subagent_spans[1:])
    root_attrs = _attrs(root)
    assert root_attrs["subagent.async"] == "true"
    assert root_attrs["subagent.id"] == "agent-1"
    assert root_attrs["subagent.type"] == "synthetic-explorer"
    assert root_attrs["input.value"] == _attrs(agent_tool)["input.value"] or "Read hello.py" in root_attrs["input.value"]
    assert root_attrs["output.value"] == "Function: greeting; return: SYNTHETIC_TOOL_OK"
    assert root["startTimeUnixNano"] == "1767272401000000000"  # SubagentStart timestamp
    assert state.get(async_subagents.STATE_KEY) is None
    assert state.get("subagent_agent-1_start_time") is None
    assert state.get("subagent_agent-1_prompt") is None
    assert state.get("pending_subagents") is None


def test_async_subagent_stop_during_a_later_turn_still_attaches_to_the_original_turn(tmp_path: Path):
    state = _state(tmp_path)
    sent = []
    p1, p2, p3, p4 = _patches(state, sent)
    with p1, p2, p3, p4:
        _handle_subagent_start(_subagent_start())
        _handle_stop(_stop())
        # A new user turn started before the background agent finished.
        state.set("current_trace_id", "e" * 32)
        state.set("current_trace_span_id", "f" * 16)
        state.set("trace_count", "2")
        _handle_subagent_stop(_subagent_stop())

    assert len(sent) == 2
    assert {span["traceId"] for span in _spans(sent[1])} == {TRACE_ID}
    assert state.get("pending_subagents") is None
    assert state.get("current_trace_id") == "e" * 32


def test_async_subagent_without_transcript_exports_a_single_agent_span(tmp_path: Path):
    state = _state(tmp_path)
    sent = []
    p1, p2, p3, p4 = _patches(state, sent)
    with p1, p2, p3, p4:
        _handle_subagent_start(_subagent_start())
        _handle_stop(_stop())
        _handle_subagent_stop(_subagent_stop(agent_transcript_path=str(tmp_path / "missing.jsonl")))

    assert len(sent) == 2
    spans = _spans(sent[1])
    assert [span["name"] for span in spans] == ["Subagent: synthetic-explorer"]
    assert spans[0]["parentSpanId"] == next(span for span in _spans(sent[0]) if span["name"] == "Agent")["spanId"]


def test_foreground_subagent_keeps_the_buffered_path(tmp_path: Path):
    state = _state(tmp_path)
    sent = []
    p1, p2, p3, p4 = _patches(state, sent)
    with p1, p2, p3, p4:
        _handle_subagent_start(_subagent_start())
        _handle_subagent_stop(_subagent_stop())
        assert sent == []
        assert state.get("pending_subagents") is not None
        _handle_stop(_stop())

    assert len(sent) == 1
    assert "Subagent: synthetic-explorer" in [span["name"] for span in _spans(sent[0])]
    assert state.get(async_subagents.STATE_KEY) is None


def test_parents_are_not_recorded_when_the_turn_export_fails(tmp_path: Path):
    state = _state(tmp_path)
    with (
        mock.patch("tracing.claude_code.hooks.handlers.resolve_session", return_value=state),
        mock.patch("tracing.claude_code.hooks.handlers.resolve_transcript_path", return_value=MAIN_FIXTURE),
        mock.patch("tracing.claude_code.hooks.handlers.get_timestamp_ms", side_effect=itertools.count(1767272401000, 1000)),
        mock.patch("tracing.claude_code.hooks.handlers.send_span", return_value=False),
    ):
        _handle_subagent_start(_subagent_start())
        _handle_stop(_stop())
    assert state.get(async_subagents.STATE_KEY) is None
    assert state.get("current_trace_id") == TRACE_ID


def test_task_notification_turn_carries_trigger_attributes_in_both_export_paths(tmp_path: Path):
    for transcript in (MAIN_FIXTURE, None):
        state = _state(tmp_path / ("hf" if transcript else "legacy"), current_trace_prompt=TASK_NOTIFICATION)
        sent = []
        p1, p2, p3, p4 = _patches(state, sent, transcript=transcript)
        with p1, p2, p3, p4:
            _handle_stop(_stop())
        assert len(sent) == 1
        root = _spans(sent[0])[0]
        assert root["name"] == "Turn 1"
        attrs = _attrs(root)
        assert attrs["turn.trigger"] == "task-notification"
        assert attrs["subagent.id"] == "agent-1"


def test_task_notification_attrs_parses_only_notification_prompts():
    assert async_subagents.task_notification_attrs("fix the bug") == {}
    assert async_subagents.task_notification_attrs(None) == {}
    assert async_subagents.task_notification_attrs("  <task-notification><status>done</status></task-notification>") == {
        "turn.trigger": "task-notification"
    }
    assert async_subagents.task_notification_attrs(TASK_NOTIFICATION) == {
        "turn.trigger": "task-notification",
        "subagent.id": "agent-1",
    }


def test_pop_parent_removes_only_the_requested_agent(tmp_path: Path):
    state = _state(tmp_path)
    parents = {
        "agent-1": {"trace_id": TRACE_ID, "span_id": "1" * 16, "turn_id": "1", "prompt": "one"},
        "agent-2": {"trace_id": TRACE_ID, "span_id": "2" * 16, "turn_id": "1", "prompt": "two"},
    }
    assert async_subagents.record_parents(state, parents) is True
    assert async_subagents.pop_parent(state, "agent-1") == parents["agent-1"]
    assert async_subagents.decode_parents(state.get(async_subagents.STATE_KEY)) == {"agent-2": parents["agent-2"]}
    assert async_subagents.pop_parent(state, "agent-1") is None
    assert async_subagents.pop_parent(state, "") is None
    assert async_subagents.pop_parent(state, "agent-2") == parents["agent-2"]
    assert state.get(async_subagents.STATE_KEY) is None


def test_decode_parents_tolerates_malformed_state():
    assert async_subagents.decode_parents(None) == {}
    assert async_subagents.decode_parents("") == {}
    assert async_subagents.decode_parents("{not json") == {}
    assert async_subagents.decode_parents(json.dumps(["list"])) == {}
    assert async_subagents.decode_parents(json.dumps({"a": "not-a-dict", "b": {"span_id": "x"}})) == {"b": {"span_id": "x"}}


def test_reparent_root_returns_a_new_payload_and_leaves_the_input_untouched():
    payload = {
        "resourceSpans": [
            {
                "resource": {},
                "scopeSpans": [{"scope": {}, "spans": [{"spanId": "root"}, {"spanId": "child", "parentSpanId": "root"}]}],
            }
        ]
    }
    snapshot = json.dumps(payload, sort_keys=True)
    result = async_subagents.reparent_root(payload, "root", "agent-tool")
    assert json.dumps(payload, sort_keys=True) == snapshot
    spans = result["resourceSpans"][0]["scopeSpans"][0]["spans"]
    assert spans[0] == {"spanId": "root", "parentSpanId": "agent-tool"}
    assert spans[1] == {"spanId": "child", "parentSpanId": "root"}


def _assistant_line(text: str) -> str:
    return json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}})


def test_wait_returns_immediately_when_the_final_record_is_already_on_disk(tmp_path: Path):
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(_assistant_line("All   done\nhere.") + "\n")
    sleeps = []
    assert transcript_settle.wait_for_final_assistant(transcript, 0, "All done here.", timeout_ms=1500, sleep=sleeps.append)
    assert sleeps == []


def test_wait_ignores_records_before_the_turn_start_line(tmp_path: Path):
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(_assistant_line("Earlier turn answer") + "\n" + json.dumps({"type": "user"}) + "\n")
    clock = itertools.count(0, 0.1)
    assert (
        transcript_settle.wait_for_final_assistant(
            transcript, 1, "Earlier turn answer", timeout_ms=300, sleep=lambda _: None, clock=lambda: next(clock)
        )
        is False
    )


def test_wait_polls_until_the_record_appears(tmp_path: Path):
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(json.dumps({"type": "user", "message": {"content": "hi"}}) + "\n")
    sleeps = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 2:
            with transcript.open("a") as handle:
                handle.write(_assistant_line("The final answer is 42.") + "\n")

    assert transcript_settle.wait_for_final_assistant(transcript, 0, "The final answer is 42.", timeout_ms=1500, sleep=sleep)
    assert sleeps == [0.1, 0.1]


def test_wait_gives_up_at_the_deadline_and_never_blocks_with_zero_budget(tmp_path: Path):
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(json.dumps({"type": "user"}) + "\n")
    sleeps = []
    clock = itertools.count(0, 0.1)
    assert (
        transcript_settle.wait_for_final_assistant(
            transcript, 0, "never written", timeout_ms=250, sleep=sleeps.append, clock=lambda: next(clock)
        )
        is False
    )
    assert len(sleeps) == 2  # polled at t=0.1 and t=0.2, gave up once the clock passed 0.25
    assert transcript_settle.wait_for_final_assistant(transcript, 0, "never written", timeout_ms=0, sleep=sleeps.append) is False
    assert len(sleeps) == 2
    assert transcript_settle.wait_for_final_assistant(None, 0, "anything", timeout_ms=1500, sleep=sleeps.append) is True
    assert transcript_settle.wait_for_final_assistant(transcript, 0, "", timeout_ms=1500, sleep=sleeps.append) is True
    assert len(sleeps) == 2


def test_settle_timeout_reads_env_and_falls_back_on_garbage(monkeypatch):
    monkeypatch.delenv(transcript_settle.SETTLE_ENV, raising=False)
    assert transcript_settle.settle_timeout_ms() == transcript_settle.DEFAULT_SETTLE_MS
    monkeypatch.setenv(transcript_settle.SETTLE_ENV, "0")
    assert transcript_settle.settle_timeout_ms() == 0
    monkeypatch.setenv(transcript_settle.SETTLE_ENV, "250")
    assert transcript_settle.settle_timeout_ms() == 250
    monkeypatch.setenv(transcript_settle.SETTLE_ENV, "-5")
    assert transcript_settle.settle_timeout_ms() == transcript_settle.DEFAULT_SETTLE_MS
    monkeypatch.setenv(transcript_settle.SETTLE_ENV, "soon")
    assert transcript_settle.settle_timeout_ms() == transcript_settle.DEFAULT_SETTLE_MS


def test_stop_waits_for_the_transcript_but_the_orphan_fail_safe_does_not(tmp_path: Path):
    state = _state(tmp_path)
    sent = []
    p1, p2, p3, p4 = _patches(state, sent)
    waits = []
    with p1, p2, p3, p4, mock.patch(
        "tracing.claude_code.hooks.handlers.wait_for_final_assistant", side_effect=lambda *a, **k: waits.append(a) or True
    ):
        _handle_user_prompt_submit({"session_id": SESSION, "transcript_path": str(MAIN_FIXTURE), "prompt": "next"})
        assert len(sent) == 1  # orphaned Turn 1 exported by the fail-safe
        assert waits == []
        _handle_stop(_stop())
    assert len(sent) == 2
    assert len(waits) == 1
    assert waits[0][2] == "SUBAGENT_SCHEMA_OK"
