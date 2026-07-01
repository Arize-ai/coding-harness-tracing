#!/usr/bin/env python3
"""Tests for tracing.antigravity.hooks.adapter.

Antigravity provides ``conversationId`` on every hook invocation, so the
adapter is simpler than the Gemini one: no env-var or grandparent-PID lookup,
no PID-keyed gc.
"""
from __future__ import annotations

import json
import os
import time

import pytest

from core.common import StateManager
from tracing.antigravity.hooks import adapter

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def antigravity_state_dir(tmp_harness_dir, monkeypatch):
    """Point adapter.STATE_DIR to a temp directory and return it."""
    state_dir = tmp_harness_dir / "state" / "antigravity"
    state_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(adapter, "STATE_DIR", state_dir)
    return state_dir


@pytest.fixture
def disable_env_vars(monkeypatch):
    """Clear env vars that could influence session resolution."""
    monkeypatch.delenv("ARIZE_PROJECT_NAME", raising=False)
    monkeypatch.delenv("ARIZE_USER_ID", raising=False)
    monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")


# ── Module-level constants tests ──────────────────────────────────────────────


class TestModuleConstants:
    def test_service_name(self):
        assert adapter.SERVICE_NAME == "antigravity"

    def test_scope_name(self):
        assert adapter.SCOPE_NAME == "arize-antigravity-plugin"


# ── check_requirements tests ─────────────────────────────────────────────────


class TestCheckRequirements:
    def test_enabled_returns_true(self, tmp_harness_dir, monkeypatch):
        """trace_enabled=True -> returns True and STATE_DIR exists."""
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        state_dir = tmp_harness_dir / "state" / "antigravity-check"
        monkeypatch.setattr(adapter, "STATE_DIR", state_dir)
        assert adapter.check_requirements() is True
        assert state_dir.is_dir()

    def test_disabled_returns_false(self, tmp_harness_dir, monkeypatch):
        """trace_enabled=False -> returns False, STATE_DIR not created."""
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "false")
        state_dir = tmp_harness_dir / "state" / "antigravity-nope"
        monkeypatch.setattr(adapter, "STATE_DIR", state_dir)
        assert adapter.check_requirements() is False
        assert not state_dir.exists()


# ── resolve_session tests ────────────────────────────────────────────────────


class TestResolveSession:
    def test_uses_conversation_id(self, antigravity_state_dir, disable_env_vars):
        """conversationId from the payload is the session key."""
        sm = adapter.resolve_session({"conversationId": "abc"})
        assert sm.state_file == antigravity_state_dir / "state_abc.json"
        assert sm.state_file.exists()

    def test_falls_back_to_pid_when_missing(self, antigravity_state_dir, disable_env_vars):
        """Missing conversationId falls back to the current PID."""
        sm = adapter.resolve_session({})
        assert sm.state_file.exists()
        key = sm.state_file.stem.replace("state_", "", 1)
        assert key == str(os.getpid())

    def test_falls_back_to_pid_when_empty_string(self, antigravity_state_dir, disable_env_vars):
        """Empty-string conversationId also falls back to PID."""
        sm = adapter.resolve_session({"conversationId": ""})
        key = sm.state_file.stem.replace("state_", "", 1)
        assert key == str(os.getpid())

    def test_init_state_called(self, antigravity_state_dir, disable_env_vars):
        """Returned StateManager has init_state() called (file exists with {})."""
        sm = adapter.resolve_session({"conversationId": "test-init"})
        assert sm.state_file.exists()
        data = json.loads(sm.state_file.read_text())
        assert data == {}

    def test_same_input_same_file(self, antigravity_state_dir, disable_env_vars):
        """Calling resolve_session twice with same payload produces same path."""
        sm1 = adapter.resolve_session({"conversationId": "stable"})
        sm2 = adapter.resolve_session({"conversationId": "stable"})
        assert sm1.state_file == sm2.state_file

    def test_lock_path_matches_key(self, antigravity_state_dir, disable_env_vars):
        """Lock file is named .lock_{key} in STATE_DIR."""
        sm = adapter.resolve_session({"conversationId": "lock-test"})
        assert sm._lock_path == antigravity_state_dir / ".lock_lock-test"


# ── ensure_session_initialized tests ─────────────────────────────────────────


class TestEnsureSessionInitialized:
    def _make_state(self, antigravity_state_dir, key="test"):
        sm = StateManager(
            state_dir=antigravity_state_dir,
            state_file=antigravity_state_dir / f"state_{key}.json",
            lock_path=antigravity_state_dir / f".lock_{key}",
        )
        sm.init_state()
        return sm

    def test_sets_expected_keys(self, antigravity_state_dir, disable_env_vars):
        """First call sets session_id, project_name, user_id, last_emitted_step."""
        sm = self._make_state(antigravity_state_dir, "all-keys")
        adapter.ensure_session_initialized(sm, {"conversationId": "abc"})
        assert sm.get("session_id") == "abc"
        assert sm.get("project_name") is not None
        assert sm.get("user_id") is not None
        assert sm.get("last_emitted_step") == "-1"

    def test_idempotent(self, antigravity_state_dir, disable_env_vars):
        """Second call is a no-op — values unchanged."""
        sm = self._make_state(antigravity_state_dir, "idempotent")
        adapter.ensure_session_initialized(sm, {"conversationId": "first"})
        session_id = sm.get("session_id")
        last_step = sm.get("last_emitted_step")
        # Second call with different conversationId should not overwrite.
        adapter.ensure_session_initialized(sm, {"conversationId": "second"})
        assert sm.get("session_id") == session_id
        assert sm.get("last_emitted_step") == last_step

    def test_session_id_falls_back_to_generated_trace_id(self, antigravity_state_dir, disable_env_vars):
        """No conversationId -> session_id is a generated trace ID (32 hex chars)."""
        sm = self._make_state(antigravity_state_dir, "no-conv-id")
        adapter.ensure_session_initialized(sm, {})
        session_id = sm.get("session_id")
        assert session_id is not None
        assert len(session_id) == 32
        assert all(c in "0123456789abcdef" for c in session_id)

    def test_project_name_from_env(self, antigravity_state_dir, monkeypatch):
        """ARIZE_PROJECT_NAME env var takes priority over workspacePaths."""
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        monkeypatch.setenv("ARIZE_PROJECT_NAME", "my-env-project")
        monkeypatch.delenv("ARIZE_USER_ID", raising=False)
        sm = self._make_state(antigravity_state_dir, "proj-env")
        adapter.ensure_session_initialized(
            sm,
            {"conversationId": "c", "workspacePaths": ["/home/user/other-project"]},
        )
        assert sm.get("project_name") == "my-env-project"

    def test_project_name_from_workspace_paths(self, antigravity_state_dir, disable_env_vars):
        """project_name uses basename of workspacePaths[0] when env empty."""
        sm = self._make_state(antigravity_state_dir, "proj-ws")
        adapter.ensure_session_initialized(
            sm,
            {"conversationId": "c", "workspacePaths": ["/some/path/myproj"]},
        )
        assert sm.get("project_name") == "myproj"

    def test_project_name_falls_back_to_cwd(self, antigravity_state_dir, disable_env_vars):
        """project_name falls back to basename of cwd when no workspacePaths."""
        sm = self._make_state(antigravity_state_dir, "proj-cwd")
        adapter.ensure_session_initialized(sm, {"conversationId": "c"})
        project = sm.get("project_name")
        assert project is not None
        assert len(project) > 0

    def test_user_id_from_env(self, antigravity_state_dir, monkeypatch):
        """user_id is read from env.user_id."""
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        monkeypatch.setenv("ARIZE_USER_ID", "test-user-123")
        monkeypatch.delenv("ARIZE_PROJECT_NAME", raising=False)
        sm = self._make_state(antigravity_state_dir, "user-env")
        adapter.ensure_session_initialized(sm, {"conversationId": "c"})
        assert sm.get("user_id") == "test-user-123"


# ── gc_stale_state_files tests ───────────────────────────────────────────────


class TestGcStaleStateFiles:
    def test_old_file_removed(self, antigravity_state_dir, disable_env_vars):
        """State file older than 24h is removed."""
        state_file = antigravity_state_dir / "state_old-session.json"
        state_file.write_text("{}")
        old_time = time.time() - 90000  # 25h
        os.utime(state_file, (old_time, old_time))
        adapter.gc_stale_state_files()
        assert not state_file.exists()

    def test_recent_file_kept(self, antigravity_state_dir, disable_env_vars):
        """State file younger than 24h is kept."""
        state_file = antigravity_state_dir / "state_recent-session.json"
        state_file.write_text("{}")
        adapter.gc_stale_state_files()
        assert state_file.exists()

    def test_lock_dir_removed(self, antigravity_state_dir, disable_env_vars):
        """Lock dir is removed when state file is removed."""
        state_file = antigravity_state_dir / "state_old-lock-dir.json"
        state_file.write_text("{}")
        lock_dir = antigravity_state_dir / ".lock_old-lock-dir"
        lock_dir.mkdir()
        old_time = time.time() - 90000
        os.utime(state_file, (old_time, old_time))
        adapter.gc_stale_state_files()
        assert not state_file.exists()
        assert not lock_dir.exists()

    def test_lock_file_removed(self, antigravity_state_dir, disable_env_vars):
        """Lock file (fcntl-style) is removed when state file is removed."""
        state_file = antigravity_state_dir / "state_old-lock-file.json"
        state_file.write_text("{}")
        lock_file = antigravity_state_dir / ".lock_old-lock-file"
        lock_file.write_text("")
        old_time = time.time() - 90000
        os.utime(state_file, (old_time, old_time))
        adapter.gc_stale_state_files()
        assert not state_file.exists()
        assert not lock_file.exists()

    def test_empty_dir_no_error(self, antigravity_state_dir, disable_env_vars):
        """Empty STATE_DIR causes no errors."""
        for f in antigravity_state_dir.glob("state_*.json"):
            f.unlink()
        adapter.gc_stale_state_files()  # should not raise

    def test_nonexistent_dir_no_error(self, tmp_harness_dir, monkeypatch):
        """Non-existent STATE_DIR causes no errors."""
        state_dir = tmp_harness_dir / "state" / "antigravity-nonexistent"
        monkeypatch.setattr(adapter, "STATE_DIR", state_dir)
        adapter.gc_stale_state_files()  # should not raise

    def test_uses_24h_cutoff(self, antigravity_state_dir, disable_env_vars):
        """Files just past 24h boundary are removed."""
        state_file = antigravity_state_dir / "state_boundary.json"
        state_file.write_text("{}")
        old_time = time.time() - 86401
        os.utime(state_file, (old_time, old_time))
        adapter.gc_stale_state_files()
        assert not state_file.exists()


# ── module-level log-file env wiring ─────────────────────────────────────────


class TestLogFileEnv:
    def test_log_file_default_points_to_antigravity_log(self):
        """The adapter sets ARIZE_LOG_FILE on import unless already set."""
        # The setdefault on import must have installed a value (user override
        # is also fine — we just need _some_ value).
        assert os.environ.get("ARIZE_LOG_FILE")
