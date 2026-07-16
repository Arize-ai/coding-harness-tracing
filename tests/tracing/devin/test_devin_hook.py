"""Tests for tracing.devin.hooks.handlers — the SessionEnd span emitter.

Covers:
- _token_attrs: OpenInference token-count convention, zero omission
- emit_session_spans: span tree shape (AGENT root + LLM steps + TOOL calls),
  trace/parent wiring, token totals, redaction toggles, attribute content
- main(): end-to-end (temp transcript + sessions.db), idempotency, foreign
  events, malformed stdin, trace-disabled — all fail-soft returning 0
"""

from __future__ import annotations

import json
import sqlite3
import sys
from io import StringIO
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from core.common import env
from tracing.devin.hooks import adapter, handlers
from tracing.devin.transcript import parse_transcript

FIXTURE = Path(__file__).parent / "fixtures" / "session.json"


# ---------------------------------------------------------------------------
# OTLP helpers (mirrors the Kiro handler-test pattern)
# ---------------------------------------------------------------------------


def _span_obj(span_dict: dict) -> dict:
    """Extract the inner span object from an OTLP build_span result."""
    return span_dict["resourceSpans"][0]["scopeSpans"][0]["spans"][0]


def _span_attrs(span_dict: dict) -> dict[str, Any]:
    """Flatten OTLP span attributes into a {key: value} map."""
    raw = _span_obj(span_dict).get("attributes", [])
    out: dict[str, Any] = {}
    for a in raw:
        for v in a["value"].values():
            out[a["key"]] = v
            break
    return out


def _kind(span_dict: dict) -> str:
    return _span_attrs(span_dict).get("openinference.span.kind", "")


def _load_fixture() -> dict:
    return json.loads(FIXTURE.read_text())


def _make_sessions_db(path, rows):
    """Create a sessions.db with (id, working_directory, last_activity_at) rows."""
    con = sqlite3.connect(str(path))
    try:
        con.execute("CREATE TABLE sessions (id TEXT, working_directory TEXT, last_activity_at INTEGER)")
        con.executemany(
            "INSERT INTO sessions (id, working_directory, last_activity_at) VALUES (?, ?, ?)",
            rows,
        )
        con.commit()
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    """Enable tracing and content logging; clear overrides that skew resolution."""
    monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
    monkeypatch.setenv("ARIZE_LOG_PROMPTS", "true")
    monkeypatch.setenv("ARIZE_LOG_TOOL_CONTENT", "true")
    monkeypatch.delenv("ARIZE_PROJECT_NAME", raising=False)
    monkeypatch.delenv("ARIZE_USER_ID", raising=False)
    monkeypatch.delenv("DEVIN_PROJECT_DIR", raising=False)
    env.invalidate_caches()


@pytest.fixture
def captured_spans():
    """Patch send_span where the handler imports it; collect emitted payloads."""
    sent: list[dict] = []
    with mock.patch.object(handlers, "send_span", side_effect=lambda s: sent.append(s)):
        yield sent


# ===========================================================================
# _token_attrs
# ===========================================================================


class TestTokenAttrs:
    def test_full(self):
        attrs = handlers._token_attrs(10, 5, 3)
        assert attrs["llm.token_count.prompt"] == 10
        assert attrs["llm.token_count.completion"] == 5
        assert attrs["llm.token_count.total"] == 15
        assert attrs["llm.token_count.prompt_details.cache_read"] == 3

    def test_all_zero_yields_empty(self):
        assert handlers._token_attrs(0, 0, 0) == {}

    def test_cached_defaults_to_zero_and_omitted(self):
        attrs = handlers._token_attrs(10, 5)
        assert attrs["llm.token_count.total"] == 15
        assert "llm.token_count.prompt_details.cache_read" not in attrs

    def test_completion_only(self):
        attrs = handlers._token_attrs(0, 5)
        assert "llm.token_count.prompt" not in attrs
        assert attrs["llm.token_count.completion"] == 5
        assert attrs["llm.token_count.total"] == 5

    def test_zero_cached_omitted_even_with_tokens(self):
        attrs = handlers._token_attrs(10, 0, 0)
        assert attrs["llm.token_count.prompt"] == 10
        assert attrs["llm.token_count.total"] == 10
        assert "llm.token_count.prompt_details.cache_read" not in attrs


# ===========================================================================
# emit_session_spans
# ===========================================================================


class TestEmitSessionSpans:
    def _emit(self, captured_spans, data=None):
        parsed = parse_transcript(data if data is not None else _load_fixture())
        handlers.emit_session_spans(parsed)
        return captured_spans

    def test_span_counts_and_kinds(self, captured_spans):
        """1 AGENT root + 2 LLM steps + 1 TOOL = 4 spans."""
        self._emit(captured_spans)
        assert len(captured_spans) == 4
        kinds = [_kind(s) for s in captured_spans]
        assert kinds.count("AGENT") == 1
        assert kinds.count("LLM") == 2
        assert kinds.count("TOOL") == 1

    def test_single_trace_and_root_has_no_parent(self, captured_spans):
        self._emit(captured_spans)
        trace_ids = {_span_obj(s)["traceId"] for s in captured_spans}
        assert len(trace_ids) == 1

        roots = [s for s in captured_spans if _kind(s) == "AGENT"]
        assert len(roots) == 1
        root_obj = _span_obj(roots[0])
        assert root_obj.get("parentSpanId", "") == ""

    def test_llm_steps_parented_to_root(self, captured_spans):
        self._emit(captured_spans)
        root_obj = _span_obj(next(s for s in captured_spans if _kind(s) == "AGENT"))
        root_id = root_obj["spanId"]
        for s in captured_spans:
            if _kind(s) == "LLM":
                assert _span_obj(s)["parentSpanId"] == root_id

    def test_tool_parented_to_an_llm_span(self, captured_spans):
        self._emit(captured_spans)
        llm_ids = {_span_obj(s)["spanId"] for s in captured_spans if _kind(s) == "LLM"}
        tool = next(s for s in captured_spans if _kind(s) == "TOOL")
        assert _span_obj(tool)["parentSpanId"] in llm_ids

    def test_root_token_totals(self, captured_spans):
        self._emit(captured_spans)
        root_attrs = _span_attrs(next(s for s in captured_spans if _kind(s) == "AGENT"))
        assert root_attrs["llm.token_count.prompt"] == 31620
        assert root_attrs["llm.token_count.completion"] == 429
        assert root_attrs["llm.token_count.total"] == 32049
        # total_cached_tokens is 0 → cache_read omitted
        assert "llm.token_count.prompt_details.cache_read" not in root_attrs

    def test_root_metadata_attrs(self, captured_spans):
        self._emit(captured_spans)
        root_attrs = _span_attrs(next(s for s in captured_spans if _kind(s) == "AGENT"))
        assert root_attrs["session.id"] == "test-session"
        assert root_attrs["llm.model_name"] == "SWE-1.6 Slow"
        assert root_attrs["devin.backend"] == "Windsurf"
        assert root_attrs["devin.agent_version"] == "3000.1.27"
        # input.value = joined user prompts; output.value = last agent text
        assert "weather in Seattle" in root_attrs["input.value"]
        assert root_attrs["output.value"] == "Here's the current weather..."

    def test_root_name(self, captured_spans):
        self._emit(captured_spans)
        root = next(s for s in captured_spans if _kind(s) == "AGENT")
        assert _span_obj(root)["name"] == "Devin Session test-session"

    def test_tool_span_content(self, captured_spans):
        self._emit(captured_spans)
        tool = next(s for s in captured_spans if _kind(s) == "TOOL")
        attrs = _span_attrs(tool)
        assert attrs["tool.name"] == "web_search"
        assert json.loads(attrs["input.value"]) == {"query": "Seattle weather today"}
        assert attrs["output.value"] == "# Web Search Results..."
        assert _span_obj(tool)["name"] == "Tool: web_search"

    def test_llm_step_attrs(self, captured_spans):
        self._emit(captured_spans)
        llm_spans = [s for s in captured_spans if _kind(s) == "LLM"]
        # Step 9 carries the tool call + per-step tokens.
        step9 = next(s for s in llm_spans if _span_obj(s)["name"] == "LLM step 9")
        a9 = _span_attrs(step9)
        assert a9["llm.token_count.prompt"] == 15093
        assert a9["llm.token_count.completion"] == 101
        assert a9["llm.token_count.total"] == 15194
        assert a9["llm.model_name"] == "SWE-1.6 Slow"
        assert a9["llm.reasoning"] == "The user is asking for the weather..."
        # output_messages is a JSON string echoing the assistant text
        msgs = json.loads(a9["llm.output_messages"])
        assert msgs[0]["message.role"] == "assistant"
        assert msgs[0]["message.content"] == "I'll search for the current weather in Seattle for you."

    def test_prompt_redaction(self, captured_spans, monkeypatch):
        monkeypatch.setenv("ARIZE_LOG_PROMPTS", "false")
        env.invalidate_caches()
        self._emit(captured_spans)
        root_attrs = _span_attrs(next(s for s in captured_spans if _kind(s) == "AGENT"))
        assert root_attrs["input.value"].startswith("<redacted (")
        assert root_attrs["output.value"].startswith("<redacted (")
        # LLM reasoning + output also redacted
        step9 = next(s for s in captured_spans if _kind(s) == "LLM" and _span_obj(s)["name"] == "LLM step 9")
        a9 = _span_attrs(step9)
        assert a9["output.value"].startswith("<redacted (")
        assert a9["llm.reasoning"].startswith("<redacted (")

    def test_tool_content_redaction(self, captured_spans, monkeypatch):
        monkeypatch.setenv("ARIZE_LOG_TOOL_CONTENT", "false")
        env.invalidate_caches()
        self._emit(captured_spans)
        tool = next(s for s in captured_spans if _kind(s) == "TOOL")
        attrs = _span_attrs(tool)
        assert attrs["input.value"].startswith("<redacted (")
        assert attrs["output.value"].startswith("<redacted (")
        # tool.name is metadata, never redacted
        assert attrs["tool.name"] == "web_search"

    def test_empty_transcript_emits_root_only(self, captured_spans):
        """A garbage/empty dict still yields exactly one AGENT root span."""
        self._emit(captured_spans, data={})
        assert len(captured_spans) == 1
        assert _kind(captured_spans[0]) == "AGENT"
        # No token attrs when everything is zero.
        attrs = _span_attrs(captured_spans[0])
        assert "llm.token_count.prompt" not in attrs
        assert _span_obj(captured_spans[0]).get("parentSpanId", "") == ""

    def test_service_and_scope_names(self, captured_spans):
        from tracing.devin.constants import SCOPE_NAME, SERVICE_NAME

        self._emit(captured_spans)
        payload = captured_spans[0]["resourceSpans"][0]
        svc = payload["resource"]["attributes"][0]["value"]["stringValue"]
        scope = payload["scopeSpans"][0]["scope"]["name"]
        assert svc == SERVICE_NAME
        assert scope == SCOPE_NAME


# ===========================================================================
# main() end-to-end
# ===========================================================================


def _invoke_main(payload_str: str) -> int:
    with mock.patch.object(sys, "stdin", StringIO(payload_str)):
        return handlers.main()


class TestMain:
    @pytest.fixture
    def wired(self, tmp_path, monkeypatch):
        """Wire adapter state/DB/transcript dirs to temp and return project_dir."""
        state = tmp_path / "state"
        transcripts = tmp_path / "transcripts"
        transcripts.mkdir()
        db = tmp_path / "sessions.db"
        project_dir = str(tmp_path / "proj")

        _make_sessions_db(db, [("test-session", project_dir, 100)])
        (transcripts / "test-session.json").write_text(FIXTURE.read_text())

        monkeypatch.setattr(adapter, "STATE_DIR", state)
        monkeypatch.setattr(adapter, "SESSIONS_DB", db)
        monkeypatch.setattr(adapter, "TRANSCRIPTS_DIR", transcripts)
        monkeypatch.setenv("DEVIN_PROJECT_DIR", project_dir)
        return project_dir

    def test_session_end_emits_and_returns_zero(self, wired, captured_spans):
        rc = _invoke_main(json.dumps({"hook_event_name": "SessionEnd"}))
        assert rc == 0
        assert len(captured_spans) == 4
        kinds = [_kind(s) for s in captured_spans]
        assert kinds.count("AGENT") == 1
        assert kinds.count("LLM") == 2
        assert kinds.count("TOOL") == 1

    def test_idempotent_second_call_no_reemit(self, wired, captured_spans):
        assert _invoke_main(json.dumps({"hook_event_name": "SessionEnd"})) == 0
        assert len(captured_spans) == 4
        # Second SessionEnd for the same session must not double-emit.
        assert _invoke_main(json.dumps({"hook_event_name": "SessionEnd"})) == 0
        assert len(captured_spans) == 4

    def test_foreign_event_ignored(self, wired, captured_spans):
        rc = _invoke_main(json.dumps({"hook_event_name": "Stop"}))
        assert rc == 0
        assert captured_spans == []

    def test_malformed_stdin_returns_zero(self, wired, captured_spans):
        rc = _invoke_main("not json {{{")
        assert rc == 0
        assert captured_spans == []

    def test_non_dict_stdin_returns_zero(self, wired, captured_spans):
        rc = _invoke_main(json.dumps([1, 2, 3]))
        assert rc == 0
        assert captured_spans == []

    def test_no_transcript_found_returns_zero(self, tmp_path, monkeypatch, captured_spans):
        """DB has no matching dir and transcripts dir is empty → no spans, rc 0."""
        state = tmp_path / "state"
        transcripts = tmp_path / "transcripts"
        transcripts.mkdir()
        db = tmp_path / "sessions.db"
        _make_sessions_db(db, [])
        monkeypatch.setattr(adapter, "STATE_DIR", state)
        monkeypatch.setattr(adapter, "SESSIONS_DB", db)
        monkeypatch.setattr(adapter, "TRANSCRIPTS_DIR", transcripts)
        monkeypatch.setattr(adapter, "_POLL_INTERVAL", 0.0)
        monkeypatch.setenv("DEVIN_PROJECT_DIR", str(tmp_path / "proj"))

        rc = _invoke_main(json.dumps({"hook_event_name": "SessionEnd"}))
        assert rc == 0
        assert captured_spans == []

    def test_trace_disabled_returns_zero_no_spans(self, wired, captured_spans, monkeypatch):
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "false")
        env.invalidate_caches()
        rc = _invoke_main(json.dumps({"hook_event_name": "SessionEnd"}))
        assert rc == 0
        assert captured_spans == []

    def test_malformed_transcript_file_returns_zero(self, tmp_path, monkeypatch, captured_spans):
        """A transcript that isn't valid JSON is handled fail-soft."""
        state = tmp_path / "state"
        transcripts = tmp_path / "transcripts"
        transcripts.mkdir()
        db = tmp_path / "sessions.db"
        project_dir = str(tmp_path / "proj")
        _make_sessions_db(db, [("test-session", project_dir, 100)])
        (transcripts / "test-session.json").write_text("not json {{{")
        monkeypatch.setattr(adapter, "STATE_DIR", state)
        monkeypatch.setattr(adapter, "SESSIONS_DB", db)
        monkeypatch.setattr(adapter, "TRANSCRIPTS_DIR", transcripts)
        monkeypatch.setenv("DEVIN_PROJECT_DIR", project_dir)

        rc = _invoke_main(json.dumps({"hook_event_name": "SessionEnd"}))
        assert rc == 0
        assert captured_spans == []

    def test_internal_exception_never_propagates(self, wired, captured_spans, monkeypatch):
        """A bug inside emission is swallowed; main() still returns 0."""
        monkeypatch.setattr(handlers, "emit_session_spans", mock.Mock(side_effect=RuntimeError("boom")))
        rc = _invoke_main(json.dumps({"hook_event_name": "SessionEnd"}))
        assert rc == 0

    def test_mark_emitted_written(self, wired, captured_spans, tmp_path):
        _invoke_main(json.dumps({"hook_event_name": "SessionEnd"}))
        assert adapter.already_emitted("test-session") is True
