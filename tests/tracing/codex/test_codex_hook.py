#!/usr/bin/env python3
"""Tests for tracing.codex.hooks.handlers — rollout-driven notify handler."""

from __future__ import annotations

import json
import os
import sys
from unittest import mock

import pytest

from tracing.codex.hooks.handlers import (
    _build_and_send_spans,
    _find_rollout_file,
    _handle_notify,
    _send_legacy_single_span,
    notify,
)
from tracing.codex.hooks.transcript import _extract_turn_from_rollout, _iso_to_ms


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

    @pytest.mark.parametrize("value", [123, [], {}, None])
    def test_non_string_returns_zero(self, value):
        assert _iso_to_ms(value) == 0


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

    @pytest.mark.parametrize("session_id", ["*", "../other", "a/b", "a\\b", "[abc]"])
    def test_rejects_pattern_and_path_characters(self, tmp_path, session_id):
        _write_rollout(tmp_path, "private-session", _evt({"type": "task_started", "turn_id": "t"}))
        assert _find_rollout_file(session_id, sessions_root=tmp_path / "sessions") is None

    def test_rejects_mismatched_session_meta(self, tmp_path):
        _write_rollout(
            tmp_path,
            "requested-session",
            {
                "timestamp": "2026-05-20T00:00:00Z",
                "type": "session_meta",
                "payload": {"id": "different-session"},
            },
        )
        assert _find_rollout_file("requested-session", sessions_root=tmp_path / "sessions") is None

    def test_rejects_symlink_target_outside_sessions_root(self, tmp_path):
        sessions = tmp_path / "sessions"
        sessions.mkdir()
        outside = tmp_path / "rollout-outside-safe-session.jsonl"
        outside.write_text(json.dumps(_evt({"type": "task_started", "turn_id": "t"})) + "\n")
        (sessions / outside.name).symlink_to(outside)
        assert _find_rollout_file("safe-session", sessions_root=sessions) is None


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
            _evt({"type": "user_message", "message": "inspect"}),
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

    def test_reconstructs_llm_cycles_and_assigns_tools_to_the_originating_response(self, tmp_path):
        path = _write_rollout(
            tmp_path,
            "s1",
            _evt({"type": "task_started", "turn_id": "t1", "started_at": 1000}),
            _resp(
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "inspect the repository"}],
                }
            ),
            _resp(
                {
                    "type": "function_call",
                    "name": "exec_command",
                    "arguments": '{"cmd":"ls"}',
                    "call_id": "c1",
                },
                ts="2026-05-20T00:00:01.000Z",
            ),
            {
                "timestamp": "2026-05-20T00:00:02.000Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "last_token_usage": {
                            "input_tokens": 100,
                            "cached_input_tokens": 80,
                            "output_tokens": 20,
                            "reasoning_output_tokens": 5,
                            "total_tokens": 120,
                        }
                    },
                },
            },
            _resp(
                {"type": "function_call_output", "call_id": "c1", "output": "README.md"},
                ts="2026-05-20T00:00:03.000Z",
            ),
            _resp(
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "Repository inspected."}],
                },
                ts="2026-05-20T00:00:04.000Z",
            ),
            {
                "timestamp": "2026-05-20T00:00:05.000Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "last_token_usage": {
                            "input_tokens": 130,
                            "cached_input_tokens": 100,
                            "output_tokens": 10,
                            "reasoning_output_tokens": 2,
                            "total_tokens": 140,
                        }
                    },
                },
            },
            _evt(
                {
                    "type": "task_complete",
                    "turn_id": "t1",
                    "last_agent_message": "Repository inspected.",
                    "completed_at": 1010,
                }
            ),
        )

        turn = _extract_turn_from_rollout(path, "t1")

        assert turn is not None
        assert turn["user_prompt"] == "inspect the repository"
        assert len(turn["llm_calls"]) == 2
        first, second = turn["llm_calls"]
        assert first["input_value"] == "inspect the repository"
        assert first["token_usage"] == {
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "total_tokens": 120,
            "cached_input_tokens": 80,
            "non_cached_input_tokens": 0,
            "reasoning_output_tokens": 5,
            "model": "",
        }
        assert [tool["call_id"] for tool in first["tool_calls"]] == ["c1"]
        assert first["tool_calls"][0]["output"] == "README.md"
        assert second["input_value"] == "README.md"
        assert second["assistant_output"] == "Repository inspected."
        assert second["token_usage"]["total_tokens"] == 140
        assert second["tool_calls"] == []

    def test_retains_unclosed_reasoning_response_without_token_count(self, tmp_path):
        path = _write_rollout(
            tmp_path,
            "s1",
            _evt({"type": "task_started", "turn_id": "t1"}),
            _resp(
                {
                    "type": "reasoning",
                    "summary": [{"type": "summary_text", "text": "I should inspect the files."}],
                    "content": None,
                },
                ts="2026-05-20T00:00:01.000Z",
            ),
            {
                "timestamp": "2026-05-20T00:00:02.000Z",
                "type": "event_msg",
                "payload": {"type": "task_complete", "turn_id": "t1"},
            },
        )

        turn = _extract_turn_from_rollout(path, "t1")

        assert turn is not None
        assert len(turn["llm_calls"]) == 1
        assert turn["llm_calls"][0]["token_usage"] is None
        assert turn["llm_calls"][0]["end_ts"] == _iso_to_ms("2026-05-20T00:00:02.000Z")

    def test_reconstructs_custom_and_local_shell_call_variants(self, tmp_path):
        path = _write_rollout(
            tmp_path,
            "s1",
            _evt({"type": "task_started", "turn_id": "t1"}),
            _resp(
                {
                    "type": "custom_tool_call",
                    "call_id": "custom-1",
                    "name": "python",
                    "input": "print(1)",
                    "status": "completed",
                }
            ),
            _evt({"type": "token_count", "info": {"last_token_usage": {"input_tokens": 3, "output_tokens": 1}}}),
            _resp({"type": "custom_tool_call_output", "call_id": "custom-1", "output": "1"}),
            _resp(
                {
                    "type": "local_shell_call",
                    "call_id": "shell-1",
                    "status": "completed",
                    "action": {
                        "type": "exec",
                        "command": ["bash", "-lc", "pwd"],
                        "timeout_ms": 1000,
                        "working_directory": "/workspace",
                    },
                }
            ),
            _evt({"type": "token_count", "info": {"last_token_usage": {"input_tokens": 4, "output_tokens": 2}}}),
            _evt({"type": "task_complete", "turn_id": "t1"}),
        )

        turn = _extract_turn_from_rollout(path, "t1")

        assert turn is not None
        assert len(turn["llm_calls"]) == 2
        custom = turn["llm_calls"][0]["tool_calls"][0]
        shell = turn["llm_calls"][1]["tool_calls"][0]
        assert (custom["tool"], custom["args"], custom["output"]) == ("python", "print(1)", "1")
        assert turn["llm_calls"][1]["input_value"] == "1"
        assert shell["tool"] == "local_shell"
        assert json.loads(shell["args"])["command"] == ["bash", "-lc", "pwd"]
        assert shell["output"] == "completed"

    def test_reconstructs_tool_search_and_image_generation_variants(self, tmp_path):
        path = _write_rollout(
            tmp_path,
            "s1",
            _evt({"type": "task_started", "turn_id": "t1"}),
            _resp(
                {
                    "type": "tool_search_call",
                    "id": "search-item-1",
                    "call_id": "search-call-1",
                    "execution": "list_tools",
                    "arguments": {"query": "formatter"},
                }
            ),
            _evt({"type": "token_count", "info": {"last_token_usage": {"input_tokens": 3, "output_tokens": 1}}}),
            _resp(
                {
                    "type": "tool_search_output",
                    "call_id": "search-call-1",
                    "status": "completed",
                    "execution": "list_tools",
                    "tools": [{"name": "black"}, {"name": "ruff"}],
                }
            ),
            _resp(
                {
                    "type": "image_generation_call",
                    "id": "image-item-1",
                    "status": "completed",
                    "revised_prompt": "a small diagram",
                    "result": "data:image/png;base64,AAAA",
                }
            ),
            _evt({"type": "token_count", "info": {"last_token_usage": {"input_tokens": 5, "output_tokens": 2}}}),
            _evt({"type": "task_complete", "turn_id": "t1"}),
        )

        turn = _extract_turn_from_rollout(path, "t1")

        assert turn is not None
        assert len(turn["llm_calls"]) == 2
        search = turn["llm_calls"][0]["tool_calls"][0]
        image = turn["llm_calls"][1]["tool_calls"][0]
        expected_search_output = '[{"name":"black"},{"name":"ruff"}]'
        assert (search["tool"], search["args"], search["output"]) == (
            "tool_search",
            '{"arguments":{"query":"formatter"},"execution":"list_tools"}',
            expected_search_output,
        )
        assert turn["llm_calls"][1]["input_value"] == expected_search_output
        image_result = "data:image/png;base64,AAAA"
        assert (image["tool"], image["args"], image["output"]) == (
            "image_generation",
            "a small diagram",
            f"<image result omitted ({len(image_result)} chars)>",
        )

    def test_duplicate_call_ids_pair_outputs_fifo_and_normalize_structured_output(self, tmp_path):
        path = _write_rollout(
            tmp_path,
            "s1",
            _evt({"type": "task_started", "turn_id": "t1"}),
            _resp({"type": "function_call", "name": "first", "arguments": "{}", "call_id": "duplicate"}),
            _resp({"type": "function_call", "name": "second", "arguments": "{}", "call_id": "duplicate"}),
            _resp(
                {"type": "function_call_output", "call_id": "duplicate", "output": [{"type": "text", "text": "one"}]}
            ),
            _resp({"type": "function_call_output", "call_id": "duplicate", "output": "two"}),
            _evt({"type": "token_count", "info": {"last_token_usage": {"input_tokens": 4, "output_tokens": 2}}}),
            _evt({"type": "task_complete", "turn_id": "t1"}),
        )

        turn = _extract_turn_from_rollout(path, "t1")

        assert turn is not None
        tools = turn["llm_calls"][0]["tool_calls"]
        assert [tool["output"] for tool in tools] == ['[{"text":"one","type":"text"}]', "two"]

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

    def test_custom_tool_call_pairs_with_output_by_call_id(self, tmp_path):
        path = _write_rollout(
            tmp_path,
            "s1",
            _evt({"type": "task_started", "turn_id": "t1"}),
            _resp(
                {
                    "type": "custom_tool_call",
                    "status": "completed",
                    "name": "exec",
                    "call_id": "custom-1",
                    "input": 'await tools.exec_command({cmd: "ls"});',
                },
                ts="2026-05-20T00:00:01.000Z",
            ),
            _resp(
                {
                    "type": "custom_tool_call_output",
                    "call_id": "custom-1",
                    "output": "file1\nfile2",
                },
                ts="2026-05-20T00:00:02.000Z",
            ),
            _evt({"type": "task_complete", "turn_id": "t1"}),
        )
        turn = _extract_turn_from_rollout(path, "t1")
        assert len(turn["tool_calls"]) == 1
        tc = turn["tool_calls"][0]
        assert tc["tool"] == "exec"
        assert tc["call_id"] == "custom-1"
        assert tc["args"] == 'await tools.exec_command({cmd: "ls"});'
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
        assert turn is not None
        assert len(turn["tool_calls"]) == 1
        tc = turn["tool_calls"][0]
        assert tc["tool"] == "web_search"
        assert tc["call_id"] == "ws_1"
        assert tc["args"] == "weather"
        assert tc["output"] == "completed"
        assert len(turn["llm_calls"]) == 1
        assert turn["llm_calls"][0]["tool_calls"] == [tc]

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
            _evt({"type": "user_message", "message": "one"}),
            _evt({"type": "task_complete", "turn_id": "t1"}),
            _evt({"type": "task_started", "turn_id": "t2"}),
            _evt({"type": "user_message", "message": "two"}),
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

    def test_skips_valid_json_with_wrong_structural_shapes(self, tmp_path):
        path = tmp_path / "rollout.jsonl"
        records = [
            [],
            {"type": "event_msg", "timestamp": 123, "payload": []},
            _evt({"type": "task_started", "turn_id": "t1"}),
            {
                "type": "turn_context",
                "timestamp": 456,
                "payload": {"turn_id": "t1", "sandbox_policy": "invalid"},
            },
            _evt({"type": "token_count", "info": []}),
            _resp({"type": "web_search_call", "action": "invalid"}),
            _evt({"type": "user_message", "message": "hi"}),
            _evt({"type": "task_complete", "turn_id": "t1"}),
        ]
        path.write_text("\n".join(json.dumps(record) for record in records) + "\n")
        turn = _extract_turn_from_rollout(path, "t1")
        assert turn is not None
        assert turn["user_prompt"] == "hi"

    def test_matching_task_complete_is_a_hard_turn_boundary(self, tmp_path):
        path = _write_rollout(
            tmp_path,
            "s1",
            _evt({"type": "task_started", "turn_id": "t1"}),
            _resp({"type": "message", "role": "assistant", "content": [{"text": "actual"}]}),
            _evt({"type": "task_complete", "turn_id": "t1", "last_agent_message": "actual"}),
            _resp({"type": "message", "role": "assistant", "content": [{"text": "trailing foreign"}]}),
        )
        turn = _extract_turn_from_rollout(path, "t1")
        assert turn["assistant_output"] == "actual"
        assert turn["llm_calls"][0]["assistant_output"] == "actual"

    def test_mismatched_task_complete_is_ignored(self, tmp_path):
        path = _write_rollout(
            tmp_path,
            "s1",
            _evt({"type": "task_started", "turn_id": "t1"}),
            _evt({"type": "task_complete", "turn_id": "other", "last_agent_message": "wrong"}),
            _resp({"type": "message", "role": "assistant", "content": [{"text": "actual"}]}),
            _evt({"type": "task_complete", "turn_id": "t1", "last_agent_message": "actual"}),
        )
        turn = _extract_turn_from_rollout(path, "t1")
        assert turn["assistant_output"] == "actual"

    def test_accumulates_multiple_assistant_messages_in_one_response(self, tmp_path):
        path = _write_rollout(
            tmp_path,
            "s1",
            _evt({"type": "task_started", "turn_id": "t1"}),
            _resp({"type": "message", "role": "assistant", "content": [{"text": "commentary"}]}),
            _resp({"type": "message", "role": "assistant", "content": [{"text": "final"}]}),
            _evt({"type": "token_count", "info": {"last_token_usage": {"input_tokens": 1}}}),
            _evt({"type": "task_complete", "turn_id": "t1"}),
        )
        turn = _extract_turn_from_rollout(path, "t1")
        assert turn["llm_calls"][0]["assistant_output"] == "commentary\nfinal"

    def test_rejects_invalid_token_counts_individually(self, tmp_path):
        path = _write_rollout(
            tmp_path,
            "s1",
            _evt({"type": "task_started", "turn_id": "t1"}),
            _resp({"type": "reasoning"}),
            _evt(
                {
                    "type": "token_count",
                    "info": {
                        "last_token_usage": {
                            "input_tokens": -3,
                            "output_tokens": True,
                            "total_tokens": "secret-metadata",
                            "cached_input_tokens": 2,
                        }
                    },
                }
            ),
            _evt({"type": "task_complete", "turn_id": "t1"}),
        )
        turn = _extract_turn_from_rollout(path, "t1")
        usage = turn["llm_calls"][0]["token_usage"]
        assert usage["prompt_tokens"] is None
        assert usage["completion_tokens"] is None
        assert usage["total_tokens"] is None
        assert usage["cached_input_tokens"] == 2
        assert "secret-metadata" not in json.dumps(turn["token_usage"])

    @pytest.mark.parametrize(
        ("action", "tool", "args"),
        [
            (
                {"type": "find_in_page", "url": "https://example.com", "pattern": "needle"},
                "find_in_page",
                '{"pattern":"needle","url":"https://example.com"}',
            ),
            ({"type": "search", "queries": ["one", "two"]}, "web_search", '["one","two"]'),
            (
                {"type": "other", "description": "provider action"},
                "web_search_other",
                '{"description":"provider action","type":"other"}',
            ),
        ],
    )
    def test_preserves_official_web_search_variants(self, tmp_path, action, tool, args):
        path = _write_rollout(
            tmp_path,
            "s1",
            _evt({"type": "task_started", "turn_id": "t1"}),
            _resp({"type": "web_search_call", "status": "completed", "action": action}),
            _evt({"type": "task_complete", "turn_id": "t1"}),
        )
        entry = _extract_turn_from_rollout(path, "t1")["tool_calls"][0]
        assert (entry["tool"], entry["args"]) == (tool, args)

    def test_task_started_without_turn_evidence_returns_none(self, tmp_path):
        path = _write_rollout(tmp_path, "s1", _evt({"type": "task_started", "turn_id": "t1"}))
        assert _extract_turn_from_rollout(path, "t1") is None


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

    def test_high_fidelity_tree_uses_chain_root_and_per_response_llm_spans(self):
        turn = {
            "trace_count": 1,
            "turn_start_ms": 1000,
            "turn_end_ms": 5000,
            "duration_ms": 4000,
            "user_prompt": "inspect",
            "assistant_output": "done",
            "model": "gpt-5.5",
            "cwd": "/x/workspace",
            "permission_mode": "on-request",
            "sandbox_mode": "workspace-write",
            "token_usage": {"prompt_tokens": 230, "completion_tokens": 30, "total_tokens": 260},
            "tool_calls": [],
            "llm_calls": [
                {
                    "start_ts": 1100,
                    "end_ts": 2000,
                    "assistant_output": "",
                    "token_usage": {
                        "prompt_tokens": 100,
                        "completion_tokens": 20,
                        "total_tokens": 120,
                        "cached_input_tokens": 80,
                        "non_cached_input_tokens": 0,
                        "reasoning_output_tokens": 5,
                        "model": "gpt-5.5",
                    },
                    "tool_calls": [
                        {
                            "tool": "exec_command",
                            "args": '{"cmd":"ls"}',
                            "output": "README.md",
                            "call_id": "c1",
                            "start_ts": 1200,
                            "end_ts": 3000,
                            "decision": None,
                        }
                    ],
                },
                {
                    "start_ts": 4000,
                    "end_ts": 5000,
                    "assistant_output": "done",
                    "token_usage": {
                        "prompt_tokens": 130,
                        "completion_tokens": 10,
                        "total_tokens": 140,
                        "cached_input_tokens": 100,
                        "non_cached_input_tokens": 0,
                        "reasoning_output_tokens": 2,
                        "model": "gpt-5.5",
                    },
                    "tool_calls": [],
                },
            ],
        }

        sent, patcher = self._send_capture()
        with patcher:
            _build_and_send_spans("sess-1", "turn-1", turn)

        spans = sent[0]["resourceSpans"][0]["scopeSpans"][0]["spans"]
        assert [span["name"] for span in spans] == [
            "Turn 1",
            "LLM call 1: gpt-5.5",
            "exec_command",
            "LLM call 2: gpt-5.5",
        ]

        root, first_llm, tool, second_llm = spans
        assert "parentSpanId" not in root
        assert first_llm["parentSpanId"] == root["spanId"]
        assert tool["parentSpanId"] == first_llm["spanId"]
        assert second_llm["parentSpanId"] == root["spanId"]

        root_attrs = _attrs_of_span(root)
        assert root_attrs["openinference.span.kind"]["stringValue"] == "CHAIN"
        assert "llm.token_count.total" not in root_attrs
        first_llm_attrs = _attrs_of_span(first_llm)
        second_llm_attrs = _attrs_of_span(second_llm)
        assert first_llm_attrs["llm.token_count.prompt"]["intValue"] == 100
        assert first_llm_attrs["llm.token_count.prompt_details.cache_read"]["intValue"] == 80
        assert first_llm_attrs["llm.token_count.completion_details.reasoning"]["intValue"] == 5
        assert first_llm_attrs["llm.token_count.total"]["intValue"] == 120
        assert second_llm_attrs["llm.token_count.prompt_details.cache_read"]["intValue"] == 100
        assert second_llm_attrs["llm.token_count.total"]["intValue"] == 140

    def test_high_fidelity_content_opt_out_redacts_every_content_attribute(self, monkeypatch):
        secret = "SECRET-PAYLOAD-MUST-NOT-LEAK"
        monkeypatch.setenv("ARIZE_LOG_PROMPTS", "false")
        monkeypatch.setenv("ARIZE_LOG_TOOL_DETAILS", "false")
        monkeypatch.setenv("ARIZE_LOG_TOOL_CONTENT", "false")
        sent = []
        monkeypatch.setattr(
            "tracing.codex.hooks.handlers.send_span_to_backend", lambda payload: sent.append(payload) or True
        )

        _build_and_send_spans(
            "thread-private",
            "turn-private",
            {
                "trace_count": 1,
                "turn_start_ms": 1000,
                "turn_end_ms": 3000,
                "user_prompt": f"prompt {secret}",
                "assistant_output": f"answer {secret}",
                "cwd": "/workspace/project",
                "model": "gpt-test",
                "permission_mode": "on-request",
                "sandbox_mode": "workspace-write",
                "token_usage": {},
                "tool_calls": [],
                "llm_calls": [
                    {
                        "start_ts": 1100,
                        "end_ts": 2900,
                        "input_value": f"llm input {secret}",
                        "assistant_output": f"llm output {secret}",
                        "token_usage": {},
                        "tool_calls": [
                            {
                                "tool": "exec_command",
                                "args": f"args {secret}",
                                "output": f"output {secret}",
                                "call_id": "call-private",
                                "start_ts": 1200,
                                "end_ts": 2000,
                                "decision": None,
                            }
                        ],
                    }
                ],
            },
        )

        serialized = json.dumps(sent)
        assert secret not in serialized
        assert "<redacted (" in serialized

    def test_tool_output_gate_applies_to_reconstructed_llm_input_independently(self, monkeypatch):
        secret = "TOOL-OUTPUT-ONLY-SECRET"
        monkeypatch.setenv("ARIZE_LOG_PROMPTS", "true")
        monkeypatch.setenv("ARIZE_LOG_TOOL_CONTENT", "false")
        sent = []
        monkeypatch.setattr(
            "tracing.codex.hooks.handlers.send_span_to_backend", lambda payload: sent.append(payload) or True
        )

        _build_and_send_spans(
            "thread-private",
            "turn-private",
            {
                "trace_count": 1,
                "turn_start_ms": 1000,
                "turn_end_ms": 2000,
                "user_prompt": "safe prompt",
                "assistant_output": "safe answer",
                "token_usage": {},
                "tool_calls": [],
                "llm_calls": [
                    {
                        "start_ts": 1100,
                        "end_ts": 1900,
                        "input_value": f"safe prompt\n{secret}",
                        "input_parts": [
                            {"kind": "prompt", "value": "safe prompt"},
                            {"kind": "tool_output", "value": secret},
                        ],
                        "assistant_output": "safe answer",
                        "token_usage": {},
                        "tool_calls": [],
                    }
                ],
            },
        )

        spans = sent[0]["resourceSpans"][0]["scopeSpans"][0]["spans"]
        llm_input = _attrs_of_span(spans[1])["input.value"]["stringValue"]
        assert "safe prompt" in llm_input
        assert secret not in llm_input
        assert "<redacted (" in llm_input

    def test_bounds_all_exported_content_attributes(self, monkeypatch):
        huge = "A" * 70000 + "TAIL_SECRET"
        monkeypatch.setenv("ARIZE_LOG_PROMPTS", "true")
        monkeypatch.setenv("ARIZE_LOG_TOOL_DETAILS", "true")
        monkeypatch.setenv("ARIZE_LOG_TOOL_CONTENT", "true")
        sent = []
        monkeypatch.setattr(
            "tracing.codex.hooks.handlers.send_span_to_backend", lambda payload: sent.append(payload) or True
        )
        tool = {
            "tool": "bounded_tool",
            "args": huge,
            "output": huge,
            "call_id": "bounded-call",
            "start_ts": 1200,
            "end_ts": 1800,
            "decision": None,
        }
        _build_and_send_spans(
            "bounded-session",
            "bounded-turn",
            {
                "trace_count": 1,
                "turn_start_ms": 1000,
                "turn_end_ms": 2000,
                "user_prompt": huge,
                "assistant_output": huge,
                "token_usage": {},
                "tool_calls": [tool],
                "llm_calls": [
                    {
                        "start_ts": 1100,
                        "end_ts": 1900,
                        "input_value": huge,
                        "input_parts": [
                            {"kind": "prompt", "value": huge},
                            {"kind": "tool_output", "value": huge},
                        ],
                        "assistant_output": huge,
                        "token_usage": {},
                        "tool_calls": [tool],
                    }
                ],
            },
        )

        spans = sent[0]["resourceSpans"][0]["scopeSpans"][0]["spans"]
        content_values = []
        for span in spans:
            attrs = _attrs_of_span(span)
            for key in ("input.value", "output.value", "llm.output_messages"):
                if key in attrs:
                    content_values.append(attrs[key]["stringValue"])
        assert content_values
        assert all(len(value) <= 65600 for value in content_values)
        assert "TAIL_SECRET" not in json.dumps(sent)
        assert any("<truncated (" in value for value in content_values)

    def test_high_fidelity_send_failure_is_fail_soft(self, monkeypatch):
        errors = []
        monkeypatch.setattr("tracing.codex.hooks.handlers.send_span_to_backend", lambda _payload: False)
        monkeypatch.setattr("tracing.codex.hooks.handlers.error", errors.append)

        _build_and_send_spans(
            "thread-failure",
            "turn-failure",
            {
                "trace_count": 1,
                "turn_start_ms": 1000,
                "turn_end_ms": 2000,
                "user_prompt": "hello",
                "assistant_output": "goodbye",
                "token_usage": {},
                "tool_calls": [],
                "llm_calls": [
                    {
                        "start_ts": 1100,
                        "end_ts": 1900,
                        "input_value": "hello",
                        "assistant_output": "goodbye",
                        "token_usage": {},
                        "tool_calls": [],
                    }
                ],
            },
        )

        assert errors == ["Failed to send span to backend"]

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

    def test_partial_rollout_without_turn_evidence_uses_notify_fallback(self, _isolate_sessions_root):
        _write_rollout(
            _isolate_sessions_root,
            "partial-session",
            _evt({"type": "task_started", "turn_id": "t1"}),
        )
        sent = []
        with mock.patch(
            "tracing.codex.hooks.handlers.send_span_to_backend",
            side_effect=lambda payload: (sent.append(payload), True)[1],
        ):
            _handle_notify(
                {
                    "type": "agent-turn-complete",
                    "thread-id": "partial-session",
                    "turn-id": "t1",
                    "input-messages": [{"role": "user", "content": "notify prompt"}],
                    "last-assistant-message": "notify answer",
                }
            )

        spans = sent[0]["resourceSpans"][0]["scopeSpans"][0]["spans"]
        assert len(spans) == 1
        attrs = _attrs_of_span(spans[0])
        assert attrs["codex.notify_fallback"]["stringValue"] == "true"
        assert attrs["input.value"]["stringValue"] == "notify prompt"
        assert attrs["output.value"]["stringValue"] == "notify answer"

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
        assert len(spans) == 3  # CHAIN root + inferred LLM response + TOOL child
        root_attrs = _attrs_of_span(spans[0])
        llm_attrs = _attrs_of_span(spans[1])
        tool_attrs = _attrs_of_span(spans[2])
        assert root_attrs["openinference.span.kind"]["stringValue"] == "CHAIN"
        assert root_attrs["input.value"]["stringValue"] == "do something"
        assert root_attrs["output.value"]["stringValue"] == "done"
        assert "llm.token_count.total" not in root_attrs
        assert llm_attrs["openinference.span.kind"]["stringValue"] == "LLM"
        assert llm_attrs["llm.token_count.total"]["intValue"] == 6
        assert spans[1]["parentSpanId"] == spans[0]["spanId"]
        assert tool_attrs["tool.name"]["stringValue"] == "exec_command"
        assert spans[2]["parentSpanId"] == spans[1]["spanId"]


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

    def test_uses_canonical_codex_string_input_messages(self):
        sent = []
        with mock.patch(
            "tracing.codex.hooks.handlers.send_span_to_backend",
            side_effect=lambda p: (sent.append(p), True)[1],
        ):
            _send_legacy_single_span(
                "sess-live",
                "turn-live",
                {
                    "last-assistant-message": "done",
                    "input-messages": ["first instruction", "follow-up instruction"],
                },
            )

        spans = sent[0]["resourceSpans"][0]["scopeSpans"][0]["spans"]
        attrs = _attrs_of_span(spans[0])
        assert attrs["input.value"]["stringValue"] == "first instruction\nfollow-up instruction"
        assert attrs["output.value"]["stringValue"] == "done"


class TestReviewerRegressions:
    def test_session_meta_after_twenty_noise_records_is_validated(self, tmp_path):
        noise = [{"type": "noise", "payload": {}} for _ in range(20)]
        _write_rollout(
            tmp_path,
            "requested-session",
            *noise,
            {"type": "session_meta", "payload": {"id": "different-session"}},
        )
        assert _find_rollout_file("requested-session", sessions_root=tmp_path / "sessions") is None

    @pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO is not available on this platform")
    def test_matching_fifo_is_rejected_without_blocking(self, tmp_path):
        folder = tmp_path / "sessions" / "2026" / "01" / "01"
        folder.mkdir(parents=True)
        os.mkfifo(folder / "rollout-synthetic-stall.jsonl")
        assert _find_rollout_file("stall", sessions_root=tmp_path / "sessions") is None

    @pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO is not available on this platform")
    def test_validated_rollout_replaced_by_fifo_is_rejected_at_parse(self, tmp_path):
        folder = tmp_path / "sessions" / "2026" / "01" / "01"
        folder.mkdir(parents=True)
        path = folder / "rollout-synthetic-stall.jsonl"
        path.write_text(json.dumps({"type": "session_meta", "payload": {"id": "stall"}}) + "\n")
        found = _find_rollout_file("stall", sessions_root=tmp_path / "sessions")
        assert found == path
        path.unlink()
        os.mkfifo(path)
        assert _extract_turn_from_rollout(found, "turn") is None

    def test_duplicate_user_event_and_durable_item_are_collapsed(self, tmp_path):
        path = _write_rollout(
            tmp_path,
            "s1",
            _evt({"type": "task_started", "turn_id": "t1"}),
            _evt({"type": "user_message", "message": "hello"}),
            _resp({"type": "message", "role": "user", "content": [{"text": "hello"}]}),
            _resp({"type": "reasoning"}),
            _evt({"type": "token_count", "info": {"last_token_usage": {"input_tokens": 1}}}),
        )
        turn = _extract_turn_from_rollout(path, "t1")
        assert turn["llm_calls"][0]["input_value"] == "hello"

    def test_incomplete_response_timestamps_are_monotonic_and_output_is_aggregated(self, tmp_path):
        path = _write_rollout(
            tmp_path,
            "s1",
            {
                "timestamp": "2026-01-01T00:00:00Z",
                "type": "event_msg",
                "payload": {"type": "task_started", "turn_id": "t1"},
            },
            {
                "timestamp": "2026-01-01T00:00:04Z",
                "type": "event_msg",
                "payload": {"type": "agent_message", "message": "durable final"},
            },
            {
                "timestamp": "2026-01-01T00:00:05Z",
                "type": "response_item",
                "payload": {"type": "message", "role": "assistant", "content": [{"text": "durable final"}]},
            },
        )
        turn = _extract_turn_from_rollout(path, "t1")
        call = turn["llm_calls"][0]
        assert call["end_ts"] >= call["start_ts"]
        assert turn["turn_end_ms"] >= call["start_ts"]
        assert turn["assistant_output"] == "durable final"

    def test_malformed_token_record_does_not_split_response(self, tmp_path):
        path = _write_rollout(
            tmp_path,
            "s1",
            _evt({"type": "task_started", "turn_id": "t1"}),
            _resp({"type": "reasoning"}),
            _evt(
                {
                    "type": "token_count",
                    "info": {
                        "last_token_usage": {
                            "input_tokens": "bad",
                            "output_tokens": True,
                            "total_tokens": -1,
                        }
                    },
                }
            ),
            _resp({"type": "message", "role": "assistant", "content": [{"text": "done"}]}),
            _evt({"type": "token_count", "info": {"last_token_usage": {"output_tokens": 1}}}),
        )
        turn = _extract_turn_from_rollout(path, "t1")
        assert len(turn["llm_calls"]) == 1
        assert turn["llm_calls"][0]["assistant_output"] == "done"

    def test_boolean_started_at_uses_record_timestamp(self, tmp_path):
        path = _write_rollout(
            tmp_path,
            "s1",
            _evt({"type": "task_started", "turn_id": "t1", "started_at": True}),
            _evt({"type": "user_message", "message": "hello"}),
        )
        turn = _extract_turn_from_rollout(path, "t1")
        assert turn["turn_start_ms"] > 1000

    def test_empty_task_complete_remains_metadata_only(self, tmp_path):
        path = _write_rollout(
            tmp_path,
            "s1",
            _evt({"type": "task_started", "turn_id": "t1"}),
            _evt({"type": "task_complete", "turn_id": "t1"}),
        )
        assert _extract_turn_from_rollout(path, "t1") is None

    @pytest.mark.parametrize("result", ["QUJDREVGR0g=", {"data": "data:image/png;base64,AAAA"}])
    def test_image_generation_result_is_always_omitted(self, tmp_path, result):
        path = _write_rollout(
            tmp_path,
            "s1",
            _evt({"type": "task_started", "turn_id": "t1"}),
            _resp({"type": "image_generation_call", "id": "img1", "result": result}),
        )
        output = _extract_turn_from_rollout(path, "t1")["tool_calls"][0]["output"]
        assert output.startswith("<image result omitted (")
        assert "base64" not in output

    def test_hostile_utf8_rollout_uses_notify_fallback(self, _isolate_sessions_root):
        path = _write_rollout(_isolate_sessions_root, "bad-utf8")
        path.write_bytes(b"\xff\xfe hostile\n")
        sent = []
        with mock.patch(
            "tracing.codex.hooks.handlers.send_span_to_backend",
            side_effect=lambda payload: (sent.append(payload), True)[1],
        ):
            _handle_notify(
                {
                    "type": "agent-turn-complete",
                    "thread-id": "bad-utf8",
                    "turn-id": "t1",
                    "last-assistant-message": "fallback",
                }
            )
        attrs = _attrs_of_span(sent[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0])
        assert attrs["codex.notify_fallback"]["stringValue"] == "true"
        assert attrs["output.value"]["stringValue"] == "fallback"

    def test_fallback_bounds_content_and_metadata(self, monkeypatch):
        huge = "x" * 70000
        monkeypatch.setenv("ARIZE_PROJECT_NAME", huge)
        monkeypatch.setenv("ARIZE_USER_ID", huge)
        sent = []
        with mock.patch(
            "tracing.codex.hooks.handlers.send_span_to_backend",
            side_effect=lambda payload: (sent.append(payload), True)[1],
        ):
            _send_legacy_single_span(
                huge,
                huge,
                {"input-messages": huge, "last-assistant-message": huge},
            )
        attrs = _attrs_of_span(sent[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0])
        for key in ("input.value", "output.value", "llm.output_messages"):
            assert len(attrs[key]["stringValue"]) <= 65536
        assert len(attrs["session.id"]["stringValue"]) <= 128
        assert len(attrs["codex.turn_id"]["stringValue"]) <= 128
        assert len(attrs["project.name"]["stringValue"]) <= 4096
        assert len(attrs["user.id"]["stringValue"]) <= 4096
