#!/usr/bin/env python3
"""Tests for tracing.claude_code.hooks.adapter — session resolution, init, GC, requirements."""
import json
import os
import subprocess
from pathlib import Path
from unittest.mock import mock_open, patch

import pytest

from core.common import StateManager
from tracing.claude_code.hooks import adapter
from tracing.claude_code.hooks.adapter import resolve_transcript_path

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def claude_state_dir(tmp_harness_dir, monkeypatch):
    """Point adapter.STATE_DIR to a temp directory and return it."""
    state_dir = tmp_harness_dir / "state" / "claude-code"
    state_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(adapter, "STATE_DIR", state_dir)
    return state_dir


@pytest.fixture
def disable_env_vars(monkeypatch):
    """Clear env vars that could influence session resolution."""
    monkeypatch.delenv("CLAUDE_SESSION_KEY", raising=False)
    monkeypatch.delenv("ARIZE_PROJECT_NAME", raising=False)
    monkeypatch.delenv("ARIZE_USER_ID", raising=False)
    monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")


# ── resolve_session tests ────────────────────────────────────────────────────


class TestResolveSession:
    def test_session_id_from_input(self, claude_state_dir, disable_env_vars):
        """input with session_id -> state file uses that key."""
        sm = adapter.resolve_session({"session_id": "sess-abc"})
        assert sm.state_file == claude_state_dir / "state_sess-abc.json"
        assert sm.state_file.exists()

    def test_session_key_from_env(self, claude_state_dir, disable_env_vars, monkeypatch):
        """CLAUDE_SESSION_KEY env var is used when no session_id in input."""
        monkeypatch.setenv("CLAUDE_SESSION_KEY", "custom-key")
        sm = adapter.resolve_session({})
        assert sm.state_file == claude_state_dir / "state_custom-key.json"

    def test_fallback_to_pid(self, claude_state_dir, disable_env_vars, monkeypatch):
        """Without session_id or env var, falls back to PID-based key."""
        # Mock _get_grandparent_pid to return a known value
        monkeypatch.setattr(adapter, "_get_grandparent_pid", lambda: "12345")
        sm = adapter.resolve_session({})
        assert sm.state_file == claude_state_dir / "state_12345.json"

    def test_init_state_called(self, claude_state_dir, disable_env_vars):
        """Returned StateManager has init_state() called (file exists with {})."""
        sm = adapter.resolve_session({"session_id": "test-init"})
        assert sm.state_file.exists()
        data = json.loads(sm.state_file.read_text())
        assert data == {}

    def test_same_input_same_file(self, claude_state_dir, disable_env_vars):
        """Calling resolve_session twice with same input returns same file path."""
        sm1 = adapter.resolve_session({"session_id": "stable"})
        sm2 = adapter.resolve_session({"session_id": "stable"})
        assert sm1.state_file == sm2.state_file

    def test_session_id_takes_priority_over_env(self, claude_state_dir, disable_env_vars, monkeypatch):
        """session_id in input takes priority over CLAUDE_SESSION_KEY."""
        monkeypatch.setenv("CLAUDE_SESSION_KEY", "env-key")
        sm = adapter.resolve_session({"session_id": "input-key"})
        assert sm.state_file == claude_state_dir / "state_input-key.json"


# ── ensure_session_initialized tests ─────────────────────────────────────────


class TestEnsureSessionInitialized:
    def _make_state(self, claude_state_dir, key="test"):
        sm = StateManager(
            state_dir=claude_state_dir,
            state_file=claude_state_dir / f"state_{key}.json",
            lock_path=claude_state_dir / f".lock_{key}",
        )
        sm.init_state()
        return sm

    def test_sets_all_keys(self, claude_state_dir, disable_env_vars):
        """First call sets all expected keys."""
        sm = self._make_state(claude_state_dir, "all-keys")
        adapter.ensure_session_initialized(sm, {"session_id": "sid-1"})
        assert sm.get("session_id") == "sid-1"
        assert sm.get("session_start_time") is not None
        assert sm.get("project_name") is not None
        assert sm.get("trace_count") == "0"
        assert sm.get("tool_count") == "0"
        assert sm.get("user_id") is not None

    def test_idempotent(self, claude_state_dir, disable_env_vars):
        """Second call is a no-op — values unchanged."""
        sm = self._make_state(claude_state_dir, "idempotent")
        adapter.ensure_session_initialized(sm, {"session_id": "sid-2"})
        start_time = sm.get("session_start_time")
        adapter.ensure_session_initialized(sm, {"session_id": "sid-different"})
        assert sm.get("session_id") == "sid-2"
        assert sm.get("session_start_time") == start_time

    def test_session_id_from_input(self, claude_state_dir, disable_env_vars):
        """session_id from input is used when present."""
        sm = self._make_state(claude_state_dir, "from-input")
        adapter.ensure_session_initialized(sm, {"session_id": "my-session"})
        assert sm.get("session_id") == "my-session"

    def test_session_id_generated(self, claude_state_dir, disable_env_vars):
        """session_id is generated (32-hex) when not in input."""
        sm = self._make_state(claude_state_dir, "generated")
        adapter.ensure_session_initialized(sm, {})
        sid = sm.get("session_id")
        assert sid is not None
        assert len(sid) == 32
        int(sid, 16)  # should not raise

    def test_project_name_from_env(self, claude_state_dir, monkeypatch):
        """ARIZE_PROJECT_NAME env var takes priority over cwd."""
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        monkeypatch.setenv("ARIZE_PROJECT_NAME", "my-env-project")
        monkeypatch.delenv("ARIZE_USER_ID", raising=False)
        sm = self._make_state(claude_state_dir, "proj-env")
        adapter.ensure_session_initialized(sm, {"cwd": "/home/user/other-project"})
        assert sm.get("project_name") == "my-env-project"

    def test_project_name_from_cwd_input(self, claude_state_dir, disable_env_vars):
        """project_name from input cwd -> basename extracted."""
        sm = self._make_state(claude_state_dir, "proj-cwd")
        adapter.ensure_session_initialized(sm, {"cwd": "/home/user/my-project"})
        assert sm.get("project_name") == "my-project"

    def test_project_name_from_config(self, claude_state_dir, monkeypatch):
        """config.json project_name is used when no env override is set (issue #74)."""
        from core.common import env as core_env

        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        monkeypatch.delenv("ARIZE_PROJECT_NAME", raising=False)
        cfg = {"harnesses": {"claude-code": {"project_name": "from-config", "target": "phoenix"}}}
        monkeypatch.setattr("core.config.load_config", lambda: cfg)
        core_env.invalidate_caches()

        sm = self._make_state(claude_state_dir, "proj-config")
        adapter.ensure_session_initialized(sm, {"cwd": "/home/user/other-project"})
        assert sm.get("project_name") == "from-config"

    def test_phoenix_project_env_used_on_phoenix_config(self, claude_state_dir, monkeypatch):
        """On the Phoenix backend, PHOENIX_PROJECT wins over config project_name."""
        from core.common import env as core_env

        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        monkeypatch.delenv("ARIZE_PROJECT_NAME", raising=False)
        monkeypatch.setenv("PHOENIX_PROJECT", "from-phoenix-env")
        cfg = {"harnesses": {"claude-code": {"project_name": "from-config", "target": "phoenix"}}}
        monkeypatch.setattr("core.config.load_config", lambda: cfg)
        core_env.invalidate_caches()

        sm = self._make_state(claude_state_dir, "proj-phoenix-env")
        adapter.ensure_session_initialized(sm, {"cwd": "/home/user/other-project"})
        assert sm.get("project_name") == "from-phoenix-env"

    def test_arize_project_name_ignored_on_phoenix_config(self, claude_state_dir, monkeypatch):
        """A baked ARIZE_PROJECT_NAME does not shadow config on the Phoenix backend (issue #74)."""
        from core.common import env as core_env

        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        monkeypatch.setenv("ARIZE_PROJECT_NAME", "claude-code")  # installer's baked default
        cfg = {"harnesses": {"claude-code": {"project_name": "my-phoenix-project", "target": "phoenix"}}}
        monkeypatch.setattr("core.config.load_config", lambda: cfg)
        core_env.invalidate_caches()

        sm = self._make_state(claude_state_dir, "proj-arize-ignored")
        adapter.ensure_session_initialized(sm, {"cwd": "/home/user/other-project"})
        assert sm.get("project_name") == "my-phoenix-project"

    def test_user_id_from_env(self, claude_state_dir, monkeypatch):
        """ARIZE_USER_ID env var takes priority over input."""
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        monkeypatch.setenv("ARIZE_USER_ID", "env-user")
        monkeypatch.delenv("ARIZE_PROJECT_NAME", raising=False)
        sm = self._make_state(claude_state_dir, "uid-env")
        adapter.ensure_session_initialized(sm, {"user_id": "input-user"})
        assert sm.get("user_id") == "env-user"

    def test_counters_start_at_zero(self, claude_state_dir, disable_env_vars):
        """trace_count and tool_count start at '0'."""
        sm = self._make_state(claude_state_dir, "counters")
        adapter.ensure_session_initialized(sm, {})
        assert sm.get("trace_count") == "0"
        assert sm.get("tool_count") == "0"


# ── gc_stale_state_files tests ───────────────────────────────────────────────


class TestGcStaleStateFiles:
    def test_dead_pid_removed(self, claude_state_dir, disable_env_vars, monkeypatch):
        """state file for a dead PID is removed."""
        dead_pid = 99999
        state_file = claude_state_dir / f"state_{dead_pid}.json"
        state_file.write_text("{}")
        monkeypatch.setattr(adapter, "_is_pid_alive", lambda pid: False)
        adapter.gc_stale_state_files()
        assert not state_file.exists()

    def test_live_pid_kept(self, claude_state_dir, disable_env_vars, monkeypatch):
        """state file for a live PID is kept."""
        live_pid = os.getpid()
        state_file = claude_state_dir / f"state_{live_pid}.json"
        state_file.write_text("{}")
        monkeypatch.setattr(adapter, "_is_pid_alive", lambda pid: pid == live_pid)
        adapter.gc_stale_state_files()
        assert state_file.exists()

    def test_non_numeric_key_kept(self, claude_state_dir, disable_env_vars):
        """state file with non-numeric key (session-id based) is never GC'd."""
        state_file = claude_state_dir / "state_sess-abc123.json"
        state_file.write_text("{}")
        adapter.gc_stale_state_files()
        assert state_file.exists()

    def test_lock_dir_removed(self, claude_state_dir, disable_env_vars, monkeypatch):
        """Lock dir is removed when state file is removed."""
        dead_pid = 99998
        state_file = claude_state_dir / f"state_{dead_pid}.json"
        state_file.write_text("{}")
        lock_dir = claude_state_dir / f".lock_{dead_pid}"
        lock_dir.mkdir()
        monkeypatch.setattr(adapter, "_is_pid_alive", lambda pid: False)
        adapter.gc_stale_state_files()
        assert not state_file.exists()
        assert not lock_dir.exists()

    def test_empty_dir_no_error(self, claude_state_dir, disable_env_vars):
        """Empty STATE_DIR causes no errors."""
        # Remove any files that might exist
        for f in claude_state_dir.glob("state_*.json"):
            f.unlink()
        adapter.gc_stale_state_files()  # should not raise


# ── check_requirements tests ─────────────────────────────────────────────────


class TestCheckRequirements:
    def test_enabled_returns_true(self, tmp_harness_dir, monkeypatch):
        """trace_enabled=True -> returns True and STATE_DIR exists."""
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        state_dir = tmp_harness_dir / "state" / "claude-code-check"
        monkeypatch.setattr(adapter, "STATE_DIR", state_dir)
        assert adapter.check_requirements() is True
        assert state_dir.is_dir()

    def test_disabled_returns_false(self, tmp_harness_dir, monkeypatch):
        """trace_enabled=False -> returns False, STATE_DIR not created."""
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "false")
        state_dir = tmp_harness_dir / "state" / "claude-code-nope"
        monkeypatch.setattr(adapter, "STATE_DIR", state_dir)
        assert adapter.check_requirements() is False
        assert not state_dir.exists()


# ── _get_grandparent_pid tests ───────────────────────────────────────────────


class TestGetGrandparentPid:
    def test_reads_from_proc_stat(self, monkeypatch):
        """Linux path: reads grandparent PID from /proc/{ppid}/stat."""
        monkeypatch.setattr(os, "getppid", lambda: 100)
        fake_stat = "100 (python) S 456 0 0 0"
        with patch("builtins.open", mock_open(read_data=fake_stat)):
            result = adapter._get_grandparent_pid()
        assert result == "456"

    def test_falls_back_to_ps_command(self, monkeypatch):
        """When /proc read fails, falls back to ps command."""
        monkeypatch.setattr(os, "getppid", lambda: 100)
        with patch("builtins.open", side_effect=OSError("no /proc")):
            with patch("subprocess.check_output", return_value=b"  789  \n"):
                result = adapter._get_grandparent_pid()
        assert result == "789"

    def test_falls_back_to_ppid(self, monkeypatch):
        """When both /proc and ps fail, falls back to parent PID."""
        monkeypatch.setattr(os, "getppid", lambda: 42)
        with patch("builtins.open", side_effect=OSError("no /proc")):
            with patch(
                "subprocess.check_output",
                side_effect=subprocess.SubprocessError("ps failed"),
            ):
                result = adapter._get_grandparent_pid()
        assert result == "42"

    def test_ppid_zero_returns_own_pid(self, monkeypatch):
        """When ppid is 0, returns current process PID."""
        monkeypatch.setattr(os, "getppid", lambda: 0)
        result = adapter._get_grandparent_pid()
        assert result == str(os.getpid())


# ── _is_pid_alive tests ─────────────────────────────────────────────────────


class TestIsPidAlive:
    def test_own_pid_is_alive(self):
        """Current process PID should be alive."""
        assert adapter._is_pid_alive(os.getpid()) is True

    def test_dead_pid(self):
        """A very high PID that doesn't exist should be dead."""
        assert adapter._is_pid_alive(99999) is False

    def test_zero_returns_false(self):
        """PID 0 should return False."""
        assert adapter._is_pid_alive(0) is False

    def test_negative_returns_false(self):
        """Negative PID should return False."""
        assert adapter._is_pid_alive(-1) is False


# ── resolve_transcript_path tests ─────────────────────────────────────────


class TestResolveTranscriptPath:
    def test_returns_transcript_path_when_present_and_exists(self, tmp_path):
        """Uses transcript_path from input when the file exists."""
        tf = tmp_path / "session.jsonl"
        tf.write_text("{}\n")
        result = resolve_transcript_path({"transcript_path": str(tf)}, "sess-1")
        assert result == tf

    def test_returns_agent_transcript_path_when_present_and_exists(self, tmp_path):
        """Uses agent_transcript_path from input when transcript_path is absent."""
        tf = tmp_path / "agent.jsonl"
        tf.write_text("{}\n")
        result = resolve_transcript_path({"agent_transcript_path": str(tf)}, "sess-1")
        assert result == tf

    def test_agent_transcript_path_takes_priority_over_main(self, tmp_path):
        """agent_transcript_path is checked before transcript_path."""
        main_tf = tmp_path / "main.jsonl"
        main_tf.write_text("{}\n")
        agent_tf = tmp_path / "agent.jsonl"
        agent_tf.write_text("{}\n")
        result = resolve_transcript_path(
            {"transcript_path": str(main_tf), "agent_transcript_path": str(agent_tf)},
            "sess-1",
        )
        assert result == agent_tf

    def test_falls_back_to_canonical_path(self, tmp_path, monkeypatch):
        """Derives canonical path from cwd + session_id when no transcript_path in input."""
        session_id = "abc123"
        cwd = "/fake/test/dir"
        slug = cwd.replace("/", "-")
        project_dir = tmp_path / ".claude" / "projects" / slug
        project_dir.mkdir(parents=True)
        tf = project_dir / f"{session_id}.jsonl"
        tf.write_text("{}\n")
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        result = resolve_transcript_path({"cwd": cwd}, session_id)
        assert result == tf

    def test_falls_back_to_cwd_when_no_cwd_in_input(self, tmp_path, monkeypatch):
        """Uses os.getcwd() when input has no cwd field."""
        session_id = "def456"
        fake_cwd = "/proc/fake/cwd"
        slug = fake_cwd.replace("/", "-")
        project_dir = tmp_path / ".claude" / "projects" / slug
        project_dir.mkdir(parents=True)
        tf = project_dir / f"{session_id}.jsonl"
        tf.write_text("{}\n")
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setattr(os, "getcwd", lambda: fake_cwd)
        result = resolve_transcript_path({}, session_id)
        assert result == tf

    def test_returns_none_when_no_session_id(self):
        """Returns None when session_id is empty and no paths in input."""
        result = resolve_transcript_path({}, "")
        assert result is None

    def test_returns_none_when_file_does_not_exist(self, tmp_path, monkeypatch):
        """Returns None when canonical path doesn't exist on disk."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        result = resolve_transcript_path({"cwd": "/some/dir"}, "no-such-session")
        assert result is None

    def test_returns_none_when_transcript_path_not_a_file(self, tmp_path):
        """Returns None when transcript_path is provided but doesn't exist on disk."""
        result = resolve_transcript_path({"transcript_path": str(tmp_path / "nonexistent.jsonl")}, "sess-1")
        # Should fall through to canonical path, which also won't exist
        assert result is None

    def test_expands_tilde_in_transcript_path(self, tmp_path, monkeypatch):
        """Handles ~ in transcript_path via Path.expanduser()."""
        session_id = "tilde-test"
        project_dir = tmp_path / ".claude" / "projects" / "test"
        project_dir.mkdir(parents=True)
        tf = project_dir / f"{session_id}.jsonl"
        tf.write_text("{}\n")
        monkeypatch.setenv("HOME", str(tmp_path))
        result = resolve_transcript_path({"transcript_path": f"~/.claude/projects/test/{session_id}.jsonl"}, session_id)
        assert result is not None
        assert result.is_file()
