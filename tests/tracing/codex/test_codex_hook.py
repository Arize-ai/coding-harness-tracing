#!/usr/bin/env python3
"""Tests for tracing.codex.hooks.handlers — the Codex notify hook handler."""

import json
import sys
from unittest import mock

import pytest

from core.common import StateManager
from tracing.codex.hooks.handlers import (
    _as_text,
    _extract_token_counts,
    _extract_user_prompt,
    _find_token_usage,
    _flex_get,
    _handle_notify,
    _send_legacy_single_span,
    _send_span,
    notify,
)


@pytest.fixture(autouse=True)
def _mock_sleep(monkeypatch):
    """Mock time.sleep to prevent real delays while tracking calls."""
    sleep_calls = []
    monkeypatch.setattr("time.sleep", lambda s: sleep_calls.append(s))
    return sleep_calls


@pytest.fixture(autouse=True)
def _enable_logging(monkeypatch):
    """Existing assertions expect raw content; opt in to all logging by default."""
    monkeypatch.setenv("ARIZE_LOG_PROMPTS", "true")
    monkeypatch.setenv("ARIZE_LOG_TOOL_DETAILS", "true")
    monkeypatch.setenv("ARIZE_LOG_TOOL_CONTENT", "true")


@pytest.fixture
def codex_state_dir(tmp_harness_dir, monkeypatch):
    """Ensure ~/.arize/harness/state/codex exists and is what the adapter sees."""
    import core.constants as c
    import tracing.codex.hooks.adapter as adapter

    state_dir = c.STATE_BASE_DIR / "codex"
    state_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(adapter, "STATE_DIR", state_dir)
    return state_dir


# ---------------------------------------------------------------------------
# Event filtering tests
# ---------------------------------------------------------------------------


class TestEventFiltering:

    def test_agent_turn_complete_processed(self, codex_state_dir, monkeypatch):
        """type: agent-turn-complete with no prior hook state sends a single span."""
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")

        sent = []
        with mock.patch("tracing.codex.hooks.handlers._send_span", side_effect=lambda p: sent.append(p)):
            _handle_notify(
                {
                    "type": "agent-turn-complete",
                    "thread-id": "t1",
                    "turn-id": "turn1",
                    "input-messages": [{"role": "user", "content": "hello"}],
                    "last-assistant-message": "world",
                }
            )

        assert len(sent) == 1
        assert "resourceSpans" in sent[0]

    def test_non_agent_turn_ignored(self, monkeypatch):
        """type: session-start is ignored."""
        monkeypatch.setenv("ARIZE_VERBOSE", "true")
        _handle_notify({"type": "session-start"})

    def test_missing_type_ignored(self):
        """Missing type field is ignored."""
        _handle_notify({})


# ---------------------------------------------------------------------------
# _flex_get tests
# ---------------------------------------------------------------------------


class TestFlexGet:

    def test_hyphenated_key(self):
        assert _flex_get({"thread-id": "abc"}, "thread-id", "thread_id", "threadId") == "abc"

    def test_underscored_key(self):
        assert _flex_get({"thread_id": "abc"}, "thread-id", "thread_id", "threadId") == "abc"

    def test_camel_case_key(self):
        assert _flex_get({"threadId": "abc"}, "thread-id", "thread_id", "threadId") == "abc"

    def test_none_returns_default(self):
        assert _flex_get({}, "thread-id", "thread_id", "threadId") == ""

    def test_custom_default(self):
        assert _flex_get({}, "a", "b", default="fallback") == "fallback"

    def test_first_match_wins(self):
        d = {"thread-id": "first", "thread_id": "second"}
        assert _flex_get(d, "thread-id", "thread_id") == "first"

    def test_skips_empty_string(self):
        d = {"thread-id": "", "thread_id": "found"}
        assert _flex_get(d, "thread-id", "thread_id") == "found"

    def test_skips_none_value(self):
        d = {"thread-id": None, "thread_id": "found"}
        assert _flex_get(d, "thread-id", "thread_id") == "found"


# ---------------------------------------------------------------------------
# _as_text tests
# ---------------------------------------------------------------------------


class TestAsText:

    def test_none(self):
        assert _as_text(None) == ""

    def test_string(self):
        assert _as_text("hello") == "hello"

    def test_list(self):
        assert _as_text(["a", "b"]) == "a\nb"

    def test_dict_text_key(self):
        assert _as_text({"text": "hello"}) == "hello"

    def test_dict_content_key(self):
        assert _as_text({"content": "hello"}) == "hello"

    def test_nested_dict(self):
        assert _as_text({"content": {"text": "nested"}}) == "nested"

    def test_dict_fallback_json(self):
        result = _as_text({"foo": "bar"})
        assert "foo" in result
        assert "bar" in result

    def test_number(self):
        assert _as_text(42) == "42"

    def test_nested_list_of_dicts(self):
        data = [{"text": "a"}, {"text": "b"}]
        assert _as_text(data) == "a\nb"

    def test_deeply_nested(self):
        data = {"content": {"message": {"text": "deep"}}}
        assert _as_text(data) == "deep"

    def test_empty_string(self):
        assert _as_text("") == ""

    def test_empty_list(self):
        assert _as_text([]) == ""


# ---------------------------------------------------------------------------
# _extract_user_prompt tests
# ---------------------------------------------------------------------------


class TestExtractUserPrompt:

    def test_list_of_messages_last_user(self):
        messages = [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "response"},
            {"role": "user", "content": "second"},
        ]
        assert _extract_user_prompt(messages) == "second"

    def test_list_of_strings(self):
        assert _extract_user_prompt(["", "hello", "world"]) == "world"

    def test_plain_string(self):
        assert _extract_user_prompt("hello") == "hello"

    def test_empty_list(self):
        assert _extract_user_prompt([]) == ""

    def test_none_input(self):
        assert _extract_user_prompt(None) == ""

    def test_nested_content(self):
        messages = [{"role": "user", "content": {"text": "nested"}}]
        assert _extract_user_prompt(messages) == "nested"

    def test_mixed_types_in_list(self):
        """If no user-role message, falls back to last string."""
        messages = [{"role": "assistant", "content": "skip"}, "fallback"]
        assert _extract_user_prompt(messages) == "fallback"


# ---------------------------------------------------------------------------
# Truncation and empty assistant tests
# ---------------------------------------------------------------------------


class TestTruncationAndDefaults:

    def test_empty_assistant_becomes_no_response(self, codex_state_dir, monkeypatch):
        """Empty assistant output becomes '(No response)'."""
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")

        sent = []
        with mock.patch("tracing.codex.hooks.handlers._send_span", side_effect=lambda p: sent.append(p)):
            _handle_notify(
                {
                    "type": "agent-turn-complete",
                    "thread-id": "t1",
                    "input-messages": "hello",
                    "last-assistant-message": "",
                }
            )

        span = sent[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        attrs = {a["key"]: a["value"] for a in span["attributes"]}
        assert attrs["output.value"]["stringValue"] == "(No response)"


# ---------------------------------------------------------------------------
# Token enrichment tests (from payload)
# ---------------------------------------------------------------------------


class TestFindTokenUsage:

    def test_finds_at_root(self):
        data = {"token_usage": {"prompt_tokens": 10, "completion_tokens": 20}}
        assert _find_token_usage(data) == {"prompt_tokens": 10, "completion_tokens": 20}

    def test_finds_in_last_assistant_message(self):
        data = {"last-assistant-message": {"usage": {"prompt_tokens": 5}}}
        assert _find_token_usage(data) == {"prompt_tokens": 5}

    def test_returns_none_when_absent(self):
        assert _find_token_usage({"type": "agent-turn-complete"}) is None

    def test_finds_hyphenated_key(self):
        data = {"token-usage": {"input_tokens": 42}}
        assert _find_token_usage(data) == {"input_tokens": 42}


class TestExtractTokenCounts:

    def test_standard_keys(self):
        usage = {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}
        counts = _extract_token_counts(usage)
        assert counts == {"prompt": 10, "completion": 20, "total": 30}

    def test_camel_case_keys(self):
        usage = {"inputTokens": 15, "outputTokens": 25}
        counts = _extract_token_counts(usage)
        assert counts["prompt"] == 15
        assert counts["completion"] == 25

    def test_auto_compute_total(self):
        usage = {"prompt_tokens": 10, "completion_tokens": 20}
        counts = _extract_token_counts(usage)
        assert counts["total"] == 30

    def test_string_values_converted(self):
        usage = {"prompt_tokens": "10", "completion_tokens": "20"}
        counts = _extract_token_counts(usage)
        assert counts["prompt"] == 10
        assert counts["completion"] == 20
        assert counts["total"] == 30

    def test_empty_usage(self):
        counts = _extract_token_counts({})
        assert counts == {"prompt": None, "completion": None, "total": None}


# ---------------------------------------------------------------------------
# Hooks-active vs fallback (slim notify path)
# ---------------------------------------------------------------------------


class TestSlimNotify:

    def test_hooks_active_stages_state_and_skips_span(self, codex_state_dir, monkeypatch):
        """When hooks have populated state, notify only stages data — no span sent."""
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")

        # Pre-populate state to mark the session as hooks-active.
        state_file = codex_state_dir / "state_t-hooks.yaml"
        lock_path = codex_state_dir / ".lock_t-hooks"
        sm = StateManager(state_dir=codex_state_dir, state_file=state_file, lock_path=lock_path)
        sm.init_state()
        sm.set("session_id", "t-hooks")
        sm.set("project_name", "codex")
        sm.set("trace_count", "1")
        sm.set("model", "gpt-5")

        sent = []
        with mock.patch("tracing.codex.hooks.handlers.send_span_to_backend", side_effect=lambda p: sent.append(p)):
            _handle_notify(
                {
                    "type": "agent-turn-complete",
                    "thread-id": "t-hooks",
                    "turn-id": "turn-1",
                    "input-messages": [{"role": "user", "content": "hi"}],
                    "last-assistant-message": "hello back",
                    "token_usage": {"prompt_tokens": 11, "completion_tokens": 22, "total_tokens": 33, "model": "gpt-5"},
                }
            )

        # Slim path: state has pending_token_usage (JSON-encoded — Stop must
        # json.loads it) and last_assistant_message, but no span was sent.
        sm_read = StateManager(state_dir=codex_state_dir, state_file=state_file)
        pending = json.loads(sm_read.get("pending_token_usage"))
        assert pending["prompt_tokens"] == 11
        assert pending["completion_tokens"] == 22
        assert pending["total_tokens"] == 33
        assert pending["model"] == "gpt-5"
        assert "hello back" in sm_read.get("last_assistant_message")
        assert sent == []

    def test_fallback_sends_single_span_when_hooks_inactive(self, codex_state_dir, monkeypatch):
        """When hooks haven't run, notify falls back to a single LLM span."""
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")

        sent = []
        with mock.patch("tracing.codex.hooks.handlers.send_span_to_backend", side_effect=lambda p: sent.append(p)):
            _handle_notify(
                {
                    "type": "agent-turn-complete",
                    "thread-id": "t-fallback",
                    "turn-id": "turn-1",
                    "input-messages": [{"role": "user", "content": "hi"}],
                    "last-assistant-message": "hello back",
                    "token_usage": {"prompt_tokens": 11, "completion_tokens": 22, "total_tokens": 33},
                }
            )

        assert len(sent) == 1
        spans = sent[0]["resourceSpans"][0]["scopeSpans"][0]["spans"]
        assert len(spans) == 1  # single span, not multi
        attrs = {a["key"]: a["value"] for a in spans[0]["attributes"]}
        assert attrs["llm.token_count.prompt"]["intValue"] == 11
        assert attrs["llm.token_count.completion"]["intValue"] == 22
        assert attrs["llm.token_count.total"]["intValue"] == 33

    def test_hooks_active_via_turn_start_ms(self, codex_state_dir, monkeypatch):
        """turn_start_ms alone is also a hooks-active signal (UserPromptSubmit ran)."""
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")

        state_file = codex_state_dir / "state_t-turn.yaml"
        lock_path = codex_state_dir / ".lock_t-turn"
        sm = StateManager(state_dir=codex_state_dir, state_file=state_file, lock_path=lock_path)
        sm.init_state()
        sm.set("session_id", "t-turn")
        sm.set("project_name", "codex")
        sm.set("trace_count", "0")
        sm.set("turn_start_ms", "1716203456789")
        # No `model` set — only the turn_start_ms branch of hooks_active matters.

        sent = []
        with mock.patch("tracing.codex.hooks.handlers.send_span_to_backend", side_effect=lambda p: sent.append(p)):
            _handle_notify(
                {
                    "type": "agent-turn-complete",
                    "thread-id": "t-turn",
                    "input-messages": "hi",
                    "last-assistant-message": "yo",
                    "token_usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
                }
            )

        assert sent == []  # no span sent, Stop will handle it

    def test_hooks_active_stores_pending_usage_as_json(self, codex_state_dir, monkeypatch):
        """pending_token_usage is serialized as JSON in state."""
        import json as _json

        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")

        state_file = codex_state_dir / "state_t-json.yaml"
        lock_path = codex_state_dir / ".lock_t-json"
        sm = StateManager(state_dir=codex_state_dir, state_file=state_file, lock_path=lock_path)
        sm.init_state()
        sm.set("session_id", "t-json")
        sm.set("project_name", "codex")
        sm.set("trace_count", "0")
        sm.set("model", "gpt-5")

        with mock.patch("tracing.codex.hooks.handlers.send_span_to_backend"):
            _handle_notify(
                {
                    "type": "agent-turn-complete",
                    "thread-id": "t-json",
                    "input-messages": "hi",
                    "last-assistant-message": "yo",
                    "token_usage": {
                        "prompt_tokens": 11,
                        "completion_tokens": 22,
                        "total_tokens": 33,
                        "model": "gpt-5",
                    },
                }
            )

        sm_read = StateManager(state_dir=codex_state_dir, state_file=state_file)
        raw = sm_read.get("pending_token_usage")
        parsed = _json.loads(raw)
        assert parsed["prompt_tokens"] == 11
        assert parsed["completion_tokens"] == 22
        assert parsed["total_tokens"] == 33
        assert parsed["model"] == "gpt-5"

    def test_fallback_with_no_token_usage(self, codex_state_dir, monkeypatch):
        """Fallback still sends a span when there's no token_usage in the payload."""
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")

        sent = []
        with mock.patch("tracing.codex.hooks.handlers.send_span_to_backend", side_effect=lambda p: sent.append(p)):
            _handle_notify(
                {
                    "type": "agent-turn-complete",
                    "thread-id": "t-no-tokens",
                    "input-messages": "hi",
                    "last-assistant-message": "yo",
                }
            )

        assert len(sent) == 1
        spans = sent[0]["resourceSpans"][0]["scopeSpans"][0]["spans"]
        assert len(spans) == 1
        attr_keys = {a["key"] for a in spans[0]["attributes"]}
        # No token attrs when usage absent — but span still built.
        assert "llm.token_count.prompt" not in attr_keys
        assert "openinference.span.kind" in attr_keys


# ---------------------------------------------------------------------------
# Span sending tests
# ---------------------------------------------------------------------------


class TestSendSpan:

    def test_send_span_delegates_to_backend_sender(self):
        """Codex hook sends completed spans via core.common.send_span()."""
        payload = {
            "resourceSpans": [
                {
                    "resource": {"attributes": []},
                    "scopeSpans": [{"scope": {"name": "test"}, "spans": [{"name": "test-span"}]}],
                }
            ]
        }

        with mock.patch("tracing.codex.hooks.handlers.send_span_to_backend", return_value=True) as mock_send:
            _send_span(payload)

        mock_send.assert_called_once_with(payload)

    def test_send_span_logs_error_when_backend_send_fails(self, capsys):
        """Backend send failures are surfaced as Codex hook errors."""
        payload = {
            "resourceSpans": [
                {
                    "resource": {"attributes": []},
                    "scopeSpans": [{"scope": {"name": "test"}, "spans": [{"name": "test-span"}]}],
                }
            ]
        }

        with mock.patch("tracing.codex.hooks.handlers.send_span_to_backend", return_value=False):
            _send_span(payload)

        assert "Failed to send span to backend" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# _send_legacy_single_span direct tests
# ---------------------------------------------------------------------------


class TestSendLegacySingleSpan:

    def test_builds_single_span_with_token_counts(self, codex_state_dir):
        """Direct call to _send_legacy_single_span builds a single LLM span."""
        state_file = codex_state_dir / "state_t-legacy.yaml"
        lock_path = codex_state_dir / ".lock_t-legacy"
        sm = StateManager(state_dir=codex_state_dir, state_file=state_file, lock_path=lock_path)
        sm.init_state()
        sm.set("session_id", "t-legacy")
        sm.set("project_name", "codex")
        sm.set("trace_count", "0")

        sent = []
        with mock.patch("tracing.codex.hooks.handlers.send_span_to_backend", side_effect=lambda p: sent.append(p)):
            _send_legacy_single_span(
                sm,
                thread_id="t-legacy",
                turn_id="turn-1",
                user_prompt="hi",
                assistant_output="hello",
                usage={"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
            )

        assert len(sent) == 1
        spans = sent[0]["resourceSpans"][0]["scopeSpans"][0]["spans"]
        assert len(spans) == 1
        attrs = {a["key"]: a["value"] for a in spans[0]["attributes"]}
        assert attrs["openinference.span.kind"]["stringValue"] == "LLM"
        assert attrs["input.value"]["stringValue"] == "hi"
        assert attrs["output.value"]["stringValue"] == "hello"
        assert attrs["llm.token_count.total"]["intValue"] == 3


# ---------------------------------------------------------------------------
# Error handling tests
# ---------------------------------------------------------------------------


class TestErrorHandling:

    def test_exception_in_handle_notify_caught(self, monkeypatch, capsys):
        """Exception in _handle_notify is caught by notify()."""
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")

        with (
            mock.patch("tracing.codex.hooks.handlers._handle_notify", side_effect=RuntimeError("boom")),
            mock.patch.object(sys, "argv", ["hook", '{"type":"agent-turn-complete"}']),
        ):
            notify()

        captured = capsys.readouterr()
        assert "boom" in captured.err

    def test_invalid_json_handled(self, monkeypatch, capsys):
        """Invalid JSON in argv is handled gracefully."""
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")

        with mock.patch.object(sys, "argv", ["hook", "not-json"]):
            notify()

        captured = capsys.readouterr()
        assert "codex notify hook failed" in captured.err

    def test_no_argv_uses_empty_json(self, monkeypatch):
        """No sys.argv[1] defaults to empty JSON."""
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")

        with mock.patch.object(sys, "argv", ["hook"]):
            notify()
