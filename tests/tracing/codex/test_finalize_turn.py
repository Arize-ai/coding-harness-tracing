#!/usr/bin/env python3
"""Tests for tracing.codex.hooks.handlers._finalize_turn_and_ship.

Under the deferred-parent architecture, the notify handler (and the
session hook's orphan-finalize path) call this function to build and ship
the full multi-span tree for a completed turn. It reads pending_trace_id
and pending_parent_span_id from state (set by UserPromptSubmit), reads the
tool-span JSONL, and emits one OTLP payload containing the parent LLM span
plus all TOOL child spans.
"""

from __future__ import annotations

from unittest import mock

import pytest

from core.common import StateManager
from tracing.codex.hooks import span_buffer
from tracing.codex.hooks.handlers import _finalize_turn_and_ship


@pytest.fixture(autouse=True)
def _enable_logging(monkeypatch):
    """Opt in to raw content so assertions can check redacted text."""
    monkeypatch.setenv("ARIZE_LOG_PROMPTS", "true")
    monkeypatch.setenv("ARIZE_LOG_TOOL_DETAILS", "true")
    monkeypatch.setenv("ARIZE_LOG_TOOL_CONTENT", "true")
    monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")


@pytest.fixture
def codex_state(tmp_harness_dir, monkeypatch):
    """Build a StateManager + span_buffer pointed at the temp harness dir."""
    import core.constants as c
    import tracing.codex.hooks.adapter as adapter

    state_dir = c.STATE_BASE_DIR / "codex"
    state_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(adapter, "STATE_DIR", state_dir)
    monkeypatch.setattr(span_buffer, "STATE_DIR", state_dir)

    thread_id = "thread-fin"
    sm = StateManager(
        state_dir=state_dir,
        state_file=state_dir / f"state_{thread_id}.yaml",
        lock_path=state_dir / f".lock_{thread_id}",
    )
    sm.init_state()
    return {"manager": sm, "state_dir": state_dir, "thread_id": thread_id}


def _seed_session(sm: StateManager) -> None:
    sm.set("session_id", "thread-fin")
    sm.set("project_name", "codex")
    sm.set("trace_count", "0")
    sm.set("model", "gpt-5")
    sm.set("permission_mode", "default")
    sm.set("sandbox_mode", "workspace-write")


def _seed_pending_ids(sm: StateManager) -> None:
    """Mimic UserPromptSubmit pre-generating the trace+parent IDs."""
    sm.set("pending_trace_id", "trace-aaaa")
    sm.set("pending_parent_span_id", "span-bbbb")


def _attrs_of_span(span: dict) -> dict:
    return {a["key"]: a["value"] for a in span["attributes"]}


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestFullHappyPath:

    def test_multi_span_with_state_jsonl_and_payload(self, codex_state):
        sm = codex_state["manager"]
        thread_id = codex_state["thread_id"]
        _seed_session(sm)
        _seed_pending_ids(sm)
        sm.set("turn_start_ms", "1000")
        sm.set("user_prompt", "do something")

        span_buffer.append_tool_start(thread_id, call_id="call_a", tool="shell", args='{"cmd":"ls"}', ts_ms=1100)
        span_buffer.append_tool_end(thread_id, call_id="call_a", output="file1\nfile2", ts_ms=1200)

        token_usage = {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30, "model": "gpt-5"}

        sent: list = []
        with mock.patch(
            "tracing.codex.hooks.handlers.send_span_to_backend",
            side_effect=lambda p: (sent.append(p), True)[1],
        ):
            _finalize_turn_and_ship(sm, thread_id, assistant_output="hi", token_usage=token_usage, turn_id="t1")

        assert len(sent) == 1
        spans = sent[0]["resourceSpans"][0]["scopeSpans"][0]["spans"]
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
        assert parent_attrs["codex.turn_id"]["stringValue"] == "t1"

        child_attrs = _attrs_of_span(spans[1])
        assert child_attrs["openinference.span.kind"]["stringValue"] == "TOOL"
        assert child_attrs["tool.name"]["stringValue"] == "shell"
        assert child_attrs["codex.tool.call_id"]["stringValue"] == "call_a"

        # JSONL and turn-scoped state are cleared post-send.
        assert not span_buffer.spans_path(thread_id).exists()
        sm_after = StateManager(
            state_dir=codex_state["state_dir"],
            state_file=codex_state["state_dir"] / f"state_{thread_id}.yaml",
        )
        assert not sm_after.get("turn_start_ms")
        assert not sm_after.get("user_prompt")
        assert not sm_after.get("pending_trace_id")
        assert not sm_after.get("pending_parent_span_id")


# ---------------------------------------------------------------------------
# Parent IDs come from state (set by UserPromptSubmit)
# ---------------------------------------------------------------------------


class TestParentIdsFromState:

    def test_parent_span_id_matches_pre_seeded(self, codex_state):
        sm = codex_state["manager"]
        thread_id = codex_state["thread_id"]
        _seed_session(sm)
        sm.set("pending_trace_id", "my-trace-id")
        sm.set("pending_parent_span_id", "my-span-id")
        sm.set("turn_start_ms", "1000")
        sm.set("user_prompt", "hi")

        sent: list = []
        with mock.patch(
            "tracing.codex.hooks.handlers.send_span_to_backend",
            side_effect=lambda p: (sent.append(p), True)[1],
        ):
            _finalize_turn_and_ship(sm, thread_id, assistant_output="hi", token_usage=None)

        span = sent[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        assert span["spanId"] == "my-span-id"
        assert span["traceId"] == "my-trace-id"


# ---------------------------------------------------------------------------
# No tool spans -> single-span payload, not multi-span
# ---------------------------------------------------------------------------


class TestNoToolSpans:

    def test_single_parent_span_when_no_jsonl(self, codex_state):
        sm = codex_state["manager"]
        thread_id = codex_state["thread_id"]
        _seed_session(sm)
        _seed_pending_ids(sm)
        sm.set("turn_start_ms", "1000")
        sm.set("user_prompt", "ping")

        sent: list = []
        with mock.patch(
            "tracing.codex.hooks.handlers.send_span_to_backend",
            side_effect=lambda p: (sent.append(p), True)[1],
        ):
            _finalize_turn_and_ship(sm, thread_id, assistant_output="pong", token_usage=None)

        assert len(sent) == 1
        spans = sent[0]["resourceSpans"][0]["scopeSpans"][0]["spans"]
        assert len(spans) == 1
        assert spans[0]["name"].startswith("Turn ")


# ---------------------------------------------------------------------------
# Orphan turn: no assistant_output -> "(No response)"
# ---------------------------------------------------------------------------


class TestOrphanTurnFallback:

    def test_no_response_when_assistant_output_none(self, codex_state):
        sm = codex_state["manager"]
        thread_id = codex_state["thread_id"]
        _seed_session(sm)
        _seed_pending_ids(sm)
        sm.set("turn_start_ms", "1000")
        sm.set("user_prompt", "hello?")

        sent: list = []
        with mock.patch(
            "tracing.codex.hooks.handlers.send_span_to_backend",
            side_effect=lambda p: (sent.append(p), True)[1],
        ):
            _finalize_turn_and_ship(sm, thread_id, assistant_output=None, token_usage=None)

        assert len(sent) == 1
        parent_attrs = _attrs_of_span(sent[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0])
        assert parent_attrs["output.value"]["stringValue"] == "(No response)"
        # No llm.output_messages attr when assistant_output is None
        assert "llm.output_messages" not in parent_attrs


# ---------------------------------------------------------------------------
# Fallback path: no pending IDs -> legacy single-span
# ---------------------------------------------------------------------------


class TestLegacyFallback:

    def test_no_pending_ids_falls_back_to_legacy(self, codex_state):
        sm = codex_state["manager"]
        thread_id = codex_state["thread_id"]
        _seed_session(sm)
        # No pending_trace_id / pending_parent_span_id set: notify fired
        # before any lifecycle hook ran (first-run, hooks not yet trusted).
        sm.set("user_prompt", "hi")

        token_usage = {"prompt_tokens": 5, "completion_tokens": 6, "total_tokens": 11, "model": "gpt-5"}
        sent: list = []
        with mock.patch(
            "tracing.codex.hooks.handlers.send_span_to_backend",
            side_effect=lambda p: (sent.append(p), True)[1],
        ):
            _finalize_turn_and_ship(sm, thread_id, assistant_output="ok", token_usage=token_usage, turn_id="t9")

        assert len(sent) == 1
        spans = sent[0]["resourceSpans"][0]["scopeSpans"][0]["spans"]
        # Legacy path always builds a single span (no tool children).
        assert len(spans) == 1
        attrs = _attrs_of_span(spans[0])
        assert attrs["llm.token_count.total"]["intValue"] == 11


# ---------------------------------------------------------------------------
# trace_count increments
# ---------------------------------------------------------------------------


class TestTraceCountIncrements:

    def test_increments_existing_count(self, codex_state):
        sm = codex_state["manager"]
        thread_id = codex_state["thread_id"]
        _seed_session(sm)
        _seed_pending_ids(sm)
        sm.set("trace_count", "2")
        sm.set("turn_start_ms", "1000")
        sm.set("user_prompt", "hi")

        with mock.patch("tracing.codex.hooks.handlers.send_span_to_backend", return_value=True):
            _finalize_turn_and_ship(sm, thread_id, assistant_output="ok", token_usage=None)

        sm_after = StateManager(
            state_dir=codex_state["state_dir"],
            state_file=codex_state["state_dir"] / f"state_{thread_id}.yaml",
        )
        assert sm_after.get("trace_count") == "3"


# ---------------------------------------------------------------------------
# Permission decision surfaces on the tool child span
# ---------------------------------------------------------------------------


class TestPermissionDecision:

    def test_decision_attached_to_tool_span(self, codex_state):
        sm = codex_state["manager"]
        thread_id = codex_state["thread_id"]
        _seed_session(sm)
        _seed_pending_ids(sm)
        sm.set("turn_start_ms", "1000")
        sm.set("user_prompt", "hi")

        span_buffer.append_tool_start(thread_id, call_id="c1", tool="shell", args="{}", ts_ms=1100)
        span_buffer.append_permission(thread_id, call_id="c1", decision="allow", ts_ms=1110)
        span_buffer.append_tool_end(thread_id, call_id="c1", output="ok", ts_ms=1200)

        sent: list = []
        with mock.patch(
            "tracing.codex.hooks.handlers.send_span_to_backend",
            side_effect=lambda p: (sent.append(p), True)[1],
        ):
            _finalize_turn_and_ship(sm, thread_id, assistant_output="ok", token_usage=None)

        spans = sent[0]["resourceSpans"][0]["scopeSpans"][0]["spans"]
        child_attrs = _attrs_of_span(spans[1])
        assert child_attrs["codex.tool.approval_status"]["stringValue"] == "allow"


# ---------------------------------------------------------------------------
# User ID surfaces on parent span
# ---------------------------------------------------------------------------


class TestUserIdSurfaces:

    def test_user_id_on_parent(self, codex_state):
        sm = codex_state["manager"]
        thread_id = codex_state["thread_id"]
        _seed_session(sm)
        _seed_pending_ids(sm)
        sm.set("user_id", "alice")
        sm.set("turn_start_ms", "1000")
        sm.set("user_prompt", "hi")

        sent: list = []
        with mock.patch(
            "tracing.codex.hooks.handlers.send_span_to_backend",
            side_effect=lambda p: (sent.append(p), True)[1],
        ):
            _finalize_turn_and_ship(sm, thread_id, assistant_output="ok", token_usage=None)

        parent_attrs = _attrs_of_span(sent[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0])
        assert parent_attrs["user.id"]["stringValue"] == "alice"


# ---------------------------------------------------------------------------
# Model name falls back from token_usage when state model is empty
# ---------------------------------------------------------------------------


class TestModelFallbackFromTokens:

    def test_model_pulled_from_token_dict(self, codex_state):
        sm = codex_state["manager"]
        thread_id = codex_state["thread_id"]
        _seed_session(sm)
        sm.delete("model")  # erase state model so token_usage.model wins
        _seed_pending_ids(sm)
        sm.set("turn_start_ms", "1000")
        sm.set("user_prompt", "hi")

        token_usage = {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3, "model": "gpt-5-turbo"}
        sent: list = []
        with mock.patch(
            "tracing.codex.hooks.handlers.send_span_to_backend",
            side_effect=lambda p: (sent.append(p), True)[1],
        ):
            _finalize_turn_and_ship(sm, thread_id, assistant_output="ok", token_usage=token_usage)

        parent_attrs = _attrs_of_span(sent[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0])
        assert parent_attrs["llm.model_name"]["stringValue"] == "gpt-5-turbo"


# ---------------------------------------------------------------------------
# Backend failure: state still cleaned up, span_buffer still deleted
# ---------------------------------------------------------------------------


class TestBackendFailureStillCleansUp:

    def test_state_cleared_even_when_send_fails(self, codex_state):
        sm = codex_state["manager"]
        thread_id = codex_state["thread_id"]
        _seed_session(sm)
        _seed_pending_ids(sm)
        sm.set("turn_start_ms", "1000")
        sm.set("user_prompt", "hi")
        span_buffer.append_tool_start(thread_id, call_id="c1", tool="shell", args="{}", ts_ms=1100)
        span_buffer.append_tool_end(thread_id, call_id="c1", output="ok", ts_ms=1200)

        with mock.patch("tracing.codex.hooks.handlers.send_span_to_backend", return_value=False):
            _finalize_turn_and_ship(sm, thread_id, assistant_output="ok", token_usage=None)

        assert not span_buffer.spans_path(thread_id).exists()
        sm_after = StateManager(
            state_dir=codex_state["state_dir"],
            state_file=codex_state["state_dir"] / f"state_{thread_id}.yaml",
        )
        assert not sm_after.get("turn_start_ms")
        assert not sm_after.get("pending_trace_id")
