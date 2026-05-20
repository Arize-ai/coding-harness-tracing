#!/usr/bin/env python3
"""Tests for tracing.codex.hooks.stop -- the Codex Stop hook handler."""

from __future__ import annotations

import json
from unittest import mock

import pytest

from core.common import StateManager
from tracing.codex.hooks import span_buffer
from tracing.codex.hooks.stop import finalize_turn, main


@pytest.fixture(autouse=True)
def _mock_sleep(monkeypatch):
    """Mock time.sleep to prevent real delays while tracking calls."""
    sleep_calls: list = []
    monkeypatch.setattr("tracing.codex.hooks.stop.time.sleep", lambda s: sleep_calls.append(s))
    return sleep_calls


@pytest.fixture(autouse=True)
def _enable_logging(monkeypatch):
    """Existing assertions expect raw content; opt in to all logging by default."""
    monkeypatch.setenv("ARIZE_LOG_PROMPTS", "true")
    monkeypatch.setenv("ARIZE_LOG_TOOL_DETAILS", "true")
    monkeypatch.setenv("ARIZE_LOG_TOOL_CONTENT", "true")
    monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")


@pytest.fixture
def codex_state(tmp_harness_dir, monkeypatch):
    """Create a Codex StateManager pointed at the temp harness dir.

    Patches both the adapter's STATE_DIR and span_buffer's STATE_DIR so
    finalize_turn and span_buffer reads/writes hit the same temp tree.
    """
    import core.constants as c
    import tracing.codex.hooks.adapter as adapter

    state_dir = c.STATE_BASE_DIR / "codex"
    state_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(adapter, "STATE_DIR", state_dir)
    monkeypatch.setattr(span_buffer, "STATE_DIR", state_dir)

    thread_id = "thread-stop"
    sm = StateManager(
        state_dir=state_dir,
        state_file=state_dir / f"state_{thread_id}.yaml",
        lock_path=state_dir / f".lock_{thread_id}",
    )
    sm.init_state()
    return {
        "manager": sm,
        "state_dir": state_dir,
        "thread_id": thread_id,
    }


def _seed_session(sm: StateManager) -> None:
    """Seed the common session-scoped fields on a state manager."""
    sm.set("session_id", "thread-stop")
    sm.set("project_name", "codex")
    sm.set("trace_count", "0")
    sm.set("model", "gpt-5")
    sm.set("permission_mode", "default")
    sm.set("sandbox_mode", "workspace-write")


def _attrs_of(span_payload: dict) -> dict:
    """Return parent span attributes as a {key: otlp-value-dict} map."""
    span = span_payload["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    return {a["key"]: a["value"] for a in span["attributes"]}


def _attrs_of_span(span: dict) -> dict:
    return {a["key"]: a["value"] for a in span["attributes"]}


# ---------------------------------------------------------------------------
# Test 1: Full happy path
# ---------------------------------------------------------------------------


class TestFullHappyPath:

    def test_multi_span_with_state_and_jsonl(self, codex_state):
        sm = codex_state["manager"]
        thread_id = codex_state["thread_id"]
        _seed_session(sm)
        sm.set("turn_start_ms", "1000")
        sm.set("user_prompt", "do something")
        sm.set(
            "pending_token_usage",
            json.dumps({"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30, "model": "gpt-5"}),
        )
        sm.set("last_assistant_message", "hi")

        span_buffer.append_tool_start(thread_id, call_id="call_a", tool="shell", args='{"cmd":"ls"}', ts_ms=1100)
        span_buffer.append_tool_end(thread_id, call_id="call_a", output="file1\nfile2", ts_ms=1200)

        sent: list = []
        with mock.patch(
            "tracing.codex.hooks.stop.send_span_to_backend",
            side_effect=lambda p: (sent.append(p), True)[1],
        ):
            finalize_turn(sm, thread_id)

        assert len(sent) == 1
        payload = sent[0]
        spans = payload["resourceSpans"][0]["scopeSpans"][0]["spans"]
        assert len(spans) == 2  # parent + 1 child

        parent_attrs = _attrs_of_span(spans[0])
        assert parent_attrs["llm.token_count.prompt"]["intValue"] == 10
        assert parent_attrs["llm.token_count.completion"]["intValue"] == 20
        assert parent_attrs["llm.token_count.total"]["intValue"] == 30
        assert parent_attrs["llm.model_name"]["stringValue"] == "gpt-5"
        assert parent_attrs["codex.approval_mode"]["stringValue"] == "default"
        assert parent_attrs["codex.sandbox_mode"]["stringValue"] == "workspace-write"
        assert parent_attrs["input.value"]["stringValue"] == "do something"
        assert parent_attrs["output.value"]["stringValue"] == "hi"

        child_attrs = _attrs_of_span(spans[1])
        assert child_attrs["openinference.span.kind"]["stringValue"] == "TOOL"
        assert child_attrs["tool.name"]["stringValue"] == "shell"
        assert child_attrs["codex.tool.call_id"]["stringValue"] == "call_a"

        assert not span_buffer.spans_path(thread_id).exists()

        # Re-resolve fresh manager and verify turn-scoped keys are cleared.
        sm_after = StateManager(
            state_dir=codex_state["state_dir"],
            state_file=codex_state["state_dir"] / f"state_{thread_id}.yaml",
        )
        assert not sm_after.get("turn_start_ms")
        assert not sm_after.get("user_prompt")
        assert not sm_after.get("pending_token_usage")
        assert not sm_after.get("last_assistant_message")


# ---------------------------------------------------------------------------
# Test 2: No tool spans -> single-span payload (not multi-span)
# ---------------------------------------------------------------------------


class TestNoToolSpans:

    def test_single_parent_span_when_no_jsonl(self, codex_state):
        sm = codex_state["manager"]
        thread_id = codex_state["thread_id"]
        _seed_session(sm)
        sm.set("turn_start_ms", "1000")
        sm.set("user_prompt", "ping")
        sm.set(
            "pending_token_usage",
            json.dumps({"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3, "model": "gpt-5"}),
        )
        sm.set("last_assistant_message", "pong")

        sent: list = []
        with mock.patch(
            "tracing.codex.hooks.stop.send_span_to_backend",
            side_effect=lambda p: (sent.append(p), True)[1],
        ):
            finalize_turn(sm, thread_id)

        assert len(sent) == 1
        spans = sent[0]["resourceSpans"][0]["scopeSpans"][0]["spans"]
        assert len(spans) == 1
        assert spans[0]["name"].startswith("Turn ")


# ---------------------------------------------------------------------------
# Test 3: No tokens -- polling exhausts within window
# ---------------------------------------------------------------------------


class TestPollingWithoutTokens:

    def test_polls_then_sends_without_tokens(self, codex_state, _mock_sleep):
        sm = codex_state["manager"]
        thread_id = codex_state["thread_id"]
        _seed_session(sm)
        sm.set("turn_start_ms", "1000")
        sm.set("user_prompt", "hello")
        # Neither last_assistant_message nor pending_token_usage seeded —
        # the wait should poll the full window before giving up.

        sent: list = []
        with mock.patch(
            "tracing.codex.hooks.stop.send_span_to_backend",
            side_effect=lambda p: (sent.append(p), True)[1],
        ):
            finalize_turn(sm, thread_id)

        assert len(sent) == 1
        parent_attrs = _attrs_of(sent[0])
        assert "llm.token_count.prompt" not in parent_attrs
        assert "llm.token_count.completion" not in parent_attrs
        assert "llm.token_count.total" not in parent_attrs
        # 40 sleeps of 0.05s each = 2000 ms total polling
        assert len(_mock_sleep) == 40
        assert all(abs(s - 0.05) < 1e-9 for s in _mock_sleep)


# ---------------------------------------------------------------------------
# Test 4: Late tokens arrive during poll
# ---------------------------------------------------------------------------


class TestLateTokens:

    def test_picks_up_tokens_mid_poll(self, codex_state, monkeypatch):
        sm = codex_state["manager"]
        thread_id = codex_state["thread_id"]
        _seed_session(sm)
        sm.set("turn_start_ms", "1000")
        sm.set("user_prompt", "hello")
        # No pre-seeded notify data: wait should poll until fake_sleep injects
        # both pending_token_usage and last_assistant_message.

        sleep_calls: list = []
        state_file = codex_state["state_dir"] / f"state_{thread_id}.yaml"

        def fake_sleep(s):
            sleep_calls.append(s)
            if len(sleep_calls) == 4:
                writer = StateManager(
                    state_dir=codex_state["state_dir"],
                    state_file=state_file,
                    lock_path=codex_state["state_dir"] / f".lock_{thread_id}",
                )
                writer.set(
                    "pending_token_usage",
                    json.dumps({"prompt_tokens": 7, "completion_tokens": 11, "total_tokens": 18, "model": "gpt-5"}),
                )
                writer.set("last_assistant_message", "hi")

        monkeypatch.setattr("tracing.codex.hooks.stop.time.sleep", fake_sleep)

        sent: list = []
        with mock.patch(
            "tracing.codex.hooks.stop.send_span_to_backend",
            side_effect=lambda p: (sent.append(p), True)[1],
        ):
            finalize_turn(sm, thread_id)

        assert len(sent) == 1
        parent_attrs = _attrs_of(sent[0])
        assert parent_attrs["llm.token_count.prompt"]["intValue"] == 7
        assert parent_attrs["llm.token_count.completion"]["intValue"] == 11
        assert parent_attrs["llm.token_count.total"]["intValue"] == 18
        # Polling stops as soon as data appears -- well short of the 40-sleep timeout.
        assert len(sleep_calls) < 10


# ---------------------------------------------------------------------------
# Test 5: Empty state + no JSONL -> nothing to finalize, no send
# ---------------------------------------------------------------------------


class TestNothingToFinalize:

    def test_empty_state_returns_without_sending(self, codex_state, _mock_sleep):
        sm = codex_state["manager"]
        thread_id = codex_state["thread_id"]
        _seed_session(sm)  # only session-scoped keys; no turn data

        with mock.patch("tracing.codex.hooks.stop.send_span_to_backend") as send:
            finalize_turn(sm, thread_id)

        send.assert_not_called()
        # Empty-state guard must run before token polling -- no sleeps spent.
        assert _mock_sleep == []


# ---------------------------------------------------------------------------
# Test 6: finalize_turn callable directly
# ---------------------------------------------------------------------------


class TestFinalizeTurnDirect:

    def test_direct_call_matches_full_path(self, codex_state):
        from tracing.codex.hooks.stop import finalize_turn as ft

        sm = codex_state["manager"]
        thread_id = codex_state["thread_id"]
        _seed_session(sm)
        sm.set("turn_start_ms", "1000")
        sm.set("user_prompt", "hi")
        sm.set(
            "pending_token_usage",
            json.dumps({"prompt_tokens": 4, "completion_tokens": 6, "total_tokens": 10, "model": "gpt-5"}),
        )
        sm.set("last_assistant_message", "yo")
        span_buffer.append_tool_start(thread_id, call_id="c1", tool="shell", args="{}", ts_ms=1100)
        span_buffer.append_tool_end(thread_id, call_id="c1", output="ok", ts_ms=1200)

        sent: list = []
        with mock.patch(
            "tracing.codex.hooks.stop.send_span_to_backend",
            side_effect=lambda p: (sent.append(p), True)[1],
        ):
            ft(sm, thread_id)

        assert len(sent) == 1
        spans = sent[0]["resourceSpans"][0]["scopeSpans"][0]["spans"]
        assert len(spans) == 2


# ---------------------------------------------------------------------------
# Test 7: trace_count increments
# ---------------------------------------------------------------------------


class TestTraceCountIncrements:

    def test_increments_existing_count(self, codex_state):
        sm = codex_state["manager"]
        thread_id = codex_state["thread_id"]
        _seed_session(sm)
        sm.set("trace_count", "2")
        sm.set("turn_start_ms", "1000")
        sm.set("user_prompt", "hi")
        sm.set("last_assistant_message", "ok")

        with mock.patch(
            "tracing.codex.hooks.stop.send_span_to_backend",
            return_value=True,
        ):
            finalize_turn(sm, thread_id)

        sm_after = StateManager(
            state_dir=codex_state["state_dir"],
            state_file=codex_state["state_dir"] / f"state_{thread_id}.yaml",
        )
        assert sm_after.get("trace_count") == "3"


# ---------------------------------------------------------------------------
# Test 8: Malformed / missing thread_id in payload -> main() returns
# ---------------------------------------------------------------------------


class TestMainMalformedPayload:

    def test_missing_session_id_skips(self, codex_state, monkeypatch, capsys):
        monkeypatch.setattr("sys.stdin", _StdinStub("{}"))
        with mock.patch("tracing.codex.hooks.stop.send_span_to_backend") as send:
            main()
        send.assert_not_called()


# ---------------------------------------------------------------------------
# Test 9: Tracing disabled
# ---------------------------------------------------------------------------


class TestTracingDisabled:

    def test_disabled_returns_without_sending(self, codex_state, monkeypatch):
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "false")
        payload = {"session_id": codex_state["thread_id"]}
        monkeypatch.setattr("sys.stdin", _StdinStub(json.dumps(payload)))
        with mock.patch("tracing.codex.hooks.stop.send_span_to_backend") as send:
            main()
        send.assert_not_called()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _StdinStub:
    """Minimal stand-in for sys.stdin that returns a fixed string from read()."""

    def __init__(self, payload: str) -> None:
        self._payload = payload

    def read(self) -> str:
        return self._payload
