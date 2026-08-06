#!/usr/bin/env python3
"""Claude Code adapter — session resolution, initialization, and garbage collection.

Replaces claude-code-tracing/hooks/common.sh (107 lines). Owns Claude-specific
session logic; individual hook events are handled by handlers.py.
"""
import os
import platform
import subprocess
from pathlib import Path
from typing import Optional

from core.common import StateManager, env, generate_trace_id, get_timestamp_ms, log, redirect_stderr_to_log_file
from core.constants import HARNESSES, STATE_BASE_DIR

# --- Module-level constants derived from HARNESSES ---
_HARNESS = HARNESSES["claude-code"]
SERVICE_NAME = _HARNESS["service_name"]  # "claude-code"
SCOPE_NAME = _HARNESS["scope_name"]  # "arize-claude-plugin"
STATE_DIR = STATE_BASE_DIR / _HARNESS["state_subdir"]  # ~/.arize/harness/state/claude-code

# Route hook stderr to a per-harness log file unless the user already set one.
os.environ.setdefault("ARIZE_LOG_FILE", str(_HARNESS["default_log_file"]))
redirect_stderr_to_log_file()


def _get_grandparent_pid() -> str:
    """Get the grandparent PID for session key derivation.

    Claude Code spawns: claude(grandparent) -> node(parent) -> hook(this process).
    The bash version uses `ps -o ppid= -p "$PPID"` to get grandparent PID.

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


def resolve_session(input_json: dict) -> StateManager:
    """Resolve the per-session state file from hook input JSON.

    Priority for session key (matches bash lines 31-44):
    1. input_json["session_id"] — if present and non-empty
    2. CLAUDE_SESSION_KEY env var — if set
    3. Grandparent PID — Claude Code spawns: claude(grandparent) -> node(parent) -> hook.
       On Windows, falls back to parent PID.

    Returns a StateManager instance with state_file and lock_path set.
    Calls init_state() to ensure the file exists.
    """
    session_key = None

    # 1. Check input JSON
    sid = input_json.get("session_id", "")
    if sid:
        session_key = sid

    # 2. Check env var
    if not session_key:
        env_key = os.environ.get("CLAUDE_SESSION_KEY", "")
        if env_key:
            session_key = env_key

    # 3. Fall back to PID-based key
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
    """Idempotent session initialization. No-op if session_id already in state.

    Sets the following state keys (matching bash lines 71-82):
    - session_id: from input_json or generate_trace_id()
    - session_start_time: get_timestamp_ms() as string
    - project_name: framework-scoped env override or config.json, else basename of input_json["cwd"], or cwd
    - trace_count: "0"
    - tool_count: "0"
    - user_id: from env.get_user_id(SERVICE_NAME), then input_json["user_id"], then ""
    """
    # Skip if already initialized
    existing = state.get("session_id")
    if existing is not None:
        return

    # session_id
    session_id = input_json.get("session_id", "")
    if not session_id:
        session_id = generate_trace_id()

    # project_name: framework-scoped env override > config.json > cwd basename.
    # Reading config here (rather than only env.project_name) means edits to
    # harnesses.claude-code.project_name take effect and PHOENIX_PROJECT is
    # honored on the Phoenix backend — see issue #74.
    project_name = env.project_name_for(SERVICE_NAME)
    if not project_name:
        cwd = input_json.get("cwd", "")
        project_name = os.path.basename(cwd) if cwd else os.path.basename(os.getcwd())

    state.set("session_id", session_id)
    state.set("session_start_time", str(get_timestamp_ms()))
    state.set("project_name", project_name)
    state.set("trace_count", "0")
    state.set("tool_count", "0")

    # user_id priority: env var, then hook input
    user_id = env.get_user_id(SERVICE_NAME)
    if not user_id:
        user_id = input_json.get("user_id", "")
    state.set("user_id", user_id)

    log(f"Session initialized: {session_id}")


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


def gc_stale_state_files() -> None:
    """Remove state files for PIDs that are no longer running.

    Only cleans numeric (PID-based) filenames: state_12345.json
    Skips non-numeric session keys: state_sess-abc123.json
    Matches bash lines 90-99.
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


def resolve_transcript_path(input_json: dict, session_id: str) -> Optional[Path]:
    """Return the transcript .jsonl path for this session, or None if it can't be located.

    Claude Code v2 dropped ``transcript_path`` from Stop input.  When present in
    the input we trust it; otherwise we derive the canonical path from cwd + session_id.
    Falls back to the current process cwd if the input has no ``cwd`` either.
    """
    # Prefer agent_transcript_path (subagent's own log) over transcript_path (main session)
    # so SubagentStop resolves to the correct transcript.
    raw = input_json.get("agent_transcript_path") or input_json.get("transcript_path") or ""
    if raw:
        p = Path(raw).expanduser()
        if p.is_file():
            return p

    if not session_id:
        return None

    cwd = input_json.get("cwd") or os.getcwd()
    slug = cwd.replace("/", "-")
    candidate = Path.home() / ".claude" / "projects" / slug / f"{session_id}.jsonl"
    if candidate.is_file():
        return candidate
    return None


def check_requirements() -> bool:
    """Check if tracing is enabled and initialize state directory.

    Returns False (and the hook should exit 0) if tracing is disabled.
    Matches bash: [[ "$ARIZE_TRACE_ENABLED" != "true" ]] && exit 0
    """
    if not env.trace_enabled:
        return False
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    return True
