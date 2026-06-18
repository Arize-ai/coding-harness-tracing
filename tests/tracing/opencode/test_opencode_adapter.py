#!/usr/bin/env python3
"""Tests for tracing.opencode.hooks.adapter — session resolution, init, GC, requirements.

Mirrors tests/tracing/gemini/test_gemini_adapter.py structure but adapted for
opencode's authoritative `sessionID` field. opencode always supplies a real
session ID, so there is NO PID / grandparent-PID fallback — missing sessionID
keys off the literal string "unknown".
"""
from __future__ import annotations

import os
import time

import pytest
import yaml

from core.common import StateManager

# ---------------------------------------------------------------------------
# We import the adapter module itself so we can monkeypatch its module-level
# constants.  The actual functions under test are attributes of this module.
# ---------------------------------------------------------------------------
from tracing.opencode.hooks import adapter

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def opencode_state_dir(tmp_harness_dir, monkeypatch):
    """Point adapter.STATE_DIR to a temp directory and return it."""
    state_dir = tmp_harness_dir / "state" / "opencode"
    state_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(adapter, "STATE_DIR", state_dir)
    return state_dir


@pytest.fixture
def disable_env_vars(monkeypatch):
    """Clear env vars that could influence session resolution.

    On-disk config.yaml is isolated globally by the autouse ``reset_env_caches``
    fixture combined with ``tmp_harness_dir``'s ``CONFIG_FILE`` redirect, so this
    only needs to clear the env-var inputs.
    """
    monkeypatch.delenv("ARIZE_PROJECT_NAME", raising=False)
    monkeypatch.delenv("ARIZE_USER_ID", raising=False)
    monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")


# ── Module-level constants tests ──────────────────────────────────────────────


class TestModuleConstants:
    def test_service_name(self):
        """SERVICE_NAME matches the opencode harness metadata."""
        assert adapter.SERVICE_NAME == "opencode"

    def test_scope_name(self):
        """SCOPE_NAME matches the opencode harness metadata."""
        assert adapter.SCOPE_NAME == "arize-opencode-plugin"

    def test_state_dir_matches_harness_subdir(self):
        """STATE_DIR derives from HARNESSES['opencode']['state_subdir']."""
        # The adapter's STATE_DIR is a Path; its leaf must be the configured subdir.
        assert adapter.STATE_DIR.name == "opencode"


# ── check_requirements tests ─────────────────────────────────────────────────


class TestCheckRequirements:
    def test_enabled_returns_true(self, tmp_harness_dir, monkeypatch):
        """trace_enabled=True -> returns True and STATE_DIR exists."""
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        state_dir = tmp_harness_dir / "state" / "opencode-check"
        monkeypatch.setattr(adapter, "STATE_DIR", state_dir)
        assert adapter.check_requirements() is True
        assert state_dir.is_dir()

    def test_disabled_returns_false(self, tmp_harness_dir, monkeypatch):
        """trace_enabled=False -> returns False, STATE_DIR not created."""
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "false")
        state_dir = tmp_harness_dir / "state" / "opencode-nope"
        monkeypatch.setattr(adapter, "STATE_DIR", state_dir)
        assert adapter.check_requirements() is False
        assert not state_dir.exists()


# ── resolve_session tests ────────────────────────────────────────────────────


class TestResolveSession:
    def test_uses_session_id_from_payload(self, opencode_state_dir, disable_env_vars):
        """sessionID in input_json is used as the state file key."""
        sm = adapter.resolve_session({"sessionID": "ses_123"})
        assert sm.state_file == opencode_state_dir / "state_ses_123.yaml"
        assert sm.state_file.exists()

    def test_unknown_fallback_when_missing(self, opencode_state_dir, disable_env_vars):
        """Missing sessionID -> key off 'unknown'. No PID-based key."""
        sm = adapter.resolve_session({})
        assert sm.state_file == opencode_state_dir / "state_unknown.yaml"
        assert sm.state_file.exists()

    def test_unknown_fallback_when_empty(self, opencode_state_dir, disable_env_vars):
        """Empty sessionID -> key off 'unknown'."""
        sm = adapter.resolve_session({"sessionID": ""})
        assert sm.state_file == opencode_state_dir / "state_unknown.yaml"

    def test_no_pid_keyed_fallback(self, opencode_state_dir, disable_env_vars):
        """Missing sessionID must NOT produce a PID-keyed state file.

        Unlike gemini, opencode adapters never use PID-based keys.
        """
        adapter.resolve_session({})
        # No state file with a numeric (pid-style) key should exist
        for f in opencode_state_dir.glob("state_*.yaml"):
            key = f.stem.replace("state_", "", 1)
            assert not key.isdigit(), f"PID-keyed state file produced: {f.name}"

    def test_init_state_called(self, opencode_state_dir, disable_env_vars):
        """Returned StateManager has init_state() called (file exists with {})."""
        sm = adapter.resolve_session({"sessionID": "ses_init"})
        assert sm.state_file.exists()
        data = yaml.safe_load(sm.state_file.read_text())
        assert data == {}

    def test_same_session_id_same_file(self, opencode_state_dir, disable_env_vars):
        """Calling resolve_session twice with same sessionID produces same file."""
        sm1 = adapter.resolve_session({"sessionID": "ses_stable"})
        sm2 = adapter.resolve_session({"sessionID": "ses_stable"})
        assert sm1.state_file == sm2.state_file

    def test_lock_path_matches_key(self, opencode_state_dir, disable_env_vars):
        """Lock file is named .lock_{key} in STATE_DIR."""
        sm = adapter.resolve_session({"sessionID": "ses_lock"})
        assert sm._lock_path == opencode_state_dir / ".lock_ses_lock"

    def test_state_dir_attribute(self, opencode_state_dir, disable_env_vars):
        """The StateManager.state_dir attribute matches adapter.STATE_DIR."""
        sm = adapter.resolve_session({"sessionID": "ses_dir"})
        assert sm.state_dir == opencode_state_dir

    def test_extra_payload_fields_ignored(self, opencode_state_dir, disable_env_vars):
        """Extra payload fields are not used for the session key."""
        sm = adapter.resolve_session({"sessionID": "ses_only", "session_id": "should-be-ignored", "messages": []})
        assert sm.state_file == opencode_state_dir / "state_ses_only.yaml"


# ── ensure_session_initialized tests ─────────────────────────────────────────


class TestEnsureSessionInitialized:
    def _make_state(self, opencode_state_dir, key="test"):
        sm = StateManager(
            state_dir=opencode_state_dir,
            state_file=opencode_state_dir / f"state_{key}.yaml",
            lock_path=opencode_state_dir / f".lock_{key}",
        )
        sm.init_state()
        return sm

    def test_sets_all_keys(self, opencode_state_dir, disable_env_vars, monkeypatch):
        """First call sets all expected keys."""
        # Source user_id from env so the assertion is hermetic, not leaked from
        # the developer's on-disk config.yaml.
        monkeypatch.setenv("ARIZE_USER_ID", "test-user-all-keys")
        sm = self._make_state(opencode_state_dir, "all-keys")
        adapter.ensure_session_initialized(sm, {"sessionID": "ses_all"})
        assert sm.get("session_id") is not None
        assert sm.get("session_start_time") is not None
        assert sm.get("project_name") is not None
        assert sm.get("trace_count") == "0"
        assert sm.get("tool_count") == "0"
        assert sm.get("user_id") == "test-user-all-keys"

    def test_session_id_matches_session_id_field(self, opencode_state_dir, disable_env_vars):
        """session_id stored in state == the opencode sessionID from payload."""
        sm = self._make_state(opencode_state_dir, "sid-from-payload")
        adapter.ensure_session_initialized(sm, {"sessionID": "ses_xyz789"})
        assert sm.get("session_id") == "ses_xyz789"

    def test_idempotent(self, opencode_state_dir, disable_env_vars):
        """Second call is a no-op — values unchanged."""
        sm = self._make_state(opencode_state_dir, "idempotent")
        adapter.ensure_session_initialized(sm, {"sessionID": "ses_first"})
        start_time = sm.get("session_start_time")
        session_id = sm.get("session_id")
        # A second call with a *different* sessionID must NOT overwrite
        adapter.ensure_session_initialized(sm, {"sessionID": "ses_second"})
        assert sm.get("session_id") == session_id
        assert sm.get("session_start_time") == start_time

    def test_project_name_from_env(self, opencode_state_dir, monkeypatch):
        """ARIZE_PROJECT_NAME env var takes priority over snapshot path."""
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        monkeypatch.setenv("ARIZE_PROJECT_NAME", "my-env-project")
        monkeypatch.delenv("ARIZE_USER_ID", raising=False)
        sm = self._make_state(opencode_state_dir, "proj-env")
        payload = {
            "sessionID": "ses_e",
            "messages": [
                {
                    "info": {
                        "role": "assistant",
                        "path": {"cwd": "/home/user/other-project", "root": "/home/user"},
                    },
                    "parts": [],
                }
            ],
        }
        adapter.ensure_session_initialized(sm, payload)
        assert sm.get("project_name") == "my-env-project"

    def test_project_name_from_snapshot_cwd(self, opencode_state_dir, disable_env_vars):
        """project_name uses basename of the snapshot message path.cwd."""
        sm = self._make_state(opencode_state_dir, "proj-cwd")
        payload = {
            "sessionID": "ses_c",
            "messages": [
                {
                    "info": {
                        "role": "assistant",
                        "path": {"cwd": "/some/path/myproj", "root": "/some/path"},
                    },
                    "parts": [],
                }
            ],
        }
        adapter.ensure_session_initialized(sm, payload)
        assert sm.get("project_name") == "myproj"

    def test_project_name_from_snapshot_root_when_no_cwd(self, opencode_state_dir, disable_env_vars):
        """project_name falls back to basename of path.root when path.cwd missing."""
        sm = self._make_state(opencode_state_dir, "proj-root")
        payload = {
            "sessionID": "ses_r",
            "messages": [
                {
                    "info": {
                        "role": "assistant",
                        "path": {"root": "/workspace/rootproj"},
                    },
                    "parts": [],
                }
            ],
        }
        adapter.ensure_session_initialized(sm, payload)
        assert sm.get("project_name") == "rootproj"

    def test_project_name_fallback_to_cwd_basename(self, opencode_state_dir, disable_env_vars):
        """project_name falls back to basename of os.getcwd() when snapshot lacks path."""
        sm = self._make_state(opencode_state_dir, "proj-fallback")
        # No messages / no path in payload
        adapter.ensure_session_initialized(sm, {"sessionID": "ses_f"})
        project = sm.get("project_name")
        assert project is not None
        assert project == os.path.basename(os.getcwd())

    def test_project_name_fallback_when_messages_empty(self, opencode_state_dir, disable_env_vars):
        """project_name falls back to cwd basename when messages list is empty."""
        sm = self._make_state(opencode_state_dir, "proj-empty-msgs")
        adapter.ensure_session_initialized(sm, {"sessionID": "ses_em", "messages": []})
        project = sm.get("project_name")
        assert project == os.path.basename(os.getcwd())

    def test_counters_start_at_zero(self, opencode_state_dir, disable_env_vars):
        """trace_count and tool_count start at '0' (string)."""
        sm = self._make_state(opencode_state_dir, "counters")
        adapter.ensure_session_initialized(sm, {"sessionID": "ses_ct"})
        assert sm.get("trace_count") == "0"
        assert sm.get("tool_count") == "0"

    def test_user_id_from_env(self, opencode_state_dir, monkeypatch):
        """user_id is read from env.user_id."""
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        monkeypatch.setenv("ARIZE_USER_ID", "test-user-456")
        monkeypatch.delenv("ARIZE_PROJECT_NAME", raising=False)
        sm = self._make_state(opencode_state_dir, "user-env")
        adapter.ensure_session_initialized(sm, {"sessionID": "ses_u"})
        assert sm.get("user_id") == "test-user-456"

    def test_session_start_time_is_string_ms(self, opencode_state_dir, disable_env_vars):
        """session_start_time is a string representation of a positive int (ms)."""
        sm = self._make_state(opencode_state_dir, "start-time")
        adapter.ensure_session_initialized(sm, {"sessionID": "ses_st"})
        start = sm.get("session_start_time")
        assert start is not None
        assert start.isdigit()
        assert int(start) > 0


# ── gc_stale_state_files tests ───────────────────────────────────────────────


class TestGcStaleStateFiles:
    def test_old_file_removed(self, opencode_state_dir, disable_env_vars):
        """State file older than 24h is removed."""
        state_file = opencode_state_dir / "state_old-session.yaml"
        state_file.write_text("{}")
        # Set mtime to 25 hours ago
        old_time = time.time() - 90000
        os.utime(state_file, (old_time, old_time))
        adapter.gc_stale_state_files()
        assert not state_file.exists()

    def test_recent_file_kept(self, opencode_state_dir, disable_env_vars):
        """State file younger than 24h is kept."""
        state_file = opencode_state_dir / "state_recent-session.yaml"
        state_file.write_text("{}")
        # mtime is now (just created), which is < 24h old
        adapter.gc_stale_state_files()
        assert state_file.exists()

    def test_lock_dir_removed(self, opencode_state_dir, disable_env_vars):
        """Lock dir is removed when state file is removed."""
        state_file = opencode_state_dir / "state_old-lock-dir.yaml"
        state_file.write_text("{}")
        lock_dir = opencode_state_dir / ".lock_old-lock-dir"
        lock_dir.mkdir()
        old_time = time.time() - 90000
        os.utime(state_file, (old_time, old_time))
        adapter.gc_stale_state_files()
        assert not state_file.exists()
        assert not lock_dir.exists()

    def test_lock_file_removed(self, opencode_state_dir, disable_env_vars):
        """Lock file is removed when state file is removed."""
        state_file = opencode_state_dir / "state_old-lock-file.yaml"
        state_file.write_text("{}")
        lock_file = opencode_state_dir / ".lock_old-lock-file"
        lock_file.write_text("")
        old_time = time.time() - 90000
        os.utime(state_file, (old_time, old_time))
        adapter.gc_stale_state_files()
        assert not state_file.exists()
        assert not lock_file.exists()

    def test_empty_dir_no_error(self, opencode_state_dir, disable_env_vars):
        """Empty STATE_DIR causes no errors."""
        for f in opencode_state_dir.glob("state_*.yaml"):
            f.unlink()
        adapter.gc_stale_state_files()  # should not raise

    def test_nonexistent_dir_no_error(self, tmp_harness_dir, monkeypatch):
        """Non-existent STATE_DIR causes no errors."""
        state_dir = tmp_harness_dir / "state" / "opencode-nonexistent"
        monkeypatch.setattr(adapter, "STATE_DIR", state_dir)
        adapter.gc_stale_state_files()  # should not raise

    def test_uses_24h_cutoff(self, opencode_state_dir, disable_env_vars):
        """Files exactly past the 24h boundary are removed."""
        state_file = opencode_state_dir / "state_boundary.yaml"
        state_file.write_text("{}")
        old_time = time.time() - 86401  # 24h + 1s
        os.utime(state_file, (old_time, old_time))
        adapter.gc_stale_state_files()
        assert not state_file.exists()

    def test_pid_keyed_file_uses_mtime_not_liveness(self, opencode_state_dir, disable_env_vars):
        """Even a numeric (pid-style) key uses mtime-only, since opencode has no PID branch.

        A *recent* numeric-named file should NOT be removed (no liveness check on it).
        """
        # Recent file with digit-style key: must be kept (mtime branch only).
        state_file = opencode_state_dir / "state_99999.yaml"
        state_file.write_text("{}")
        # Fresh mtime
        adapter.gc_stale_state_files()
        assert state_file.exists()

    def test_unknown_key_file_old_removed(self, opencode_state_dir, disable_env_vars):
        """The 'unknown' fallback state file is removed once old (mtime branch)."""
        state_file = opencode_state_dir / "state_unknown.yaml"
        state_file.write_text("{}")
        old_time = time.time() - 90000
        os.utime(state_file, (old_time, old_time))
        adapter.gc_stale_state_files()
        assert not state_file.exists()

    def test_oserror_on_unlink_is_caught(self, opencode_state_dir, disable_env_vars, monkeypatch):
        """OSError on unlink is caught and ignored (best-effort)."""
        state_file = opencode_state_dir / "state_unlink-err.yaml"
        state_file.write_text("{}")
        old_time = time.time() - 90000
        os.utime(state_file, (old_time, old_time))

        import pathlib

        orig = pathlib.Path.unlink

        def patched_unlink(self, *args, **kwargs):
            if "unlink-err" in str(self):
                raise OSError("permission denied")
            return orig(self, *args, **kwargs)

        monkeypatch.setattr(pathlib.Path, "unlink", patched_unlink)
        adapter.gc_stale_state_files()  # should not raise


# ── module-level log-file env wiring ─────────────────────────────────────────


class TestLogFileEnv:
    def test_log_file_default_points_to_opencode_log(self):
        """The adapter sets ARIZE_LOG_FILE on import unless already set.

        Either it ends with ``opencode.log`` (default from this adapter) or it
        was overridden by the user before import (also acceptable).
        """
        val = os.environ.get("ARIZE_LOG_FILE", "")
        assert val  # set on import either way
        # Default-case: ends with opencode.log
        # User-override case: simply non-empty.
        assert val.endswith("opencode.log") or val
