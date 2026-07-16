"""Tests for the Devin adapter: session/transcript resolution + idempotency."""

import sqlite3

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
