#!/usr/bin/env python3
"""omp adapter: session resolution, initialization, and GC."""

from __future__ import annotations

import os
import time

from core.common import StateManager, env, get_timestamp_ms, log, redirect_stderr_to_log_file
from core.constants import HARNESSES, STATE_BASE_DIR

_HARNESS = HARNESSES["omp"]
SERVICE_NAME = _HARNESS["service_name"]
SCOPE_NAME = _HARNESS["scope_name"]
STATE_DIR = STATE_BASE_DIR / _HARNESS["state_subdir"]

os.environ.setdefault("ARIZE_LOG_FILE", str(_HARNESS["default_log_file"]))
redirect_stderr_to_log_file()


def check_requirements() -> bool:
    """Return True when tracing is enabled and the state directory is ready."""
    if not env.trace_enabled:
        return False
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    return True


def resolve_session(input_json: dict) -> StateManager:
    """Build a StateManager keyed by the shim-stamped omp sessionId."""
    key = input_json.get("sessionId") or "unknown"
    sm = StateManager(
        state_dir=STATE_DIR,
        state_file=STATE_DIR / f"state_{key}.yaml",
        lock_path=STATE_DIR / f".lock_{key}",
    )
    sm.init_state()
    return sm


def ensure_session_initialized(state: StateManager, input_json: dict) -> None:
    """Initialize session state once."""
    if state.get("session_id") is not None:
        return

    session_id = input_json.get("sessionId") or "unknown"
    project_name = env.project_name or os.path.basename(os.getcwd())

    state.set("session_id", session_id)
    state.set("session_start_time", str(get_timestamp_ms()))
    state.set("project_name", project_name)
    state.set("trace_count", "0")
    state.set("tool_count", "0")
    state.set("user_id", env.get_user_id(SERVICE_NAME))

    log(f"Session initialized: {session_id}")


def gc_stale_state_files() -> None:
    """Remove omp state and lock files older than 24h by mtime."""
    if not STATE_DIR.is_dir():
        return

    cutoff = time.time() - 86400
    for state_file in STATE_DIR.glob("state_*.yaml"):
        try:
            key = state_file.stem.replace("state_", "", 1)
            if state_file.stat().st_mtime >= cutoff:
                continue

            try:
                state_file.unlink(missing_ok=True)
            except OSError as exc:
                log(f"GC: failed to remove stale state file {state_file}: {exc}")

            lock_path = STATE_DIR / f".lock_{key}"
            if lock_path.is_dir():
                try:
                    lock_path.rmdir()
                except OSError as exc:
                    log(f"GC: failed to remove stale lock directory {lock_path}: {exc}")
            elif lock_path.is_file():
                try:
                    lock_path.unlink(missing_ok=True)
                except OSError as exc:
                    log(f"GC: failed to remove stale lock file {lock_path}: {exc}")
        except OSError as exc:
            log(f"GC: failed to inspect stale state candidate {state_file}: {exc}")
