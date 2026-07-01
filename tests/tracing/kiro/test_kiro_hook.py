"""Tests for tracing.kiro.hooks.handlers — Kiro CLI hook dispatcher and event handlers.

Tests cover:
- agentSpawn: session initialization, no span emitted
- userPromptSubmit: deferred turn state, raw prompt stored (not redacted)
- preToolUse / postToolUse: TOOL span emission, redaction, orphan handling
- stop: LLM span emission, redaction, sidecar enrichment, fail-soft behavior
- main(): stdin parsing, unknown events, error swallowing, trace-disabled
- Integration: full session flow exercising all five events
"""

from __future__ import annotations

import json
import sys
from io import StringIO
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from tracing.kiro.hooks import adapter

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

FIXTURE_DIR = Path(__file__).parent / "fixtures"
PROBE_DIR = FIXTURE_DIR / "probe_payloads"
SIDECAR_DIR = FIXTURE_DIR / "sidecars"

SESSION_1 = "00000000-0000-0000-0000-000000000001"
SESSION_2 = "00000000-0000-0000-0000-000000000002"


def _load_fixture(name: str) -> dict:
    return json.loads((PROBE_DIR / name).read_text())


def _span_obj(span_dict: dict) -> dict:
    """Extract the inner span object from an OTLP build_span result."""
    return span_dict["resourceSpans"][0]["scopeSpans"][0]["spans"][0]


def _span_attrs(span_dict: dict) -> dict[str, Any]:
    """Extract a flat {key: value} map from OTLP span attributes.

    Collapses {"stringValue": x} → x, {"intValue": x} → x, etc.
    """
    raw = _span_obj(span_dict).get("attributes", [])
    out: dict[str, Any] = {}
    for a in raw:
        val = a["value"]
        # OTLP value is a dict with one key like stringValue, intValue, etc.
        for v in val.values():
            out[a["key"]] = v
            break
    return out


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _patch_kiro_state(tmp_path, monkeypatch):
    """Redirect adapter STATE_DIR and KIRO_SESSIONS_DIR to temp."""
    state_dir = tmp_path / "state" / "kiro"
    state_dir.mkdir(parents=True)
    monkeypatch.setattr(adapter, "STATE_DIR", state_dir)

    sessions_dir = tmp_path / "sessions" / "cli"
    sessions_dir.mkdir(parents=True)
    monkeypatch.setattr(adapter, "KIRO_SESSIONS_DIR", sessions_dir)
    return state_dir


@pytest.fixture(autouse=True)
def _disable_env_vars(monkeypatch):
    """Clear env vars that could influence session resolution."""
    monkeypatch.delenv("KIRO_SESSION_ID", raising=False)
    monkeypatch.delenv("KIRO_AGENT_PATH", raising=False)
    monkeypatch.delenv("ARIZE_PROJECT_NAME", raising=False)
    monkeypatch.delenv("ARIZE_USER_ID", raising=False)
    monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
    monkeypatch.setenv("ARIZE_LOG_PROMPTS", "true")
    monkeypatch.setenv("ARIZE_LOG_TOOL_CONTENT", "true")


@pytest.fixture
def captured_spans():
    """Mock send_span and collect all span payloads."""
    sent: list[dict] = []
    with mock.patch(
        "tracing.kiro.hooks.handlers.send_span",
        side_effect=lambda s: sent.append(s),
    ):
        yield sent


def _invoke_main(input_json: dict) -> int:
    """Call handlers.main() with input_json piped to stdin."""
    from tracing.kiro.hooks.handlers import main

    with mock.patch.object(sys, "stdin", StringIO(json.dumps(input_json))):
        return main()


def _invoke_handler(handler_name: str, input_json: dict, state):
    """Call a specific _handle_* function directly."""
    from tracing.kiro.hooks import handlers

    fn = getattr(handlers, handler_name)
    fn(input_json, state)


def _make_state(tmp_path) -> Any:
    """Create a StateManager pointing at tmp_path."""
    from core.common import StateManager

    state_dir = tmp_path / "state" / "kiro"
    state_dir.mkdir(parents=True, exist_ok=True)
    sm = StateManager(state_dir=state_dir)
    sm.init_state()
    return sm


# ---------------------------------------------------------------------------
# TestAgentSpawn
# ---------------------------------------------------------------------------


class TestAgentSpawn:
    def test_initializes_session_no_span(self, captured_spans, tmp_path):
        """agentSpawn creates state with session_id but emits no span."""
        payload = _load_fixture("agent_spawn.json")
        _invoke_main(payload)

        assert len(captured_spans) == 0

        # Verify state file was created
        state_dir = tmp_path / "state" / "kiro"
        state_files = list(state_dir.glob("state_*.json"))
        assert len(state_files) >= 1


# ---------------------------------------------------------------------------
# TestUserPromptSubmit
# ---------------------------------------------------------------------------


class TestUserPromptSubmit:
    def test_saves_pending_turn_no_span(self, captured_spans, tmp_path):
        """userPromptSubmit stores turn state but emits no span."""
        payload = _load_fixture("user_prompt_submit.json")
        _invoke_main(payload)

        assert len(captured_spans) == 0

        # Check that state has the expected keys
        state_dir = tmp_path / "state" / "kiro"
        state_files = list(state_dir.glob(f"state_{SESSION_1}.json"))
        assert len(state_files) == 1

        from core.common import StateManager

        sm = StateManager(
            state_dir=state_dir,
            state_file=state_files[0],
            lock_path=state_dir / f".lock_{SESSION_1}",
        )
        assert sm.get("pending_turn_trace_id") is not None
        assert len(sm.get("pending_turn_trace_id")) == 32
        assert sm.get("pending_turn_span_id") is not None
        assert len(sm.get("pending_turn_span_id")) == 16
        assert sm.get("pending_turn_start_ms") is not None
        assert int(sm.get("pending_turn_start_ms")) > 0
        assert sm.get("pending_turn_prompt") == "what's the current time and date?"

    def test_increments_trace_count(self, captured_spans, tmp_path):
        """trace_count goes from 1 after first call to 2 after second."""
        payload = _load_fixture("user_prompt_submit.json")
        _invoke_main(payload)

        state_dir = tmp_path / "state" / "kiro"
        from core.common import StateManager

        state_file = state_dir / f"state_{SESSION_1}.json"
        sm = StateManager(state_dir=state_dir, state_file=state_file, lock_path=state_dir / f".lock_{SESSION_1}")
        assert sm.get("trace_count") == "1"

        # Second call
        with mock.patch.object(sys, "stdin", StringIO(json.dumps(payload))):
            from tracing.kiro.hooks.handlers import main

            main()

        sm2 = StateManager(state_dir=state_dir, state_file=state_file, lock_path=state_dir / f".lock_{SESSION_1}")
        assert sm2.get("trace_count") == "2"

    def test_stores_raw_prompt_not_redacted(self, captured_spans, tmp_path, monkeypatch):
        """Even with ARIZE_LOG_PROMPTS=false, the raw prompt is stored in state.

        Redaction happens at span emission time (in stop), not at storage time.
        """
        monkeypatch.setenv("ARIZE_LOG_PROMPTS", "false")
        payload = _load_fixture("user_prompt_submit.json")
        _invoke_main(payload)

        state_dir = tmp_path / "state" / "kiro"
        from core.common import StateManager

        state_file = state_dir / f"state_{SESSION_1}.json"
        sm = StateManager(state_dir=state_dir, state_file=state_file, lock_path=state_dir / f".lock_{SESSION_1}")
        stored_prompt = sm.get("pending_turn_prompt")
        assert stored_prompt == "what's the current time and date?"
        assert not stored_prompt.startswith("<redacted")


# ---------------------------------------------------------------------------
# TestToolFlow
# ---------------------------------------------------------------------------


class TestToolFlow:
    def test_pre_then_post_emits_one_tool_span(self, captured_spans):
        """preToolUse + postToolUse emits exactly 1 TOOL span."""
        pre = _load_fixture("pre_tool_use.json")
        post = _load_fixture("post_tool_use.json")
        _invoke_main(pre)
        _invoke_main(post)

        assert len(captured_spans) == 1
        attrs = _span_attrs(captured_spans[0])
        assert attrs["openinference.span.kind"] == "TOOL"
        assert attrs["tool.name"] == "read"
        assert attrs.get("tool.description") == "Read the contents of install.sh"

    def test_tool_span_carries_input_and_output(self, captured_spans):
        """TOOL span has JSON-serialized tool_input and tool_response."""
        pre = _load_fixture("pre_tool_use.json")
        post = _load_fixture("post_tool_use.json")
        _invoke_main(pre)
        _invoke_main(post)

        assert len(captured_spans) == 1
        attrs = _span_attrs(captured_spans[0])

        input_val = json.loads(attrs["input.value"])
        assert input_val["operations"][0]["path"] == "install.sh"

        output_val = json.loads(attrs["output.value"])
        assert "items" in output_val

    def test_log_tool_content_false_redacts(self, captured_spans, monkeypatch):
        """With ARIZE_LOG_TOOL_CONTENT=false, input.value and output.value are redacted."""
        monkeypatch.setenv("ARIZE_LOG_TOOL_CONTENT", "false")
        pre = _load_fixture("pre_tool_use.json")
        post = _load_fixture("post_tool_use.json")
        _invoke_main(pre)
        _invoke_main(post)

        assert len(captured_spans) == 1
        attrs = _span_attrs(captured_spans[0])
        assert attrs["input.value"].startswith("<redacted (")
        assert attrs["output.value"].startswith("<redacted (")

    def test_post_without_pre_emits_orphan_span(self, captured_spans):
        """postToolUse without matching preToolUse still emits a TOOL span."""
        post = _load_fixture("post_tool_use.json")
        _invoke_main(post)

        assert len(captured_spans) == 1
        attrs = _span_attrs(captured_spans[0])
        assert attrs["openinference.span.kind"] == "TOOL"
        # Orphan span has no parent (no pending turn)
        span_obj = _span_obj(captured_spans[0])
        assert "parentSpanId" not in span_obj or span_obj.get("parentSpanId") == ""

    def test_tool_span_parented_to_turn(self, captured_spans, tmp_path):
        """TOOL span's parent_span_id matches the pending turn's span_id."""
        prompt = _load_fixture("user_prompt_submit.json")
        # Use session 2 for tool events to match session 1
        pre = _load_fixture("pre_tool_use.json")
        post = _load_fixture("post_tool_use.json")

        # Override session_id in pre/post to match the prompt session
        pre["session_id"] = SESSION_1
        post["session_id"] = SESSION_1

        _invoke_main(prompt)
        _invoke_main(pre)
        _invoke_main(post)

        assert len(captured_spans) == 1
        tool_span = _span_obj(captured_spans[0])

        # Read the pending turn span_id from state
        state_dir = tmp_path / "state" / "kiro"
        from core.common import StateManager

        state_file = state_dir / f"state_{SESSION_1}.json"
        sm = StateManager(state_dir=state_dir, state_file=state_file, lock_path=state_dir / f".lock_{SESSION_1}")
        turn_span_id = sm.get("pending_turn_span_id")

        assert tool_span.get("parentSpanId") == turn_span_id


# ---------------------------------------------------------------------------
# TestStop
# ---------------------------------------------------------------------------


class TestStop:
    def test_emits_llm_span_with_assistant_response(self, captured_spans):
        """userPromptSubmit → stop emits 1 LLM span with assistant_response."""
        prompt = _load_fixture("user_prompt_submit.json")
        stop = _load_fixture("stop.json")
        _invoke_main(prompt)
        _invoke_main(stop)

        assert len(captured_spans) == 1
        attrs = _span_attrs(captured_spans[0])
        assert attrs["openinference.span.kind"] == "LLM"
        assert "Friday, May 8, 2026" in attrs["output.value"]

    def test_session_id_is_kiro_uuid(self, captured_spans):
        """session.id attribute is the Kiro payload UUID, not a fresh trace_id."""
        prompt = _load_fixture("user_prompt_submit.json")
        stop = _load_fixture("stop.json")
        _invoke_main(prompt)
        _invoke_main(stop)

        assert len(captured_spans) == 1
        attrs = _span_attrs(captured_spans[0])
        assert attrs["session.id"] == SESSION_1

    def test_log_prompts_false_redacts_both_input_and_output(self, captured_spans, monkeypatch):
        """With ARIZE_LOG_PROMPTS=false, both input.value and output.value are redacted."""
        monkeypatch.setenv("ARIZE_LOG_PROMPTS", "false")
        prompt = _load_fixture("user_prompt_submit.json")
        stop = _load_fixture("stop.json")
        _invoke_main(prompt)
        _invoke_main(stop)

        assert len(captured_spans) == 1
        attrs = _span_attrs(captured_spans[0])
        assert attrs["input.value"].startswith("<redacted (")
        assert attrs["output.value"].startswith("<redacted (")

    def test_llm_output_messages_carries_redacted_output(self, captured_spans, monkeypatch):
        """llm.output_messages JSON contains the same redacted text as output.value."""
        monkeypatch.setenv("ARIZE_LOG_PROMPTS", "false")
        prompt = _load_fixture("user_prompt_submit.json")
        stop = _load_fixture("stop.json")
        _invoke_main(prompt)
        _invoke_main(stop)

        attrs = _span_attrs(captured_spans[0])
        messages = json.loads(attrs["llm.output_messages"])
        assert isinstance(messages, list)
        assert len(messages) >= 1
        msg_content = messages[0]["message.content"]
        assert msg_content.startswith("<redacted (")
        # Should match the output.value redaction
        assert msg_content == attrs["output.value"]

    def test_stop_without_user_prompt_submit_emits_best_effort(self, captured_spans):
        """stop without prior userPromptSubmit still emits a basic LLM span."""
        stop = _load_fixture("stop.json")
        _invoke_main(stop)

        assert len(captured_spans) == 1
        attrs = _span_attrs(captured_spans[0])
        assert attrs["openinference.span.kind"] == "LLM"
        # input.value should be empty since no prompt was stored
        assert attrs["input.value"] == ""
        # output.value still has the assistant response
        assert "Friday, May 8, 2026" in attrs["output.value"]

    def test_clears_pending_turn_keys(self, captured_spans, tmp_path):
        """After stop, pending_turn_* keys are cleared (empty strings)."""
        prompt = _load_fixture("user_prompt_submit.json")
        stop = _load_fixture("stop.json")
        _invoke_main(prompt)
        _invoke_main(stop)

        state_dir = tmp_path / "state" / "kiro"
        from core.common import StateManager

        state_file = state_dir / f"state_{SESSION_1}.json"
        sm = StateManager(state_dir=state_dir, state_file=state_file, lock_path=state_dir / f".lock_{SESSION_1}")
        for key in (
            "pending_turn_trace_id",
            "pending_turn_span_id",
            "pending_turn_start_ms",
            "pending_turn_prompt",
        ):
            val = sm.get(key)
            assert val == "" or val is None, f"{key} should be cleared but was {val!r}"


# ---------------------------------------------------------------------------
# TestStopSidecarEnrichment
# ---------------------------------------------------------------------------


class TestStopSidecarEnrichment:
    """Tests that stop handler enriches the LLM span with sidecar data."""

    def _place_sidecar(self, tmp_path, data: dict, session_id: str = SESSION_1):
        """Write a sidecar JSON file to the mocked KIRO_SESSIONS_DIR."""
        sessions_dir = tmp_path / "sessions" / "cli"
        sessions_dir.mkdir(parents=True, exist_ok=True)
        path = sessions_dir / f"{session_id}.json"
        path.write_text(json.dumps(data))

    def _load_sidecar_fixture(self, name: str) -> dict:
        return json.loads((SIDECAR_DIR / name).read_text())

    def test_enriches_with_model_and_tokens(self, captured_spans, tmp_path):
        """Sidecar with nonzero tokens populates llm.model_name and token counts."""
        sidecar = self._load_sidecar_fixture("session_complete.json")
        self._place_sidecar(tmp_path, sidecar)

        prompt = _load_fixture("user_prompt_submit.json")
        stop = _load_fixture("stop.json")
        _invoke_main(prompt)
        _invoke_main(stop)

        assert len(captured_spans) == 1
        attrs = _span_attrs(captured_spans[0])
        assert attrs["llm.model_name"] == "claude-sonnet-4"
        assert attrs["llm.token_count.prompt"] == 1234
        assert attrs["llm.token_count.completion"] == 567
        assert attrs["llm.token_count.total"] == 1801

    def test_enriches_with_cost_and_metering(self, captured_spans, tmp_path):
        """Sidecar metering_usage populates kiro.cost.credits and kiro.metering_usage."""
        sidecar = self._load_sidecar_fixture("session_complete.json")
        self._place_sidecar(tmp_path, sidecar)

        prompt = _load_fixture("user_prompt_submit.json")
        stop = _load_fixture("stop.json")
        _invoke_main(prompt)
        _invoke_main(stop)

        attrs = _span_attrs(captured_spans[0])
        assert attrs["kiro.cost.credits"] == pytest.approx(0.103, abs=1e-9)
        metering = json.loads(attrs["kiro.metering_usage"])
        assert isinstance(metering, list)
        assert len(metering) == 2

    def test_enriches_with_turn_duration_and_agent_name(self, captured_spans, tmp_path):
        """Sidecar provides turn_duration_ms, agent_name, context_usage_percentage."""
        sidecar = self._load_sidecar_fixture("session_complete.json")
        self._place_sidecar(tmp_path, sidecar)

        prompt = _load_fixture("user_prompt_submit.json")
        stop = _load_fixture("stop.json")
        _invoke_main(prompt)
        _invoke_main(stop)

        attrs = _span_attrs(captured_spans[0])
        assert attrs["kiro.turn_duration_ms"] == 10151
        assert attrs["kiro.agent_name"] == "arize-traced"
        assert attrs["kiro.context_usage_percentage"] == 4.7

    def test_no_sidecar_no_enrichment(self, captured_spans, tmp_path):
        """Without a sidecar file, LLM span has basic attrs but no enrichment."""
        prompt = _load_fixture("user_prompt_submit.json")
        stop = _load_fixture("stop.json")
        _invoke_main(prompt)
        _invoke_main(stop)

        assert len(captured_spans) == 1
        attrs = _span_attrs(captured_spans[0])
        # Basic attrs present
        assert "session.id" in attrs
        assert "openinference.span.kind" in attrs
        # Enrichment attrs absent
        for key in (
            "llm.model_name",
            "llm.token_count.prompt",
            "llm.token_count.completion",
            "llm.token_count.total",
            "kiro.cost.credits",
            "kiro.metering_usage",
            "kiro.turn_duration_ms",
            "kiro.agent_name",
            "kiro.context_usage_percentage",
        ):
            assert key not in attrs, f"{key} should not be present without sidecar"

    def test_no_turns_yields_session_level_only(self, captured_spans, tmp_path):
        """Sidecar with empty user_turn_metadatas: session-level fields only."""
        sidecar = self._load_sidecar_fixture("session_no_turns.json")
        self._place_sidecar(tmp_path, sidecar)

        prompt = _load_fixture("user_prompt_submit.json")
        stop = _load_fixture("stop.json")
        _invoke_main(prompt)
        _invoke_main(stop)

        attrs = _span_attrs(captured_spans[0])
        assert attrs["llm.model_name"] == "auto"
        assert attrs["kiro.agent_name"] == "arize-traced"
        assert attrs["kiro.context_usage_percentage"] == 0.0
        # Turn-level attrs absent
        for key in (
            "llm.token_count.prompt",
            "llm.token_count.completion",
            "llm.token_count.total",
            "kiro.cost.credits",
            "kiro.metering_usage",
            "kiro.turn_duration_ms",
        ):
            assert key not in attrs, f"{key} should not be present without turns"

    def test_zero_tokens_omitted_from_span(self, captured_spans, tmp_path):
        """Token counts of 0 are treated as unknown and omitted."""
        sidecar = self._load_sidecar_fixture("session_complete.json")
        # Override token counts to 0
        turn = sidecar["session_state"]["conversation_metadata"]["user_turn_metadatas"][0]
        turn["input_token_count"] = 0
        turn["output_token_count"] = 0
        self._place_sidecar(tmp_path, sidecar)

        prompt = _load_fixture("user_prompt_submit.json")
        stop = _load_fixture("stop.json")
        _invoke_main(prompt)
        _invoke_main(stop)

        attrs = _span_attrs(captured_spans[0])
        assert "llm.token_count.prompt" not in attrs
        assert "llm.token_count.completion" not in attrs
        assert "llm.token_count.total" not in attrs
        # Cost still attached because metering_usage has nonzero values
        assert attrs["kiro.cost.credits"] == pytest.approx(0.103, abs=1e-9)

    def test_uses_correct_turn_index(self, captured_spans, tmp_path):
        """With multiple turns, stop uses trace_count-1 as the turn index."""
        sidecar = self._load_sidecar_fixture("session_complete.json")
        # Build 3 turns with distinct token counts
        import copy

        base_turn = sidecar["session_state"]["conversation_metadata"]["user_turn_metadatas"][0]
        turns = []
        for i, (inp, out) in enumerate([(100, 200), (300, 400), (500, 600)]):
            t = copy.deepcopy(base_turn)
            t["input_token_count"] = inp
            t["output_token_count"] = out
            turns.append(t)
        sidecar["session_state"]["conversation_metadata"]["user_turn_metadatas"] = turns
        self._place_sidecar(tmp_path, sidecar)

        # Submit twice to get trace_count=2, then stop
        prompt = _load_fixture("user_prompt_submit.json")
        _invoke_main(prompt)  # trace_count becomes 1
        # We need a stop to clear pending keys, then a second prompt
        stop1 = _load_fixture("stop.json")
        _invoke_main(stop1)  # emits span for turn 1

        _invoke_main(prompt)  # trace_count becomes 2
        stop2 = _load_fixture("stop.json")
        _invoke_main(stop2)  # emits span for turn 2

        # The second stop should use turn_index=1 (trace_count=2, 0-indexed=1)
        assert len(captured_spans) == 2
        attrs = _span_attrs(captured_spans[1])
        assert attrs["llm.token_count.prompt"] == 300
        assert attrs["llm.token_count.completion"] == 400

    def test_malformed_sidecar_logs_and_skips(self, captured_spans, tmp_path):
        """Malformed sidecar JSON does not crash; basic LLM span still emitted."""
        sessions_dir = tmp_path / "sessions" / "cli"
        sessions_dir.mkdir(parents=True, exist_ok=True)
        (sessions_dir / f"{SESSION_1}.json").write_text("not json")

        prompt = _load_fixture("user_prompt_submit.json")
        stop = _load_fixture("stop.json")
        _invoke_main(prompt)
        _invoke_main(stop)

        assert len(captured_spans) == 1
        attrs = _span_attrs(captured_spans[0])
        assert attrs["openinference.span.kind"] == "LLM"
        # No enrichment attrs
        assert "llm.model_name" not in attrs
        assert "kiro.cost.credits" not in attrs


# ---------------------------------------------------------------------------
# TestMain
# ---------------------------------------------------------------------------


class TestMain:
    def test_unknown_event_no_crash_no_span(self, captured_spans):
        """Unknown hook_event_name returns 0, no spans emitted."""
        payload = {
            "hook_event_name": "unknown",
            "session_id": SESSION_1,
            "cwd": "/tmp",
        }
        rc = _invoke_main(payload)
        assert rc == 0
        assert len(captured_spans) == 0

    def test_malformed_stdin_returns_zero(self, captured_spans):
        """Non-JSON stdin causes main() to return 0 without crash."""
        from tracing.kiro.hooks.handlers import main

        with mock.patch.object(sys, "stdin", StringIO("not json")):
            rc = main()
        assert rc == 0
        assert len(captured_spans) == 0

    def test_handler_exception_does_not_propagate(self, captured_spans):
        """If a handler raises, main() still returns 0."""
        from tracing.kiro.hooks import handlers

        with mock.patch.dict(handlers._DISPATCH, {"stop": mock.Mock(side_effect=RuntimeError("boom"))}):
            stop = _load_fixture("stop.json")
            rc = _invoke_main(stop)
        assert rc == 0

    def test_trace_disabled_returns_early(self, captured_spans, tmp_path, monkeypatch):
        """With ARIZE_TRACE_ENABLED=false, main() returns 0, no state created."""
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "false")
        payload = _load_fixture("agent_spawn.json")
        rc = _invoke_main(payload)
        assert rc == 0
        assert len(captured_spans) == 0

        # No state files should have been created
        state_dir = tmp_path / "state" / "kiro"
        state_files = list(state_dir.glob("state_*.json"))
        assert len(state_files) == 0


# ---------------------------------------------------------------------------
# TestIntegration
# ---------------------------------------------------------------------------


class TestIntegration:
    """Full session flow — exercises all five events end-to-end."""

    def test_full_session_emits_one_llm_and_one_tool_span(self, captured_spans, tmp_path):
        """agentSpawn → userPromptSubmit → preToolUse → postToolUse → stop
        emits exactly 1 TOOL span and 1 LLM span.
        """
        spawn = _load_fixture("agent_spawn.json")
        prompt = _load_fixture("user_prompt_submit.json")
        pre = _load_fixture("pre_tool_use.json")
        post = _load_fixture("post_tool_use.json")
        stop = _load_fixture("stop.json")

        # Normalize all to session 1
        for p in (spawn, prompt, pre, post, stop):
            p["session_id"] = SESSION_1

        for payload in (spawn, prompt, pre, post, stop):
            _invoke_main(payload)

        assert len(captured_spans) == 2

        # First emitted should be the TOOL span (from postToolUse)
        tool_attrs = _span_attrs(captured_spans[0])
        assert tool_attrs["openinference.span.kind"] == "TOOL"
        assert tool_attrs["tool.name"] == "read"
        assert tool_attrs["session.id"] == SESSION_1

        # Second emitted should be the LLM span (from stop)
        llm_attrs = _span_attrs(captured_spans[1])
        assert llm_attrs["openinference.span.kind"] == "LLM"
        assert llm_attrs["session.id"] == SESSION_1
        assert "Friday, May 8, 2026" in llm_attrs["output.value"]

        # TOOL span should be parented to the LLM span
        tool_span = _span_obj(captured_spans[0])
        llm_span = _span_obj(captured_spans[1])
        assert tool_span.get("parentSpanId") == llm_span["spanId"]

        # Both share the same trace_id
        assert tool_span["traceId"] == llm_span["traceId"]
