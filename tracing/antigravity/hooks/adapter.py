#!/usr/bin/env python3
"""Antigravity adapter — session resolution, initialization, and GC.

Antigravity provides ``conversationId`` on every hook invocation, so session
keying is straightforward: no environment variable fallback, no grandparent-PID
lookup. If the field is somehow missing we derive a key from the transcript
path instead — PreInvocation and Stop run as separate processes, so the key
must be process-independent for the pair to share the emission watermark. With
neither field present there is no usable key and the hooks no-op.
"""
from __future__ import annotations

import hashlib
import os
import time

from core.common import StateManager, env, generate_trace_id, log, redirect_stderr_to_log_file
from core.constants import HARNESSES, STATE_BASE_DIR

# --- Module-level constants derived from HARNESSES ---
_HARNESS = HARNESSES["antigravity"]
SERVICE_NAME = _HARNESS["service_name"]  # "antigravity"
SCOPE_NAME = _HARNESS["scope_name"]  # "arize-antigravity-plugin"
STATE_DIR = STATE_BASE_DIR / _HARNESS["state_subdir"]  # ~/.arize/harness/state/antigravity

# Route hook stderr to a per-harness log file unless the user already set one.
os.environ.setdefault("ARIZE_LOG_FILE", str(_HARNESS["default_log_file"]))
redirect_stderr_to_log_file()


def check_requirements() -> bool:
    """Return True if env.trace_enabled is True. Create STATE_DIR if so."""
    if not env.trace_enabled:
        return False
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    return True


def resolve_session(input_json: dict) -> StateManager | None:
    """Build a StateManager keyed off ``conversationId`` from the hook payload.

    Antigravity supplies ``conversationId`` on every hook invocation. If it is
    missing, fall back to a key derived from ``transcriptPath`` (which both
    PreInvocation and Stop receive).
    """
    key = input_json.get("conversationId") or ""
    if not key:
        transcript_path = input_json.get("transcriptPath") or ""
        if not transcript_path:
            return None
        key = "transcript-" + hashlib.sha256(str(transcript_path).encode("utf-8")).hexdigest()[:16]

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

    session_id = input_json.get("conversationId") or generate_trace_id()

    project_name = env.project_name
    if not project_name:
        workspaces = input_json.get("workspacePaths") or []
        first = workspaces[0] if workspaces else ""
        project_name = os.path.basename(first) if first else os.path.basename(os.getcwd())

    state.set("session_id", session_id)
    state.set("project_name", project_name)
    state.set("user_id", env.user_id)
    state.set("last_emitted_turn", "-1")

    log(f"Session initialized: {session_id}")


def gc_stale_state_files() -> None:
    """Remove state files (and their lock files) older than 24h.

    Antigravity keys state by conversation UUID, so age is the only signal we
    have for staleness — there's no PID liveness check to fall back to.
    """
    if not STATE_DIR.is_dir():
        return
    cutoff = time.time() - 86400
    for f in STATE_DIR.glob("state_*.json"):
        try:
            if f.stat().st_mtime >= cutoff:
                continue
            key = f.stem.replace("state_", "", 1)

            try:
                f.unlink(missing_ok=True)
            except OSError as e:
                log(f"Failed to remove stale state file {f}: {e}")

            lock_path = STATE_DIR / f".lock_{key}"
            if lock_path.is_dir():
                try:
                    lock_path.rmdir()
                except OSError:
                    # Best-effort cleanup: ignore lock-dir removal failures.
                    pass
            elif lock_path.is_file():
                try:
                    lock_path.unlink(missing_ok=True)
                except OSError as e:
                    log(f"Failed to remove stale lock file {lock_path}: {e}")
        except OSError:
            # Best-effort GC: ignore per-file stat/access errors and continue scanning.
            pass
