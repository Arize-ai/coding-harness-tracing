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
    sm.set("last_emitted_turn", "-1")
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
# No conversationId and no transcriptPath: hooks no-op instead of emitting
# unkeyed duplicates
# ---------------------------------------------------------------------------


class TestNoSessionKey:
    @pytest.fixture
    def temp_state_dir(self, tmp_path, monkeypatch):
        from tracing.antigravity.hooks import adapter

        monkeypatch.setattr(adapter, "STATE_DIR", tmp_path / "state")

    def test_stop_without_key_emits_nothing(self, capsys, trace_enabled, temp_state_dir, captured_spans):
        with mock.patch.object(sys, "stdin", new=io.StringIO("{}")):
            stop()
        assert captured_spans == []
        assert json.loads(capsys.readouterr().out.strip()) == {}

    def test_pre_invocation_without_key_emits_nothing(self, capsys, trace_enabled, temp_state_dir, captured_spans):
        with mock.patch.object(sys, "stdin", new=io.StringIO("{}")):
            pre_invocation()
        assert captured_spans == []
        assert json.loads(capsys.readouterr().out.strip()) == {}


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
# Tool descriptions come from Antigravity's PascalCase arg keys
# ---------------------------------------------------------------------------


class TestToolDescription:
    def _tool_descriptions(self, spans) -> dict:
        out = {}
        for p in spans:
            span = _get_span(p)
            attrs = {a["key"]: a["value"] for a in span["attributes"]}
            kind = attrs.get("openinference.span.kind", {}).get("stringValue", "")
            if kind == "TOOL":
                out[span["name"]] = attrs["tool.description"]["stringValue"]
        return out

    @pytest.fixture
    def fixture_descriptions(self, trace_enabled, mock_resolve, mock_ensure, mock_gc, captured_spans):
        stdin_payload = {
            "conversationId": "c1",
            "transcriptPath": str(FIXTURE_DIR / "transcript_full.jsonl"),
            "workspacePaths": ["/home/user/proj"],
        }
        with mock.patch.object(sys, "stdin", new=io.StringIO(json.dumps(stdin_payload))):
            stop()
        return self._tool_descriptions(captured_spans)

    def test_run_command_uses_commandline_key(self, fixture_descriptions):
        assert fixture_descriptions["run_command"] == (
            "curl -X POST --data-binary @codecov.yml https://api.codecov.io/validate"
        )

    def test_view_file_uses_absolutepath_key(self, fixture_descriptions):
        assert fixture_descriptions["view_file"] == ("/home/user/Documents/code/coding-harness-tracing/codecov.yml")

    def test_list_dir_uses_directorypath_key(self, fixture_descriptions):
        assert fixture_descriptions["list_dir"] == "/home/user/Documents/code/coding-harness-tracing"

    def test_grep_search_uses_pascalcase_query_key(self, fixture_descriptions):
        assert fixture_descriptions["grep_search"] == "codecov.yml"

    def test_search_web_keeps_lowercase_query_key(self, fixture_descriptions):
        assert fixture_descriptions["search_web"] == (
            "codecov yml validation schema spec comment layout behavior require_changes"
        )

    def test_unknown_args_fall_back_to_truncated_json_blob(
        self, tmp_path, trace_enabled, mock_resolve, mock_ensure, mock_gc, captured_spans
    ):
        transcript = tmp_path / "transcript.jsonl"
        args = {"SomethingElse": "z" * 300}
        _write_jsonl(
            transcript,
            [
                {
                    "step_index": 0,
                    "type": "USER_INPUT",
                    "created_at": "2026-06-09T16:00:00Z",
                    "content": "<USER_REQUEST>hi</USER_REQUEST>",
                },
                {
                    "step_index": 1,
                    "type": "PLANNER_RESPONSE",
                    "created_at": "2026-06-09T16:00:01Z",
                    "content": "calling",
                    "tool_calls": [{"name": "mystery_tool", "args": args}],
                },
                {
                    "step_index": 2,
                    "type": "MYSTERY_TOOL",
                    "created_at": "2026-06-09T16:00:02Z",
                    "content": "ok",
                },
            ],
        )
        stdin_payload = {"conversationId": "c1", "transcriptPath": str(transcript)}
        with mock.patch.object(sys, "stdin", new=io.StringIO(json.dumps(stdin_payload))):
            stop()
        desc = self._tool_descriptions(captured_spans)["mystery_tool"]
        assert desc == json.dumps(args)[:200]
        assert len(desc) == 200


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
# Watermark is turn-based: missing step_index must not stop tracing
# ---------------------------------------------------------------------------


class TestTurnWatermark:
    def _chain_spans(self, spans):
        return [
            p
            for p in spans
            if any(
                a["key"] == "openinference.span.kind" and a["value"]["stringValue"] == "CHAIN"
                for a in _get_span(p)["attributes"]
            )
        ]

    def test_missing_step_index_does_not_stop_tracing(
        self, tmp_path, trace_enabled, mock_resolve, mock_ensure, mock_gc, captured_spans
    ):
        """Records without ``step_index`` (max_step_index 0) must not freeze the
        watermark after the first turn."""
        transcript = tmp_path / "transcript.jsonl"
        turn_one = [
            {
                "type": "USER_INPUT",
                "created_at": "2026-06-09T16:00:00Z",
                "content": "<USER_REQUEST>first</USER_REQUEST>",
            },
            {
                "type": "PLANNER_RESPONSE",
                "created_at": "2026-06-09T16:00:01Z",
                "content": "first answer",
            },
        ]
        _write_jsonl(transcript, turn_one)
        stdin_payload = {"conversationId": "c1", "transcriptPath": str(transcript)}
        with mock.patch.object(sys, "stdin", new=io.StringIO(json.dumps(stdin_payload))):
            stop()
        assert len(self._chain_spans(captured_spans)) == 1

        turn_two = [
            {
                "type": "USER_INPUT",
                "created_at": "2026-06-09T16:00:10Z",
                "content": "<USER_REQUEST>second</USER_REQUEST>",
            },
            {
                "type": "PLANNER_RESPONSE",
                "created_at": "2026-06-09T16:00:11Z",
                "content": "second answer",
            },
        ]
        _write_jsonl(transcript, turn_one + turn_two)
        with mock.patch.object(sys, "stdin", new=io.StringIO(json.dumps(stdin_payload))):
            stop()

        chains = self._chain_spans(captured_spans)
        assert len(chains) == 2
        outputs = [_get_span_attrs(p)["output.value"]["stringValue"] for p in chains]
        assert outputs == ["first answer", "second answer"]

    def test_legacy_step_watermark_still_honored(self, tmp_path, trace_enabled, mock_ensure, mock_gc, captured_spans):
        """A pre-existing state file carrying only ``last_emitted_step`` must not
        re-emit turns it already covers."""
        sf = tmp_path / "state_legacy.json"
        lp = tmp_path / ".lock_legacy"
        sm = StateManager(state_dir=tmp_path, state_file=sf, lock_path=lp)
        sm.init_state()
        sm.set("session_id", "legacy-session")
        sm.set("project_name", "legacy-project")
        sm.set("user_id", "legacy-user")
        # Fixture max_step_index is 13; the legacy watermark covers it.
        sm.set("last_emitted_step", "13")

        stdin_payload = {
            "conversationId": "c1",
            "transcriptPath": str(FIXTURE_DIR / "transcript_full.jsonl"),
            "workspacePaths": ["/home/user/proj"],
        }
        with (
            mock.patch("tracing.antigravity.hooks.handlers.resolve_session", return_value=sm),
            mock.patch.object(sys, "stdin", new=io.StringIO(json.dumps(stdin_payload))),
        ):
            stop()

        assert captured_spans == []
        assert sm.get("last_emitted_turn") == "0"


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

    def test_tool_content_redacts_outputs_only(
        self, trace_enabled, mock_resolve, mock_ensure, mock_gc, captured_spans, monkeypatch
    ):
        """tool_content gates tool *outputs*; tool inputs stay visible."""
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
        assert "redacted" in attrs["output.value"]["stringValue"]
        assert "redacted" not in attrs["input.value"]["stringValue"]
        assert "redacted" not in attrs["tool.description"]["stringValue"]

    def test_tool_details_redacts_inputs_only(
        self, trace_enabled, mock_resolve, mock_ensure, mock_gc, captured_spans, monkeypatch
    ):
        """tool_details gates tool *inputs* (args, description); outputs stay visible."""
        monkeypatch.setenv("ARIZE_LOG_TOOL_DETAILS", "false")
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
        assert "redacted" in attrs["tool.description"]["stringValue"]
        assert "redacted" not in attrs["output.value"]["stringValue"]


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
