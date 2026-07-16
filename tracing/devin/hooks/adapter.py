"""Devin CLI adapter — session/transcript resolution and requirement checks.

Devin hook payloads are thin: no session_id, no token/model data. The only
environment signal is ``DEVIN_PROJECT_DIR`` (the session's working directory).
The rich data lives locally:

  * ``sessions.db`` — a live SQLite DB. Its token columns are null; we use it
    solely to map a working directory -> the current ``session_id``.
  * ``transcripts/<session_id>.json`` — a schema-versioned ATIF-v1.7 export
    written at session end. This is our real data source.

Every function here is fail-soft: on any error it logs and returns
``None``/``False``, never raising, so a hook can never crash Devin.
"""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

from tracing.devin.constants import DEFAULT_LOG_FILE

# Route hook stderr to a per-harness log file unless ARIZE_LOG_FILE is set.
os.environ.setdefault("ARIZE_LOG_FILE", str(DEFAULT_LOG_FILE))

from core.common import env, log, redirect_stderr_to_log_file  # noqa: E402
from core.constants import STATE_BASE_DIR  # noqa: E402
from tracing.devin.constants import HARNESS_NAME, SESSIONS_DB, TRANSCRIPTS_DIR  # noqa: E402

redirect_stderr_to_log_file()

STATE_DIR: Path = STATE_BASE_DIR / HARNESS_NAME
_EMITTED_FILE_NAME = "emitted.txt"
_POLL_INTERVAL = 0.1


def check_requirements() -> bool:
    """Return True if env.trace_enabled. Create STATE_DIR if so."""
    if not env.trace_enabled:
        return False
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log(f"check_requirements: could not create state dir {STATE_DIR}: {exc!r}")
        return False
    return True


def resolve_session_id(project_dir: str) -> str | None:
    """Return the most recent Devin session id for ``project_dir``.

    Opens ``SESSIONS_DB`` read-only (immutable) so we never interfere with
    Devin's live writers. Returns None if the DB is missing or on any error.
    """
    if not project_dir:
        return None
    if not SESSIONS_DB.exists():
        log(f"resolve_session_id: sessions DB not found at {SESSIONS_DB}")
        return None

    con = None
    try:
        con = sqlite3.connect(f"file:{SESSIONS_DB}?mode=ro&immutable=1", uri=True)
        row = con.execute(
            "SELECT id FROM sessions WHERE working_directory = ? " "ORDER BY last_activity_at DESC LIMIT 1",
            (project_dir,),
        ).fetchone()
    except (sqlite3.Error, OSError) as exc:
        log(f"resolve_session_id: query failed for {project_dir!r}: {exc!r}")
        return None
    finally:
        if con is not None:
            try:
                con.close()
            except sqlite3.Error as exc:
                log(f"resolve_session_id: suppressing close failure: {exc!r}")

    if row and row[0]:
        return str(row[0])
    return None


def resolve_transcript_path(project_dir: str, poll_secs: float = 2.0) -> Path | None:
    """Resolve the ATIF transcript for the session working in ``project_dir``.

    1. Map ``project_dir`` -> session id via ``resolve_session_id``.
    2. Poll up to ``poll_secs`` for ``<sid>.json`` to appear — the transcript
       may be flushed slightly after ``SessionEnd`` fires.
    3. If the sid is unknown or the file never appears, fall back to the most
       recently modified ``*.json`` in ``TRANSCRIPTS_DIR``.

    Returns the resolved ``Path`` or None if nothing is found.
    """
    sid = resolve_session_id(project_dir)
    if sid:
        target = TRANSCRIPTS_DIR / f"{sid}.json"
        deadline = time.monotonic() + max(poll_secs, 0.0)
        while True:
            try:
                if target.exists():
                    return target
            except OSError as exc:
                log(f"resolve_transcript_path: stat failed for {target}: {exc!r}")
                break
            if time.monotonic() >= deadline:
                break
            time.sleep(_POLL_INTERVAL)
        log(f"resolve_transcript_path: transcript for {sid} not found; trying mtime fallback")

    fallback = _most_recent_transcript()
    if fallback is not None:
        log(f"resolve_transcript_path: using mtime fallback {fallback.name}")
    return fallback


def _most_recent_transcript() -> Path | None:
    """Return the most recently modified ``*.json`` in TRANSCRIPTS_DIR, if any."""
    try:
        candidates = list(TRANSCRIPTS_DIR.glob("*.json"))
    except OSError as exc:
        log(f"_most_recent_transcript: glob failed in {TRANSCRIPTS_DIR}: {exc!r}")
        return None
    best: Path | None = None
    best_mtime = -1.0
    for path in candidates:
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if mtime > best_mtime:
            best_mtime = mtime
            best = path
    return best


def _emitted_file() -> Path:
    return STATE_DIR / _EMITTED_FILE_NAME


def already_emitted(session_id: str) -> bool:
    """Return True if ``session_id`` has already been emitted.

    Backed by a newline-delimited file so a repeated ``SessionEnd`` for the
    same session doesn't double-emit. Fail-soft: on any error, treat as
    not-emitted.
    """
    if not session_id:
        return False
    path = _emitted_file()
    try:
        if not path.exists():
            return False
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                if line.strip() == session_id:
                    return True
    except OSError as exc:
        log(f"already_emitted: read failed for {path}: {exc!r}")
    return False


def mark_emitted(session_id: str) -> None:
    """Record ``session_id`` as emitted. Fail-soft (no-op on error)."""
    if not session_id:
        return
    path = _emitted_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(f"{session_id}\n")
    except OSError as exc:
        log(f"mark_emitted: write failed for {path}: {exc!r}")
