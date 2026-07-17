#!/usr/bin/env python3
"""Tests for tracing.codex.hooks.handlers — rollout-driven notify handler."""

from __future__ import annotations

import json
import sys
from unittest import mock

import pytest

from tracing.codex.hooks.handlers import (
    _build_and_send_spans,
    _extract_turn_from_rollout,
    _find_rollout_file,
    _handle_notify,
    _iso_to_ms,
    _send_legacy_single_span,
    notify,
)


@pytest.fixture(autouse=True)
def _enable_logging(monkeypatch):
    """Opt in to raw content so assertions can check redacted text."""
    monkeypatch.setenv("ARIZE_LOG_PROMPTS", "true")
    monkeypatch.setenv("ARIZE_LOG_TOOL_DETAILS", "true")
    monkeypatch.setenv("ARIZE_LOG_TOOL_CONTENT", "true")
    monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")


@pytest.fixture(autouse=True)
def _isolate_sessions_root(tmp_path, monkeypatch):
    """Point _CODEX_SESSIONS_ROOT at a temp dir so tests don't read real rollouts."""
    import tracing.codex.hooks.handlers as h

    monkeypatch.setattr(h, "_CODEX_SESSIONS_ROOT", tmp_path / "sessions")
    return tmp_path


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _write_rollout(tmp_path, session_id, *records, day="20"):
    """Write a rollout JSONL under the standard YYYY/MM/DD path. Returns the path."""
    folder = tmp_path / "sessions" / "2026" / "05" / day
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"rollout-2026-05-{day}T00-00-00-{session_id}.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return path


def _evt(payload):
    return {"timestamp": "2026-05-20T00:00:00.000Z", "type": "event_msg", "payload": payload}


def _resp(payload, ts="2026-05-20T00:00:01.000Z"):
    return {"timestamp": ts, "type": "response_item", "payload": payload}


def _attrs_of_span(span):
    return {a["key"]: a["value"] for a in span["attributes"]}


# ---------------------------------------------------------------------------
# _iso_to_ms
# ---------------------------------------------------------------------------


class TestIsoToMs:

    def test_valid_iso_with_Z(self):
        assert _iso_to_ms("2026-05-20T23:42:45.649Z") > 0

    def test_valid_iso_with_offset(self):
        assert _iso_to_ms("2026-05-20T23:42:45.649+00:00") > 0

    def test_empty_returns_zero(self):
        assert _iso_to_ms("") == 0

    def test_bad_format_returns_zero(self):
        assert _iso_to_ms("not-a-date") == 0


# ---------------------------------------------------------------------------
# _find_rollout_file
# ---------------------------------------------------------------------------


class TestFindRolloutFile:

    def test_returns_none_when_root_missing(self, tmp_path):
        nonexistent = tmp_path / "no" / "sessions"
        assert _find_rollout_file("s1", sessions_root=nonexistent) is None

    def test_returns_none_for_empty_session_id(self, tmp_path):
        (tmp_path / "sessions").mkdir()
        assert _find_rollout_file("", sessions_root=tmp_path / "sessions") is None

    def test_matches_by_session_id(self, tmp_path):
        path = _write_rollout(tmp_path, "sess-abc", _evt({"type": "task_started", "turn_id": "t"}))
        found = _find_rollout_file("sess-abc", sessions_root=tmp_path / "sessions")
        assert found == path


# ---------------------------------------------------------------------------
# _extract_turn_from_rollout
# ---------------------------------------------------------------------------


class TestExtractTurnFromRollout:

    def test_returns_none_for_missing_file(self, tmp_path):
        from pathlib import Path

        assert _extract_turn_from_rollout(Path(str(tmp_path / "missing.jsonl")), "t1") is None

    def test_returns_none_for_empty_turn_id(self, tmp_path):
        path = _write_rollout(tmp_path, "s1", _evt({"type": "task_started", "turn_id": "t1"}))
        assert _extract_turn_from_rollout(path, "") is None

    def test_returns_none_when_turn_not_found(self, tmp_path):
        path = _write_rollout(tmp_path, "s1", _evt({"type": "task_started", "turn_id": "other"}))
        assert _extract_turn_from_rollout(path, "missing") is None

    def test_extracts_user_prompt_and_assistant_message(self, tmp_path):
        path = _write_rollout(
            tmp_path,
            "s1",
            _evt({"type": "task_started", "turn_id": "t1", "started_at": 1000}),
            _evt({"type": "user_message", "message": "hello"}),
            _evt({"type": "agent_message", "message": "world"}),
            _evt(
                {
                    "type": "task_complete",
                    "turn_id": "t1",
                    "last_agent_message": "world",
                    "completed_at": 1010,
                    "duration_ms": 10000,
                }
            ),
        )
        turn = _extract_turn_from_rollout(path, "t1")
        assert turn is not None
        assert turn["user_prompt"] == "hello"
        assert turn["assistant_output"] == "world"
        assert turn["turn_start_ms"] == 1000 * 1000
        assert turn["turn_end_ms"] == 1010 * 1000
        assert turn["duration_ms"] == 10000

    def test_task_complete_overrides_intermediate_agent_message(self, tmp_path):
        path = _write_rollout(
            tmp_path,
            "s1",
            _evt({"type": "task_started", "turn_id": "t1"}),
            _evt({"type": "agent_message", "message": "interim"}),
            _evt({"type": "task_complete", "turn_id": "t1", "last_agent_message": "final"}),
        )
        turn = _extract_turn_from_rollout(path, "t1")
        assert turn["assistant_output"] == "final"

    def test_turn_context_supplies_model_cwd_permission(self, tmp_path):
        path = _write_rollout(
            tmp_path,
            "s1",
            _evt({"type": "task_started", "turn_id": "t1"}),
            {
                "timestamp": "2026-05-20T00:00:00Z",
                "type": "turn_context",
                "payload": {
                    "turn_id": "t1",
                    "model": "gpt-5.5",
                    "cwd": "/x",
                    "approval_policy": "on-request",
                    "sandbox_policy": {"type": "workspace-write"},
                },
            },
            _evt({"type": "task_complete", "turn_id": "t1"}),
        )
        turn = _extract_turn_from_rollout(path, "t1")
        assert turn["model"] == "gpt-5.5"
        assert turn["cwd"] == "/x"
        assert turn["permission_mode"] == "on-request"
        assert turn["sandbox_mode"] == "workspace-write"

    def test_token_count_summed(self, tmp_path):
        path = _write_rollout(
            tmp_path,
            "s1",
            _evt({"type": "task_started", "turn_id": "t1"}),
            _evt(
                {
                    "type": "token_count",
                    "info": {
                        "last_token_usage": {
                            "input_tokens": 100,
                            "output_tokens": 20,
                            "total_tokens": 120,
                            "cached_input_tokens": 80,
                            "reasoning_output_tokens": 5,
                        }
                    },
                }
            ),
            _evt(
                {
                    "type": "token_count",
                    "info": {
                        "last_token_usage": {
                            "input_tokens": 50,
                            "output_tokens": 10,
                            "total_tokens": 60,
                        }
                    },
                }
            ),
            _evt({"type": "task_complete", "turn_id": "t1"}),
        )
        turn = _extract_turn_from_rollout(path, "t1")
        usage = turn["token_usage"]
        assert usage["prompt_tokens"] == 150
        assert usage["completion_tokens"] == 30
        assert usage["total_tokens"] == 180
        assert usage["cached_input_tokens"] == 80
        assert usage["reasoning_output_tokens"] == 5

    def test_function_call_pairs_with_output_by_call_id(self, tmp_path):
        path = _write_rollout(
            tmp_path,
            "s1",
            _evt({"type": "task_started", "turn_id": "t1"}),
            _resp(
                {"type": "function_call", "name": "exec_command", "arguments": '{"cmd":"ls"}', "call_id": "c1"},
                ts="2026-05-20T00:00:01.000Z",
            ),
            _resp(
                {"type": "function_call_output", "call_id": "c1", "output": "file1\nfile2"},
                ts="2026-05-20T00:00:02.000Z",
            ),
            _evt({"type": "task_complete", "turn_id": "t1"}),
        )
        turn = _extract_turn_from_rollout(path, "t1")
        assert len(turn["tool_calls"]) == 1
        tc = turn["tool_calls"][0]
        assert tc["tool"] == "exec_command"
        assert tc["call_id"] == "c1"
        assert tc["args"] == '{"cmd":"ls"}'
        assert tc["output"] == "file1\nfile2"
        assert tc["end_ts"] > tc["start_ts"]

    def test_web_search_call_pairs_with_preceding_end(self, tmp_path):
        path = _write_rollout(
            tmp_path,
            "s1",
            _evt({"type": "task_started", "turn_id": "t1"}),
            _evt({"type": "web_search_end", "call_id": "ws_1", "query": "weather"}),
            _resp({"type": "web_search_call", "status": "completed", "action": {"type": "search", "query": "weather"}}),
            _evt({"type": "task_complete", "turn_id": "t1"}),
        )
        turn = _extract_turn_from_rollout(path, "t1")
        assert len(turn["tool_calls"]) == 1
        tc = turn["tool_calls"][0]
        assert tc["tool"] == "web_search"
        assert tc["call_id"] == "ws_1"
        assert tc["args"] == "weather"
        assert tc["output"] == "completed"

    def test_web_search_open_page_action(self, tmp_path):
        path = _write_rollout(
            tmp_path,
            "s1",
            _evt({"type": "task_started", "turn_id": "t1"}),
            _evt(
                {
                    "type": "web_search_end",
                    "call_id": "ws_2",
                    "action": {"type": "open_page", "url": "https://example.com"},
                }
            ),
            _resp(
                {
                    "type": "web_search_call",
                    "status": "completed",
                    "action": {"type": "open_page", "url": "https://example.com"},
                }
            ),
            _evt({"type": "task_complete", "turn_id": "t1"}),
        )
        turn = _extract_turn_from_rollout(path, "t1")
        assert turn["tool_calls"][0]["tool"] == "open_page"
        assert turn["tool_calls"][0]["args"] == "https://example.com"

    def test_stops_at_next_task_started(self, tmp_path):
        path = _write_rollout(
            tmp_path,
            "s1",
            _evt({"type": "task_started", "turn_id": "t1"}),
            _resp({"type": "function_call", "name": "x", "arguments": "", "call_id": "c1"}),
            _evt({"type": "task_started", "turn_id": "t2"}),
            _resp({"type": "function_call", "name": "y", "arguments": "", "call_id": "c2"}),
        )
        turn = _extract_turn_from_rollout(path, "t1")
        assert len(turn["tool_calls"]) == 1
        assert turn["tool_calls"][0]["tool"] == "x"

    def test_trace_count_counts_task_started_events(self, tmp_path):
        path = _write_rollout(
            tmp_path,
            "s1",
            _evt({"type": "task_started", "turn_id": "t1"}),
            _evt({"type": "task_complete", "turn_id": "t1"}),
            _evt({"type": "task_started", "turn_id": "t2"}),
            _evt({"type": "task_complete", "turn_id": "t2"}),
        )
        # trace_count for t1 is 1; for t2 is 2.
        assert _extract_turn_from_rollout(path, "t1")["trace_count"] == 1
        assert _extract_turn_from_rollout(path, "t2")["trace_count"] == 2

    def test_skips_malformed_lines(self, tmp_path):
        path = tmp_path / "rollout.jsonl"
        path.write_text(
            "not json\n"
            + json.dumps(_evt({"type": "task_started", "turn_id": "t1", "started_at": 1000}))
            + "\n"
            + "{also broken\n"
            + json.dumps(_evt({"type": "user_message", "message": "hi"}))
            + "\n"
            + json.dumps(_evt({"type": "task_complete", "turn_id": "t1"}))
            + "\n"
        )
        turn = _extract_turn_from_rollout(path, "t1")
        assert turn["user_prompt"] == "hi"


# ---------------------------------------------------------------------------
# _build_and_send_spans
# ---------------------------------------------------------------------------


class TestBuildAndSendSpans:

    def _send_capture(self):
        sent = []
        return sent, mock.patch(
            "tracing.codex.hooks.handlers.send_span_to_backend",
            side_effect=lambda p: (sent.append(p), True)[1],
        )

    def test_project_name_from_config(self, monkeypatch):
        """project.name comes from config.json when no env override is set (#74)."""
        from core.common import env as core_env

        monkeypatch.delenv("ARIZE_PROJECT_NAME", raising=False)
        cfg = {"harnesses": {"codex": {"project_name": "from-config", "target": "phoenix"}}}
        monkeypatch.setattr("core.config.load_config", lambda: cfg)
        core_env.invalidate_caches()

        turn = {
            "trace_count": 1,
            "turn_start_ms": 1000,
            "turn_end_ms": 2000,
            "user_prompt": "hi",
            "assistant_output": "hello",
        }
        sent, patcher = self._send_capture()
        with patcher:
            _build_and_send_spans("sess-1", "turn-1", turn)

        parent_attrs = _attrs_of_span(sent[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0])
        assert parent_attrs["project.name"]["stringValue"] == "from-config"

    def test_multi_span_with_one_tool(self, monkeypatch):
        monkeypatch.setenv("ARIZE_PROJECT_NAME", "codex")
        turn = {
            "trace_count": 1,
            "turn_start_ms": 1000,
            "turn_end_ms": 2000,
            "duration_ms": 1000,
            "user_prompt": "hi",
            "assistant_output": "hello",
            "model": "gpt-5.5",
            "cwd": "/x/workspace",
            "permission_mode": "on-request",
            "sandbox_mode": "workspace-write",
            "token_usage": {
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "total_tokens": 15,
                "model": "gpt-5.5",
            },
            "tool_calls": [
                {
                    "tool": "exec_command",
                    "args": '{"cmd":"ls"}',
                    "output": "ok",
                    "call_id": "c1",
                    "start_ts": 1100,
                    "end_ts": 1200,
                    "decision": None,
                },
            ],
        }

        sent, patcher = self._send_capture()
        with patcher:
            _build_and_send_spans("sess-1", "turn-1", turn)

        assert len(sent) == 1
        spans = sent[0]["resourceSpans"][0]["scopeSpans"][0]["spans"]
        assert len(spans) == 2  # parent + 1 child

        parent_attrs = _attrs_of_span(spans[0])
        assert parent_attrs["input.value"]["stringValue"] == "hi"
        assert parent_attrs["output.value"]["stringValue"] == "hello"
        assert parent_attrs["llm.model_name"]["stringValue"] == "gpt-5.5"
        assert parent_attrs["codex.approval_mode"]["stringValue"] == "on-request"
        assert parent_attrs["codex.sandbox_mode"]["stringValue"] == "workspace-write"
        assert parent_attrs["codex.cwd"]["stringValue"] == "/x/workspace"
        assert parent_attrs["codex.workspace"]["stringValue"] == "workspace"
        assert parent_attrs["llm.token_count.total"]["intValue"] == 15

        child_attrs = _attrs_of_span(spans[1])
        assert child_attrs["tool.name"]["stringValue"] == "exec_command"
        assert child_attrs["codex.tool.call_id"]["stringValue"] == "c1"
        assert child_attrs["codex.cwd"]["stringValue"] == "/x/workspace"
        assert child_attrs["codex.workspace"]["stringValue"] == "workspace"
        assert child_attrs["input.value"]["stringValue"] == '{"cmd":"ls"}'
        assert child_attrs["output.value"]["stringValue"] == "ok"

    def test_single_span_when_no_tool_calls(self):
        turn = {
            "trace_count": 1,
            "turn_start_ms": 1000,
            "turn_end_ms": 1500,
            "duration_ms": 500,
            "user_prompt": "hi",
            "assistant_output": "hello",
            "model": "",
            "cwd": "",
            "permission_mode": "",
            "sandbox_mode": "",
            "token_usage": None,
            "tool_calls": [],
        }
        sent, patcher = self._send_capture()
        with patcher:
            _build_and_send_spans("sess-1", "turn-1", turn)
        spans = sent[0]["resourceSpans"][0]["scopeSpans"][0]["spans"]
        assert len(spans) == 1

    def test_no_response_when_assistant_output_empty(self):
        turn = {
            "trace_count": 1,
            "turn_start_ms": 1000,
            "turn_end_ms": 1500,
            "duration_ms": 0,
            "user_prompt": "hi",
            "assistant_output": "",
            "model": "",
            "cwd": "",
            "permission_mode": "",
            "sandbox_mode": "",
            "token_usage": None,
            "tool_calls": [],
        }
        sent, patcher = self._send_capture()
        with patcher:
            _build_and_send_spans("sess-1", "turn-1", turn)
        parent_attrs = _attrs_of_span(sent[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0])
        assert parent_attrs["output.value"]["stringValue"] == "(No response)"


# ---------------------------------------------------------------------------
# _handle_notify end-to-end
# ---------------------------------------------------------------------------


class TestHandleNotify:

    def test_ignores_non_agent_turn_complete(self):
        with mock.patch("tracing.codex.hooks.handlers.send_span_to_backend") as send:
            _handle_notify({"type": "session-start"})
        send.assert_not_called()

    def test_falls_back_to_single_span_when_no_rollout(self, _isolate_sessions_root):
        sent = []
        with mock.patch(
            "tracing.codex.hooks.handlers.send_span_to_backend",
            side_effect=lambda p: (sent.append(p), True)[1],
        ):
            _handle_notify(
                {
                    "type": "agent-turn-complete",
                    "thread-id": "no-rollout-yet",
                    "turn-id": "t1",
                    "input-messages": [{"role": "user", "content": "hi"}],
                    "last-assistant-message": "hello",
                }
            )

        assert len(sent) == 1
        spans = sent[0]["resourceSpans"][0]["scopeSpans"][0]["spans"]
        assert len(spans) == 1
        attrs = _attrs_of_span(spans[0])
        assert attrs["codex.notify_fallback"]["stringValue"] == "true"
        assert attrs["output.value"]["stringValue"] == "hello"

    def test_extracts_from_rollout_and_ships_multi_span(self, _isolate_sessions_root):
        _write_rollout(
            _isolate_sessions_root,
            "sess-real",
            _evt({"type": "task_started", "turn_id": "turn-1", "started_at": 1000}),
            _evt({"type": "user_message", "message": "do something"}),
            _resp({"type": "function_call", "name": "exec_command", "arguments": '{"cmd":"ls"}', "call_id": "c1"}),
            _resp({"type": "function_call_output", "call_id": "c1", "output": "file"}),
            _evt(
                {
                    "type": "token_count",
                    "info": {"last_token_usage": {"input_tokens": 5, "output_tokens": 1, "total_tokens": 6}},
                }
            ),
            _evt({"type": "task_complete", "turn_id": "turn-1", "last_agent_message": "done", "completed_at": 1010}),
        )

        sent = []
        with mock.patch(
            "tracing.codex.hooks.handlers.send_span_to_backend",
            side_effect=lambda p: (sent.append(p), True)[1],
        ):
            _handle_notify(
                {
                    "type": "agent-turn-complete",
                    "thread-id": "sess-real",
                    "turn-id": "turn-1",
                }
            )

        assert len(sent) == 1
        spans = sent[0]["resourceSpans"][0]["scopeSpans"][0]["spans"]
        assert len(spans) == 2  # parent + 1 tool child
        parent_attrs = _attrs_of_span(spans[0])
        assert parent_attrs["input.value"]["stringValue"] == "do something"
        assert parent_attrs["output.value"]["stringValue"] == "done"
        assert parent_attrs["llm.token_count.total"]["intValue"] == 6
        child_attrs = _attrs_of_span(spans[1])
        assert child_attrs["tool.name"]["stringValue"] == "exec_command"


# ---------------------------------------------------------------------------
# Notify CLI entry-point
# ---------------------------------------------------------------------------


class TestNotifyEntryPoint:

    def test_tracing_disabled_returns_early(self, monkeypatch):
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "false")
        with mock.patch.object(sys, "argv", ["hook", "{}"]):
            with mock.patch("tracing.codex.hooks.handlers.send_span_to_backend") as send:
                notify()
        send.assert_not_called()

    def test_missing_argv_defaults_to_empty(self, monkeypatch):
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        with mock.patch.object(sys, "argv", ["hook"]):
            with mock.patch("tracing.codex.hooks.handlers.send_span_to_backend") as send:
                notify()
        send.assert_not_called()

    def test_malformed_json_does_not_raise(self, monkeypatch):
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        with mock.patch.object(sys, "argv", ["hook", "not json"]):
            notify()  # caught internally; no raise


# ---------------------------------------------------------------------------
# Legacy single-span fallback
# ---------------------------------------------------------------------------


class TestSendLegacySingleSpan:

    def test_emits_one_span_with_fallback_marker(self):
        sent = []
        with mock.patch(
            "tracing.codex.hooks.handlers.send_span_to_backend",
            side_effect=lambda p: (sent.append(p), True)[1],
        ):
            _send_legacy_single_span(
                "sess-x",
                "turn-x",
                {"last-assistant-message": "hi", "input-messages": [{"role": "user", "content": "yo"}]},
            )
        assert len(sent) == 1
        spans = sent[0]["resourceSpans"][0]["scopeSpans"][0]["spans"]
        assert len(spans) == 1
        attrs = _attrs_of_span(spans[0])
        assert attrs["codex.notify_fallback"]["stringValue"] == "true"
        assert attrs["input.value"]["stringValue"] == "yo"
        assert attrs["output.value"]["stringValue"] == "hi"
