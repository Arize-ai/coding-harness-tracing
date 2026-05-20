"""Tests for tracing.codex.hooks.span_buffer."""

from __future__ import annotations

import json

import pytest

from tracing.codex.hooks import span_buffer


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    """Redirect STATE_DIR for span_buffer to a temp dir."""
    monkeypatch.setattr(span_buffer, "STATE_DIR", tmp_path)
    return tmp_path


# ---------------------------------------------------------------------------
# spans_path
# ---------------------------------------------------------------------------


def test_spans_path_returns_expected_location(state_dir):
    assert span_buffer.spans_path("abc") == state_dir / "spans_abc.jsonl"


# ---------------------------------------------------------------------------
# append_*
# ---------------------------------------------------------------------------


def test_append_tool_start_writes_single_line(state_dir):
    span_buffer.append_tool_start("t1", call_id="abc", tool="shell", args='{"cmd":"ls"}', ts_ms=1234)

    path = state_dir / "spans_t1.jsonl"
    content = path.read_text(encoding="utf-8")
    assert content.endswith("\n")
    lines = content.splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row == {
        "kind": "tool_start",
        "call_id": "abc",
        "tool": "shell",
        "args": '{"cmd":"ls"}',
        "ts_ms": 1234,
    }


def test_two_appends_produce_two_lines_in_order(state_dir):
    span_buffer.append_tool_start("t1", call_id="abc", tool="shell", args="{}", ts_ms=100)
    span_buffer.append_tool_end("t1", call_id="abc", output="ok", ts_ms=200)

    path = state_dir / "spans_t1.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["kind"] == "tool_start"
    assert json.loads(lines[1])["kind"] == "tool_end"


def test_append_creates_state_dir_if_missing(tmp_path, monkeypatch):
    nested = tmp_path / "nested" / "state"
    monkeypatch.setattr(span_buffer, "STATE_DIR", nested)
    assert not nested.exists()

    span_buffer.append_tool_start("t1", call_id="abc", tool="shell", args="{}", ts_ms=1)
    assert nested.exists()
    assert (nested / "spans_t1.jsonl").exists()


# ---------------------------------------------------------------------------
# read_all
# ---------------------------------------------------------------------------


def test_read_all_returns_rows_in_order(state_dir):
    span_buffer.append_tool_start("t1", call_id="a", tool="shell", args="{}", ts_ms=1)
    span_buffer.append_permission("t1", call_id="a", decision="allow", ts_ms=2)
    span_buffer.append_tool_end("t1", call_id="a", output="done", ts_ms=3)

    rows = span_buffer.read_all("t1")
    assert [r["kind"] for r in rows] == ["tool_start", "permission", "tool_end"]
    assert [r["ts_ms"] for r in rows] == [1, 2, 3]


def test_read_all_returns_empty_when_missing(state_dir):
    assert span_buffer.read_all("nonexistent") == []


def test_read_all_skips_malformed_line(state_dir):
    path = state_dir / "spans_t1.jsonl"
    path.write_text(
        '{"kind":"tool_start","call_id":"a","ts_ms":1}\n'
        "this is not json\n"
        '{"kind":"tool_end","call_id":"a","ts_ms":3}\n',
        encoding="utf-8",
    )

    rows = span_buffer.read_all("t1")
    assert len(rows) == 2
    assert rows[0]["kind"] == "tool_start"
    assert rows[1]["kind"] == "tool_end"


def test_read_all_skips_blank_lines(state_dir):
    path = state_dir / "spans_t1.jsonl"
    path.write_text(
        '{"kind":"tool_start","call_id":"a","ts_ms":1}\n' "\n" "   \n" '{"kind":"tool_end","call_id":"a","ts_ms":3}\n',
        encoding="utf-8",
    )

    rows = span_buffer.read_all("t1")
    assert len(rows) == 2


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


def test_delete_removes_file(state_dir):
    span_buffer.append_tool_start("t1", call_id="a", tool="shell", args="{}", ts_ms=1)
    path = state_dir / "spans_t1.jsonl"
    assert path.exists()

    span_buffer.delete("t1")
    assert not path.exists()


def test_delete_is_idempotent(state_dir):
    span_buffer.delete("never-existed")
    span_buffer.delete("never-existed")


# ---------------------------------------------------------------------------
# join_by_call_id
# ---------------------------------------------------------------------------


def test_join_pairs_tool_start_and_tool_end():
    rows = [
        {
            "kind": "tool_start",
            "call_id": "A",
            "tool": "shell",
            "args": "{}",
            "ts_ms": 100,
        },
        {"kind": "tool_end", "call_id": "A", "output": "ok", "ts_ms": 200},
    ]
    result = span_buffer.join_by_call_id(rows)
    assert result == [
        {
            "call_id": "A",
            "tool": "shell",
            "args": "{}",
            "output": "ok",
            "decision": None,
            "start_ts_ms": 100,
            "end_ts_ms": 200,
        }
    ]


def test_join_attaches_permission_decision():
    rows = [
        {
            "kind": "tool_start",
            "call_id": "A",
            "tool": "shell",
            "args": "{}",
            "ts_ms": 100,
        },
        {"kind": "permission", "call_id": "A", "decision": "allow", "ts_ms": 150},
        {"kind": "tool_end", "call_id": "A", "output": "ok", "ts_ms": 200},
    ]
    result = span_buffer.join_by_call_id(rows)
    assert len(result) == 1
    assert result[0]["decision"] == "allow"


def test_join_returns_entries_in_first_appearance_order():
    rows = [
        {"kind": "tool_start", "call_id": "A", "tool": "shell", "args": "{}", "ts_ms": 1},
        {"kind": "tool_start", "call_id": "B", "tool": "edit", "args": "{}", "ts_ms": 2},
        {"kind": "tool_end", "call_id": "B", "output": "b-done", "ts_ms": 3},
        {"kind": "tool_end", "call_id": "A", "output": "a-done", "ts_ms": 4},
    ]
    result = span_buffer.join_by_call_id(rows)
    assert [e["call_id"] for e in result] == ["A", "B"]
    by_id = {e["call_id"]: e for e in result}
    assert by_id["A"]["output"] == "a-done"
    assert by_id["A"]["end_ts_ms"] == 4
    assert by_id["B"]["output"] == "b-done"
    assert by_id["B"]["end_ts_ms"] == 3


def test_join_last_permission_wins():
    rows = [
        {"kind": "permission", "call_id": "A", "decision": "allow", "ts_ms": 1},
        {"kind": "permission", "call_id": "A", "decision": "deny", "ts_ms": 2},
    ]
    result = span_buffer.join_by_call_id(rows)
    assert len(result) == 1
    assert result[0]["decision"] == "deny"


def test_join_skips_row_without_call_id():
    rows = [
        {"kind": "tool_start", "call_id": "A", "tool": "shell", "args": "{}", "ts_ms": 1},
        {"kind": "tool_end", "output": "orphan", "ts_ms": 2},  # missing call_id
        {"kind": "tool_end", "call_id": "A", "output": "ok", "ts_ms": 3},
    ]
    result = span_buffer.join_by_call_id(rows)
    assert len(result) == 1
    assert result[0]["call_id"] == "A"
    assert result[0]["output"] == "ok"
