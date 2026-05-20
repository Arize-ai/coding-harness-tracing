#!/usr/bin/env python3
"""Additional coverage for tracing.codex.hooks.stop.

Complements test_stop_hook.py by covering main() entry-point branches,
backend send failures, permission decisions, multi-tool ordering, and the
'(No response)' fallback. Mirrors the fixture conventions from
test_stop_hook.py.
"""

from __future__ import annotations

import json
from unittest import mock

import pytest

from core.common import StateManager
from tracing.codex.hooks import span_buffer
from tracing.codex.hooks.stop import finalize_turn, main


@pytest.fixture(autouse=True)
def _mock_sleep(monkeypatch):
    sleep_calls: list = []
    monkeypatch.setattr("tracing.codex.hooks.stop.time.sleep", lambda s: sleep_calls.append(s))
    return sleep_calls


@pytest.fixture(autouse=True)
def _enable_logging(monkeypatch):
    monkeypatch.setenv("ARIZE_LOG_PROMPTS", "true")
    monkeypatch.setenv("ARIZE_LOG_TOOL_DETAILS", "true")
    monkeypatch.setenv("ARIZE_LOG_TOOL_CONTENT", "true")
    monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")


@pytest.fixture
def codex_state(tmp_harness_dir, monkeypatch):
    """Set up state directory + a pre-initialized StateManager for one thread."""
    import core.constants as c
    import tracing.codex.hooks.adapter as adapter

    state_dir = c.STATE_BASE_DIR / "codex"
    state_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(adapter, "STATE_DIR", state_dir)
    monkeypatch.setattr(span_buffer, "STATE_DIR", state_dir)

    thread_id = "thread-extra"
    sm = StateManager(
        state_dir=state_dir,
        state_file=state_dir / f"state_{thread_id}.yaml",
        lock_path=state_dir / f".lock_{thread_id}",
    )
    sm.init_state()
    return {"manager": sm, "state_dir": state_dir, "thread_id": thread_id}


def _seed_session(sm: StateManager, thread_id: str = "thread-extra") -> None:
    sm.set("session_id", thread_id)
    sm.set("project_name", "codex")
    sm.set("trace_count", "0")
    sm.set("model", "gpt-5")
    sm.set("permission_mode", "default")
    sm.set("sandbox_mode", "workspace-write")


def _attrs(span: dict) -> dict:
    return {a["key"]: a["value"] for a in span["attributes"]}


def _parent_and_children(payload: dict):
    spans = payload["resourceSpans"][0]["scopeSpans"][0]["spans"]
    return spans[0], spans[1:]


class _StdinStub:
    def __init__(self, text: str) -> None:
        self._text = text

    def read(self) -> str:
        return self._text


# ---------------------------------------------------------------------------
# main() entry point: happy path via stdin
# ---------------------------------------------------------------------------


class TestMainStdinHappyPath:
    def test_main_dispatches_finalize_turn(self, codex_state, monkeypatch):
        sm = codex_state["manager"]
        thread_id = codex_state["thread_id"]
        _seed_session(sm, thread_id)
        sm.set("turn_start_ms", "1000")
        sm.set("user_prompt", "x")
        sm.set("last_assistant_message", "y")
        sm.set(
            "pending_token_usage",
            json.dumps({"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7, "model": "gpt-5"}),
        )

        payload = {"session_id": thread_id, "hook_event_name": "Stop", "cwd": "/x"}
        monkeypatch.setattr("sys.stdin", _StdinStub(json.dumps(payload)))

        sent: list = []
        with mock.patch(
            "tracing.codex.hooks.stop.send_span_to_backend",
            side_effect=lambda p: (sent.append(p), True)[1],
        ):
            main()

        assert len(sent) == 1
        parent, _ = _parent_and_children(sent[0])
        assert _attrs(parent)["input.value"]["stringValue"] == "x"


# ---------------------------------------------------------------------------
# main() defensive branches
# ---------------------------------------------------------------------------


class TestMainEdgeCases:
    def test_empty_stdin_returns_silently(self, codex_state, monkeypatch):
        monkeypatch.setattr("sys.stdin", _StdinStub("   \n"))
        with mock.patch("tracing.codex.hooks.stop.send_span_to_backend") as send:
            main()
        send.assert_not_called()

    def test_malformed_json_is_caught(self, codex_state, monkeypatch):
        monkeypatch.setattr("sys.stdin", _StdinStub("{not-json"))
        with mock.patch("tracing.codex.hooks.stop.send_span_to_backend") as send:
            # Must not raise; exception path inside main() catches it.
            main()
        send.assert_not_called()

    def test_thread_id_fallback_when_session_id_missing(self, codex_state, monkeypatch):
        sm = codex_state["manager"]
        thread_id = codex_state["thread_id"]
        _seed_session(sm, thread_id)
        sm.set("turn_start_ms", "1000")
        sm.set("user_prompt", "p")
        sm.set("last_assistant_message", "r")

        # Payload uses 'thread_id' rather than 'session_id'; main should still resolve.
        payload = {"thread_id": thread_id, "hook_event_name": "Stop"}
        monkeypatch.setattr("sys.stdin", _StdinStub(json.dumps(payload)))

        sent: list = []
        with mock.patch(
            "tracing.codex.hooks.stop.send_span_to_backend",
            side_effect=lambda p: (sent.append(p), True)[1],
        ):
            main()

        assert len(sent) == 1


# ---------------------------------------------------------------------------
# Backend send-failure semantics (per "What NOT to do" line in spec):
# even if backend returns False or raises, state cleanup + JSONL delete must
# still happen so subsequent turns aren't poisoned.
# ---------------------------------------------------------------------------


class TestBackendFailureStillCleansUp:
    def test_backend_returns_false_still_clears_state_and_jsonl(self, codex_state):
        sm = codex_state["manager"]
        thread_id = codex_state["thread_id"]
        _seed_session(sm, thread_id)
        sm.set("turn_start_ms", "1000")
        sm.set("user_prompt", "p")
        sm.set("last_assistant_message", "r")
        span_buffer.append_tool_start(thread_id, call_id="c1", tool="shell", args="{}", ts_ms=1100)
        span_buffer.append_tool_end(thread_id, call_id="c1", output="ok", ts_ms=1200)

        with mock.patch(
            "tracing.codex.hooks.stop.send_span_to_backend",
            return_value=False,  # simulate non-2xx etc.
        ):
            finalize_turn(sm, thread_id)

        assert not span_buffer.spans_path(thread_id).exists()
        sm_after = StateManager(
            state_dir=codex_state["state_dir"],
            state_file=codex_state["state_dir"] / f"state_{thread_id}.yaml",
        )
        assert not sm_after.get("user_prompt")
        assert not sm_after.get("last_assistant_message")
        assert not sm_after.get("turn_start_ms")

    def test_backend_raises_still_clears_state_and_jsonl(self, codex_state):
        sm = codex_state["manager"]
        thread_id = codex_state["thread_id"]
        _seed_session(sm, thread_id)
        sm.set("turn_start_ms", "1000")
        sm.set("user_prompt", "p")
        sm.set("last_assistant_message", "r")
        span_buffer.append_tool_start(thread_id, call_id="c1", tool="shell", args="{}", ts_ms=1100)
        span_buffer.append_tool_end(thread_id, call_id="c1", output="ok", ts_ms=1200)

        with mock.patch(
            "tracing.codex.hooks.stop.send_span_to_backend",
            side_effect=RuntimeError("connection refused"),
        ):
            finalize_turn(sm, thread_id)  # must not raise

        assert not span_buffer.spans_path(thread_id).exists()
        sm_after = StateManager(
            state_dir=codex_state["state_dir"],
            state_file=codex_state["state_dir"] / f"state_{thread_id}.yaml",
        )
        assert not sm_after.get("user_prompt")


# ---------------------------------------------------------------------------
# Permission decision propagates to TOOL span attributes
# ---------------------------------------------------------------------------


class TestPermissionDecisionAttached:
    def test_decision_becomes_approval_status_attr(self, codex_state):
        sm = codex_state["manager"]
        thread_id = codex_state["thread_id"]
        _seed_session(sm, thread_id)
        sm.set("turn_start_ms", "1000")
        sm.set("user_prompt", "ask")
        sm.set("last_assistant_message", "done")

        span_buffer.append_tool_start(thread_id, call_id="c1", tool="shell", args="{}", ts_ms=1100)
        span_buffer.append_permission(thread_id, call_id="c1", decision="allow", ts_ms=1110)
        span_buffer.append_tool_end(thread_id, call_id="c1", output="ok", ts_ms=1200)

        sent: list = []
        with mock.patch(
            "tracing.codex.hooks.stop.send_span_to_backend",
            side_effect=lambda p: (sent.append(p), True)[1],
        ):
            finalize_turn(sm, thread_id)

        _, children = _parent_and_children(sent[0])
        assert len(children) == 1
        ca = _attrs(children[0])
        assert ca["codex.tool.approval_status"]["stringValue"] == "allow"
        assert ca["codex.tool.call_id"]["stringValue"] == "c1"


# ---------------------------------------------------------------------------
# Multiple tools preserve first-appearance order from join_by_call_id.
# ---------------------------------------------------------------------------


class TestMultipleToolSpansOrdered:
    def test_two_tools_in_order(self, codex_state):
        sm = codex_state["manager"]
        thread_id = codex_state["thread_id"]
        _seed_session(sm, thread_id)
        sm.set("turn_start_ms", "1000")
        sm.set("user_prompt", "do stuff")
        sm.set("last_assistant_message", "all done")

        span_buffer.append_tool_start(thread_id, call_id="c1", tool="shell", args="{}", ts_ms=1100)
        span_buffer.append_tool_start(thread_id, call_id="c2", tool="apply_patch", args="{}", ts_ms=1200)
        span_buffer.append_tool_end(thread_id, call_id="c1", output="ok1", ts_ms=1300)
        span_buffer.append_tool_end(thread_id, call_id="c2", output="ok2", ts_ms=1400)

        sent: list = []
        with mock.patch(
            "tracing.codex.hooks.stop.send_span_to_backend",
            side_effect=lambda p: (sent.append(p), True)[1],
        ):
            finalize_turn(sm, thread_id)

        _, children = _parent_and_children(sent[0])
        assert len(children) == 2
        assert _attrs(children[0])["tool.name"]["stringValue"] == "shell"
        assert _attrs(children[1])["tool.name"]["stringValue"] == "apply_patch"


# ---------------------------------------------------------------------------
# user_id from state surfaces on the parent span as user.id
# ---------------------------------------------------------------------------


class TestUserIdSurfaces:
    def test_user_id_attribute(self, codex_state):
        sm = codex_state["manager"]
        thread_id = codex_state["thread_id"]
        _seed_session(sm, thread_id)
        sm.set("user_id", "u-42")
        sm.set("turn_start_ms", "1000")
        sm.set("user_prompt", "p")
        sm.set("last_assistant_message", "r")

        sent: list = []
        with mock.patch(
            "tracing.codex.hooks.stop.send_span_to_backend",
            side_effect=lambda p: (sent.append(p), True)[1],
        ):
            finalize_turn(sm, thread_id)

        parent, _ = _parent_and_children(sent[0])
        assert _attrs(parent)["user.id"]["stringValue"] == "u-42"


# ---------------------------------------------------------------------------
# "(No response)" fallback when no assistant message present but other turn
# data IS present (so the turn is still finalized).
# ---------------------------------------------------------------------------


class TestNoResponseFallback:
    def test_output_value_falls_back_when_message_blank(self, codex_state):
        sm = codex_state["manager"]
        thread_id = codex_state["thread_id"]
        _seed_session(sm, thread_id)
        sm.set("turn_start_ms", "1000")
        sm.set("user_prompt", "are you there?")
        # last_assistant_message intentionally absent
        sm.set(
            "pending_token_usage",
            json.dumps({"prompt_tokens": 1, "completion_tokens": 0, "total_tokens": 1, "model": "gpt-5"}),
        )

        sent: list = []
        with mock.patch(
            "tracing.codex.hooks.stop.send_span_to_backend",
            side_effect=lambda p: (sent.append(p), True)[1],
        ):
            finalize_turn(sm, thread_id)

        parent, _ = _parent_and_children(sent[0])
        pa = _attrs(parent)
        assert pa["output.value"]["stringValue"] == "(No response)"
        # llm.output_messages omitted when no real message
        assert "llm.output_messages" not in pa


# ---------------------------------------------------------------------------
# Token model fallback: when state.model missing, model is sourced from the
# pending_token_usage block.
# ---------------------------------------------------------------------------


class TestModelFallbackFromTokens:
    def test_model_pulled_from_token_block(self, codex_state):
        sm = codex_state["manager"]
        thread_id = codex_state["thread_id"]
        # Seed without state-level model; everything else still present.
        sm.set("session_id", thread_id)
        sm.set("project_name", "codex")
        sm.set("trace_count", "0")
        sm.set("turn_start_ms", "1000")
        sm.set("user_prompt", "hi")
        sm.set("last_assistant_message", "ok")
        sm.set(
            "pending_token_usage",
            json.dumps({"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2, "model": "gpt-5-mini"}),
        )

        sent: list = []
        with mock.patch(
            "tracing.codex.hooks.stop.send_span_to_backend",
            side_effect=lambda p: (sent.append(p), True)[1],
        ):
            finalize_turn(sm, thread_id)

        parent, _ = _parent_and_children(sent[0])
        assert _attrs(parent)["llm.model_name"]["stringValue"] == "gpt-5-mini"


# ---------------------------------------------------------------------------
# Build payload uses multi-span shape iff there are children.
# ---------------------------------------------------------------------------


class TestPayloadShape:
    def test_multi_span_when_children_present(self, codex_state):
        sm = codex_state["manager"]
        thread_id = codex_state["thread_id"]
        _seed_session(sm, thread_id)
        sm.set("turn_start_ms", "1000")
        sm.set("user_prompt", "p")
        sm.set("last_assistant_message", "r")
        span_buffer.append_tool_start(thread_id, call_id="c1", tool="shell", args="{}", ts_ms=1100)
        span_buffer.append_tool_end(thread_id, call_id="c1", output="ok", ts_ms=1200)

        sent: list = []
        with mock.patch(
            "tracing.codex.hooks.stop.send_span_to_backend",
            side_effect=lambda p: (sent.append(p), True)[1],
        ):
            finalize_turn(sm, thread_id)

        spans = sent[0]["resourceSpans"][0]["scopeSpans"][0]["spans"]
        # Parent + child share the same trace and parent_span_id linkage
        assert spans[0]["traceId"] == spans[1]["traceId"]
        assert spans[1].get("parentSpanId") == spans[0]["spanId"]
