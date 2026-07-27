#!/usr/bin/env python3
"""omp (Oh My Pi) adapter — session resolution, initialization, and GC.

omp always supplies an authoritative ``sessionId`` (camelCase — the TypeScript
shim stamps it on every forwarded payload, reading it from the hook context for
events whose payload is empty). So, like opencode, this adapter has no PID /
grandparent-PID heuristics. Missing/empty ``sessionId`` keys off a per-process
``"unknown-<pid>"`` fallback (see ``_session_key``) so two concurrent id-less
runs cannot collide on a single shared state file.

Unlike opencode there is no snapshot/message payload to mine for the project
name, so the derivation chain is simply
``env.project_name_for(SERVICE_NAME)`` (framework env override or config.json)
-> ``os.path.basename(os.getcwd())`` -> ``HARNESS_NAME``.
"""
from __future__ import annotations

import os
import time

from core.common import StateManager, env, get_timestamp_ms, log, redirect_stderr_to_log_file
from core.constants import HARNESSES, STATE_BASE_DIR
from tracing.omp.constants import HARNESS_NAME

# --- Module-level constants derived from HARNESSES ---
_HARNESS = HARNESSES["omp"]
SERVICE_NAME = _HARNESS["service_name"]  # "omp"
SCOPE_NAME = _HARNESS["scope_name"]  # "arize-omp-plugin"
STATE_DIR = STATE_BASE_DIR / _HARNESS["state_subdir"]  # ~/.arize/harness/state/omp

# Route hook stderr to a per-harness log file unless the user already set one.
os.environ.setdefault("ARIZE_LOG_FILE", str(_HARNESS["default_log_file"]))
redirect_stderr_to_log_file()


def check_requirements() -> bool:
    """Return True if env.trace_enabled is True. Create STATE_DIR if so."""
    if not env.trace_enabled:
        return False
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    return True


def _session_key(input_json: dict) -> str:
    """Return the omp ``sessionId``, or a per-process fallback when it is absent.

    omp normally supplies an authoritative ``sessionId``. When it is missing or
    empty we key off ``"unknown-<pid>"`` rather than a shared ``"unknown"``
    literal, so two concurrent id-less runs cannot clobber each other's state
    file. Within a single event's process the pid is stable, so ``resolve_session``
    and ``ensure_session_initialized`` agree on the key; span correlation across a
    run's separate detached processes is already impossible without a real
    ``sessionId``, so no correlation is lost by using the pid here.
    """
    return input_json.get("sessionId") or f"unknown-{os.getpid()}"


def resolve_session(input_json: dict) -> StateManager:
    """Build a StateManager keyed off the omp ``sessionId`` payload field.

    Missing/empty sessionId falls back to a per-process ``"unknown-<pid>"`` key
    (see ``_session_key``) — omp adapters never use PID-based keys for real
    sessions.
    """
    key = _session_key(input_json)

    state_file = STATE_DIR / f"state_{key}.json"
    lock_path = STATE_DIR / f".lock_{key}"

    sm = StateManager(
        state_dir=STATE_DIR,
        state_file=state_file,
        lock_path=lock_path,
    )
    sm.init_state()
    return sm


def ensure_session_initialized(state: StateManager, input_json: dict) -> None:
    """Idempotent session initialization."""
    existing = state.get("session_id")
    if existing is not None:
        return

    session_id = _session_key(input_json)

    project_name = env.project_name_for(SERVICE_NAME)
    if not project_name:
        project_name = os.path.basename(os.getcwd()) or HARNESS_NAME

    state.set("session_id", session_id)
    state.set("session_start_time", str(get_timestamp_ms()))
    state.set("project_name", project_name)
    state.set("trace_count", "0")
    state.set("tool_count", "0")
    state.set("user_id", env.get_user_id(SERVICE_NAME))

    log(f"Session initialized: {session_id}")


def gc_stale_state_files() -> None:
    """Remove state files (and their lock files) older than 24h by mtime.

    omp keys are session IDs (not PIDs), so this is the mtime-only branch — no
    PID-liveness check.
    """
    if not STATE_DIR.is_dir():
        return
    cutoff = time.time() - 86400
    for f in STATE_DIR.glob("state_*.json"):
        try:
            key = f.stem.replace("state_", "", 1)
            if f.stat().st_mtime >= cutoff:
                continue

            try:
                f.unlink(missing_ok=True)
            except OSError as e:
                log(f"GC: failed to remove stale state file {f}: {e}")

            lock_path = STATE_DIR / f".lock_{key}"
            if lock_path.is_dir():
                try:
                    lock_path.rmdir()
                except OSError as e:
                    log(f"GC: failed to remove stale lock directory {lock_path}: {e}")
            elif lock_path.is_file():
                try:
                    lock_path.unlink(missing_ok=True)
                except OSError as e:
                    log(f"GC: failed to remove stale lock file {lock_path}: {e}")
        except OSError as e:
            log(f"GC: failed to inspect stale state candidate {f}: {e}")
