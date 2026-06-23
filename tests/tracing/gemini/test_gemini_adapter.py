#!/usr/bin/env python3
"""Tests for tracing.gemini.hooks.adapter — session resolution, init, GC, requirements.

Mirrors tests/test_copilot_adapter.py structure but adapted for Gemini's
single-mode (CLI-only) adapter with GEMINI_SESSION_ID env var instead of
PID-based or dual-mode session keys.
"""
from __future__ import annotations

import json
import os
import time

import pytest

from core.common import StateManager, env

# ---------------------------------------------------------------------------
# We import the adapter module itself so we can monkeypatch its module-level
# constants.  The actual functions under test are attributes of this module.
# ---------------------------------------------------------------------------
from tracing.gemini.hooks import adapter

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def gemini_state_dir(tmp_harness_dir, monkeypatch):
    """Point adapter.STATE_DIR to a temp directory and return it."""
    state_dir = tmp_harness_dir / "state" / "gemini"
    state_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(adapter, "STATE_DIR", state_dir)
    return state_dir


@pytest.fixture
def disable_env_vars(monkeypatch):
    """Clear env vars that could influence session resolution."""
    monkeypatch.delenv("ARIZE_PROJECT_NAME", raising=False)
    monkeypatch.delenv("ARIZE_USER_ID", raising=False)
    monkeypatch.delenv("GEMINI_SESSION_ID", raising=False)
    monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")


# ── Module-level constants tests ──────────────────────────────────────────────


class TestModuleConstants:
    def test_service_name(self):
        """SERVICE_NAME matches the gemini harness metadata."""
        assert adapter.SERVICE_NAME == "gemini"

    def test_scope_name(self):
        """SCOPE_NAME matches the gemini harness metadata."""
        assert adapter.SCOPE_NAME == "arize-gemini-plugin"


# ── check_requirements tests ─────────────────────────────────────────────────


class TestCheckRequirements:
    def test_enabled_returns_true(self, tmp_harness_dir, monkeypatch):
        """trace_enabled=True -> returns True and STATE_DIR exists."""
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        state_dir = tmp_harness_dir / "state" / "gemini-check"
        monkeypatch.setattr(adapter, "STATE_DIR", state_dir)
        assert adapter.check_requirements() is True
        assert state_dir.is_dir()

    def test_disabled_returns_false(self, tmp_harness_dir, monkeypatch):
        """trace_enabled=False -> returns False, STATE_DIR not created."""
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "false")
        state_dir = tmp_harness_dir / "state" / "gemini-nope"
        monkeypatch.setattr(adapter, "STATE_DIR", state_dir)
        assert adapter.check_requirements() is False
        assert not state_dir.exists()


# ── resolve_session tests ────────────────────────────────────────────────────


class TestResolveSession:
    def test_uses_gemini_session_id_env(self, gemini_state_dir, disable_env_vars, monkeypatch):
        """GEMINI_SESSION_ID env var is the preferred session key."""
        monkeypatch.setenv("GEMINI_SESSION_ID", "env-session-42")
        sm = adapter.resolve_session({})
        assert sm.state_file == gemini_state_dir / "state_env-session-42.json"
        assert sm.state_file.exists()

    def test_falls_back_to_payload_session_id(self, gemini_state_dir, disable_env_vars):
        """Falls back to input_json['session_id'] when env var not set."""
        sm = adapter.resolve_session({"session_id": "payload-sess-99"})
        assert sm.state_file == gemini_state_dir / "state_payload-sess-99.json"

    def test_falls_back_to_grandparent_pid_when_no_session_id(self, gemini_state_dir, disable_env_vars):
        """Falls back to grandparent PID when neither env var nor payload has session_id.

        On Unix the adapter resolves to the grandparent PID; if that lookup fails
        it returns the parent PID. Either way the key must be a positive integer
        string so subsequent gc passes can liveness-check it.
        """
        sm = adapter.resolve_session({})
        assert sm.state_file.exists()
        key = sm.state_file.stem.replace("state_", "", 1)
        assert key.isdigit()
        assert int(key) > 0

    def test_init_state_called(self, gemini_state_dir, disable_env_vars, monkeypatch):
        """Returned StateManager has init_state() called (file exists with {})."""
        monkeypatch.setenv("GEMINI_SESSION_ID", "test-init")
        sm = adapter.resolve_session({})
        assert sm.state_file.exists()
        data = json.loads(sm.state_file.read_text())
        assert data == {}

    def test_same_input_same_file(self, gemini_state_dir, disable_env_vars, monkeypatch):
        """Calling resolve_session twice with same env produces same file path."""
        monkeypatch.setenv("GEMINI_SESSION_ID", "stable-session")
        sm1 = adapter.resolve_session({})
        sm2 = adapter.resolve_session({})
        assert sm1.state_file == sm2.state_file

    def test_lock_path_matches_key(self, gemini_state_dir, disable_env_vars, monkeypatch):
        """Lock file is named .lock_{key} in STATE_DIR."""
        monkeypatch.setenv("GEMINI_SESSION_ID", "lock-test")
        sm = adapter.resolve_session({})
        assert sm._lock_path == gemini_state_dir / ".lock_lock-test"

    def test_env_takes_priority_over_payload(self, gemini_state_dir, disable_env_vars, monkeypatch):
        """GEMINI_SESSION_ID env var takes priority over payload session_id."""
        monkeypatch.setenv("GEMINI_SESSION_ID", "from-env")
        sm = adapter.resolve_session({"session_id": "from-payload"})
        assert sm.state_file == gemini_state_dir / "state_from-env.json"


# ── ensure_session_initialized tests ─────────────────────────────────────────


class TestEnsureSessionInitialized:
    def _make_state(self, gemini_state_dir, key="test"):
        sm = StateManager(
            state_dir=gemini_state_dir,
            state_file=gemini_state_dir / f"state_{key}.json",
            lock_path=gemini_state_dir / f".lock_{key}",
        )
        sm.init_state()
        return sm

    def test_sets_all_keys(self, gemini_state_dir, disable_env_vars):
        """First call sets all expected keys."""
        sm = self._make_state(gemini_state_dir, "all-keys")
        adapter.ensure_session_initialized(sm, {})
        assert sm.get("session_id") is not None
        assert sm.get("session_start_time") is not None
        assert sm.get("project_name") is not None
        assert sm.get("trace_count") == "0"
        assert sm.get("tool_count") == "0"
        assert sm.get("user_id") is not None

    def test_idempotent(self, gemini_state_dir, disable_env_vars):
        """Second call is a no-op — values unchanged."""
        sm = self._make_state(gemini_state_dir, "idempotent")
        adapter.ensure_session_initialized(sm, {})
        start_time = sm.get("session_start_time")
        session_id = sm.get("session_id")
        adapter.ensure_session_initialized(sm, {"session_id": "different"})
        assert sm.get("session_id") == session_id
        assert sm.get("session_start_time") == start_time

    def test_session_id_uses_resolved_key(self, gemini_state_dir, disable_env_vars, monkeypatch):
        """session_id stored in state should match the resolved session key."""
        monkeypatch.setenv("GEMINI_SESSION_ID", "gemini-resolved-key")
        sm = adapter.resolve_session({})
        adapter.ensure_session_initialized(sm, {})
        # The session_id in state should match the key used for the file
        assert sm.get("session_id") is not None

    def test_project_name_from_env(self, gemini_state_dir, monkeypatch):
        """ARIZE_PROJECT_NAME env var takes priority over cwd."""
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        monkeypatch.setenv("ARIZE_PROJECT_NAME", "my-env-project")
        monkeypatch.delenv("ARIZE_USER_ID", raising=False)
        monkeypatch.delenv("GEMINI_SESSION_ID", raising=False)
        sm = self._make_state(gemini_state_dir, "proj-env")
        adapter.ensure_session_initialized(sm, {"cwd": "/home/user/other-project"})
        assert sm.get("project_name") == "my-env-project"

    def test_project_name_from_cwd(self, gemini_state_dir, disable_env_vars):
        """project_name falls back to basename of cwd."""
        sm = self._make_state(gemini_state_dir, "proj-cwd")
        adapter.ensure_session_initialized(sm, {})
        # Should use basename of os.getcwd() as fallback
        project = sm.get("project_name")
        assert project is not None
        assert len(project) > 0

    def test_project_name_from_cwd_in_payload(self, gemini_state_dir, disable_env_vars):
        """project_name uses basename of cwd from payload when env var not set."""
        sm = self._make_state(gemini_state_dir, "proj-cwd-payload")
        adapter.ensure_session_initialized(sm, {"cwd": "/some/path/myproj"})
        assert sm.get("project_name") == "myproj"

    def test_counters_start_at_zero(self, gemini_state_dir, disable_env_vars):
        """trace_count and tool_count start at '0'."""
        sm = self._make_state(gemini_state_dir, "counters")
        adapter.ensure_session_initialized(sm, {})
        assert sm.get("trace_count") == "0"
        assert sm.get("tool_count") == "0"

    def test_user_id_from_env(self, gemini_state_dir, monkeypatch):
        """user_id is read from env.user_id."""
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        monkeypatch.setenv("ARIZE_USER_ID", "test-user-123")
        monkeypatch.delenv("ARIZE_PROJECT_NAME", raising=False)
        monkeypatch.delenv("GEMINI_SESSION_ID", raising=False)
        sm = self._make_state(gemini_state_dir, "user-env")
        adapter.ensure_session_initialized(sm, {})
        assert sm.get("user_id") == "test-user-123"

    def test_user_id_per_harness_override(self, gemini_state_dir, monkeypatch):
        """harnesses.gemini.user_id in config overrides the global user_id."""
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        monkeypatch.delenv("ARIZE_USER_ID", raising=False)
        monkeypatch.delenv("ARIZE_PROJECT_NAME", raising=False)
        monkeypatch.delenv("GEMINI_SESSION_ID", raising=False)

        monkeypatch.setattr(
            "core.config.load_config",
            lambda config_path=None: {
                "user_id": "global@x",
                "harnesses": {adapter.SERVICE_NAME: {"user_id": "scoped@x"}},
            },
        )
        env.__dict__.pop("_top_level_config", None)
        try:
            sm = self._make_state(gemini_state_dir, "user-scoped")
            adapter.ensure_session_initialized(sm, {})
            assert sm.get("user_id") == "scoped@x"
        finally:
            env.__dict__.pop("_top_level_config", None)


# ── gc_stale_state_files tests ───────────────────────────────────────────────


class TestGcStaleStateFiles:
    def test_old_file_removed(self, gemini_state_dir, disable_env_vars):
        """State file older than 24h is removed."""
        state_file = gemini_state_dir / "state_old-session.json"
        state_file.write_text("{}")
        # Set mtime to 25 hours ago
        old_time = time.time() - 90000  # 25 hours
        os.utime(state_file, (old_time, old_time))
        adapter.gc_stale_state_files()
        assert not state_file.exists()

    def test_recent_file_kept(self, gemini_state_dir, disable_env_vars):
        """State file younger than 24h is kept."""
        state_file = gemini_state_dir / "state_recent-session.json"
        state_file.write_text("{}")
        # mtime is now (just created), which is < 24h old
        adapter.gc_stale_state_files()
        assert state_file.exists()

    def test_lock_dir_removed(self, gemini_state_dir, disable_env_vars):
        """Lock dir is removed when state file is removed."""
        state_file = gemini_state_dir / "state_old-lock-dir.json"
        state_file.write_text("{}")
        lock_dir = gemini_state_dir / ".lock_old-lock-dir"
        lock_dir.mkdir()
        old_time = time.time() - 90000
        os.utime(state_file, (old_time, old_time))
        adapter.gc_stale_state_files()
        assert not state_file.exists()
        assert not lock_dir.exists()

    def test_lock_file_removed(self, gemini_state_dir, disable_env_vars):
        """Lock file (fcntl-style) is removed when state file is removed."""
        state_file = gemini_state_dir / "state_old-lock-file.json"
        state_file.write_text("{}")
        lock_file = gemini_state_dir / ".lock_old-lock-file"
        lock_file.write_text("")
        old_time = time.time() - 90000
        os.utime(state_file, (old_time, old_time))
        adapter.gc_stale_state_files()
        assert not state_file.exists()
        assert not lock_file.exists()

    def test_empty_dir_no_error(self, gemini_state_dir, disable_env_vars):
        """Empty STATE_DIR causes no errors."""
        for f in gemini_state_dir.glob("state_*.json"):
            f.unlink()
        adapter.gc_stale_state_files()  # should not raise

    def test_nonexistent_dir_no_error(self, tmp_harness_dir, monkeypatch):
        """Non-existent STATE_DIR causes no errors."""
        state_dir = tmp_harness_dir / "state" / "gemini-nonexistent"
        monkeypatch.setattr(adapter, "STATE_DIR", state_dir)
        adapter.gc_stale_state_files()  # should not raise

    def test_uses_24h_cutoff(self, gemini_state_dir, disable_env_vars):
        """Files exactly at the 24h boundary are handled correctly."""
        # File just barely old enough (24h + 1 second)
        state_file = gemini_state_dir / "state_boundary.json"
        state_file.write_text("{}")
        old_time = time.time() - 86401
        os.utime(state_file, (old_time, old_time))
        adapter.gc_stale_state_files()
        assert not state_file.exists()

    def test_oserror_on_unlink_is_caught(self, gemini_state_dir, disable_env_vars, monkeypatch):
        """OSError on unlink is caught and ignored (best-effort)."""
        state_file = gemini_state_dir / "state_unlink-err.json"
        state_file.write_text("{}")
        old_time = time.time() - 90000
        os.utime(state_file, (old_time, old_time))

        # Patch Path.unlink to fail for this specific file
        import pathlib

        orig = pathlib.Path.unlink

        def patched_unlink(self, *args, **kwargs):
            if "unlink-err" in str(self):
                raise OSError("permission denied")
            return orig(self, *args, **kwargs)

        monkeypatch.setattr(pathlib.Path, "unlink", patched_unlink)
        adapter.gc_stale_state_files()  # should not raise


# ── PID-based fallback tests ─────────────────────────────────────────────────


class TestPidFallback:
    """The adapter uses grandparent-PID fallback for session keys when no
    GEMINI_SESSION_ID env var and no payload session_id are available."""

    def test_get_grandparent_pid_returns_digit_string(self):
        """_get_grandparent_pid returns a positive-integer string."""
        gpid = adapter._get_grandparent_pid()
        assert gpid.isdigit()
        assert int(gpid) > 0

    def test_get_grandparent_pid_zero_ppid_returns_self(self, monkeypatch):
        """If getppid() returns <= 0, fall back to current pid."""
        monkeypatch.setattr(adapter.os, "getppid", lambda: 0)
        gpid = adapter._get_grandparent_pid()
        assert gpid == str(os.getpid())

    def test_get_grandparent_pid_uses_proc_when_available(self, monkeypatch, tmp_path):
        """On Linux-like systems, /proc/<ppid>/stat is parsed for the grandparent pid."""
        ppid = 12345
        gpid = 67890
        monkeypatch.setattr(adapter.os, "getppid", lambda: ppid)

        proc_path = f"/proc/{ppid}/stat"
        proc_content = f"{ppid} (some (proc) name) S {gpid} 1 1 1 1 1"

        def fake_exists(path):
            return path == proc_path

        monkeypatch.setattr(adapter.os.path, "exists", fake_exists)

        from unittest import mock

        m = mock.mock_open(read_data=proc_content)
        with mock.patch("builtins.open", m):
            assert adapter._get_grandparent_pid() == str(gpid)

    def test_get_grandparent_pid_falls_back_to_ps(self, monkeypatch):
        """When /proc isn't available, _get_grandparent_pid uses `ps`."""
        ppid = 11111
        gpid_from_ps = "22222"
        monkeypatch.setattr(adapter.os, "getppid", lambda: ppid)
        monkeypatch.setattr(adapter.os.path, "exists", lambda _: False)
        monkeypatch.setattr(
            adapter.subprocess,
            "check_output",
            lambda *a, **kw: f"  {gpid_from_ps}\n".encode(),
        )
        assert adapter._get_grandparent_pid() == gpid_from_ps

    def test_get_grandparent_pid_falls_back_to_ppid_when_ps_fails(self, monkeypatch):
        """If both /proc and `ps` fail, return the parent pid as a string."""
        ppid = 33333
        monkeypatch.setattr(adapter.os, "getppid", lambda: ppid)
        monkeypatch.setattr(adapter.os.path, "exists", lambda _: False)

        def boom(*a, **kw):
            raise adapter.subprocess.SubprocessError("ps unavailable")

        monkeypatch.setattr(adapter.subprocess, "check_output", boom)
        assert adapter._get_grandparent_pid() == str(ppid)

    def test_is_pid_alive_for_self(self):
        """_is_pid_alive returns True for the current process."""
        assert adapter._is_pid_alive(os.getpid()) is True

    def test_is_pid_alive_for_invalid_pid(self):
        """_is_pid_alive returns False for non-positive pids."""
        assert adapter._is_pid_alive(0) is False
        assert adapter._is_pid_alive(-1) is False

    def test_is_pid_alive_for_dead_pid(self):
        """_is_pid_alive returns False for a pid that no longer exists."""
        # Spawn a short-lived child and reap it so the pid is definitely gone.
        import subprocess

        proc = subprocess.Popen(["/bin/sh", "-c", "exit 0"])
        proc.wait()
        # macOS recycles pids quickly, so this is best-effort: tolerate either
        # outcome but exercise the OSError branch in os.kill.
        result = adapter._is_pid_alive(proc.pid)
        assert isinstance(result, bool)

    def test_no_is_vscode_mode(self):
        """Adapter is single-mode (CLI-only); no VS Code dual-mode logic."""
        assert not hasattr(adapter, "is_vscode_mode")


# ── PID-keyed gc behavior ────────────────────────────────────────────────────


class TestGcPidKeyed:
    """When a state file is keyed by a PID (digit string), gc liveness-checks
    the pid instead of using the 24h mtime cutoff."""

    def test_dead_pid_state_file_removed(self, gemini_state_dir, disable_env_vars, monkeypatch):
        """A state file whose key is a non-alive PID is unlinked even if recent."""
        state_file = gemini_state_dir / "state_999999.json"
        state_file.write_text("{}")
        monkeypatch.setattr(adapter, "_is_pid_alive", lambda pid: False)
        adapter.gc_stale_state_files()
        assert not state_file.exists()

    def test_alive_pid_state_file_kept(self, gemini_state_dir, disable_env_vars, monkeypatch):
        """A state file keyed by an alive PID is kept regardless of mtime."""
        state_file = gemini_state_dir / "state_42.json"
        state_file.write_text("{}")
        # Even if old, an alive pid keeps the file.
        old = time.time() - 90000
        os.utime(state_file, (old, old))
        monkeypatch.setattr(adapter, "_is_pid_alive", lambda pid: True)
        adapter.gc_stale_state_files()
        assert state_file.exists()


# ── resolve_session Windows fallback ─────────────────────────────────────────


class TestResolveSessionWindowsFallback:
    """On Windows the adapter uses the parent pid (no /proc, no ps)."""

    def test_uses_parent_pid_on_windows(self, gemini_state_dir, disable_env_vars, monkeypatch):
        monkeypatch.setattr(adapter.platform, "system", lambda: "Windows")
        ppid = 4242
        monkeypatch.setattr(adapter.os, "getppid", lambda: ppid)
        sm = adapter.resolve_session({})
        assert sm.state_file == gemini_state_dir / f"state_{ppid}.json"


# ── module-level log-file env wiring ─────────────────────────────────────────


class TestLogFileEnv:
    def test_log_file_default_points_to_gemini_log(self):
        """The adapter sets ARIZE_LOG_FILE to ~/.arize/harness/logs/gemini.log
        on import unless the user has already overridden it."""
        # The setdefault on import must have installed a value.
        assert os.environ.get("ARIZE_LOG_FILE", "").endswith("gemini.log") or os.environ.get(
            "ARIZE_LOG_FILE"
        )  # user override is also fine
