"""Tests for tracing.devin.hooks.handlers — the Stop/SessionEnd span emitter.

Covers:
- _token_attrs: OpenInference token convention incl. cache read/write subsets
- emit_interaction: span tree shape (AGENT root + LLM steps + TOOL calls),
  trace/parent wiring, token totals, the reasoning->output validity fix
- flush_session: DB-sourced emission, request_id dedup / watermark
- main(): dispatch on Stop and SessionEnd, foreign events ignored, malformed
  stdin, trace-disabled — all fail-soft returning 0
"""

from __future__ import annotations

import json
import sqlite3
import sys
from io import StringIO
from typing import Any
from unittest import mock

import pytest

from core.common import env
from tracing.devin.hooks import adapter, handlers
from tracing.devin.session_db import LlmStep, ToolCall

# ---------------------------------------------------------------------------
# OTLP helpers
# ---------------------------------------------------------------------------


def _span_obj(span_dict: dict) -> dict:
    return span_dict["resourceSpans"][0]["scopeSpans"][0]["spans"][0]


def _span_attrs(span_dict: dict) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for a in _span_obj(span_dict).get("attributes", []):
        for v in a["value"].values():
            out[a["key"]] = v
            break
    return out


def _kind(span_dict: dict) -> str:
    return _span_attrs(span_dict).get("openinference.span.kind", "")


def _step(
    request_id,
    content="",
    thinking="",
    model="swe-1.6",
    prompt=0,
    completion=0,
    cache_read=0,
    cache_write=0,
    tools=None,
    node_id=0,
    start=0,
    end=0,
):
    return LlmStep(
        request_id=request_id,
        content=content,
        thinking=thinking,
        model_name=model,
        prompt_tokens=prompt,
        completion_tokens=completion,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
        tool_calls=tools or [],
        node_id=node_id,
        start_ms=start,
        end_ms=end,
    )


def _build_db(path, session_rows, node_rows):
    con = sqlite3.connect(str(path))
    try:
        con.execute(
            "CREATE TABLE sessions (id TEXT, working_directory TEXT, backend_type TEXT, "
            "model TEXT, last_activity_at INTEGER, main_chain_id INTEGER)"
        )
        con.executemany(
            "INSERT INTO sessions (id, working_directory, backend_type, model, last_activity_at, main_chain_id) "
            "VALUES (?,?,?,?,?,?)",
            session_rows,
        )
        con.execute(
            "CREATE TABLE message_nodes (row_id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, "
            "node_id INTEGER, parent_node_id INTEGER, chat_message TEXT, created_at INTEGER, metadata TEXT)"
        )
        con.executemany(
            "INSERT INTO message_nodes (session_id, node_id, parent_node_id, chat_message, created_at) VALUES (?,?,?,?,?)",
            [(sid, nid, None, json.dumps(msg), 0) for sid, nid, msg in node_rows],
        )
        con.execute(
            "CREATE TABLE prompt_history (id INTEGER PRIMARY KEY AUTOINCREMENT, content TEXT, "
            "timestamp INTEGER, session_id TEXT, is_shell INTEGER DEFAULT 0)"
        )
        con.commit()
    finally:
        con.close()


def _asst(request_id, content="", thinking="", inp=0, out=0, tools=None):
    return {
        "role": "assistant",
        "content": content,
        "thinking": thinking,
        "tool_calls": tools or [],
        "metadata": {
            "request_id": request_id,
            "generation_model": "swe-1.6",
            "started_generation_at": None,
            "created_at": None,
            "metrics": {"input_tokens": inp, "output_tokens": out, "cache_read_tokens": 0, "cache_creation_tokens": 0},
        },
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
    monkeypatch.setenv("ARIZE_LOG_PROMPTS", "true")
    monkeypatch.setenv("ARIZE_LOG_TOOL_CONTENT", "true")
    monkeypatch.delenv("ARIZE_PROJECT_NAME", raising=False)
    monkeypatch.delenv("ARIZE_USER_ID", raising=False)
    monkeypatch.delenv("DEVIN_PROJECT_DIR", raising=False)
    env.invalidate_caches()


@pytest.fixture
def captured_spans():
    sent: list[dict] = []
    with mock.patch.object(handlers, "send_span", side_effect=lambda s: sent.append(s)):
        yield sent


# ===========================================================================
# _token_attrs
# ===========================================================================


class TestTokenAttrs:
    def test_full_with_cache_read_and_write(self):
        attrs = handlers._token_attrs(10, 5, 3, 2)
        assert attrs["llm.token_count.prompt"] == 10
        assert attrs["llm.token_count.completion"] == 5
        assert attrs["llm.token_count.total"] == 15
        assert attrs["llm.token_count.prompt_details.cache_read"] == 3
        assert attrs["llm.token_count.prompt_details.cache_write"] == 2

    def test_all_zero_yields_empty(self):
        assert handlers._token_attrs(0, 0, 0, 0) == {}

    def test_cache_defaults_omitted(self):
        attrs = handlers._token_attrs(10, 5)
        assert attrs["llm.token_count.total"] == 15
        assert "llm.token_count.prompt_details.cache_read" not in attrs
        assert "llm.token_count.prompt_details.cache_write" not in attrs


# ===========================================================================
# emit_interaction
# ===========================================================================


class TestEmitInteraction:
    def _emit(self, captured, steps, prompt="do a thing", meta=None):
        handlers.emit_interaction("sess-1", steps, prompt, meta or {"backend": "Windsurf", "model": "SWE-1.6"})
        return captured

    def test_span_counts_and_kinds(self, captured_spans):
        steps = [
            _step("A", content="hi", prompt=100, completion=10),
            _step(
                "B", content="done", prompt=50, completion=5, tools=[ToolCall("t1", "web_search", {"q": "x"}, "result")]
            ),
        ]
        self._emit(captured_spans, steps)
        kinds = [_kind(s) for s in captured_spans]
        assert kinds.count("AGENT") == 1
        assert kinds.count("LLM") == 2
        assert kinds.count("TOOL") == 1

    def test_single_trace_root_no_parent(self, captured_spans):
        self._emit(captured_spans, [_step("A", content="hi", prompt=1, completion=1)])
        trace_ids = {_span_obj(s)["traceId"] for s in captured_spans}
        assert len(trace_ids) == 1
        root = next(s for s in captured_spans if _kind(s) == "AGENT")
        assert _span_obj(root).get("parentSpanId", "") == ""

    def test_llm_parented_to_root_and_tool_to_llm(self, captured_spans):
        steps = [_step("A", content="c", prompt=1, completion=1, tools=[ToolCall("t", "grep", {}, "")])]
        self._emit(captured_spans, steps)
        root_id = _span_obj(next(s for s in captured_spans if _kind(s) == "AGENT"))["spanId"]
        llm = next(s for s in captured_spans if _kind(s) == "LLM")
        assert _span_obj(llm)["parentSpanId"] == root_id
        tool = next(s for s in captured_spans if _kind(s) == "TOOL")
        assert _span_obj(tool)["parentSpanId"] == _span_obj(llm)["spanId"]

    def test_tool_input_gated_by_tool_details_flag(self, captured_spans, monkeypatch):
        """Arguments are what the tool was asked to do -> ARIZE_LOG_TOOL_DETAILS."""
        monkeypatch.setenv("ARIZE_LOG_TOOL_DETAILS", "false")
        monkeypatch.setenv("ARIZE_LOG_TOOL_CONTENT", "true")
        env.invalidate_caches()
        steps = [
            _step("A", content="c", prompt=1, completion=1, tools=[ToolCall("t", "bash", {"cmd": "secret"}, "out")])
        ]

        self._emit(captured_spans, steps)

        tool = _span_attrs(next(s for s in captured_spans if _kind(s) == "TOOL"))
        assert "secret" not in tool["input.value"]
        assert tool["input.value"].startswith("<redacted (")
        assert tool["output.value"] == "out"

    def test_tool_output_gated_by_tool_content_flag(self, captured_spans, monkeypatch):
        """Results are what the tool returned -> ARIZE_LOG_TOOL_CONTENT."""
        monkeypatch.setenv("ARIZE_LOG_TOOL_DETAILS", "true")
        monkeypatch.setenv("ARIZE_LOG_TOOL_CONTENT", "false")
        env.invalidate_caches()
        steps = [
            _step(
                "A", content="c", prompt=1, completion=1, tools=[ToolCall("t", "bash", {"cmd": "ls"}, "file contents")]
            )
        ]

        self._emit(captured_spans, steps)

        tool = _span_attrs(next(s for s in captured_spans if _kind(s) == "TOOL"))
        assert json.loads(tool["input.value"]) == {"cmd": "ls"}
        assert "file contents" not in tool["output.value"]
        assert tool["output.value"].startswith("<redacted (")

    def test_root_token_totals_summed(self, captured_spans):
        steps = [
            _step("A", content="x", prompt=100, completion=10, cache_read=40),
            _step("B", content="y", prompt=50, completion=5, cache_read=10),
        ]
        self._emit(captured_spans, steps)
        root = _span_attrs(next(s for s in captured_spans if _kind(s) == "AGENT"))
        assert root["llm.token_count.prompt"] == 150
        assert root["llm.token_count.completion"] == 15
        assert root["llm.token_count.total"] == 165
        assert root["llm.token_count.prompt_details.cache_read"] == 50

    def test_root_input_output_and_meta(self, captured_spans):
        steps = [
            _step("A", content="first", prompt=1, completion=1),
            _step("B", content="final answer", prompt=1, completion=1),
        ]
        self._emit(captured_spans, steps, prompt="my question")
        root = _span_attrs(next(s for s in captured_spans if _kind(s) == "AGENT"))
        assert root["input.value"] == "my question"
        assert root["output.value"] == "final answer"  # last step's content
        assert root["session.id"] == "sess-1"
        assert root["devin.backend"] == "Windsurf"

    def test_validity_fix_empty_content_uses_reasoning(self, captured_spans):
        """An LLM step with no content but reasoning must still emit output.value."""
        steps = [_step("A", content="", thinking="my reasoning", prompt=10, completion=1)]
        self._emit(captured_spans, steps)
        llm = _span_attrs(next(s for s in captured_spans if _kind(s) == "LLM"))
        assert llm["output.value"] == "my reasoning"
        assert llm["llm.reasoning"] == "my reasoning"
        msgs = json.loads(llm["llm.output_messages"])
        assert msgs[0]["message.content"] == "my reasoning"

    def test_no_steps_emits_nothing(self, captured_spans):
        handlers.emit_interaction("sess-1", [], "prompt", {})
        assert captured_spans == []


# ===========================================================================
# flush_session — DB-sourced emission + watermark
# ===========================================================================


class TestFlushSession:
    def _db(self, tmp_path, nodes):
        db = tmp_path / "sessions.db"
        _build_db(db, [("sess-1", "/work/proj", "Windsurf", "SWE-1.6", 100, 3)], nodes)
        return db

    def test_emits_new_generations_then_dedupes(self, tmp_path, monkeypatch, captured_spans):
        db = self._db(
            tmp_path,
            [
                ("sess-1", 1, {"role": "user", "content": "hi"}),
                ("sess-1", 2, _asst("req-A", content="answer", inp=100, out=10)),
            ],
        )
        monkeypatch.setattr(handlers, "SESSIONS_DB", db)
        monkeypatch.setattr(adapter, "STATE_DIR", tmp_path / "state")

        assert handlers.flush_session("/work/proj") == 1
        assert [_kind(s) for s in captured_spans].count("AGENT") == 1

        captured_spans.clear()
        # Second flush: nothing new -> no spans.
        assert handlers.flush_session("/work/proj") == 0
        assert captured_spans == []

    def test_second_turn_emits_only_new(self, tmp_path, monkeypatch, captured_spans):
        nodes = [
            ("sess-1", 1, {"role": "user", "content": "q1"}),
            ("sess-1", 2, _asst("req-A", content="a1", inp=10, out=1)),
        ]
        db = self._db(tmp_path, nodes)
        monkeypatch.setattr(handlers, "SESSIONS_DB", db)
        monkeypatch.setattr(adapter, "STATE_DIR", tmp_path / "state")

        assert handlers.flush_session("/work/proj") == 1
        captured_spans.clear()

        # Append a second generation (a new turn) to the live DB.
        con = sqlite3.connect(str(db))
        con.execute(
            "INSERT INTO message_nodes (session_id, node_id, parent_node_id, chat_message, created_at) VALUES (?,?,?,?,?)",
            ("sess-1", 5, None, json.dumps(_asst("req-B", content="a2", inp=20, out=2)), 0),
        )
        con.commit()
        con.close()

        assert handlers.flush_session("/work/proj") == 1
        llm = [s for s in captured_spans if _kind(s) == "LLM"]
        assert len(llm) == 1
        assert _span_attrs(llm[0])["output.value"] == "a2"

    def test_unresolved_session_noops(self, tmp_path, monkeypatch, captured_spans):
        db = self._db(tmp_path, [])
        monkeypatch.setattr(handlers, "SESSIONS_DB", db)
        monkeypatch.setattr(adapter, "STATE_DIR", tmp_path / "state")
        assert handlers.flush_session("/nowhere") == 0
        assert captured_spans == []


# ===========================================================================
# main() dispatch
# ===========================================================================


class TestMain:
    def _run(self, monkeypatch, payload):
        monkeypatch.setattr(sys, "stdin", StringIO(json.dumps(payload)))
        return handlers.main()

    def test_stop_triggers_flush(self, tmp_path, monkeypatch):
        db = tmp_path / "sessions.db"
        _build_db(
            db,
            [("sess-1", "/work/proj", "Windsurf", "m", 100, 2)],
            [("sess-1", 2, _asst("req-A", content="a", inp=10, out=1))],
        )
        monkeypatch.setattr(handlers, "SESSIONS_DB", db)
        monkeypatch.setattr(adapter, "STATE_DIR", tmp_path / "state")
        monkeypatch.setenv("DEVIN_PROJECT_DIR", "/work/proj")
        calls: list[str] = []
        monkeypatch.setattr(handlers, "flush_session", lambda pd: calls.append(pd) or 1)

        assert self._run(monkeypatch, {"hook_event_name": "Stop", "stop_hook_active": False}) == 0
        assert calls == ["/work/proj"]

    def test_session_end_triggers_flush(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DEVIN_PROJECT_DIR", "/work/proj")
        calls: list[str] = []
        monkeypatch.setattr(handlers, "flush_session", lambda pd: calls.append(pd) or 1)
        assert self._run(monkeypatch, {"hook_event_name": "SessionEnd"}) == 0
        assert calls == ["/work/proj"]

    def test_foreign_event_ignored(self, monkeypatch):
        called = []
        monkeypatch.setattr(handlers, "flush_session", lambda pd: called.append(pd))
        assert self._run(monkeypatch, {"hook_event_name": "PreToolUse"}) == 0
        assert called == []

    def test_malformed_stdin_noops(self, monkeypatch):
        monkeypatch.setattr(sys, "stdin", StringIO("{not json"))
        monkeypatch.setattr(handlers, "flush_session", lambda pd: pytest.fail("should not flush"))
        assert handlers.main() == 0

    def test_non_dict_stdin_noops(self, monkeypatch):
        monkeypatch.setattr(handlers, "flush_session", lambda pd: pytest.fail("should not flush"))
        assert self._run(monkeypatch, [1, 2, 3]) == 0

    def test_trace_disabled_noops(self, monkeypatch):
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "false")
        env.invalidate_caches()
        monkeypatch.setattr(handlers, "flush_session", lambda pd: pytest.fail("should not flush"))
        assert self._run(monkeypatch, {"hook_event_name": "Stop"}) == 0
