#!/usr/bin/env python3
"""Tests for tracing.antigravity.hooks.handlers — Stop and PreInvocation."""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from unittest import mock

import pytest

from core.common import StateManager
from tracing.antigravity.hooks import handlers as handlers_mod
from tracing.antigravity.hooks.handlers import _print_response, _read_stdin, pre_invocation, stop

FIXTURE_DIR = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_spans(payload):
    return payload["resourceSpans"][0]["scopeSpans"][0]["spans"]


def _get_span(payload):
    return _get_spans(payload)[0]


def _get_span_attrs(payload):
    span = _get_span(payload)
    return {a["key"]: a["value"] for a in span["attributes"]}


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def state(tmp_path):
    """A StateManager with a temp state file, pre-initialized."""
    sf = tmp_path / "state_test.json"
    lp = tmp_path / ".lock_test"
    sm = StateManager(state_dir=tmp_path, state_file=sf, lock_path=lp)
    sm.init_state()
    sm.set("session_id", "test-session-antigravity")
    sm.set("project_name", "test-antigravity-project")
    sm.set("user_id", "test-user")
    sm.set("last_emitted_step", "-1")
    return sm


@pytest.fixture
def mock_resolve(state):
    with mock.patch("tracing.antigravity.hooks.handlers.resolve_session", return_value=state) as m:
        yield m


@pytest.fixture
def mock_ensure():
    with mock.patch("tracing.antigravity.hooks.handlers.ensure_session_initialized") as m:
        yield m


@pytest.fixture
def mock_gc():
    with mock.patch("tracing.antigravity.hooks.handlers.gc_stale_state_files") as m:
        yield m


@pytest.fixture
def captured_spans():
    """Mock _send_span_async and collect all payloads emitted by handlers.

    Patching _send_span_async (rather than send_span) lets tests run
    synchronously without forking, regardless of the ARIZE_DISABLE_FORK env.
    """
    sent = []
    with mock.patch(
        "tracing.antigravity.hooks.handlers._send_span_async",
        side_effect=lambda s: sent.append(s),
    ):
        yield sent


@pytest.fixture
def trace_enabled(monkeypatch):
    monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")


# ---------------------------------------------------------------------------
# _read_stdin tests
# ---------------------------------------------------------------------------


class TestReadStdin:
    def test_empty_stdin(self):
        with mock.patch.object(sys, "stdin", new=io.StringIO("")):
            assert _read_stdin() == {}

    def test_malformed_json(self):
        with mock.patch.object(sys, "stdin", new=io.StringIO("not json")):
            assert _read_stdin() == {}

    def test_valid_json(self):
        with mock.patch.object(sys, "stdin", new=io.StringIO('{"conversationId": "c1"}')):
            assert _read_stdin() == {"conversationId": "c1"}


# ---------------------------------------------------------------------------
# _print_response tests
# ---------------------------------------------------------------------------


class TestPrintResponse:
    def test_prints_empty_json(self, capsys):
        _print_response()
        out = capsys.readouterr().out.strip()
        assert json.loads(out) == {}

    def test_no_continue_field(self, capsys):
        """Must NOT emit 'continue' (would force agent loop to re-enter)."""
        _print_response()
        raw = capsys.readouterr().out
        assert "continue" not in raw


# ---------------------------------------------------------------------------
# Stdout discipline of entry points
# ---------------------------------------------------------------------------


class TestEntryStdoutDiscipline:
    def test_pre_invocation_empty_stdin_prints_empty(self, capsys, trace_enabled, mock_resolve, mock_ensure):
        with mock.patch.object(sys, "stdin", new=io.StringIO("")):
            pre_invocation()
        captured = capsys.readouterr()
        assert json.loads(captured.out.strip()) == {}
        assert "continue" not in captured.out

    def test_stop_empty_stdin_prints_empty(self, capsys, trace_enabled, mock_resolve, mock_ensure, mock_gc):
        with mock.patch.object(sys, "stdin", new=io.StringIO("")):
            stop()
        captured = capsys.readouterr()
        assert json.loads(captured.out.strip()) == {}
        assert "continue" not in captured.out

    def test_pre_invocation_exception_still_prints_empty(self, capsys, trace_enabled):
        with (
            mock.patch.object(sys, "stdin", new=io.StringIO('{"conversationId": "c"}')),
            mock.patch(
                "tracing.antigravity.hooks.handlers._handle_pre_invocation",
                side_effect=RuntimeError("boom"),
            ),
        ):
            pre_invocation()
        captured = capsys.readouterr()
        assert json.loads(captured.out.strip()) == {}
        assert "boom" in captured.err

    def test_stop_exception_still_prints_empty(self, capsys, trace_enabled):
        with (
            mock.patch.object(sys, "stdin", new=io.StringIO('{"conversationId": "c"}')),
            mock.patch(
                "tracing.antigravity.hooks.handlers._handle_stop",
                side_effect=RuntimeError("kaboom"),
            ),
        ):
            stop()
        captured = capsys.readouterr()
        assert json.loads(captured.out.strip()) == {}
        assert "kaboom" in captured.err


# ---------------------------------------------------------------------------
# Single-turn emission from the real fixture
# ---------------------------------------------------------------------------


class TestStopSingleTurnFixture:
    @pytest.fixture
    def stop_with_fixture(self, trace_enabled, mock_resolve, mock_ensure, mock_gc, captured_spans):
        stdin_payload = {
            "conversationId": "c1",
            "transcriptPath": str(FIXTURE_DIR / "transcript_full.jsonl"),
            "workspacePaths": ["/home/user/proj"],
        }
        with mock.patch.object(sys, "stdin", new=io.StringIO(json.dumps(stdin_payload))):
            stop()
        return captured_spans

    def test_emits_one_turn_span(self, stop_with_fixture):
        chain_spans = [
            p
            for p in stop_with_fixture
            if any(
                a["key"] == "openinference.span.kind" and a["value"]["stringValue"] == "CHAIN"
                for a in _get_span(p)["attributes"]
            )
        ]
        assert len(chain_spans) == 1
        assert _get_span(chain_spans[0])["name"] == "Turn"

    def test_emits_five_tool_spans(self, stop_with_fixture):
        tool_spans = [
            p
            for p in stop_with_fixture
            if any(
                a["key"] == "openinference.span.kind" and a["value"]["stringValue"] == "TOOL"
                for a in _get_span(p)["attributes"]
            )
        ]
        assert len(tool_spans) == 5
        names = [_get_span(p)["name"] for p in tool_spans]
        assert names == [
            "grep_search",
            "list_dir",
            "view_file",
            "search_web",
            "run_command",
        ]

    def test_emits_six_llm_spans(self, stop_with_fixture):
        """One LLM span per PLANNER_RESPONSE record (fixture has 6)."""
        llm_spans = [
            p
            for p in stop_with_fixture
            if any(
                a["key"] == "openinference.span.kind" and a["value"]["stringValue"] == "LLM"
                for a in _get_span(p)["attributes"]
            )
        ]
        assert len(llm_spans) == 6

    def test_children_share_turn_trace_and_parent(self, stop_with_fixture):
        chain_payload = next(
            p
            for p in stop_with_fixture
            if any(
                a["key"] == "openinference.span.kind" and a["value"]["stringValue"] == "CHAIN"
                for a in _get_span(p)["attributes"]
            )
        )
        chain_span = _get_span(chain_payload)
        trace_id = chain_span["traceId"]
        root_id = chain_span["spanId"]

        for payload in stop_with_fixture:
            span = _get_span(payload)
            if span is chain_span or span["name"] == "Turn":
                continue
            assert span["traceId"] == trace_id
            assert span["parentSpanId"] == root_id

    def test_turn_input_mentions_codecov(self, stop_with_fixture):
        chain_payload = next(
            p
            for p in stop_with_fixture
            if any(
                a["key"] == "openinference.span.kind" and a["value"]["stringValue"] == "CHAIN"
                for a in _get_span(p)["attributes"]
            )
        )
        attrs = _get_span_attrs(chain_payload)
        assert "codecov" in attrs["input.value"]["stringValue"]

    def test_llm_model_name_set(self, stop_with_fixture):
        llm_payload = next(
            p
            for p in stop_with_fixture
            if any(
                a["key"] == "openinference.span.kind" and a["value"]["stringValue"] == "LLM"
                for a in _get_span(p)["attributes"]
            )
        )
        attrs = _get_span_attrs(llm_payload)
        assert attrs["llm.model_name"]["stringValue"] == "Gemini 3.5 Flash (Medium)"

    def test_no_token_count_attributes(self, stop_with_fixture):
        """Antigravity withholds tokens — we must not invent them."""
        for payload in stop_with_fixture:
            for attr in _get_span(payload)["attributes"]:
                assert not attr["key"].startswith("llm.token_count"), f"unexpected token attr emitted: {attr['key']}"


# ---------------------------------------------------------------------------
# High-water-mark dedup: re-running Stop emits nothing the second time
# ---------------------------------------------------------------------------


class TestStopIdempotent:
    def test_second_stop_emits_nothing(self, trace_enabled, mock_resolve, mock_ensure, mock_gc, captured_spans):
        stdin_payload = {
            "conversationId": "c1",
            "transcriptPath": str(FIXTURE_DIR / "transcript_full.jsonl"),
            "workspacePaths": ["/home/user/proj"],
        }
        with mock.patch.object(sys, "stdin", new=io.StringIO(json.dumps(stdin_payload))):
            stop()
        first_count = len(captured_spans)
        assert first_count > 0

        with mock.patch.object(sys, "stdin", new=io.StringIO(json.dumps(stdin_payload))):
            stop()
        assert len(captured_spans) == first_count


# ---------------------------------------------------------------------------
# PreInvocation excludes the final turn
# ---------------------------------------------------------------------------


class TestPreInvocationExcludesFinal:
    def test_single_turn_emits_nothing(self, trace_enabled, mock_resolve, mock_ensure, captured_spans):
        """With one turn in the transcript (which is the *final* turn),
        PreInvocation emits zero spans."""
        stdin_payload = {
            "conversationId": "c1",
            "transcriptPath": str(FIXTURE_DIR / "transcript_full.jsonl"),
            "workspacePaths": ["/home/user/proj"],
        }
        with mock.patch.object(sys, "stdin", new=io.StringIO(json.dumps(stdin_payload))):
            pre_invocation()
        assert captured_spans == []

    def test_two_turn_inline_emits_first_only(self, tmp_path, trace_enabled, mock_resolve, mock_ensure, captured_spans):
        """A two-turn transcript: PreInvocation emits the first turn only."""
        transcript = tmp_path / "transcript.jsonl"
        _write_jsonl(
            transcript,
            [
                {
                    "step_index": 0,
                    "type": "USER_INPUT",
                    "created_at": "2026-06-09T16:00:00Z",
                    "content": "<USER_REQUEST>first</USER_REQUEST>",
                },
                {
                    "step_index": 1,
                    "type": "PLANNER_RESPONSE",
                    "created_at": "2026-06-09T16:00:01Z",
                    "content": "first answer",
                },
                {
                    "step_index": 2,
                    "type": "USER_INPUT",
                    "created_at": "2026-06-09T16:00:10Z",
                    "content": "<USER_REQUEST>second</USER_REQUEST>",
                },
                {
                    "step_index": 3,
                    "type": "PLANNER_RESPONSE",
                    "created_at": "2026-06-09T16:00:11Z",
                    "content": "second answer",
                },
            ],
        )
        stdin_payload = {
            "conversationId": "c1",
            "transcriptPath": str(transcript),
            "workspacePaths": ["/home/user/proj"],
        }
        with mock.patch.object(sys, "stdin", new=io.StringIO(json.dumps(stdin_payload))):
            pre_invocation()

        chain_spans = [
            p
            for p in captured_spans
            if any(
                a["key"] == "openinference.span.kind" and a["value"]["stringValue"] == "CHAIN"
                for a in _get_span(p)["attributes"]
            )
        ]
        assert len(chain_spans) == 1
        attrs = _get_span_attrs(chain_spans[0])
        assert attrs["input.value"]["stringValue"] == "first"
        assert attrs["output.value"]["stringValue"] == "first answer"


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------


class TestRedaction:
    def test_prompts_redacted(self, trace_enabled, mock_resolve, mock_ensure, mock_gc, captured_spans, monkeypatch):
        monkeypatch.setenv("ARIZE_LOG_PROMPTS", "false")
        stdin_payload = {
            "conversationId": "c1",
            "transcriptPath": str(FIXTURE_DIR / "transcript_full.jsonl"),
            "workspacePaths": ["/home/user/proj"],
        }
        with mock.patch.object(sys, "stdin", new=io.StringIO(json.dumps(stdin_payload))):
            stop()

        chain_payload = next(
            p
            for p in captured_spans
            if any(
                a["key"] == "openinference.span.kind" and a["value"]["stringValue"] == "CHAIN"
                for a in _get_span(p)["attributes"]
            )
        )
        attrs = _get_span_attrs(chain_payload)
        assert "redacted" in attrs["input.value"]["stringValue"]
        assert "redacted" in attrs["output.value"]["stringValue"]

        llm_payload = next(
            p
            for p in captured_spans
            if any(
                a["key"] == "openinference.span.kind" and a["value"]["stringValue"] == "LLM"
                for a in _get_span(p)["attributes"]
            )
        )
        llm_attrs = _get_span_attrs(llm_payload)
        assert "redacted" in llm_attrs["output.value"]["stringValue"]

    def test_tool_content_redacted(
        self, trace_enabled, mock_resolve, mock_ensure, mock_gc, captured_spans, monkeypatch
    ):
        monkeypatch.setenv("ARIZE_LOG_TOOL_CONTENT", "false")
        stdin_payload = {
            "conversationId": "c1",
            "transcriptPath": str(FIXTURE_DIR / "transcript_full.jsonl"),
            "workspacePaths": ["/home/user/proj"],
        }
        with mock.patch.object(sys, "stdin", new=io.StringIO(json.dumps(stdin_payload))):
            stop()

        tool_payload = next(
            p
            for p in captured_spans
            if any(
                a["key"] == "openinference.span.kind" and a["value"]["stringValue"] == "TOOL"
                for a in _get_span(p)["attributes"]
            )
        )
        attrs = _get_span_attrs(tool_payload)
        assert "redacted" in attrs["input.value"]["stringValue"]
        assert "redacted" in attrs["output.value"]["stringValue"]


# ---------------------------------------------------------------------------
# main() dispatcher
# ---------------------------------------------------------------------------


class TestMainDispatcher:
    def test_no_args_exits(self, capsys, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["arize-hook"])
        with pytest.raises(SystemExit) as exc:
            handlers_mod.main()
        assert exc.value.code == 1
        assert "usage" in capsys.readouterr().err.lower()

    def test_unknown_handler_exits(self, capsys, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["arize-hook", "nope"])
        with pytest.raises(SystemExit) as exc:
            handlers_mod.main()
        assert exc.value.code == 1
        assert "unknown handler" in capsys.readouterr().err.lower()

    def test_dispatches_pre_invocation(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["arize-hook", "pre_invocation"])
        with mock.patch.object(handlers_mod, "pre_invocation") as m:
            handlers_mod.main()
        m.assert_called_once()

    def test_dispatches_stop(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["arize-hook", "stop"])
        with mock.patch.object(handlers_mod, "stop") as m:
            handlers_mod.main()
        m.assert_called_once()
