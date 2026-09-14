"""Turn context across normal, legacy, failed, and fail-safe exports."""

import json
from pathlib import Path
from unittest import mock

import pytest

from tracing.claude_code.hooks import adapter, handlers
from tracing.claude_code.hooks.turn_context import DENIED_COUNT_KEY

FIXTURE = Path(__file__).parent / "fixtures" / "main_tool_cycle.jsonl"


def _spans(payload):
    return payload["resourceSpans"][0]["scopeSpans"][0]["spans"]


def _attrs(span):
    return {item["key"]: next(iter(item["value"].values())) for item in span["attributes"]}


@pytest.fixture
def session(tmp_path, monkeypatch):
    monkeypatch.setattr(adapter, "STATE_DIR", tmp_path / "state")
    monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT", raising=False)
    monkeypatch.setenv("ARIZE_LOG_PROMPTS", "true")
    monkeypatch.setenv("ARIZE_LOG_TOOL_DETAILS", "true")
    # No fallback lookup in the developer's Claude directory.
    monkeypatch.setattr(
        handlers,
        "resolve_transcript_path",
        lambda payload, _: Path(payload["transcript_path"]) if payload.get("transcript_path") else None,
    )
    sent = []
    monkeypatch.setattr(handlers, "send_span", lambda payload: sent.append(payload))
    payload = {"session_id": "context-session", "cwd": "/initial/project", "permission_mode": "default"}
    handlers._handle_user_prompt_submit({**payload, "prompt": "first prompt"})
    return adapter.resolve_session(payload), sent


def _export(kind, payload):
    if kind == "failure":
        handlers._handle_stop_failure({**payload, "error": "api_error"})
    elif kind == "failsafe":
        handlers._handle_user_prompt_submit({**payload, "prompt": "next prompt"})
    else:
        handlers._handle_stop(payload)


@pytest.mark.parametrize("kind", ["modern", "legacy", "failure", "failsafe"])
@pytest.mark.parametrize("logging", [True, False])
def test_context_on_every_turn_export(session, monkeypatch, kind, logging):
    state, sent = session
    monkeypatch.setenv("ARIZE_LOG_TOOL_DETAILS", str(logging).lower())
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "sdk-cli")
    common = {"session_id": "context-session"}
    handlers._handle_permission_denied(common)
    handlers._handle_permission_denied(common)
    sent.clear()
    payload = {**common, "cwd": "/export/project", "permission_mode": "auto"}
    if kind in {"modern", "failsafe"}:
        payload["transcript_path"] = str(FIXTURE)
    _export(kind, payload)

    root = _spans(sent[0])[0]
    attrs = _attrs(root)
    assert root["name"].startswith("Turn 1")
    assert attrs["session.cwd"] == ("/export/project" if logging else "<redacted (15 chars)>")
    assert attrs["permission.mode"] == "auto"
    assert attrs["session.entrypoint"] == "sdk-cli"
    assert attrs["permission.denied_count"] == 2
    assert next(item["value"] for item in root["attributes"] if item["key"] == "permission.denied_count") == {
        "intValue": 2
    }
    assert state.get(DENIED_COUNT_KEY) == "0"
    if kind in {"modern", "failsafe"}:
        assert attrs["claude_code.version"] == "2.1.209"
        assert attrs["openinference.span.kind"] == "CHAIN"
        for child in _spans(sent[0])[1:]:
            assert "permission.denied_count" not in _attrs(child)


@pytest.mark.parametrize("mode", ["default", "acceptEdits", "plan", "bypassPermissions", "auto"])
def test_latest_mode_from_an_intermediate_hook_survives_missing_stop_fields(session, mode):
    _, sent = session
    handlers._handle_pre_tool_use({"session_id": "context-session", "permission_mode": mode, "tool_use_id": "tool"})
    handlers._handle_stop({"session_id": "context-session"})
    attrs = _attrs(_spans(sent[-1])[0])
    assert attrs["session.cwd"] == "/initial/project"
    assert attrs["permission.mode"] == mode
    assert attrs["permission.denied_count"] == 0
    assert "session.entrypoint" not in attrs
    assert "claude_code.version" not in attrs
    assert "git.branch" not in attrs


def test_empty_optional_context_is_omitted(session):
    state, sent = session
    state.delete("session_cwd")
    state.delete("permission_mode")
    handlers._handle_stop({"session_id": "context-session", "cwd": "", "permission_mode": ""})
    attrs = _attrs(_spans(sent[0])[0])
    assert attrs["permission.denied_count"] == 0
    assert (
        not {"session.cwd", "permission.mode", "session.entrypoint", "claude_code.version", "git.branch"} & attrs.keys()
    )


@pytest.mark.parametrize("stable_ids", [True, False])
@pytest.mark.parametrize("entrypoint", ["", "cli", "sdk-cli"])
def test_transcript_metadata_uses_existing_scan(session, tmp_path, monkeypatch, stable_ids, entrypoint):
    _, sent = session
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", entrypoint)
    records = [
        {"version": "2.1.270", "gitBranch": "topic", "entrypoint": "sdk-cli"},
        {
            "version": "later-version",
            "gitBranch": "later-branch",
            "message": {
                "role": "assistant",
                "model": "claude-test",
                "content": "done",
                "usage": {"input_tokens": 10, "output_tokens": 2},
            },
        },
    ]
    if stable_ids:
        records[1]["uuid"] = "assistant-1"
    transcript = tmp_path / "metadata.jsonl"
    transcript.write_text("null\ninvalid json\n" + "\n".join(json.dumps(record) for record in records) + "\n")
    with mock.patch.object(handlers, "_scan_transcript_for_usage", wraps=handlers._scan_transcript_for_usage) as scan:
        handlers._handle_stop({"session_id": "context-session", "transcript_path": str(transcript)})
    assert scan.call_count == 1
    attrs = _attrs(_spans(sent[0])[0])
    assert attrs["claude_code.version"] == "2.1.270"
    assert attrs["git.branch"] == "topic"
    assert attrs["session.entrypoint"] == (entrypoint or "sdk-cli")


@pytest.mark.parametrize("kind", ["modern", "legacy", "failure", "failsafe"])
def test_failed_export_retains_denial_count_for_retry(session, monkeypatch, kind):
    state, sent = session
    payload = {"session_id": "context-session"}
    if kind in {"modern", "failsafe"}:
        payload["transcript_path"] = str(FIXTURE)
    handlers._handle_permission_denied(payload)
    sent.clear()
    trace_id = state.get("current_trace_id")
    with mock.patch.object(handlers, "send_span", return_value=False):
        _export(kind, payload)
    assert state.get(DENIED_COUNT_KEY) == "1"
    assert state.get("current_trace_id") == trace_id
    _export(kind, payload)
    assert _attrs(_spans(sent[0])[0])["permission.denied_count"] == 1
    assert state.get(DENIED_COUNT_KEY) == "0"


def test_new_turn_resets_denials_and_denials_outside_turn_are_not_counted(session):
    state, sent = session
    handlers._handle_stop({"session_id": "context-session"})
    handlers._handle_permission_denied({"session_id": "context-session"})
    assert state.get(DENIED_COUNT_KEY) == "0"
    state.set(DENIED_COUNT_KEY, "9")
    handlers._handle_user_prompt_submit({"session_id": "context-session", "prompt": "next"})
    handlers._handle_permission_denied({"session_id": "context-session"})
    handlers._handle_stop({"session_id": "context-session"})
    assert _attrs(_spans(sent[-1])[0])["permission.denied_count"] == 1


def test_acknowledging_an_old_turn_preserves_new_turn_denials(session):
    state, _ = session
    state.set(DENIED_COUNT_KEY, "3")
    assert handlers._acknowledge_exported_turn(state, "different-trace", [], {}) is False
    assert state.get(DENIED_COUNT_KEY) == "3"


def test_failsafe_preserves_previous_range_model_usage_and_tool_tree(session, tmp_path):
    state, sent = session
    # An earlier turn must not contribute usage or model calls to this turn.
    transcript = tmp_path / "turns.jsonl"
    old = {
        "uuid": "old",
        "message": {"role": "assistant", "model": "old-model", "content": "old", "usage": {"input_tokens": 9999}},
    }
    transcript.write_text(json.dumps(old) + "\n" + FIXTURE.read_text())
    state.set("trace_start_line", "1")
    previous_trace = state.get("current_trace_id")
    handlers._handle_user_prompt_submit(
        {"session_id": "context-session", "transcript_path": str(transcript), "prompt": "next prompt"}
    )

    spans = _spans(sent[0])
    root = spans[0]
    assert root["traceId"] == previous_trace
    assert _attrs(root)["input.value"] == "first prompt"
    assert _attrs(root)["output.value"] == handlers.FAILSAFE_STOP_MESSAGE
    assert _attrs(root)["openinference.span.kind"] == "CHAIN"
    models = [span for span in spans if _attrs(span)["openinference.span.kind"] == "LLM"]
    assert len(models) == 3
    assert {_attrs(span)["llm.model_name"] for span in models} == {"qwen3-coder-next"}
    assert sum(_attrs(span)["llm.token_count.prompt"] for span in models) == 372
    assert sum(_attrs(span)["llm.token_count.completion"] for span in models) == 50
    assert [span["name"] for span in spans if _attrs(span)["openinference.span.kind"] == "TOOL"] == ["Read", "Bash"]
    assert all(span["parentSpanId"] == root["spanId"] for span in models)
    assert state.get("trace_start_line") == "7"
    assert state.get("current_trace_prompt") == "next prompt"
    assert state.get("current_trace_id") != previous_trace
