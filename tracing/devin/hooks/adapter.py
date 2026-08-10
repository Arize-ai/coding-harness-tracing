"""Devin CLI adapter — requirement checks and per-generation emit state.

Devin hook payloads are thin (no session id, no content); the rich data lives
in the live ``sessions.db`` (see :mod:`tracing.devin.session_db`). This module
owns only the pieces that are not pure DB parsing:

* ``check_requirements`` — trace-enable gate + state-dir creation.
* the emit watermark — which LLM generations we have already sent, keyed by
  ``(session_id, request_id)`` so ``Stop`` firing repeatedly (and Devin's
  branch duplication) never double-emits.

Every function is fail-soft: on any error it logs and returns a safe default,
never raising, so a hook can never crash Devin.
"""

from __future__ import annotations

import os
from pathlib import Path

from tracing.devin.constants import DEFAULT_LOG_FILE

# Route hook stderr to a per-harness log file unless ARIZE_LOG_FILE is set.
os.environ.setdefault("ARIZE_LOG_FILE", str(DEFAULT_LOG_FILE))

from core.common import env, log, redirect_stderr_to_log_file  # noqa: E402
from core.constants import STATE_BASE_DIR  # noqa: E402
from tracing.devin.constants import HARNESS_NAME  # noqa: E402

redirect_stderr_to_log_file()

STATE_DIR: Path = STATE_BASE_DIR / HARNESS_NAME
_EMITTED_FILE_NAME = "emitted_requests.txt"


def check_requirements() -> bool:
    """Return True if ``env.trace_enabled``. Create STATE_DIR if so."""
    if not env.trace_enabled:
        return False
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log(f"check_requirements: could not create state dir {STATE_DIR}: {exc!r}")
        return False
    return True


def _emitted_file() -> Path:
    return STATE_DIR / _EMITTED_FILE_NAME


def _emit_key(session_id: str, request_id: str) -> str:
    return f"{session_id}\t{request_id}"


def already_emitted(session_id: str, request_id: str) -> bool:
    """True if this ``(session_id, request_id)`` generation was already emitted.

    Fail-soft: on any read error, treat as not-emitted (better a rare duplicate
    than a silently dropped trace).
    """
    if not session_id or not request_id:
        return False
    key = _emit_key(session_id, request_id)
    path = _emitted_file()
    try:
        if not path.exists():
            return False
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                if line.rstrip("\n") == key:
                    return True
    except OSError as exc:
        log(f"already_emitted: read failed for {path}: {exc!r}")
    return False


def mark_emitted(session_id: str, request_id: str) -> None:
    """Record ``(session_id, request_id)`` as emitted. Fail-soft (no-op on error)."""
    if not session_id or not request_id:
        return
    path = _emitted_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(f"{_emit_key(session_id, request_id)}\n")
    except OSError as exc:
        log(f"mark_emitted: write failed for {path}: {exc!r}")
