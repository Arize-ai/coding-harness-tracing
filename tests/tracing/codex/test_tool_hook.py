"""Tests for tracing.codex.hooks.tool — PreToolUse/PostToolUse/PermissionRequest handler."""

from __future__ import annotations

import io
import json

import pytest

from tracing.codex.hooks import span_buffer, tool


@pytest.fixture(autouse=True)
def _enable_logging(monkeypatch):
    """Default tests run with redaction off so we can assert on raw content."""
    monkeypatch.setenv("ARIZE_LOG_PROMPTS", "true")
    monkeypatch.setenv("ARIZE_LOG_TOOL_DETAILS", "true")
    monkeypatch.setenv("ARIZE_LOG_TOOL_CONTENT", "true")
    monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    """Point span_buffer + adapter STATE_DIR at a temp dir."""
    import tracing.codex.hooks.adapter as adapter

    monkeypatch.setattr(span_buffer, "STATE_DIR", tmp_path)
    monkeypatch.setattr(adapter, "STATE_DIR", tmp_path)
    return tmp_path


def _set_stdin(monkeypatch, payload):
    """Replace sys.stdin with a StringIO containing the given payload."""
    if isinstance(payload, (dict, list)):
        text = json.dumps(payload)
    else:
        text = str(payload)
    monkeypatch.setattr("sys.stdin", io.StringIO(text))


def _read_jsonl(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# PreToolUse
# ---------------------------------------------------------------------------


class TestPreToolUse:
    def test_dict_tool_input_is_json_serialized(self, state_dir, monkeypatch):
        _set_stdin(
            monkeypatch,
            {
                "hook_event_name": "PreToolUse",
                "session_id": "t1",
                "tool_call_id": "c1",
                "tool_name": "shell",
                "tool_input": {"cmd": "ls"},
            },
        )
        tool.main()
        rows = _read_jsonl(state_dir / "spans_t1.jsonl")
        assert len(rows) == 1
        row = rows[0]
        assert row["kind"] == "tool_start"
        assert row["call_id"] == "c1"
        assert row["tool"] == "shell"
        assert row["args"] == '{"cmd":"ls"}'
        assert "ts_ms" in row

    def test_string_tool_input_passes_through(self, state_dir, monkeypatch):
        _set_stdin(
            monkeypatch,
            {
                "hook_event_name": "PreToolUse",
                "session_id": "t1",
                "tool_call_id": "c1",
                "tool_name": "shell",
                "tool_input": "raw string",
            },
        )
        tool.main()
        rows = _read_jsonl(state_dir / "spans_t1.jsonl")
        assert rows[0]["args"] == "raw string"

    def test_empty_call_id_still_appends(self, state_dir, monkeypatch):
        _set_stdin(
            monkeypatch,
            {
                "hook_event_name": "PreToolUse",
                "session_id": "t1",
                "tool_name": "shell",
                "tool_input": {"cmd": "ls"},
            },
        )
        tool.main()
        rows = _read_jsonl(state_dir / "spans_t1.jsonl")
        assert len(rows) == 1
        assert rows[0]["call_id"] == ""

    def test_missing_tool_name_falls_back(self, state_dir, monkeypatch):
        _set_stdin(
            monkeypatch,
            {
                "hook_event_name": "PreToolUse",
                "session_id": "t1",
                "tool_call_id": "c1",
            },
        )
        tool.main()
        rows = _read_jsonl(state_dir / "spans_t1.jsonl")
        assert rows[0]["tool"] == "unknown_tool"


# ---------------------------------------------------------------------------
# PostToolUse
# ---------------------------------------------------------------------------


class TestPostToolUse:
    def test_string_output(self, state_dir, monkeypatch):
        _set_stdin(
            monkeypatch,
            {
                "hook_event_name": "PostToolUse",
                "session_id": "t1",
                "tool_call_id": "c1",
                "tool_output": "hello",
            },
        )
        tool.main()
        rows = _read_jsonl(state_dir / "spans_t1.jsonl")
        assert len(rows) == 1
        row = rows[0]
        assert row["kind"] == "tool_end"
        assert row["call_id"] == "c1"
        assert row["output"] == "hello"

    def test_dict_output_is_json_serialized(self, state_dir, monkeypatch):
        _set_stdin(
            monkeypatch,
            {
                "hook_event_name": "PostToolUse",
                "session_id": "t1",
                "tool_call_id": "c1",
                "tool_output": {"result": "ok"},
            },
        )
        tool.main()
        rows = _read_jsonl(state_dir / "spans_t1.jsonl")
        assert rows[0]["output"] == '{"result":"ok"}'

    def test_tool_result_alias(self, state_dir, monkeypatch):
        _set_stdin(
            monkeypatch,
            {
                "hook_event_name": "PostToolUse",
                "session_id": "t1",
                "tool_call_id": "c1",
                "tool_result": "fallback",
            },
        )
        tool.main()
        rows = _read_jsonl(state_dir / "spans_t1.jsonl")
        assert rows[0]["output"] == "fallback"


# ---------------------------------------------------------------------------
# PermissionRequest
# ---------------------------------------------------------------------------


class TestPermissionRequest:
    def test_decision_allow(self, state_dir, monkeypatch):
        _set_stdin(
            monkeypatch,
            {
                "hook_event_name": "PermissionRequest",
                "session_id": "t1",
                "tool_call_id": "c1",
                "permission_decision": "allow",
            },
        )
        tool.main()
        rows = _read_jsonl(state_dir / "spans_t1.jsonl")
        assert len(rows) == 1
        row = rows[0]
        assert row["kind"] == "permission"
        assert row["call_id"] == "c1"
        assert row["decision"] == "allow"

    def test_decision_default_unknown(self, state_dir, monkeypatch):
        _set_stdin(
            monkeypatch,
            {
                "hook_event_name": "PermissionRequest",
                "session_id": "t1",
                "tool_call_id": "c1",
            },
        )
        tool.main()
        rows = _read_jsonl(state_dir / "spans_t1.jsonl")
        assert rows[0]["decision"] == "unknown"


# ---------------------------------------------------------------------------
# Sequencing / dispatch
# ---------------------------------------------------------------------------


class TestSequencing:
    def test_pre_permission_post_appended_in_order(self, state_dir, monkeypatch):
        for payload in [
            {
                "hook_event_name": "PreToolUse",
                "session_id": "t1",
                "tool_call_id": "c1",
                "tool_name": "shell",
                "tool_input": {"cmd": "ls"},
            },
            {
                "hook_event_name": "PermissionRequest",
                "session_id": "t1",
                "tool_call_id": "c1",
                "permission_decision": "allow",
            },
            {
                "hook_event_name": "PostToolUse",
                "session_id": "t1",
                "tool_call_id": "c1",
                "tool_output": "done",
            },
        ]:
            _set_stdin(monkeypatch, payload)
            tool.main()

        rows = _read_jsonl(state_dir / "spans_t1.jsonl")
        assert [r["kind"] for r in rows] == ["tool_start", "permission", "tool_end"]


# ---------------------------------------------------------------------------
# Negative paths
# ---------------------------------------------------------------------------


class TestNegativePaths:
    def test_missing_session_id_no_file_written(self, state_dir, monkeypatch):
        _set_stdin(
            monkeypatch,
            {
                "hook_event_name": "PreToolUse",
                "tool_call_id": "c1",
                "tool_name": "shell",
            },
        )
        tool.main()
        assert not any(state_dir.glob("spans_*.jsonl"))

    def test_unknown_event_ignored(self, state_dir, monkeypatch):
        _set_stdin(
            monkeypatch,
            {
                "hook_event_name": "SomethingElse",
                "session_id": "t1",
            },
        )
        tool.main()
        assert not any(state_dir.glob("spans_*.jsonl"))


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------


class TestRedaction:
    def test_args_redacted_when_tool_details_disabled(self, state_dir, monkeypatch):
        monkeypatch.setenv("ARIZE_LOG_TOOL_DETAILS", "false")
        _set_stdin(
            monkeypatch,
            {
                "hook_event_name": "PreToolUse",
                "session_id": "t1",
                "tool_call_id": "c1",
                "tool_name": "shell",
                "tool_input": {"cmd": "ls"},
            },
        )
        tool.main()
        rows = _read_jsonl(state_dir / "spans_t1.jsonl")
        assert rows[0]["args"].startswith("<redacted")
        assert '{"cmd":"ls"}' not in rows[0]["args"]
