"""Tests for the Devin adapter: session/transcript resolution + idempotency."""

import os
import sqlite3
from pathlib import Path

from tracing.devin.hooks import adapter


def _make_sessions_db(path, rows):
    """Create a sessions.db with (id, working_directory, last_activity_at) rows."""
    con = sqlite3.connect(str(path))
    try:
        con.execute("CREATE TABLE sessions (" "id TEXT, working_directory TEXT, last_activity_at INTEGER)")
        con.executemany(
            "INSERT INTO sessions (id, working_directory, last_activity_at) VALUES (?, ?, ?)",
            rows,
        )
        con.commit()
    finally:
        con.close()


def test_resolve_session_id_returns_newest(tmp_path, monkeypatch):
    db = tmp_path / "sessions.db"
    _make_sessions_db(
        db,
        [
            ("old-session", "/work/proj", 100),
            ("new-session", "/work/proj", 200),
        ],
    )
    monkeypatch.setattr(adapter, "SESSIONS_DB", db)

    assert adapter.resolve_session_id("/work/proj") == "new-session"


def test_resolve_session_id_missing_db(tmp_path, monkeypatch):
    monkeypatch.setattr(adapter, "SESSIONS_DB", tmp_path / "nope.db")
    assert adapter.resolve_session_id("/work/proj") is None


def test_resolve_transcript_path_by_sid(tmp_path, monkeypatch):
    db = tmp_path / "sessions.db"
    _make_sessions_db(db, [("sess-1", "/work/proj", 100)])
    transcripts = tmp_path / "transcripts"
    transcripts.mkdir()
    target = transcripts / "sess-1.json"
    target.write_text("{}")

    monkeypatch.setattr(adapter, "SESSIONS_DB", db)
    monkeypatch.setattr(adapter, "TRANSCRIPTS_DIR", transcripts)

    assert adapter.resolve_transcript_path("/work/proj") == target


def test_resolve_transcript_path_mtime_fallback(tmp_path, monkeypatch):
    # No matching sid (empty DB), but a stray transcript exists.
    db = tmp_path / "sessions.db"
    _make_sessions_db(db, [])
    transcripts = tmp_path / "transcripts"
    transcripts.mkdir()
    stray = transcripts / "other.json"
    stray.write_text("{}")

    monkeypatch.setattr(adapter, "SESSIONS_DB", db)
    monkeypatch.setattr(adapter, "TRANSCRIPTS_DIR", transcripts)

    assert adapter.resolve_transcript_path("/work/proj", poll_secs=0.0) == stray


def test_emitted_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(adapter, "STATE_DIR", tmp_path / "state")

    assert adapter.already_emitted("sess-x") is False
    adapter.mark_emitted("sess-x")
    assert adapter.already_emitted("sess-x") is True
    # An unrelated id is still unseen.
    assert adapter.already_emitted("sess-y") is False


# ---------------------------------------------------------------------------
# check_requirements
# ---------------------------------------------------------------------------


def test_check_requirements_disabled(tmp_path, monkeypatch):
    """When tracing is disabled, return False and do not create the state dir."""
    monkeypatch.setenv("ARIZE_TRACE_ENABLED", "false")
    from core.common import env

    env.invalidate_caches()
    state = tmp_path / "state"
    monkeypatch.setattr(adapter, "STATE_DIR", state)

    assert adapter.check_requirements() is False
    assert not state.exists()


def test_check_requirements_enabled_creates_state_dir(tmp_path, monkeypatch):
    """When tracing is enabled, return True and mkdir -p the state dir."""
    monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
    from core.common import env

    env.invalidate_caches()
    state = tmp_path / "state" / "nested"
    monkeypatch.setattr(adapter, "STATE_DIR", state)

    assert adapter.check_requirements() is True
    assert state.is_dir()
    # Idempotent — a second call on an existing dir still succeeds.
    assert adapter.check_requirements() is True


# ---------------------------------------------------------------------------
# resolve_session_id edge cases
# ---------------------------------------------------------------------------


def test_resolve_session_id_empty_project_dir(tmp_path, monkeypatch):
    """An empty project_dir short-circuits to None (never touches the DB)."""
    db = tmp_path / "sessions.db"
    _make_sessions_db(db, [("sess-1", "/work/proj", 100)])
    monkeypatch.setattr(adapter, "SESSIONS_DB", db)

    assert adapter.resolve_session_id("") is None


def test_resolve_session_id_no_matching_dir(tmp_path, monkeypatch):
    """A DB with no row for the requested dir resolves to None."""
    db = tmp_path / "sessions.db"
    _make_sessions_db(db, [("sess-1", "/other/proj", 100)])
    monkeypatch.setattr(adapter, "SESSIONS_DB", db)

    assert adapter.resolve_session_id("/work/proj") is None


def test_resolve_session_id_opens_read_only(tmp_path, monkeypatch):
    """The DB is opened immutable/read-only; the real file is never mutated."""
    db = tmp_path / "sessions.db"
    _make_sessions_db(db, [("sess-1", "/work/proj", 100)])
    monkeypatch.setattr(adapter, "SESSIONS_DB", db)
    before = db.stat().st_mtime_ns

    assert adapter.resolve_session_id("/work/proj") == "sess-1"
    # No sidecar journal/wal files and no mtime change from our read.
    assert db.stat().st_mtime_ns == before
    assert not (tmp_path / "sessions.db-wal").exists()


# ---------------------------------------------------------------------------
# resolve_transcript_path edge cases
# ---------------------------------------------------------------------------


def test_resolve_transcript_path_none_when_nothing_found(tmp_path, monkeypatch):
    """No matching sid and an empty transcripts dir yields None."""
    db = tmp_path / "sessions.db"
    _make_sessions_db(db, [])
    transcripts = tmp_path / "transcripts"
    transcripts.mkdir()

    monkeypatch.setattr(adapter, "SESSIONS_DB", db)
    monkeypatch.setattr(adapter, "TRANSCRIPTS_DIR", transcripts)

    assert adapter.resolve_transcript_path("/work/proj", poll_secs=0.0) is None


def test_resolve_transcript_path_sid_missing_file_falls_back(tmp_path, monkeypatch):
    """sid resolves but its transcript never appears -> mtime fallback used."""
    db = tmp_path / "sessions.db"
    _make_sessions_db(db, [("sess-1", "/work/proj", 100)])
    transcripts = tmp_path / "transcripts"
    transcripts.mkdir()
    stray = transcripts / "leftover.json"
    stray.write_text("{}")

    monkeypatch.setattr(adapter, "SESSIONS_DB", db)
    monkeypatch.setattr(adapter, "TRANSCRIPTS_DIR", transcripts)

    # sess-1.json does not exist; poll immediately expires and falls back.
    assert adapter.resolve_transcript_path("/work/proj", poll_secs=0.0) == stray


def test_resolve_transcript_path_fallback_picks_newest(tmp_path, monkeypatch):
    """The mtime fallback returns the most recently modified transcript."""
    db = tmp_path / "sessions.db"
    _make_sessions_db(db, [])
    transcripts = tmp_path / "transcripts"
    transcripts.mkdir()
    older = transcripts / "older.json"
    older.write_text("{}")
    newer = transcripts / "newer.json"
    newer.write_text("{}")
    # Force a deterministic ordering regardless of write speed / fs granularity.
    os.utime(older, (1000, 1000))
    os.utime(newer, (2000, 2000))

    monkeypatch.setattr(adapter, "SESSIONS_DB", db)
    monkeypatch.setattr(adapter, "TRANSCRIPTS_DIR", transcripts)

    assert adapter.resolve_transcript_path("/work/proj", poll_secs=0.0) == newer


def test_resolve_transcript_path_polls_for_late_flush(tmp_path, monkeypatch):
    """A transcript flushed slightly after the hook fires is picked up by polling."""
    db = tmp_path / "sessions.db"
    _make_sessions_db(db, [("sess-late", "/work/proj", 100)])
    transcripts = tmp_path / "transcripts"
    transcripts.mkdir()
    target = transcripts / "sess-late.json"

    monkeypatch.setattr(adapter, "SESSIONS_DB", db)
    monkeypatch.setattr(adapter, "TRANSCRIPTS_DIR", transcripts)

    # Speed up the poll loop and reveal the file on the second exists() check.
    monkeypatch.setattr(adapter, "_POLL_INTERVAL", 0.0)
    real_exists = Path.exists
    calls = {"n": 0}

    def flaky_exists(self):
        if self == target:
            calls["n"] += 1
            if calls["n"] == 1:
                return False
            target.write_text("{}")
            return True
        return real_exists(self)

    monkeypatch.setattr(Path, "exists", flaky_exists)

    assert adapter.resolve_transcript_path("/work/proj", poll_secs=1.0) == target
    assert calls["n"] >= 2  # proves it polled rather than returning on first check


# ---------------------------------------------------------------------------
# idempotency edge cases
# ---------------------------------------------------------------------------


def test_emitted_empty_id_is_noop(tmp_path, monkeypatch):
    """An empty session id is never 'emitted' and mark_emitted writes nothing."""
    state = tmp_path / "state"
    monkeypatch.setattr(adapter, "STATE_DIR", state)

    assert adapter.already_emitted("") is False
    adapter.mark_emitted("")  # must not raise
    assert not (state / "emitted.txt").exists()


def test_emitted_exact_match_no_substring_false_positive(tmp_path, monkeypatch):
    """A recorded id must not match ids that merely contain it as a substring."""
    monkeypatch.setattr(adapter, "STATE_DIR", tmp_path / "state")

    adapter.mark_emitted("sess-1")
    assert adapter.already_emitted("sess-1") is True
    assert adapter.already_emitted("sess-10") is False
    assert adapter.already_emitted("ess-1") is False


def test_mark_emitted_appends_multiple(tmp_path, monkeypatch):
    """Multiple ids accumulate; all remain queryable."""
    monkeypatch.setattr(adapter, "STATE_DIR", tmp_path / "state")

    for sid in ("a", "b", "c"):
        adapter.mark_emitted(sid)
    for sid in ("a", "b", "c"):
        assert adapter.already_emitted(sid) is True
    assert adapter.already_emitted("d") is False
