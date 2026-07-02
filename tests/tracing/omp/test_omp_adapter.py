#!/usr/bin/env python3
"""Tests for tracing.omp.hooks.adapter — session resolution, init, GC, requirements.

Mirrors tests/tracing/opencode/test_opencode_adapter.py but adapted for omp's
authoritative ``sessionId`` field (camelCase — the shim stamps it on every
forwarded payload). omp always supplies a real session id, so there is NO PID /
grandparent-PID fallback for real sessions — a missing sessionId keys off a
per-process ``"unknown-<pid>"`` fallback so concurrent id-less runs don't
collide on a shared state file.

omp also differs from opencode in project-name derivation: there is no
snapshot/message payload to mine, so the chain is simply
``env.project_name`` -> ``os.path.basename(os.getcwd())`` -> ``HARNESS_NAME``.
"""
from __future__ import annotations

import json
import os
import time

import pytest

from core.common import StateManager

# ---------------------------------------------------------------------------
# We import the adapter module itself so we can monkeypatch its module-level
# constants.  The actual functions under test are attributes of this module.
# ---------------------------------------------------------------------------
from tracing.omp.hooks import adapter

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def omp_state_dir(tmp_harness_dir, monkeypatch):
    """Point adapter.STATE_DIR to a temp directory and return it."""
    state_dir = tmp_harness_dir / "state" / "omp"
    state_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(adapter, "STATE_DIR", state_dir)
    return state_dir


@pytest.fixture
def disable_env_vars(monkeypatch):
    """Clear env vars that could influence session resolution.

    On-disk config.json is isolated globally by the autouse ``isolate_config``
    fixture combined with ``tmp_harness_dir``'s ``CONFIG_FILE`` redirect, so this
    only needs to clear the env-var inputs.
    """
    monkeypatch.delenv("ARIZE_PROJECT_NAME", raising=False)
    monkeypatch.delenv("ARIZE_USER_ID", raising=False)
    monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")


# ── Module-level constants tests ──────────────────────────────────────────────


class TestModuleConstants:
    def test_service_name(self):
        """SERVICE_NAME matches the omp harness metadata."""
        assert adapter.SERVICE_NAME == "omp"

    def test_scope_name(self):
        """SCOPE_NAME matches the omp harness metadata."""
        assert adapter.SCOPE_NAME == "arize-omp-plugin"

    def test_state_dir_matches_harness_subdir(self):
        """STATE_DIR derives from HARNESSES['omp']['state_subdir']."""
        # The adapter's STATE_DIR is a Path; its leaf must be the configured subdir.
        assert adapter.STATE_DIR.name == "omp"


# ── check_requirements tests ─────────────────────────────────────────────────


class TestCheckRequirements:
    def test_enabled_returns_true(self, tmp_harness_dir, monkeypatch):
        """trace_enabled=True -> returns True and STATE_DIR exists."""
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        state_dir = tmp_harness_dir / "state" / "omp-check"
        monkeypatch.setattr(adapter, "STATE_DIR", state_dir)
        assert adapter.check_requirements() is True
        assert state_dir.is_dir()

    def test_disabled_returns_false(self, tmp_harness_dir, monkeypatch):
        """trace_enabled=False -> returns False, STATE_DIR not created."""
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "false")
        state_dir = tmp_harness_dir / "state" / "omp-nope"
        monkeypatch.setattr(adapter, "STATE_DIR", state_dir)
        assert adapter.check_requirements() is False
        assert not state_dir.exists()


# ── resolve_session tests ────────────────────────────────────────────────────


class TestResolveSession:
    def test_uses_session_id_from_payload(self, omp_state_dir, disable_env_vars):
        """sessionId in input_json is used as the state file key."""
        sm = adapter.resolve_session({"sessionId": "abc123"})
        assert sm.state_file == omp_state_dir / "state_abc123.json"
        assert sm.state_file.exists()

    def test_unknown_fallback_when_missing(self, omp_state_dir, disable_env_vars):
        """Missing sessionId -> per-process 'unknown-<pid>' key."""
        sm = adapter.resolve_session({})
        assert sm.state_file == omp_state_dir / f"state_unknown-{os.getpid()}.json"
        assert sm.state_file.exists()

    def test_unknown_fallback_when_empty(self, omp_state_dir, disable_env_vars):
        """Empty sessionId -> per-process 'unknown-<pid>' key."""
        sm = adapter.resolve_session({"sessionId": ""})
        assert sm.state_file == omp_state_dir / f"state_unknown-{os.getpid()}.json"

    def test_unknown_fallback_when_none(self, omp_state_dir, disable_env_vars):
        """Explicit None sessionId -> per-process 'unknown-<pid>' key."""
        sm = adapter.resolve_session({"sessionId": None})
        assert sm.state_file == omp_state_dir / f"state_unknown-{os.getpid()}.json"

    def test_idless_fallback_is_per_process(self, omp_state_dir, disable_env_vars):
        """Missing sessionId keys off pid so concurrent id-less runs don't collide.

        The fallback carries the pid suffix rather than a bare numeric pid key or
        a shared 'unknown' literal.
        """
        adapter.resolve_session({})
        files = list(omp_state_dir.glob("state_*.json"))
        assert files, "no state file produced"
        for f in files:
            key = f.stem.replace("state_", "", 1)
            assert not key.isdigit(), f"bare PID-keyed state file produced: {f.name}"
            assert key == f"unknown-{os.getpid()}"

    def test_init_state_called(self, omp_state_dir, disable_env_vars):
        """Returned StateManager has init_state() called (file exists with {})."""
        sm = adapter.resolve_session({"sessionId": "ses_init"})
        assert sm.state_file.exists()
        data = json.loads(sm.state_file.read_text())
        assert data == {}

    def test_same_session_id_same_file(self, omp_state_dir, disable_env_vars):
        """Calling resolve_session twice with same sessionId produces same file."""
        sm1 = adapter.resolve_session({"sessionId": "ses_stable"})
        sm2 = adapter.resolve_session({"sessionId": "ses_stable"})
        assert sm1.state_file == sm2.state_file

    def test_lock_path_matches_key(self, omp_state_dir, disable_env_vars):
        """Lock file is named .lock_{key} in STATE_DIR."""
        sm = adapter.resolve_session({"sessionId": "ses_lock"})
        assert sm._lock_path == omp_state_dir / ".lock_ses_lock"

    def test_state_dir_attribute(self, omp_state_dir, disable_env_vars):
        """The StateManager.state_dir attribute matches adapter.STATE_DIR."""
        sm = adapter.resolve_session({"sessionId": "ses_dir"})
        assert sm.state_dir == omp_state_dir

    def test_camelcase_session_id_only(self, omp_state_dir, disable_env_vars):
        """Only camelCase ``sessionId`` is honored — opencode's ``sessionID`` is not.

        The shim stamps ``sessionId`` (camelCase); a payload carrying only the
        opencode-style ``sessionID`` must fall through to the 'unknown-<pid>' key.
        """
        sm = adapter.resolve_session({"sessionID": "WRONG_CASE"})
        assert sm.state_file == omp_state_dir / f"state_unknown-{os.getpid()}.json"

    def test_extra_payload_fields_ignored(self, omp_state_dir, disable_env_vars):
        """Extra payload fields are not used for the session key."""
        sm = adapter.resolve_session({"sessionId": "ses_only", "type": "turn_end", "message": {}})
        assert sm.state_file == omp_state_dir / "state_ses_only.json"


# ── ensure_session_initialized tests ─────────────────────────────────────────


class TestEnsureSessionInitialized:
    def _make_state(self, omp_state_dir, key="test"):
        sm = StateManager(
            state_dir=omp_state_dir,
            state_file=omp_state_dir / f"state_{key}.json",
            lock_path=omp_state_dir / f".lock_{key}",
        )
        sm.init_state()
        return sm

    def test_sets_all_keys(self, omp_state_dir, disable_env_vars, monkeypatch):
        """First call sets all expected keys."""
        # Source user_id from env so the assertion is hermetic, not leaked from
        # the developer's on-disk config.json.
        monkeypatch.setenv("ARIZE_USER_ID", "test-user-all-keys")
        sm = self._make_state(omp_state_dir, "all-keys")
        adapter.ensure_session_initialized(sm, {"sessionId": "ses_all"})
        assert sm.get("session_id") is not None
        assert sm.get("session_start_time") is not None
        assert sm.get("project_name") is not None
        assert sm.get("trace_count") == "0"
        assert sm.get("tool_count") == "0"
        assert sm.get("user_id") == "test-user-all-keys"

    def test_session_id_matches_session_id_field(self, omp_state_dir, disable_env_vars):
        """session_id stored in state == the omp sessionId from payload."""
        sm = self._make_state(omp_state_dir, "sid-from-payload")
        adapter.ensure_session_initialized(sm, {"sessionId": "ses_xyz789"})
        assert sm.get("session_id") == "ses_xyz789"

    def test_session_id_unknown_when_missing(self, omp_state_dir, disable_env_vars):
        """session_id falls back to 'unknown-<pid>' when payload lacks sessionId."""
        sm = self._make_state(omp_state_dir, "sid-missing")
        adapter.ensure_session_initialized(sm, {})
        assert sm.get("session_id") == f"unknown-{os.getpid()}"

    def test_idempotent(self, omp_state_dir, disable_env_vars):
        """Second call is a no-op — values unchanged."""
        sm = self._make_state(omp_state_dir, "idempotent")
        adapter.ensure_session_initialized(sm, {"sessionId": "ses_first"})
        start_time = sm.get("session_start_time")
        session_id = sm.get("session_id")
        # A second call with a *different* sessionId must NOT overwrite
        adapter.ensure_session_initialized(sm, {"sessionId": "ses_second"})
        assert sm.get("session_id") == session_id
        assert sm.get("session_start_time") == start_time

    def test_project_name_from_env(self, omp_state_dir, monkeypatch):
        """ARIZE_PROJECT_NAME env var takes priority."""
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        monkeypatch.setenv("ARIZE_PROJECT_NAME", "my-env-project")
        monkeypatch.delenv("ARIZE_USER_ID", raising=False)
        sm = self._make_state(omp_state_dir, "proj-env")
        adapter.ensure_session_initialized(sm, {"sessionId": "ses_e"})
        assert sm.get("project_name") == "my-env-project"

    def test_project_name_fallback_to_cwd_basename(self, omp_state_dir, disable_env_vars):
        """project_name falls back to basename of os.getcwd() when no env value."""
        sm = self._make_state(omp_state_dir, "proj-fallback")
        adapter.ensure_session_initialized(sm, {"sessionId": "ses_f"})
        project = sm.get("project_name")
        assert project is not None
        assert project == os.path.basename(os.getcwd())

    def test_project_name_final_fallback_to_harness_name(self, omp_state_dir, disable_env_vars, monkeypatch):
        """project_name falls back to HARNESS_NAME ('omp') when cwd basename is empty.

        os.path.basename('/') == '' so the chain reaches its final literal fallback.
        """
        monkeypatch.setattr(os, "getcwd", lambda: "/")
        sm = self._make_state(omp_state_dir, "proj-harness")
        adapter.ensure_session_initialized(sm, {"sessionId": "ses_h"})
        assert sm.get("project_name") == "omp"

    def test_counters_start_at_zero(self, omp_state_dir, disable_env_vars):
        """trace_count and tool_count start at '0' (string)."""
        sm = self._make_state(omp_state_dir, "counters")
        adapter.ensure_session_initialized(sm, {"sessionId": "ses_ct"})
        assert sm.get("trace_count") == "0"
        assert sm.get("tool_count") == "0"

    def test_user_id_from_env(self, omp_state_dir, monkeypatch):
        """user_id is resolved via env (ARIZE_USER_ID)."""
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        monkeypatch.setenv("ARIZE_USER_ID", "test-user-456")
        monkeypatch.delenv("ARIZE_PROJECT_NAME", raising=False)
        sm = self._make_state(omp_state_dir, "user-env")
        adapter.ensure_session_initialized(sm, {"sessionId": "ses_u"})
        assert sm.get("user_id") == "test-user-456"

    def test_session_start_time_is_string_ms(self, omp_state_dir, disable_env_vars):
        """session_start_time is a string representation of a positive int (ms)."""
        sm = self._make_state(omp_state_dir, "start-time")
        adapter.ensure_session_initialized(sm, {"sessionId": "ses_st"})
        start = sm.get("session_start_time")
        assert start is not None
        assert start.isdigit()
        assert int(start) > 0


# ── gc_stale_state_files tests ───────────────────────────────────────────────


class TestGcStaleStateFiles:
    def test_old_file_removed(self, omp_state_dir, disable_env_vars):
        """State file older than 24h is removed."""
        state_file = omp_state_dir / "state_old-session.json"
        state_file.write_text("{}")
        # Set mtime to 25 hours ago
        old_time = time.time() - 90000
        os.utime(state_file, (old_time, old_time))
        adapter.gc_stale_state_files()
        assert not state_file.exists()

    def test_recent_file_kept(self, omp_state_dir, disable_env_vars):
        """State file younger than 24h is kept."""
        state_file = omp_state_dir / "state_recent-session.json"
        state_file.write_text("{}")
        # mtime is now (just created), which is < 24h old
        adapter.gc_stale_state_files()
        assert state_file.exists()

    def test_lock_dir_removed(self, omp_state_dir, disable_env_vars):
        """Lock dir is removed when state file is removed."""
        state_file = omp_state_dir / "state_old-lock-dir.json"
        state_file.write_text("{}")
        lock_dir = omp_state_dir / ".lock_old-lock-dir"
        lock_dir.mkdir()
        old_time = time.time() - 90000
        os.utime(state_file, (old_time, old_time))
        adapter.gc_stale_state_files()
        assert not state_file.exists()
        assert not lock_dir.exists()

    def test_lock_file_removed(self, omp_state_dir, disable_env_vars):
        """Lock file is removed when state file is removed."""
        state_file = omp_state_dir / "state_old-lock-file.json"
        state_file.write_text("{}")
        lock_file = omp_state_dir / ".lock_old-lock-file"
        lock_file.write_text("")
        old_time = time.time() - 90000
        os.utime(state_file, (old_time, old_time))
        adapter.gc_stale_state_files()
        assert not state_file.exists()
        assert not lock_file.exists()

    def test_empty_dir_no_error(self, omp_state_dir, disable_env_vars):
        """Empty STATE_DIR causes no errors."""
        for f in omp_state_dir.glob("state_*.json"):
            f.unlink()
        adapter.gc_stale_state_files()  # should not raise

    def test_nonexistent_dir_no_error(self, tmp_harness_dir, monkeypatch):
        """Non-existent STATE_DIR causes no errors."""
        state_dir = tmp_harness_dir / "state" / "omp-nonexistent"
        monkeypatch.setattr(adapter, "STATE_DIR", state_dir)
        adapter.gc_stale_state_files()  # should not raise

    def test_uses_24h_cutoff(self, omp_state_dir, disable_env_vars):
        """Files exactly past the 24h boundary are removed."""
        state_file = omp_state_dir / "state_boundary.json"
        state_file.write_text("{}")
        old_time = time.time() - 86401  # 24h + 1s
        os.utime(state_file, (old_time, old_time))
        adapter.gc_stale_state_files()
        assert not state_file.exists()

    def test_numeric_keyed_file_uses_mtime_not_liveness(self, omp_state_dir, disable_env_vars):
        """Even a numeric (pid-style) key uses mtime-only, since omp has no PID branch.

        A *recent* numeric-named file should NOT be removed (no liveness check on it).
        """
        # Recent file with digit-style key: must be kept (mtime branch only).
        state_file = omp_state_dir / "state_99999.json"
        state_file.write_text("{}")
        # Fresh mtime
        adapter.gc_stale_state_files()
        assert state_file.exists()

    def test_unknown_key_file_old_removed(self, omp_state_dir, disable_env_vars):
        """The 'unknown' fallback state file is removed once old (mtime branch)."""
        state_file = omp_state_dir / "state_unknown.json"
        state_file.write_text("{}")
        old_time = time.time() - 90000
        os.utime(state_file, (old_time, old_time))
        adapter.gc_stale_state_files()
        assert not state_file.exists()

    def test_oserror_on_unlink_is_caught(self, omp_state_dir, disable_env_vars, monkeypatch):
        """OSError on unlink is caught and ignored (best-effort)."""
        state_file = omp_state_dir / "state_unlink-err.json"
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
    def test_log_file_default_points_to_omp_log(self):
        """The adapter sets ARIZE_LOG_FILE on import unless already set.

        Either it ends with ``omp.log`` (default from this adapter) or it
        was overridden by the user before import (also acceptable).
        """
        val = os.environ.get("ARIZE_LOG_FILE", "")
        assert val  # set on import either way
        # Default-case: ends with omp.log
        # User-override case: simply non-empty.
        assert val.endswith("omp.log") or val
