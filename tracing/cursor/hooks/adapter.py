#!/usr/bin/env python3
"""Cursor-specific adapter: deterministic trace IDs, state stack, sanitization.

Cursor is architecturally different from Claude Code and Codex — it uses a
single dispatcher for all 12 hook events, deterministic trace IDs from
generation IDs, and a disk-backed state stack for merging before/after hook
pairs.

Replaces cursor-tracing/hooks/common.sh (195 lines).
"""
import hashlib
import json
import os
import re

from core.common import FileLock, env, redirect_stderr_to_log_file
from core.constants import HARNESSES, STATE_BASE_DIR

# --- Module-level constants from HARNESSES["cursor"] ---
_HARNESS = HARNESSES["cursor"]
SERVICE_NAME = _HARNESS["service_name"]  # "cursor"
SCOPE_NAME = _HARNESS["scope_name"]  # "arize-cursor-plugin"
STATE_DIR = STATE_BASE_DIR / _HARNESS["state_subdir"]  # ~/.arize/harness/state/cursor
MAX_ATTR_CHARS = int(os.environ.get("CURSOR_TRACE_MAX_ATTR_CHARS", "100000"))

# Route hook stderr to a per-harness log file unless the user already set one.
os.environ.setdefault("ARIZE_LOG_FILE", str(_HARNESS["default_log_file"]))
redirect_stderr_to_log_file()


def trace_id_from_generation(gen_id: str) -> str:
    """Deterministic 32-hex trace ID from a Cursor generation_id.

    Maps one Cursor "turn" (generation) to one trace.
    Uses MD5 hash — matches bash: printf '%s' "$gen_id" | md5sum | cut -c1-32

    MD5 is NOT used for security here — it's used for deterministic mapping
    so all spans in the same generation share a trace_id.
    """
    return hashlib.md5(gen_id.encode()).hexdigest()[:32]


def span_id_16() -> str:
    """Generate 16-hex random span ID.

    Replaces bash: od -An -tx1 -N8 /dev/urandom | tr -d ' \\n' | cut -c1-16
    """
    return os.urandom(8).hex()


def sanitize(s: str) -> str:
    """Replace non-alphanumeric characters (except ._-) with underscore.

    Matches bash: printf '%s' "$1" | tr -c '[:alnum:]._-' '_'
    """
    return re.sub(r"[^a-zA-Z0-9._-]", "_", s)


def truncate_attr(s: str, max_chars: "int | None" = None) -> str:
    """Truncate string to MAX_ATTR_CHARS (default 100000).

    Matches bash: if [[ ${#str} -gt $max ]]; then printf '%s' "${str:0:$max}"
    """
    limit = max_chars if max_chars is not None else MAX_ATTR_CHARS
    return s[:limit] if len(s) > limit else s


# --- Disk-backed state stack (LIFO) ---
# Replaces bash state_push/state_pop at lines 59-132.
# Used to merge before/after hook pairs (e.g., beforeShellExecution pushes
# command + start time, afterShellExecution pops it to create a merged span).


def state_push(key: str, value: dict) -> None:
    """Push a dict onto a named stack.

    Stack file: STATE_DIR/{key}.stack.json — a JSON list.
    Uses FileLock for concurrent access.

    Matches bash state_push() at lines 59-87.
    """
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    stack_file = STATE_DIR / f"{key}.stack.json"
    lock_path = STATE_DIR / f".lock_{key}"

    with FileLock(lock_path):
        if stack_file.exists():
            try:
                data = json.loads(stack_file.read_text()) or []
            except json.JSONDecodeError:
                data = []
        else:
            data = []

        if not isinstance(data, list):
            data = []

        data.append(value)

        tmp = stack_file.with_suffix(f".tmp.{os.getpid()}")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(stack_file)


def state_pop(key: str) -> "dict | None":
    """Pop the last value from a named stack. Returns None if empty.

    Matches bash state_pop() at lines 91-132.
    """
    stack_file = STATE_DIR / f"{key}.stack.json"
    lock_path = STATE_DIR / f".lock_{key}"

    with FileLock(lock_path):
        if not stack_file.exists():
            return None

        try:
            data = json.loads(stack_file.read_text()) or []
        except json.JSONDecodeError:
            return None

        if not isinstance(data, list) or len(data) == 0:
            return None

        value = data[-1]
        data = data[:-1]

        tmp = stack_file.with_suffix(f".tmp.{os.getpid()}")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(stack_file)

    return value if isinstance(value, dict) else None


# --- Root span tracking per generation ---
# Replaces bash lines 138-155.


def gen_root_span_save(gen_id: str, span_id: str) -> None:
    """Save the root span ID for a generation.

    Written by beforeSubmitPrompt, read by all other events to set parent_span_id.
    File: STATE_DIR/root_{sanitized_gen_id}
    Contains: just the span_id as plain text.
    """
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    safe = sanitize(gen_id)
    (STATE_DIR / f"root_{safe}").write_text(span_id)


def gen_root_span_get(gen_id: str) -> str:
    """Get the root span ID for a generation. Returns "" if not found."""
    if not gen_id:
        return ""
    safe = sanitize(gen_id)
    root_file = STATE_DIR / f"root_{safe}"
    if root_file.exists():
        return root_file.read_text().strip()
    return ""


# --- Generation cleanup ---
# Replaces bash state_cleanup_generation() at lines 159-176.


def state_cleanup_generation(gen_id: str) -> None:
    """Remove all state files for a generation (called by stop hook).

    Cleans up:
    1. Root span file: root_{sanitized_gen_id}
    2. Stack files: *{sanitized_gen_id}*.stack.json
    3. Lock dirs: .lock_*{sanitized_gen_id}*

    Matches bash lines 159-176.
    """
    safe = sanitize(gen_id)

    # Root span file
    root_file = STATE_DIR / f"root_{safe}"
    root_file.unlink(missing_ok=True)

    # Stack files containing this generation ID
    for f in STATE_DIR.glob(f"*{safe}*.stack.json"):
        f.unlink(missing_ok=True)

    # Lock dirs containing this generation ID
    for d in STATE_DIR.glob(f".lock_*{safe}*"):
        if d.is_dir():
            try:
                d.rmdir()  # only works on empty dirs
            except OSError:
                pass


# --- Requirements check ---


def check_requirements() -> bool:
    """Check tracing enabled, ensure state directory exists."""
    if not env.trace_enabled:
        return False
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    return True
