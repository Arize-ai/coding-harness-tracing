"""Tests for the Devin adapter: trace-enable gate + per-generation emit state."""

from tracing.devin.hooks import adapter

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
# emit watermark — keyed by (session_id, request_id)
# ---------------------------------------------------------------------------


def test_emitted_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(adapter, "STATE_DIR", tmp_path / "state")

    assert adapter.already_emitted("sess-x", "req-1") is False
    adapter.mark_emitted("sess-x", "req-1")
    assert adapter.already_emitted("sess-x", "req-1") is True
    # A different request in the same session is still unseen.
    assert adapter.already_emitted("sess-x", "req-2") is False
    # Same request id under a different session is independent.
    assert adapter.already_emitted("sess-y", "req-1") is False


def test_emitted_empty_ids_are_noop(tmp_path, monkeypatch):
    """Empty session or request id is never 'emitted' and writes nothing."""
    state = tmp_path / "state"
    monkeypatch.setattr(adapter, "STATE_DIR", state)

    assert adapter.already_emitted("", "req-1") is False
    assert adapter.already_emitted("sess-x", "") is False
    adapter.mark_emitted("", "req-1")  # must not raise
    adapter.mark_emitted("sess-x", "")  # must not raise
    assert not (state / "emitted_requests.txt").exists()


def test_emitted_exact_match_no_substring_false_positive(tmp_path, monkeypatch):
    """A recorded key must not match keys that merely share a prefix/substring."""
    monkeypatch.setattr(adapter, "STATE_DIR", tmp_path / "state")

    adapter.mark_emitted("sess-1", "req-1")
    assert adapter.already_emitted("sess-1", "req-1") is True
    assert adapter.already_emitted("sess-1", "req-10") is False
    assert adapter.already_emitted("sess-10", "req-1") is False


def test_mark_emitted_appends_multiple(tmp_path, monkeypatch):
    """Multiple generations accumulate; all remain queryable."""
    monkeypatch.setattr(adapter, "STATE_DIR", tmp_path / "state")

    for rid in ("a", "b", "c"):
        adapter.mark_emitted("sess-1", rid)
    for rid in ("a", "b", "c"):
        assert adapter.already_emitted("sess-1", rid) is True
    assert adapter.already_emitted("sess-1", "d") is False
