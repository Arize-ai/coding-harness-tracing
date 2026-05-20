#!/usr/bin/env python3
"""Codex adapter — session resolution, initialization, and garbage collection.

Replaces codex-tracing/hooks/common.sh (152 lines). Owns Codex-specific
session logic; the notify handler is in handlers.py.

Key difference from Claude adapter: Codex uses thread-id based sessions
(from the notify payload) and time-based GC (24h), not PID-based.
"""
import os
import time
from pathlib import Path

from core.common import StateManager, env, generate_trace_id, get_timestamp_ms, log, redirect_stderr_to_log_file
from core.constants import HARNESSES, STATE_BASE_DIR

# --- Module-level constants derived from HARNESSES ---
_HARNESS = HARNESSES["codex"]
SERVICE_NAME = _HARNESS["service_name"]  # "codex"
SCOPE_NAME = _HARNESS["scope_name"]  # "arize-codex-plugin"
STATE_DIR = STATE_BASE_DIR / _HARNESS["state_subdir"]  # ~/.arize/harness/state/codex

# Route hook stderr to a per-harness log file unless the user already set one.
os.environ.setdefault("ARIZE_LOG_FILE", str(_HARNESS["default_log_file"]))
redirect_stderr_to_log_file()


def load_env_file(path: Path) -> None:
    """Source a simple KEY=VALUE env file (no shell expansion).

    Reads lines of the form ``export KEY=VALUE`` or ``KEY=VALUE`` and sets
    them in ``os.environ``. Reuses the same logic as proxy.py._load_env_file.
    """
    try:
        text = path.read_text()
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("\"'")
        if key:
            os.environ[key] = value


def resolve_session(thread_id: str) -> StateManager:
    """Resolve the per-session state file from thread-id.

    Codex provides thread-id in the notify payload, used directly as session key.
    Falls back to a random UUID if thread_id is empty.
    Matches bash common.sh resolve_session() at lines 41-51.
    """
    if not thread_id:
        thread_id = generate_trace_id()

    state_file = STATE_DIR / f"state_{thread_id}.yaml"
    lock_path = STATE_DIR / f".lock_{thread_id}"

    sm = StateManager(
        state_dir=STATE_DIR,
        state_file=state_file,
        lock_path=lock_path,
    )
    sm.init_state()
    return sm


def ensure_session_initialized(state: StateManager, thread_id: str, cwd: str) -> None:
    """Idempotent session initialization. No-op if session_id already in state.

    Sets the following state keys (matching bash common.sh lines 53-81):
    - session_id: thread_id or generate_trace_id()
    - session_start_time: get_timestamp_ms() as string
    - project_name: from ARIZE_PROJECT_NAME env, or basename of cwd
    - trace_count: "0"
    - user_id: from env.user_id or ""
    """
    existing = state.get("session_id")
    if existing is not None:
        return

    session_id = thread_id if thread_id else generate_trace_id()

    project_name = env.project_name
    if not project_name:
        project_name = os.path.basename(cwd) if cwd else os.path.basename(os.getcwd())

    state.set("session_id", session_id)
    state.set("session_start_time", str(get_timestamp_ms()))
    state.set("project_name", project_name)
    state.set("trace_count", "0")

    user_id = env.user_id
    if user_id:
        state.set("user_id", user_id)

    log(f"Session initialized: {session_id}")


def gc_stale_state_files() -> None:
    """Remove state files older than 24 hours.

    Unlike Claude's PID-based GC, Codex uses time-based GC since sessions
    are keyed by thread-id (not PID). Matches bash common.sh lines 84-105.
    """
    if not STATE_DIR.is_dir():
        return
    now = time.time()
    for f in STATE_DIR.glob("state_*.yaml"):
        try:
            file_age = now - f.stat().st_mtime
        except OSError:
            continue
        if file_age > 86400:  # 24 hours
            key = f.stem.replace("state_", "", 1)
            try:
                f.unlink(missing_ok=True)
            except OSError:
                pass
            lock_path = STATE_DIR / f".lock_{key}"
            if lock_path.is_dir():
                try:
                    lock_path.rmdir()
                except OSError:
                    pass
    for f in STATE_DIR.glob("spans_*.jsonl"):
        try:
            file_age = now - f.stat().st_mtime
        except OSError:
            continue
        if file_age > 86400:  # 24 hours, same threshold
            try:
                f.unlink(missing_ok=True)
            except OSError:
                pass


def check_requirements() -> bool:
    """Check if tracing is enabled and initialize state directory.

    Returns False (and the hook should return) if tracing is disabled.
    Matches bash: [[ "$ARIZE_TRACE_ENABLED" != "true" ]] && exit 0
    """
    if not env.trace_enabled:
        return False
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    return True
