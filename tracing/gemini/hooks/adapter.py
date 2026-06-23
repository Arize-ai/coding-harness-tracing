#!/usr/bin/env python3
"""Gemini adapter — single-mode (CLI-only) session resolution, initialization, and GC.

Gemini CLI provides GEMINI_SESSION_ID as an env var on every hook invocation,
but falls back to PID-based session keys (grandparent PID) to ensure
consistent state across all hooks in a single CLI run.
"""
from __future__ import annotations

import os
import platform
import subprocess
import time

from core.common import StateManager, env, generate_trace_id, get_timestamp_ms, log, redirect_stderr_to_log_file
from core.constants import HARNESSES, STATE_BASE_DIR

# --- Module-level constants derived from HARNESSES ---
_HARNESS = HARNESSES["gemini"]
SERVICE_NAME = _HARNESS["service_name"]  # "gemini"
SCOPE_NAME = _HARNESS["scope_name"]  # "arize-gemini-plugin"
STATE_DIR = STATE_BASE_DIR / _HARNESS["state_subdir"]  # ~/.arize/harness/state/gemini

# Route hook stderr to a per-harness log file unless the user already set one.
os.environ.setdefault("ARIZE_LOG_FILE", str(_HARNESS["default_log_file"]))
redirect_stderr_to_log_file()


def _get_grandparent_pid() -> str:
    """Get the grandparent PID for session key derivation.

    Gemini CLI spawns: gemini(grandparent) -> node(parent) -> hook(this process).
    On Unix: try reading /proc or using ps command.
    Falls back to parent PID if grandparent can't be determined.
    """
    ppid = os.getppid()
    if ppid <= 0:
        return str(os.getpid())

    # Try /proc (Linux)
    try:
        stat_path = f"{ppid}/stat"
        if os.path.exists(f"/proc/{stat_path}"):
            with open(f"/proc/{stat_path}") as f:
                raw = f.read()
            close_paren = raw.rfind(")")
            rest = raw[close_paren + 2 :].split()
            gpid = rest[1]
            if gpid.isdigit() and int(gpid) > 0:
                return gpid
    except (OSError, IndexError, ValueError):
        pass

    # Try ps command (macOS / other Unix)
    try:
        result = subprocess.check_output(
            ["ps", "-o", "ppid=", "-p", str(ppid)],
            stderr=subprocess.DEVNULL,
            timeout=2,
        )
        gpid = result.decode().strip()
        if gpid.isdigit() and int(gpid) > 0:
            return gpid
    except (subprocess.SubprocessError, OSError, ValueError):
        pass

    return str(ppid)


def _is_pid_alive(pid: int) -> bool:
    """Check if a process with the given PID is still running."""
    if pid <= 0:
        return False
    if platform.system() == "Windows":
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if handle:
                kernel32.CloseHandle(handle)
                return True
            return False
        except Exception:
            return False
    else:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False


def check_requirements() -> bool:
    """Return True if env.trace_enabled is True. Create STATE_DIR if so."""
    if not env.trace_enabled:
        return False
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    return True


def resolve_session(input_json: dict) -> StateManager:
    """Build a StateManager keyed off GEMINI_SESSION_ID env (preferred),
    else input_json sessionId (standard) or session_id (legacy),
    else grandparent PID (to keep state consistent across hooks).
    """
    key = os.environ.get("GEMINI_SESSION_ID", "")
    if not key:
        key = input_json.get("sessionId") or input_json.get("session_id", "")
    if not key:
        if platform.system() == "Windows":
            key = str(os.getppid())
        else:
            key = _get_grandparent_pid()

    state_file = STATE_DIR / f"state_{key}.yaml"
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

    # Prefer the Gemini-provided session identifier so Arize spans correlate
    # back to the same session in Gemini. Fall back to a generated trace ID
    # (never the PID, which can collide across runs).
    session_id = (
        os.environ.get("GEMINI_SESSION_ID")
        or input_json.get("sessionId")
        or input_json.get("session_id")
        or generate_trace_id()
    )

    # project_name
    project_name = env.project_name
    if not project_name:
        cwd = input_json.get("projectDir") or input_json.get("cwd", "")
        project_name = os.path.basename(cwd) if cwd else os.path.basename(os.getcwd())

    state.set("session_id", session_id)
    state.set("session_start_time", str(get_timestamp_ms()))
    state.set("project_name", project_name)
    state.set("trace_count", "0")
    state.set("tool_count", "0")

    user_id = env.get_user_id(SERVICE_NAME)
    state.set("user_id", user_id)

    log(f"Session initialized: {session_id}")


def gc_stale_state_files() -> None:
    """Remove state files (and their lock files) for processes that are no
    longer alive, or files older than 24h for non-PID keys.
    """
    if not STATE_DIR.is_dir():
        return
    cutoff = time.time() - 86400
    for f in STATE_DIR.glob("state_*.yaml"):
        try:
            key = f.stem.replace("state_", "", 1)
            should_remove = False
            if key.isdigit():
                if not _is_pid_alive(int(key)):
                    should_remove = True
            elif f.stat().st_mtime < cutoff:
                should_remove = True

            if not should_remove:
                continue

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
            elif lock_path.is_file():
                try:
                    lock_path.unlink(missing_ok=True)
                except OSError:
                    pass
        except OSError:
            pass
