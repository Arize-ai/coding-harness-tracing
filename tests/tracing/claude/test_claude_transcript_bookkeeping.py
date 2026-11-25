"""Newer Claude Code transcripts mix bookkeeping records in with the conversation."""

import json
from pathlib import Path

from core.event_model import EventStatus, ModelCallEvent, ToolEvent, TurnEvent
from tracing.claude_code.hooks.handlers import _has_stable_model_ids
from tracing.claude_code.hooks.transcript import parse_claude_transcript


def _root() -> TurnEvent:
    return TurnEvent(
        event_id="turn-1",
        session_id="test-session",
        turn_id="turn-1",
        sequence=0,
        started_at_ms=1_767_268_800_000,
        ended_at_ms=None,
        status=EventStatus.RUNNING,
        input="prompt",
    )


def test_api_request_blob_records_are_not_model_calls(tmp_path: Path):
    """api-request-blob records copy messages without a uuid. They must not become
    assistant-line-* model events, or the whole turn falls back to a root-only trace."""
    tool_use = {"type": "tool_use", "id": "toolu_1", "name": "Read", "input": {"file_path": "/x/openapi.yaml"}}
    records = [
        {
            "type": "user",
            "uuid": "u-1",
            "timestamp": "2026-01-01T12:00:00.000Z",
            "message": {"role": "user", "content": "prompt"},
        },
        {"type": "api-request-blob", "message": {"role": "system", "content": "system prompt"}},
        {"type": "api-request-blob", "message": {"role": "user", "content": "prompt"}},
        {
            "type": "assistant",
            "uuid": "a-1",
            "timestamp": "2026-01-01T12:00:01.000Z",
            "message": {
                "id": "msg_1",
                "role": "assistant",
                "model": "claude-test",
                "content": [tool_use],
                "usage": {"input_tokens": 10, "output_tokens": 5},
            },
        },
        {"type": "api-request-blob", "message": {"role": "assistant", "content": [tool_use]}},
        {
            "type": "user",
            "uuid": "u-2",
            "timestamp": "2026-01-01T12:00:02.000Z",
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "spec"}],
            },
        },
        {
            "type": "assistant",
            "uuid": "a-2",
            "timestamp": "2026-01-01T12:00:03.000Z",
            "message": {
                "id": "msg_2",
                "role": "assistant",
                "model": "claude-test",
                "content": [{"type": "text", "text": "Done"}],
                "usage": {"input_tokens": 12, "output_tokens": 3},
            },
        },
    ]
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")

    graph = parse_claude_transcript(transcript, _root())

    models = [e for e in graph.events if isinstance(e, ModelCallEvent)]
    tools = [e for e in graph.events if isinstance(e, ToolEvent)]
    assert [m.event_id for m in models] == ["a-1", "a-2"]
    assert [t.tool_call_id for t in tools] == ["toolu_1"]
    assert _has_stable_model_ids(graph)
