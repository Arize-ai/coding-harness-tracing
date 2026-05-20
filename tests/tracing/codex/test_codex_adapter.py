#!/usr/bin/env python3
"""Tests for tracing.codex.hooks.adapter — session resolution, init, GC, requirements."""
import os
import time

import pytest
import yaml

from core.common import StateManager
from tracing.codex.hooks import adapter

# ── Autouse fixture to prevent real sleeps ───────────────────────────────────


@pytest.fixture(autouse=True)
def _mock_sleep(monkeypatch):
    sleep_calls = []
    monkeypatch.setattr("time.sleep", lambda s: sleep_calls.append(s))
    return sleep_calls


# ── Shared fixtures ─────────────────────────────────────────────────────────


@pytest.fixture
def codex_state_dir(tmp_harness_dir, monkeypatch):
    """Point adapter.STATE_DIR to a temp directory and return it."""
    state_dir = tmp_harness_dir / "state" / "codex"
    state_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(adapter, "STATE_DIR", state_dir)
    return state_dir


@pytest.fixture
def disable_env_vars(monkeypatch):
    """Clear env vars that could influence session resolution."""
    monkeypatch.delenv("ARIZE_PROJECT_NAME", raising=False)
    monkeypatch.delenv("ARIZE_USER_ID", raising=False)
    monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")


# ── load_env_file tests ─────────────────────────────────────────────────────


class TestLoadEnvFile:
    def test_loads_simple_vars(self, tmp_path, monkeypatch):
        """Simple KEY=VALUE lines are loaded into os.environ."""
        monkeypatch.delenv("TEST_CODEX_FOO", raising=False)
        monkeypatch.delenv("TEST_CODEX_BAZ", raising=False)
        env_file = tmp_path / ".env"
        env_file.write_text("TEST_CODEX_FOO=bar\nTEST_CODEX_BAZ=qux\n")
        adapter.load_env_file(env_file)
        assert os.environ["TEST_CODEX_FOO"] == "bar"
        assert os.environ["TEST_CODEX_BAZ"] == "qux"
        monkeypatch.delenv("TEST_CODEX_FOO", raising=False)
        monkeypatch.delenv("TEST_CODEX_BAZ", raising=False)

    def test_handles_export_prefix(self, tmp_path, monkeypatch):
        """Lines with 'export ' prefix are handled correctly."""
        monkeypatch.delenv("TEST_CODEX_EXPORT_KEY", raising=False)
        env_file = tmp_path / ".env"
        env_file.write_text("export TEST_CODEX_EXPORT_KEY=val\n")
        adapter.load_env_file(env_file)
        assert os.environ["TEST_CODEX_EXPORT_KEY"] == "val"
        monkeypatch.delenv("TEST_CODEX_EXPORT_KEY", raising=False)

    def test_strips_quotes(self, tmp_path, monkeypatch):
        """Double and single quotes around values are stripped."""
        monkeypatch.delenv("TEST_CODEX_DQ", raising=False)
        monkeypatch.delenv("TEST_CODEX_SQ", raising=False)
        env_file = tmp_path / ".env"
        env_file.write_text("TEST_CODEX_DQ=\"quoted\"\nTEST_CODEX_SQ='single'\n")
        adapter.load_env_file(env_file)
        assert os.environ["TEST_CODEX_DQ"] == "quoted"
        assert os.environ["TEST_CODEX_SQ"] == "single"
        monkeypatch.delenv("TEST_CODEX_DQ", raising=False)
        monkeypatch.delenv("TEST_CODEX_SQ", raising=False)

    def test_skips_comments_and_blanks(self, tmp_path, monkeypatch):
        """Comments and blank lines are skipped."""
        monkeypatch.delenv("TEST_CODEX_ONLY", raising=False)
        env_file = tmp_path / ".env"
        env_file.write_text("# this is a comment\n\nTEST_CODEX_ONLY=val\n")
        adapter.load_env_file(env_file)
        assert os.environ["TEST_CODEX_ONLY"] == "val"
        monkeypatch.delenv("TEST_CODEX_ONLY", raising=False)

    def test_skips_lines_without_equals(self, tmp_path, monkeypatch):
        """Lines without '=' are skipped."""
        monkeypatch.delenv("NOEQUALS", raising=False)
        env_file = tmp_path / ".env"
        env_file.write_text("NOEQUALS\n")
        adapter.load_env_file(env_file)
        assert "NOEQUALS" not in os.environ

    def test_missing_file_no_error(self, tmp_path):
        """Calling with a nonexistent path does not raise."""
        adapter.load_env_file(tmp_path / "does_not_exist.env")  # should not raise


# ── resolve_session tests ───────────────────────────────────────────────────


class TestResolveSession:
    def test_uses_thread_id_as_key(self, codex_state_dir, disable_env_vars):
        """thread_id is used as the state file key."""
        sm = adapter.resolve_session("thread-123")
        assert sm.state_file == codex_state_dir / "state_thread-123.yaml"

    def test_empty_thread_id_generates_uuid(self, codex_state_dir, disable_env_vars):
        """Empty thread_id generates a UUID-based key."""
        sm = adapter.resolve_session("")
        assert sm.state_file.exists()
        # The generated key should be a 32-char hex string
        stem = sm.state_file.stem  # "state_<generated>"
        generated = stem.replace("state_", "", 1)
        assert len(generated) == 32
        int(generated, 16)  # should not raise

    def test_init_state_called(self, codex_state_dir, disable_env_vars):
        """Returned StateManager has init_state() called (file exists)."""
        sm = adapter.resolve_session("test-init")
        assert sm.state_file.exists()
        data = yaml.safe_load(sm.state_file.read_text())
        assert data == {}

    def test_same_thread_same_file(self, codex_state_dir, disable_env_vars):
        """Calling resolve_session twice with same thread_id returns same path."""
        sm1 = adapter.resolve_session("stable-thread")
        sm2 = adapter.resolve_session("stable-thread")
        assert sm1.state_file == sm2.state_file


# ── ensure_session_initialized tests ─────────────────────────────────────────


class TestEnsureSessionInitialized:
    def _make_state(self, codex_state_dir, key="test"):
        sm = StateManager(
            state_dir=codex_state_dir,
            state_file=codex_state_dir / f"state_{key}.yaml",
            lock_path=codex_state_dir / f".lock_{key}",
        )
        sm.init_state()
        return sm

    def test_sets_all_keys(self, codex_state_dir, disable_env_vars):
        """First call sets all expected keys."""
        sm = self._make_state(codex_state_dir, "all-keys")
        adapter.ensure_session_initialized(sm, "tid-1", "/some/project")
        assert sm.get("session_id") == "tid-1"
        assert sm.get("session_start_time") is not None
        assert sm.get("project_name") is not None
        assert sm.get("trace_count") == "0"

    def test_idempotent(self, codex_state_dir, disable_env_vars):
        """Second call is a no-op — values unchanged."""
        sm = self._make_state(codex_state_dir, "idempotent")
        adapter.ensure_session_initialized(sm, "tid-2", "/some/path")
        start_time = sm.get("session_start_time")
        adapter.ensure_session_initialized(sm, "tid-different", "/other/path")
        assert sm.get("session_id") == "tid-2"
        assert sm.get("session_start_time") == start_time

    def test_project_name_from_env(self, codex_state_dir, monkeypatch):
        """ARIZE_PROJECT_NAME env var takes priority over cwd."""
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        monkeypatch.setenv("ARIZE_PROJECT_NAME", "my-env-project")
        monkeypatch.delenv("ARIZE_USER_ID", raising=False)
        sm = self._make_state(codex_state_dir, "proj-env")
        adapter.ensure_session_initialized(sm, "tid-3", "/home/user/other-project")
        assert sm.get("project_name") == "my-env-project"

    def test_project_name_from_cwd(self, codex_state_dir, disable_env_vars):
        """project_name from cwd basename when env not set."""
        sm = self._make_state(codex_state_dir, "proj-cwd")
        adapter.ensure_session_initialized(sm, "tid-4", "/foo/bar")
        assert sm.get("project_name") == "bar"

    def test_user_id_from_env(self, codex_state_dir, monkeypatch):
        """ARIZE_USER_ID env var is set in state."""
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        monkeypatch.setenv("ARIZE_USER_ID", "env-user")
        monkeypatch.delenv("ARIZE_PROJECT_NAME", raising=False)
        sm = self._make_state(codex_state_dir, "uid-env")
        adapter.ensure_session_initialized(sm, "tid-5", "/some/path")
        assert sm.get("user_id") == "env-user"


# ── gc_stale_state_files tests ───────────────────────────────────────────────


class TestGcStaleStateFiles:
    def test_old_file_removed(self, codex_state_dir, disable_env_vars):
        """State file older than 24h is removed."""
        state_file = codex_state_dir / "state_old-thread.yaml"
        state_file.write_text("{}")
        # Set mtime to 25 hours ago
        old_time = time.time() - (25 * 3600)
        os.utime(state_file, (old_time, old_time))
        adapter.gc_stale_state_files()
        assert not state_file.exists()

    def test_recent_file_kept(self, codex_state_dir, disable_env_vars):
        """State file with current mtime is kept."""
        state_file = codex_state_dir / "state_recent-thread.yaml"
        state_file.write_text("{}")
        adapter.gc_stale_state_files()
        assert state_file.exists()

    def test_lock_dir_removed_with_state(self, codex_state_dir, disable_env_vars):
        """Lock dir is removed along with old state file."""
        state_file = codex_state_dir / "state_gc-lock.yaml"
        state_file.write_text("{}")
        lock_dir = codex_state_dir / ".lock_gc-lock"
        lock_dir.mkdir()
        old_time = time.time() - (25 * 3600)
        os.utime(state_file, (old_time, old_time))
        adapter.gc_stale_state_files()
        assert not state_file.exists()
        assert not lock_dir.exists()

    def test_empty_dir_no_error(self, codex_state_dir, disable_env_vars):
        """Empty STATE_DIR causes no errors."""
        for f in codex_state_dir.glob("state_*.yaml"):
            f.unlink()
        adapter.gc_stale_state_files()  # should not raise

    def test_gc_removes_aged_spans_jsonl(self, codex_state_dir, disable_env_vars):
        """gc_stale_state_files unlinks spans_*.jsonl files older than 24h."""
        aged = codex_state_dir / "spans_t1.jsonl"
        aged.write_text('{"kind":"tool_start"}\n')
        fresh = codex_state_dir / "spans_t2.jsonl"
        fresh.write_text('{"kind":"tool_start"}\n')
        old_time = time.time() - 25 * 3600
        os.utime(aged, (old_time, old_time))

        adapter.gc_stale_state_files()

        assert not aged.exists()
        assert fresh.exists()


# ── check_requirements tests ─────────────────────────────────────────────────


class TestCheckRequirements:
    def test_enabled_returns_true(self, tmp_harness_dir, monkeypatch):
        """trace_enabled=True -> returns True and STATE_DIR exists."""
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        state_dir = tmp_harness_dir / "state" / "codex-check"
        monkeypatch.setattr(adapter, "STATE_DIR", state_dir)
        assert adapter.check_requirements() is True
        assert state_dir.is_dir()

    def test_disabled_returns_false(self, tmp_harness_dir, monkeypatch):
        """trace_enabled=False -> returns False, STATE_DIR not created."""
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "false")
        state_dir = tmp_harness_dir / "state" / "codex-nope"
        monkeypatch.setattr(adapter, "STATE_DIR", state_dir)
        assert adapter.check_requirements() is False
        assert not state_dir.exists()
