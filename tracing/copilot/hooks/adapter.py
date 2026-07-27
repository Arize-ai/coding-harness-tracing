#!/usr/bin/env python3
"""Copilot adapter — single-mode session resolution, initialization, and GC.

The Copilot hook payload schema is unified across VS Code Copilot Chat and
`gh copilot` CLI: snake_case fields with a top-level `session_id`. We use
that as the session key directly.
"""
import os
import platform
import subprocess

from core.common import StateManager, env, generate_trace_id, get_timestamp_ms, log, redirect_stderr_to_log_file
from core.constants import HARNESSES, STATE_BASE_DIR

# --- Module-level constants derived from HARNESSES ---
_HARNESS = HARNESSES["copilot"]
SERVICE_NAME = _HARNESS["service_name"]  # "copilot"
SCOPE_NAME = _HARNESS["scope_name"]  # "arize-copilot-plugin"
STATE_DIR = STATE_BASE_DIR / _HARNESS["state_subdir"]  # ~/.arize/harness/state/copilot

# Route hook stderr to a per-harness log file unless the user already set one.
os.environ.setdefault("ARIZE_LOG_FILE", str(_HARNESS["default_log_file"]))
redirect_stderr_to_log_file()


def _get_grandparent_pid() -> str:
    """Get the grandparent PID for session key derivation.

    Copilot CLI spawns: copilot(grandparent) -> node(parent) -> hook(this process).
    Same process tree shape as Claude Code.

    On Unix: try reading /proc or using ps command.
    Falls back to parent PID if grandparent can't be determined.
    """
    ppid = os.getppid()
    if ppid <= 0:
        return str(os.getpid())

    # Try /proc (Linux)
    try:
        stat_path = f"/proc/{ppid}/stat"
        with open(stat_path) as f:
            raw = f.read()
        # comm field (index 1) is in parens and may contain spaces; find last ')'
        close_paren = raw.rfind(")")
        rest = raw[close_paren + 2 :].split()
        # rest[0] = state, rest[1] = ppid
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

    # Fallback: use parent PID directly
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


def resolve_session(input_json: dict) -> StateManager:
    """Resolve the per-session state file from hook input JSON.

    The hook payload always carries `session_id` (snake_case). Use it directly.
    Defensive fallback to grandparent PID when the payload is malformed.
    """
    session_key = input_json.get("session_id") or ""
    if not session_key:
        if platform.system() == "Windows":
            session_key = str(os.getppid())
        else:
            session_key = _get_grandparent_pid()

    state_file = STATE_DIR / f"state_{session_key}.json"
    lock_path = STATE_DIR / f".lock_{session_key}"

    sm = StateManager(
        state_dir=STATE_DIR,
        state_file=state_file,
        lock_path=lock_path,
    )
    sm.init_state()
    return sm


def ensure_session_initialized(state: StateManager, input_json: dict) -> None:
    """Idempotent session init. No-op if session_id already in state.

    State keys set:
      session_id          -- input_json["session_id"] (always populated by Copilot)
      session_start_time  -- get_timestamp_ms() as string
      project_name        -- framework-scoped env override or config.json, else basename(input_json["cwd"]),
                             else basename(getcwd())
      trace_count         -- "0"
      tool_count          -- "0"
      user_id             -- env.get_user_id(SERVICE_NAME) (Copilot does not pass user_id in payload)
    """
    if state.get("session_id") is not None:
        return

    session_id = input_json.get("session_id", "") or generate_trace_id()

    project_name = env.project_name_for(SERVICE_NAME)
    if not project_name:
        cwd = input_json.get("cwd", "")
        project_name = os.path.basename(cwd) if cwd else os.path.basename(os.getcwd())

    state.set("session_id", session_id)
    state.set("session_start_time", str(get_timestamp_ms()))
    state.set("project_name", project_name)
    state.set("trace_count", "0")
    state.set("tool_count", "0")
    state.set("user_id", env.get_user_id(SERVICE_NAME))

    log(f"Session initialized: {session_id}")


def check_requirements() -> bool:
    """Check if tracing is enabled and initialize state directory.

    Returns False (and the hook should exit 0) if tracing is disabled.
    """
    if not env.trace_enabled:
        return False
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    return True


def gc_stale_state_files() -> None:
    """Remove state files for PIDs that are no longer running.

    Only cleans numeric (PID-based) filenames: state_12345.json
    Skips non-numeric session keys: state_sess-abc123.json
    These are CLI-mode state files; VS Code mode uses sessionId strings.
    """
    if not STATE_DIR.is_dir():
        return
    for f in STATE_DIR.glob("state_*.json"):
        key = f.stem.replace("state_", "", 1)
        if not key.isdigit():
            continue
        pid = int(key)
        if not _is_pid_alive(pid):
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
