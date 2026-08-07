"""Tests for tracing.devin.session_db — live sessions.db reader.

Focus: resolving the session, deduping duplicated assistant generations by
request_id, extracting tokens/model/reasoning/tool-calls, and best-effort user
prompt / session metadata. All against a fixture SQLite DB built in tmp_path.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from tracing.devin import session_db


def _assistant(
    request_id,
    content="",
    thinking="",
    model="swe-1.6",
    inp=0,
    out=0,
    cache_read=0,
    cache_write=0,
    tools=None,
    start=None,
    end=None,
):
    return {
        "role": "assistant",
        "content": content,
        "thinking": thinking,
        "tool_calls": tools or [],
        "metadata": {
            "request_id": request_id,
            "generation_model": model,
            "started_generation_at": start,
            "created_at": end,
            "metrics": {
                "input_tokens": inp,
                "output_tokens": out,
                "cache_read_tokens": cache_read,
                "cache_creation_tokens": cache_write,
            },
        },
    }


def _user(content):
    return {"role": "user", "content": content, "metadata": {"is_user_input": True}}


def _system(content="sys"):
    return {"role": "system", "content": content, "metadata": {}}


def _build_db(path, session_rows, node_rows, prompt_rows=()):
    con = sqlite3.connect(str(path))
    try:
        con.execute(
            "CREATE TABLE sessions (id TEXT, working_directory TEXT, backend_type TEXT, "
            "model TEXT, last_activity_at INTEGER, main_chain_id INTEGER)"
        )
        con.executemany(
            "INSERT INTO sessions (id, working_directory, backend_type, model, last_activity_at, main_chain_id) "
            "VALUES (?,?,?,?,?,?)",
            session_rows,
        )
        con.execute(
            "CREATE TABLE message_nodes (row_id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, "
            "node_id INTEGER, parent_node_id INTEGER, chat_message TEXT, created_at INTEGER, metadata TEXT)"
        )
        con.executemany(
            "INSERT INTO message_nodes (session_id, node_id, parent_node_id, chat_message, created_at) "
            "VALUES (?,?,?,?,?)",
            [(sid, nid, None, json.dumps(msg), 0) for sid, nid, msg in node_rows],
        )
        con.execute(
            "CREATE TABLE prompt_history (id INTEGER PRIMARY KEY AUTOINCREMENT, content TEXT, "
            "timestamp INTEGER, session_id TEXT, is_shell INTEGER DEFAULT 0)"
        )
        con.executemany(
            "INSERT INTO prompt_history (content, timestamp, session_id, is_shell) VALUES (?,?,?,?)",
            prompt_rows,
        )
        con.commit()
    finally:
        con.close()


# ---------------------------------------------------------------------------
# resolve_session_id / connect_readonly
# ---------------------------------------------------------------------------


def test_resolve_session_id_returns_newest(tmp_path):
    db = tmp_path / "sessions.db"
    _build_db(
        db,
        [("old", "/work/proj", "Windsurf", "m", 100, 1), ("new", "/work/proj", "Windsurf", "m", 200, 1)],
        [],
    )
    con = session_db.connect_readonly(db)
    try:
        assert session_db.resolve_session_id(con, "/work/proj") == "new"
        assert session_db.resolve_session_id(con, "/nope") is None
        assert session_db.resolve_session_id(con, "") is None
    finally:
        con.close()


def test_resolve_session_id_ignores_trailing_slash(tmp_path):
    """Trailing slashes on either side must not break the match."""
    db = tmp_path / "sessions.db"
    proj = tmp_path / "proj"
    proj.mkdir()
    _build_db(
        db,
        [("plain", str(proj), "b", "m", 100, 1), ("slashed", str(proj) + "/", "b", "m", 200, 1)],
        [],
    )
    con = session_db.connect_readonly(db)
    try:
        # Both rows canonicalize to the same dir; the newest wins regardless of
        # whether the query itself carries a trailing slash.
        assert session_db.resolve_session_id(con, str(proj)) == "slashed"
        assert session_db.resolve_session_id(con, str(proj) + "/") == "slashed"
    finally:
        con.close()


def test_resolve_session_id_matches_through_symlink(tmp_path):
    """A symlinked spelling of the project dir matches the stored real path (and vice versa)."""
    real = tmp_path / "real-proj"
    real.mkdir()
    link = tmp_path / "link-proj"
    try:
        link.symlink_to(real, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("platform does not support symlinks")

    db = tmp_path / "sessions.db"
    _build_db(db, [("via-real", str(real), "b", "m", 100, 1)], [])
    con = session_db.connect_readonly(db)
    try:
        assert session_db.resolve_session_id(con, str(link)) == "via-real"
    finally:
        con.close()

    db2 = tmp_path / "sessions2.db"
    _build_db(db2, [("via-link", str(link), "b", "m", 100, 1)], [])
    con = session_db.connect_readonly(db2)
    try:
        assert session_db.resolve_session_id(con, str(real)) == "via-link"
    finally:
        con.close()


def test_resolve_session_id_matches_case_variants_that_resolve_identically(tmp_path):
    """Case-variant spellings match when the filesystem canonicalizes them.

    Only Windows canonicalizes case in ``Path.resolve()``; elsewhere (including
    case-insensitive macOS) the variants resolve to distinct strings, so the
    scenario is skipped rather than asserted.
    """
    proj = tmp_path / "CaseProj"
    proj.mkdir()
    variant = tmp_path / "caseproj"
    if variant.resolve() != proj.resolve():
        pytest.skip("filesystem does not resolve case variants identically")

    db = tmp_path / "sessions.db"
    _build_db(db, [("s", str(proj), "b", "m", 100, 1)], [])
    con = session_db.connect_readonly(db)
    try:
        assert session_db.resolve_session_id(con, str(variant)) == "s"
    finally:
        con.close()


def test_connect_readonly_does_not_mutate(tmp_path):
    db = tmp_path / "sessions.db"
    _build_db(db, [("s", "/w", "b", "m", 1, 1)], [])
    before = db.stat().st_mtime_ns
    con = session_db.connect_readonly(db)
    try:
        con.execute("SELECT 1").fetchone()
    finally:
        con.close()
    assert db.stat().st_mtime_ns == before


def test_connect_readonly_returns_none_on_failure(tmp_path):
    """Per the module contract, an unopenable DB yields None — never an exception."""
    missing = tmp_path / "does-not-exist" / "sessions.db"
    assert session_db.connect_readonly(missing, retries=1, delay=0) is None


# ---------------------------------------------------------------------------
# read_steps — dedup by request_id
# ---------------------------------------------------------------------------


def test_read_steps_dedupes_by_request_id(tmp_path):
    """Duplicated assistant nodes (same request_id, different node_id) collapse to one step."""
    db = tmp_path / "sessions.db"
    nodes = [
        ("s", 0, _system()),
        ("s", 1, _user("hello")),
        ("s", 2, _assistant("req-A", content="hi", inp=100, out=10)),
        # tree-duplication copy of the same generation under a new node_id
        ("s", 5, _assistant("req-A", content="hi", inp=100, out=10)),
        ("s", 6, _assistant("req-B", content="bye", inp=50, out=5)),
    ]
    _build_db(db, [("s", "/w", "b", "m", 1, 6)], nodes)
    con = session_db.connect_readonly(db)
    try:
        steps = session_db.read_steps(con, "s")
    finally:
        con.close()

    assert [s.request_id for s in steps] == ["req-A", "req-B"]
    assert steps[0].node_id == 2  # kept the earliest copy for ordering
    assert steps[0].prompt_tokens == 100
    assert steps[1].completion_tokens == 5


def test_read_steps_prefers_copy_with_content(tmp_path):
    """When the first copy has empty content but a later copy fills it in, keep the content."""
    db = tmp_path / "sessions.db"
    nodes = [
        ("s", 2, _assistant("req-A", content="", thinking="pondering", inp=10, out=1)),
        ("s", 5, _assistant("req-A", content="the answer", inp=10, out=1)),
    ]
    _build_db(db, [("s", "/w", "b", "m", 1, 5)], nodes)
    con = session_db.connect_readonly(db)
    try:
        steps = session_db.read_steps(con, "s")
    finally:
        con.close()

    assert len(steps) == 1
    assert steps[0].content == "the answer"
    assert steps[0].node_id == 2  # ordering anchor preserved


def test_read_steps_extracts_reasoning_tools_and_cache(tmp_path):
    db = tmp_path / "sessions.db"
    tools = [{"id": "call_1", "name": "web_search", "arguments": {"q": "seattle"}}]
    nodes = [
        (
            "s",
            2,
            _assistant(
                "req-A",
                content="",
                thinking="I should search",
                inp=200,
                out=20,
                cache_read=150,
                cache_write=30,
                tools=tools,
                start="2026-07-16T22:38:21.728756Z",
                end="2026-07-16T22:38:22.639816Z",
            ),
        ),
    ]
    _build_db(db, [("s", "/w", "b", "m", 1, 2)], nodes)
    con = session_db.connect_readonly(db)
    try:
        steps = session_db.read_steps(con, "s")
    finally:
        con.close()

    (step,) = steps
    assert step.thinking == "I should search"
    assert step.cache_read_tokens == 150
    assert step.cache_write_tokens == 30
    assert step.tool_calls[0].name == "web_search"
    assert step.tool_calls[0].arguments == {"q": "seattle"}
    assert step.start_ms > 0 and step.end_ms >= step.start_ms


def test_read_steps_skips_non_generation_nodes(tmp_path):
    """System/user nodes and assistant stubs without metrics are ignored."""
    db = tmp_path / "sessions.db"
    stub = {"role": "assistant", "content": "copied prefix", "metadata": {"request_id": "x"}}  # no metrics
    nodes = [("s", 0, _system()), ("s", 1, _user("hi")), ("s", 2, stub)]
    _build_db(db, [("s", "/w", "b", "m", 1, 2)], nodes)
    con = session_db.connect_readonly(db)
    try:
        assert session_db.read_steps(con, "s") == []
    finally:
        con.close()


# ---------------------------------------------------------------------------
# latest_user_prompt / read_session_meta
# ---------------------------------------------------------------------------


def test_latest_user_prompt_prefers_prompt_history(tmp_path):
    db = tmp_path / "sessions.db"
    _build_db(
        db,
        [("s", "/w", "b", "m", 1, 2)],
        [("s", 1, _user("node prompt"))],
        prompt_rows=[("older", 100, "s", 0), ("newest", 200, "s", 0), ("a shell cmd", 300, "s", 1)],
    )
    con = session_db.connect_readonly(db)
    try:
        # newest non-shell prompt wins; the is_shell=1 row is ignored
        assert session_db.latest_user_prompt(con, "s") == "newest"
    finally:
        con.close()


def test_latest_user_prompt_falls_back_to_message_node(tmp_path):
    db = tmp_path / "sessions.db"
    nodes = [("s", 1, _user("first")), ("s", 3, _user("second"))]
    _build_db(db, [("s", "/w", "b", "m", 1, 3)], nodes)
    con = session_db.connect_readonly(db)
    try:
        assert session_db.latest_user_prompt(con, "s") == "second"
    finally:
        con.close()


def test_read_session_meta(tmp_path):
    db = tmp_path / "sessions.db"
    _build_db(db, [("s", "/w", "Windsurf", "SWE-1.6 Slow", 1, 2)], [])
    con = session_db.connect_readonly(db)
    try:
        meta = session_db.read_session_meta(con, "s")
    finally:
        con.close()
    assert meta == {"backend": "Windsurf", "model": "SWE-1.6 Slow"}


def test_iso_to_ms_handles_z_suffix_and_garbage():
    assert session_db._iso_to_ms("2026-07-16T22:38:21.728756Z") > 0
    assert session_db._iso_to_ms("") == 0
    assert session_db._iso_to_ms("not-a-date") == 0
    assert session_db._iso_to_ms(None) == 0
