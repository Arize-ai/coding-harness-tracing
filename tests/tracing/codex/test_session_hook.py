#!/usr/bin/env python3
"""Tests for tracing.codex.hooks.session — SessionStart + UserPromptSubmit hook handler."""

import io
import json
import sys
import types
from unittest import mock

import pytest

from tracing.codex.hooks import adapter
from tracing.codex.hooks import session as session_mod


@pytest.fixture(autouse=True)
def _mock_sleep(monkeypatch):
    """Mock time.sleep to prevent real delays while tracking calls."""
    sleep_calls = []
    monkeypatch.setattr("time.sleep", lambda s: sleep_calls.append(s))
    return sleep_calls


@pytest.fixture(autouse=True)
def _enable_logging(monkeypatch):
    """Opt in to all content logging by default; tests can override."""
    monkeypatch.setenv("ARIZE_LOG_PROMPTS", "true")
    monkeypatch.setenv("ARIZE_LOG_TOOL_DETAILS", "true")
    monkeypatch.setenv("ARIZE_LOG_TOOL_CONTENT", "true")


@pytest.fixture(autouse=True)
def _skip_env_file(monkeypatch):
    """Prevent ``load_env_file`` from clobbering test env vars with values from
    the developer's real ``~/.codex/arize-env.sh``."""
    monkeypatch.setattr(session_mod, "load_env_file", lambda _path: None)


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    """Redirect adapter.STATE_DIR to a temp directory."""
    monkeypatch.setattr(adapter, "STATE_DIR", tmp_path)
    return tmp_path


@pytest.fixture
def finalize_mock(monkeypatch):
    """Inject a fake tracing.codex.hooks.stop module exposing finalize_turn.

    Lets session.py's lazy ``from tracing.codex.hooks.stop import finalize_turn``
    resolve to a MagicMock the tests can assert against.
    """
    fake = types.ModuleType("tracing.codex.hooks.stop")
    fn = mock.MagicMock()
    fake.finalize_turn = fn
    monkeypatch.setitem(sys.modules, "tracing.codex.hooks.stop", fake)
    return fn


def _set_stdin(monkeypatch, payload):
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))


def _set_stdin_raw(monkeypatch, raw: str):
    monkeypatch.setattr(sys, "stdin", io.StringIO(raw))


def _state_file(state_dir, thread_id):
    return state_dir / f"state_{thread_id}.yaml"


def _read_state(state_dir, thread_id):
    """Read the per-thread state file via a fresh StateManager."""
    from core.common import StateManager

    sm = StateManager(
        state_dir=state_dir,
        state_file=_state_file(state_dir, thread_id),
        lock_path=state_dir / f".lock_{thread_id}",
    )
    return sm


def _prep_state(state_dir, thread_id, values: dict) -> None:
    """Pre-populate the state file for a thread."""
    from core.common import StateManager

    sm = StateManager(
        state_dir=state_dir,
        state_file=_state_file(state_dir, thread_id),
        lock_path=state_dir / f".lock_{thread_id}",
    )
    sm.init_state()
    for k, v in values.items():
        sm.set(k, v)


# ---------------------------------------------------------------------------
# SessionStart
# ---------------------------------------------------------------------------


class TestSessionStart:

    def test_fresh_session(self, state_dir, finalize_mock, monkeypatch):
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        _set_stdin(
            monkeypatch,
            {
                "hook_event_name": "SessionStart",
                "session_id": "t1",
                "model": "gpt-5",
                "permission_mode": "default",
                "cwd": "/x",
                "source": "startup",
            },
        )

        session_mod.main()

        assert _state_file(state_dir, "t1").exists()
        sm = _read_state(state_dir, "t1")
        assert sm.get("model") == "gpt-5"
        assert sm.get("permission_mode") == "default"
        assert sm.get("cwd") == "/x"
        finalize_mock.assert_not_called()

    def test_resume_with_pending_turn_finalizes(self, state_dir, finalize_mock, monkeypatch):
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        _prep_state(state_dir, "t1", {"turn_start_ms": "123"})

        _set_stdin(
            monkeypatch,
            {
                "hook_event_name": "SessionStart",
                "session_id": "t1",
                "model": "gpt-5",
                "permission_mode": "default",
                "cwd": "/y",
                "source": "resume",
            },
        )

        session_mod.main()

        assert finalize_mock.call_count == 1
        # Called with (state, "t1")
        call_args = finalize_mock.call_args
        _, thread_id = call_args[0]
        assert thread_id == "t1"

        sm = _read_state(state_dir, "t1")
        assert sm.get("model") == "gpt-5"
        assert sm.get("permission_mode") == "default"
        assert sm.get("cwd") == "/y"

    def test_startup_with_pending_turn_does_not_finalize(self, state_dir, finalize_mock, monkeypatch):
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        _prep_state(state_dir, "t1", {"turn_start_ms": "123"})

        _set_stdin(
            monkeypatch,
            {
                "hook_event_name": "SessionStart",
                "session_id": "t1",
                "source": "startup",
            },
        )

        session_mod.main()

        finalize_mock.assert_not_called()


# ---------------------------------------------------------------------------
# UserPromptSubmit
# ---------------------------------------------------------------------------


class TestUserPromptSubmit:

    def test_fresh_turn(self, state_dir, finalize_mock, monkeypatch):
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        _set_stdin(
            monkeypatch,
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "t1",
                "prompt": "hello",
            },
        )

        before_ms = int(__import__("time").time() * 1000) - 5
        session_mod.main()

        sm = _read_state(state_dir, "t1")
        assert sm.get("user_prompt") == "hello"
        turn_start = sm.get("turn_start_ms")
        assert turn_start is not None
        assert int(turn_start) >= before_ms
        finalize_mock.assert_not_called()

    def test_stale_pending_turn_finalized(self, state_dir, finalize_mock, monkeypatch):
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        _prep_state(state_dir, "t1", {"turn_start_ms": "123", "user_prompt": "old"})

        _set_stdin(
            monkeypatch,
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "t1",
                "prompt": "new",
            },
        )

        session_mod.main()

        assert finalize_mock.call_count == 1
        sm = _read_state(state_dir, "t1")
        assert sm.get("user_prompt") == "new"
        new_turn_start = sm.get("turn_start_ms")
        assert new_turn_start is not None
        assert new_turn_start != "123"

    def test_redacts_prompt_when_logging_disabled(self, state_dir, finalize_mock, monkeypatch):
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        monkeypatch.setenv("ARIZE_LOG_PROMPTS", "false")

        _set_stdin(
            monkeypatch,
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "t1",
                "prompt": "secret prompt",
            },
        )

        session_mod.main()

        sm = _read_state(state_dir, "t1")
        stored = sm.get("user_prompt")
        assert stored is not None
        assert "secret prompt" not in stored
        assert "redacted" in stored.lower()


# ---------------------------------------------------------------------------
# Dispatch & error paths
# ---------------------------------------------------------------------------


class TestDispatch:

    def test_unknown_event_ignored(self, state_dir, finalize_mock, monkeypatch):
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        _set_stdin(monkeypatch, {"hook_event_name": "WhateverElse"})

        session_mod.main()

        # No state file created for any thread
        assert not any(state_dir.glob("state_*.yaml"))
        finalize_mock.assert_not_called()

    def test_malformed_json_logs_error(self, state_dir, finalize_mock, monkeypatch, capsys):
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        _set_stdin_raw(monkeypatch, "not json")

        session_mod.main()

        captured = capsys.readouterr()
        assert "codex session hook failed" in captured.err

    def test_empty_stdin(self, state_dir, finalize_mock, monkeypatch):
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        _set_stdin_raw(monkeypatch, "")

        session_mod.main()

        assert not any(state_dir.glob("state_*.yaml"))

    def test_tracing_disabled_early_return(self, state_dir, finalize_mock, monkeypatch):
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "false")
        _set_stdin(
            monkeypatch,
            {
                "hook_event_name": "SessionStart",
                "session_id": "t1",
                "model": "gpt-5",
            },
        )

        session_mod.main()

        assert not any(state_dir.glob("state_*.yaml"))
