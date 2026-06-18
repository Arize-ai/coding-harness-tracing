#!/usr/bin/env python3
"""opencode adapter — session resolution, initialization, and GC.

opencode always supplies an authoritative `sessionID` in the payload sent by the
TypeScript plugin shim, so this adapter has no PID / grandparent-PID heuristics.
Missing sessionID keys off the literal string ``"unknown"``.
"""
from __future__ import annotations

import os
import time

from core.common import StateManager, env, get_timestamp_ms, log, redirect_stderr_to_log_file
from core.constants import HARNESSES, STATE_BASE_DIR

# --- Module-level constants derived from HARNESSES ---
_HARNESS = HARNESSES["opencode"]
SERVICE_NAME = _HARNESS["service_name"]  # "opencode"
SCOPE_NAME = _HARNESS["scope_name"]  # "arize-opencode-plugin"
STATE_DIR = STATE_BASE_DIR / _HARNESS["state_subdir"]  # ~/.arize/harness/state/opencode

# Route hook stderr to a per-harness log file unless the user already set one.
os.environ.setdefault("ARIZE_LOG_FILE", str(_HARNESS["default_log_file"]))
redirect_stderr_to_log_file()


def check_requirements() -> bool:
    """Return True if env.trace_enabled is True. Create STATE_DIR if so."""
    if not env.trace_enabled:
        return False
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    return True


def resolve_session(input_json: dict) -> StateManager:
    """Build a StateManager keyed off the opencode ``sessionID`` payload field.

    Missing/empty sessionID falls back to the literal key ``"unknown"`` — opencode
    adapters never use PID-based keys.
    """
    key = input_json.get("sessionID") or "unknown"

    state_file = STATE_DIR / f"state_{key}.yaml"
    lock_path = STATE_DIR / f".lock_{key}"

    sm = StateManager(
        state_dir=STATE_DIR,
        state_file=state_file,
        lock_path=lock_path,
    )
    sm.init_state()
    return sm


def _project_name_from_snapshot(input_json: dict) -> str:
    """Best-effort project name derivation from an opencode snapshot payload.

    Looks for the first message's ``info.path.cwd`` (then ``info.path.root``)
    and returns its basename. Returns ``""`` if nothing usable is found.
    """
    messages = input_json.get("messages") or []
    if not isinstance(messages, list):
        return ""
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        info = msg.get("info") or {}
        path = info.get("path") or {}
        cwd = path.get("cwd") or ""
        if cwd:
            return os.path.basename(cwd)
        root = path.get("root") or ""
        if root:
            return os.path.basename(root)
    return ""


def ensure_session_initialized(state: StateManager, input_json: dict) -> None:
    """Idempotent session initialization."""
    existing = state.get("session_id")
    if existing is not None:
        return

    session_id = input_json.get("sessionID") or "unknown"

    project_name = env.project_name
    if not project_name:
        project_name = _project_name_from_snapshot(input_json) or os.path.basename(os.getcwd())

    state.set("session_id", session_id)
    state.set("session_start_time", str(get_timestamp_ms()))
    state.set("project_name", project_name)
    state.set("trace_count", "0")
    state.set("tool_count", "0")

    user_id = env.user_id
    state.set("user_id", user_id)

    log(f"Session initialized: {session_id}")


def gc_stale_state_files() -> None:
    """Remove state files (and their lock files) older than 24h by mtime.

    opencode keys are session IDs (not PIDs), so this is the mtime-only branch —
    no PID-liveness check.
    """
    if not STATE_DIR.is_dir():
        return
    cutoff = time.time() - 86400
    for f in STATE_DIR.glob("state_*.yaml"):
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
