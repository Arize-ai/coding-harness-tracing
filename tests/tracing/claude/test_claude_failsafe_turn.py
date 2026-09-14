"""Fail-safe closed turns: an empty turn is a CHAIN span, never an LLM span with a blank model."""

import json
from pathlib import Path

import pytest

from tracing.claude_code.hooks import adapter, handlers

FIXTURE = Path(__file__).parent / "fixtures" / "main_tool_cycle.jsonl"
SESSION = "failsafe-session"


def _spans(payload):
    return payload["resourceSpans"][0]["scopeSpans"][0]["spans"]


def _attrs(span):
    return {item["key"]: next(iter(item["value"].values())) for item in span["attributes"]}


@pytest.fixture
def session(tmp_path, monkeypatch):
    monkeypatch.setattr(adapter, "STATE_DIR", tmp_path / "state")
    monkeypatch.setenv("ARIZE_LOG_PROMPTS", "true")
    monkeypatch.setattr(
        handlers,
        "resolve_transcript_path",
        lambda payload, _: Path(payload["transcript_path"]) if payload.get("transcript_path") else None,
    )
    sent = []
    monkeypatch.setattr(handlers, "send_span", lambda payload: sent.append(payload))
    handlers._handle_user_prompt_submit({"session_id": SESSION, "prompt": "first prompt"})
    return sent


def _submit_next_prompt(transcript_path=None):
    payload = {"session_id": SESSION, "prompt": "next prompt"}
    if transcript_path is not None:
        payload["transcript_path"] = str(transcript_path)
    handlers._handle_user_prompt_submit(payload)


def test_empty_failsafe_turn_is_a_chain_span_without_llm_attributes(session, tmp_path):
    """A re-queued prompt or a task-notification burst leaves no assistant record behind."""
    transcript = tmp_path / "empty.jsonl"
    transcript.write_text(json.dumps({"type": "user", "message": {"role": "user", "content": "first prompt"}}) + "\n")
    _submit_next_prompt(transcript)

    assert len(session) == 1
    (root,) = _spans(session[0])
    attrs = _attrs(root)
    assert root["name"] == "Turn 1"
    assert attrs["openinference.span.kind"] == "CHAIN"
    assert attrs["turn.closed_by"] == "fail-safe"
    assert attrs["input.value"] == "first prompt"
    assert attrs["output.value"] == handlers.FAILSAFE_STOP_MESSAGE
    assert not [key for key in attrs if key.startswith("llm.")]


def test_empty_failsafe_turn_without_a_transcript(session):
    _submit_next_prompt()
    attrs = _attrs(_spans(session[0])[0])
    assert attrs["openinference.span.kind"] == "CHAIN"
    assert attrs["turn.closed_by"] == "fail-safe"
    assert "llm.model_name" not in attrs


def test_failsafe_turn_with_model_calls_keeps_llm_children_and_is_tagged(session):
    _submit_next_prompt(FIXTURE)
    spans = _spans(session[0])
    root = spans[0]
    assert _attrs(root)["openinference.span.kind"] == "CHAIN"
    assert _attrs(root)["turn.closed_by"] == "fail-safe"
    assert [span for span in spans if _attrs(span)["openinference.span.kind"] == "LLM"]
    assert all("turn.closed_by" not in _attrs(child) for child in spans[1:])


def test_regular_stop_without_a_transcript_stays_an_llm_span(session):
    handlers._handle_stop({"session_id": SESSION, "last_assistant_message": "done"})
    attrs = _attrs(_spans(session[0])[0])
    assert attrs["openinference.span.kind"] == "LLM"
    assert attrs["llm.model_name"] == ""
    assert attrs["output.value"] == "done"
    assert "turn.closed_by" not in attrs
