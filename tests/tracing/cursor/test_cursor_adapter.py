#!/usr/bin/env python3
"""Tests for tracing.cursor.hooks.adapter — Cursor-specific adapter module."""
import hashlib
import json
import threading

import pytest

from tracing.cursor.hooks import adapter

# ── Helpers ────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _patch_state_dir(tmp_path, monkeypatch):
    """Redirect STATE_DIR to a temp directory for every test."""
    state_dir = tmp_path / "state" / "cursor"
    state_dir.mkdir(parents=True)
    monkeypatch.setattr(adapter, "STATE_DIR", state_dir)
    return state_dir


# ── trace_id_from_generation ──────────────────────────────────────────────


class TestTraceIdFromGeneration:
    def test_returns_32_hex(self):
        result = adapter.trace_id_from_generation("gen-abc")
        assert len(result) == 32
        int(result, 16)  # must be valid hex

    def test_deterministic(self):
        a = adapter.trace_id_from_generation("gen-abc")
        b = adapter.trace_id_from_generation("gen-abc")
        assert a == b

    def test_different_inputs_differ(self):
        a = adapter.trace_id_from_generation("gen-abc")
        b = adapter.trace_id_from_generation("gen-xyz")
        assert a != b

    def test_matches_md5(self):
        """Verify output matches: echo -n 'gen-abc' | md5sum | cut -c1-32"""
        expected = hashlib.md5(b"gen-abc").hexdigest()[:32]
        assert adapter.trace_id_from_generation("gen-abc") == expected


# ── span_id_16 ────────────────────────────────────────────────────────────


class TestSpanId16:
    def test_returns_16_hex(self):
        result = adapter.span_id_16()
        assert len(result) == 16
        int(result, 16)

    def test_unique(self):
        a = adapter.span_id_16()
        b = adapter.span_id_16()
        assert a != b


# ── sanitize ──────────────────────────────────────────────────────────────


class TestSanitize:
    def test_unchanged(self):
        assert adapter.sanitize("hello") == "hello"

    def test_slash(self):
        assert adapter.sanitize("foo/bar") == "foo_bar"

    def test_preserves_dots_hyphens_underscores(self):
        assert adapter.sanitize("foo.bar-baz_qux") == "foo.bar-baz_qux"

    def test_special_chars(self):
        assert adapter.sanitize("a@b#c$d") == "a_b_c_d"

    def test_empty(self):
        assert adapter.sanitize("") == ""


# ── state_push / state_pop ────────────────────────────────────────────────


class TestStateStack:
    def test_push_pop_single(self):
        adapter.state_push("test_key", {"a": 1})
        result = adapter.state_pop("test_key")
        assert result == {"a": 1}

    def test_lifo_order(self):
        adapter.state_push("k", {"val": "A"})
        adapter.state_push("k", {"val": "B"})
        assert adapter.state_pop("k") == {"val": "B"}
        assert adapter.state_pop("k") == {"val": "A"}

    def test_pop_empty_returns_none(self):
        assert adapter.state_pop("nonexistent") is None

    def test_pop_corrupted_returns_none(self):
        stack_file = adapter.STATE_DIR / "bad.stack.json"
        stack_file.write_text(":::not valid json{{{")
        assert adapter.state_pop("bad") is None

    def test_push_creates_file(self):
        adapter.state_push("new_key", {"x": 1})
        stack_file = adapter.STATE_DIR / "new_key.stack.json"
        assert stack_file.exists()

    def test_pop_last_leaves_empty_list(self):
        adapter.state_push("k2", {"x": 1})
        adapter.state_pop("k2")
        stack_file = adapter.STATE_DIR / "k2.stack.json"
        data = json.loads(stack_file.read_text())
        assert data == []

    def test_concurrent_push(self):
        """5 threads push concurrently — all values present, no corruption."""
        errors = []

        def push_val(i):
            try:
                adapter.state_push("concurrent", {"i": i})
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=push_val, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        stack_file = adapter.STATE_DIR / "concurrent.stack.json"
        data = json.loads(stack_file.read_text())
        assert isinstance(data, list)
        assert len(data) == 5
        values = sorted(d["i"] for d in data)
        assert values == [0, 1, 2, 3, 4]

    def test_stack_file_valid_json(self):
        adapter.state_push("json_check", {"a": 1})
        adapter.state_push("json_check", {"b": 2})
        stack_file = adapter.STATE_DIR / "json_check.stack.json"
        data = json.loads(stack_file.read_text())
        assert isinstance(data, list)
        assert len(data) == 2

    def test_stack_file_is_pretty_indented(self):
        """JSON output uses indent=2 per the project's serialization convention."""
        adapter.state_push("pretty", {"a": 1, "b": [1, 2]})
        stack_file = adapter.STATE_DIR / "pretty.stack.json"
        text = stack_file.read_text()
        # indented JSON contains newlines between entries
        assert "\n" in text
        # confirm round-trip
        assert json.loads(text) == [{"a": 1, "b": [1, 2]}]

    def test_push_then_pop_roundtrip_with_nested(self):
        """JSON encoder must round-trip nested dicts/lists with the same fidelity YAML did."""
        payload = {"cmd": "echo hi", "env": {"FOO": "bar"}, "args": ["a", "b", "c"]}
        adapter.state_push("nested", payload)
        popped = adapter.state_pop("nested")
        assert popped == payload

    def test_empty_file_treated_as_empty_stack(self):
        """An empty stack file should be treated as []. push must still succeed."""
        empty = adapter.STATE_DIR / "empty.stack.json"
        empty.write_text("")
        # push should not raise — empty content → JSONDecodeError → fallback to []
        adapter.state_push("empty", {"k": 1})
        assert json.loads(empty.read_text()) == [{"k": 1}]

    def test_corrupt_file_on_push_resets_to_list(self):
        """If existing file is corrupt JSON, push resets list and appends."""
        corrupt = adapter.STATE_DIR / "corrupt.stack.json"
        corrupt.write_text("{not json")
        adapter.state_push("corrupt", {"k": "v"})
        assert json.loads(corrupt.read_text()) == [{"k": "v"}]


# ── gen_root_span ─────────────────────────────────────────────────────────


class TestGenRootSpan:
    def test_save_and_get(self):
        adapter.gen_root_span_save("gen-1", "span123")
        assert adapter.gen_root_span_get("gen-1") == "span123"

    def test_get_no_save(self):
        assert adapter.gen_root_span_get("gen-missing") == ""

    def test_get_empty_gen_id(self):
        assert adapter.gen_root_span_get("") == ""

    def test_save_overwrites(self):
        adapter.gen_root_span_save("gen-2", "old_span")
        adapter.gen_root_span_save("gen-2", "new_span")
        assert adapter.gen_root_span_get("gen-2") == "new_span"


# ── state_cleanup_generation ──────────────────────────────────────────────


class TestStateCleanupGeneration:
    def test_cleanup_removes_all_files(self):
        gen_id = "gen-cleanup"
        safe = adapter.sanitize(gen_id)

        # Create root file
        adapter.gen_root_span_save(gen_id, "span1")
        # Create stack files
        adapter.state_push(f"before_{safe}_shell", {"cmd": "ls"})
        adapter.state_push(f"before_{safe}_mcp", {"tool": "read"})

        adapter.state_cleanup_generation(gen_id)

        assert not (adapter.STATE_DIR / f"root_{safe}").exists()
        assert not list(adapter.STATE_DIR.glob(f"*{safe}*.stack.json"))

    def test_cleanup_no_files_no_error(self):
        adapter.state_cleanup_generation("gen-nonexistent")  # should not raise

    def test_cleanup_preserves_other_generations(self):
        adapter.gen_root_span_save("gen-keep", "span_keep")
        adapter.gen_root_span_save("gen-remove", "span_remove")

        adapter.state_cleanup_generation("gen-remove")

        assert adapter.gen_root_span_get("gen-keep") == "span_keep"

    def test_cleanup_nonempty_lock_dir(self):
        gen_id = "gen-lockdir"
        safe = adapter.sanitize(gen_id)
        lock_dir = adapter.STATE_DIR / f".lock_before_{safe}_shell"
        lock_dir.mkdir(parents=True)
        # Put a file inside so rmdir fails
        (lock_dir / "stale").write_text("x")

        adapter.state_cleanup_generation(gen_id)
        # dir should still exist (rmdir fails on non-empty), but no crash
        assert lock_dir.exists()


# ── conversation-scoped active turns ───────────────────────────────────────


class TestActiveTurnState:
    def test_round_trip_update_and_clear(self):
        turn = {
            "generation_id": "gen-1",
            "trace_id": "trace-1",
            "root_span_id": "root-1",
            "start_ms": 10,
            "last_activity_ms": 10,
            "thought_hashes": [],
        }
        assert adapter.active_turn_save("conv-1", turn) is True
        assert adapter.active_turn_get("conv-1") == turn
        updated = adapter.active_turn_update("conv-1", {"last_activity_ms": 20})
        assert updated["last_activity_ms"] == 20
        assert adapter.active_turn_clear("conv-1")["root_span_id"] == "root-1"
        assert adapter.active_turn_get("conv-1") is None

    def test_corrupt_state_is_absent_and_clear_removes_it(self):
        path, _ = adapter._active_turn_paths("conv-bad")
        path.write_text("{bad json")
        assert adapter.active_turn_get("conv-bad") is None
        assert adapter.active_turn_clear("conv-bad") is None
        assert not path.exists()

    def test_conversations_are_isolated(self):
        adapter.active_turn_save("conv-a", {"generation_id": "a"})
        adapter.active_turn_save("conv-b", {"generation_id": "b"})
        adapter.active_turn_clear("conv-a")
        assert adapter.active_turn_get("conv-a") is None
        assert adapter.active_turn_get("conv-b") == {"generation_id": "b"}

    def test_thought_hashes_are_exact_and_bounded(self, monkeypatch):
        monkeypatch.setattr(adapter, "MAX_THOUGHT_HASHES", 3)
        adapter.active_turn_save("conv-1", {"thought_hashes": []})
        assert adapter.active_turn_add_thought_hash("conv-1", "a", 1) is True
        assert adapter.active_turn_add_thought_hash("conv-1", "a", 2) is False
        for value in ("b", "c", "d"):
            assert adapter.active_turn_add_thought_hash("conv-1", value, 3) is True
        assert adapter.active_turn_get("conv-1")["thought_hashes"] == ["b", "c", "d"]

    def test_response_fragments_keep_observation_order(self):
        adapter.active_turn_save("conv-1", {"responses": []})
        adapter.active_turn_add_response("conv-1", "first", 10)
        adapter.active_turn_add_response("conv-1", "second", 20, "model-1")
        state = adapter.active_turn_get("conv-1")
        assert state["responses"] == [
            {"text": "first", "start_ms": 10, "observed_ms": 10},
            {"text": "second", "start_ms": 10, "observed_ms": 20},
        ]
        assert state["model"] == "model-1"

    def test_response_fragments_are_bounded(self, monkeypatch):
        monkeypatch.setattr(adapter, "MAX_RESPONSE_FRAGMENTS", 3)
        adapter.active_turn_save("conv-1", {"responses": []})
        for i in range(5):
            adapter.active_turn_add_response("conv-1", f"frag-{i}", i)
        state = adapter.active_turn_get("conv-1")
        assert [item["text"] for item in state["responses"]] == ["frag-2", "frag-3", "frag-4"]

    def test_response_add_copies_later_user_id(self):
        adapter.active_turn_save("conv-1", {"responses": [], "user_id": ""})
        adapter.active_turn_add_response("conv-1", "hi", 10, user_id="later@example.com")
        assert adapter.active_turn_get("conv-1")["user_id"] == "later@example.com"

    def test_response_add_without_user_id_leaves_existing_value(self):
        adapter.active_turn_save("conv-1", {"responses": [], "user_id": "original@example.com"})
        adapter.active_turn_add_response("conv-1", "hi", 10)
        assert adapter.active_turn_get("conv-1")["user_id"] == "original@example.com"

    def test_turn_matches_generation_empty_always_matches(self):
        turn = {"generation_id": "canonical", "generation_aliases": []}
        assert adapter.turn_matches_generation(turn, "") is True

    def test_turn_matches_generation_canonical(self):
        turn = {"generation_id": "canonical", "generation_aliases": []}
        assert adapter.turn_matches_generation(turn, "canonical") is True
        assert adapter.turn_matches_generation(turn, "other") is False

    def test_turn_matches_generation_alias(self):
        turn = {"generation_id": "canonical", "generation_aliases": ["alias-1"]}
        assert adapter.turn_matches_generation(turn, "alias-1") is True
        assert adapter.turn_matches_generation(turn, "alias-2") is False

    def test_record_alias_appends_and_bounds(self, monkeypatch):
        monkeypatch.setattr(adapter, "MAX_GENERATION_ALIASES", 2)
        adapter.active_turn_save("conv-1", {"generation_id": "canonical", "generation_aliases": []})
        adapter.active_turn_record_alias("conv-1", "a1", 1)
        adapter.active_turn_record_alias("conv-1", "a2", 2)
        adapter.active_turn_record_alias("conv-1", "a3", 3)
        assert adapter.active_turn_get("conv-1")["generation_aliases"] == ["a2", "a3"]

    def test_record_alias_of_canonical_is_a_noop(self):
        adapter.active_turn_save("conv-1", {"generation_id": "canonical", "generation_aliases": []})
        adapter.active_turn_record_alias("conv-1", "canonical", 1)
        assert adapter.active_turn_get("conv-1")["generation_aliases"] == []

    def test_record_alias_no_active_turn_is_noop(self):
        assert adapter.active_turn_record_alias("conv-missing", "a1", 1) is None

    def test_clear_if_matches_by_root_span_id(self):
        adapter.active_turn_save("conv-1", {"generation_id": "g1", "root_span_id": "root-1"})
        assert adapter.active_turn_clear_if_matches("conv-1", root_span_id="wrong") is None
        assert adapter.active_turn_get("conv-1") is not None
        cleared = adapter.active_turn_clear_if_matches("conv-1", root_span_id="root-1")
        assert cleared["root_span_id"] == "root-1"
        assert adapter.active_turn_get("conv-1") is None

    def test_clear_if_matches_by_canonical_generation(self):
        adapter.active_turn_save("conv-1", {"generation_id": "g1", "root_span_id": "root-1"})
        assert adapter.active_turn_clear_if_matches("conv-1", generation_id="wrong") is None
        assert adapter.active_turn_get("conv-1") is not None
        cleared = adapter.active_turn_clear_if_matches("conv-1", generation_id="g1")
        assert cleared["generation_id"] == "g1"

    def test_clear_if_matches_by_alias(self):
        adapter.active_turn_save(
            "conv-1", {"generation_id": "g1", "root_span_id": "root-1", "generation_aliases": ["g2"]}
        )
        assert adapter.active_turn_clear_if_matches("conv-1", generation_id="g2") is not None
        assert adapter.active_turn_get("conv-1") is None

    def test_clear_if_matches_no_active_turn_returns_none(self):
        assert adapter.active_turn_clear_if_matches("conv-missing", generation_id="g1") is None

    def test_clear_if_matches_requires_an_identity(self):
        """Calling with neither root_span_id nor generation_id is refused —
        it would otherwise have no way to express a match at all."""
        adapter.active_turn_save("conv-1", {"generation_id": "g1", "root_span_id": "root-1"})
        assert adapter.active_turn_clear_if_matches("conv-1") is None
        assert adapter.active_turn_get("conv-1") is not None

    def test_terminal_claim_is_atomic_across_generation_aliases(self):
        assert adapter.terminal_turn_claim("conv-1", "event-gen", "canonical-gen") is True
        assert adapter.terminal_turn_claim("conv-1", "canonical-gen") is False
        assert adapter.terminal_turn_claim("conv-1", "event-gen") is False

    def test_terminal_claim_is_durable_across_many_generations(self):
        """A claim for an old generation must still be recognized as a
        duplicate even after several newer generations have since claimed
        their own terminal event — a single-latest marker would forget it."""
        for gen in ("gen-1", "gen-2", "gen-3", "gen-4"):
            assert adapter.terminal_turn_claim("conv-1", gen) is True
        assert adapter.terminal_turn_claim("conv-1", "gen-1") is False

    def test_terminal_markers_are_bounded(self, monkeypatch):
        monkeypatch.setattr(adapter, "MAX_TERMINAL_MARKERS", 3)
        for i in range(5):
            adapter.terminal_turn_claim("conv-1", f"gen-{i}")
        # The oldest marker (gen-0) has fallen out of the bounded window, so
        # it is treated as unclaimed again — this is the accepted tradeoff
        # for bounded (not unbounded) growth.
        assert adapter.terminal_turn_claim("conv-1", "gen-0") is True
        # But a recent one is still remembered.
        assert adapter.terminal_turn_claim("conv-1", "gen-4") is False

    def test_mark_many_records_all_given_ids(self):
        adapter.terminal_turn_mark_many("conv-1", ["g1", "g2", "", "g3"])
        assert adapter.terminal_turn_claim("conv-1", "g1") is False
        assert adapter.terminal_turn_claim("conv-1", "g2") is False
        assert adapter.terminal_turn_claim("conv-1", "g3") is False
        assert adapter.terminal_turn_claim("conv-1", "g4") is True

    def test_mark_many_merges_with_existing(self):
        adapter.terminal_turn_claim("conv-1", "g1")
        adapter.terminal_turn_mark_many("conv-1", ["g2", "g3"])
        assert adapter.terminal_turn_claim("conv-1", "g1") is False
        assert adapter.terminal_turn_claim("conv-1", "g2") is False
        assert adapter.terminal_turn_claim("conv-1", "g3") is False


class TestTerminalNogenSentinel:
    def test_first_claim_succeeds_second_is_rejected(self):
        assert adapter.terminal_nogen_claim("conv-1") is True
        assert adapter.terminal_nogen_claim("conv-1") is False

    def test_clear_resets_only_the_sentinel(self):
        """Resetting the no-generation sentinel must never discard the
        durable per-generation marker history recorded in between."""
        adapter.terminal_turn_claim("conv-1", "real-gen")
        adapter.terminal_nogen_claim("conv-1")

        adapter.terminal_nogen_clear("conv-1")

        assert adapter.terminal_nogen_claim("conv-1") is True
        # The real generation's marker must still be there.
        assert adapter.terminal_turn_claim("conv-1", "real-gen") is False

    def test_conversations_are_isolated(self):
        assert adapter.terminal_nogen_claim("conv-a") is True
        assert adapter.terminal_nogen_claim("conv-b") is True

    def test_empty_conversation_id_is_a_noop(self):
        assert adapter.terminal_nogen_claim("") is True
        adapter.terminal_nogen_clear("")  # must not raise


# ── conversation ID collision prevention ───────────────────────────────────


class TestConversationStateKeyCollisions:
    def test_different_conversation_ids_get_different_active_turn_files(self):
        """Two conversation IDs that sanitize to the same text must not
        collide on the same active-turn state file."""
        path_a, _ = adapter._active_turn_paths("a/b")
        path_b, _ = adapter._active_turn_paths("a b")
        assert adapter.sanitize("a/b") == adapter.sanitize("a b")
        assert path_a != path_b

        adapter.active_turn_save("a/b", {"generation_id": "from-a-slash-b"})
        adapter.active_turn_save("a b", {"generation_id": "from-a-space-b"})
        assert adapter.active_turn_get("a/b")["generation_id"] == "from-a-slash-b"
        assert adapter.active_turn_get("a b")["generation_id"] == "from-a-space-b"

    def test_different_conversation_ids_get_different_terminal_markers(self):
        assert adapter.terminal_turn_claim("a/b", "gen-1") is True
        # A colliding sanitized conversation id must get its own, independent
        # terminal-marker record rather than inheriting "a/b"'s claim.
        assert adapter.terminal_turn_claim("a b", "gen-1") is True


# ── conversation_cleanup ────────────────────────────────────────────────────


class TestConversationCleanup:
    def test_removes_active_state_only(self):
        """conversation_cleanup removes the active-turn state file, but
        deliberately leaves lock files and terminal-marker history alone:

        - Lock files are never deleted while anything might still use them
          (the fcntl/msvcrt FileLock backends only unlock on release, they
          never delete the file — unlinking it here anyway would let a
          concurrent acquirer flock a *different* inode at the same path,
          breaking mutual exclusion).
        - Terminal-marker history must survive sessionEnd: a late,
          out-of-order stop can still arrive afterward, and without its
          marker it would no longer be recognized as a duplicate.
        """
        adapter.active_turn_save("conv-done", {"generation_id": "g1"})
        adapter.terminal_turn_claim("conv-done", "g1")

        active_path, active_lock = adapter._active_turn_paths("conv-done")
        terminal_path, terminal_lock = adapter._terminal_paths("conv-done")
        assert active_path.exists()
        assert terminal_path.exists()

        adapter.conversation_cleanup("conv-done")

        assert not active_path.exists()
        assert active_lock.exists()
        assert terminal_path.exists()
        assert terminal_lock.exists()
        # The retained marker still does its job: the same generation is
        # still recognized as already claimed.
        assert adapter.terminal_turn_claim("conv-done", "g1") is False

    def test_empty_conversation_id_is_a_noop(self):
        adapter.conversation_cleanup("")  # must not raise

    def test_missing_state_is_a_noop(self):
        adapter.conversation_cleanup("conv-never-existed")  # must not raise


# ── check_requirements ────────────────────────────────────────────────────


class TestCheckRequirements:
    def test_enabled(self, monkeypatch):
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        assert adapter.check_requirements() is True
        assert adapter.STATE_DIR.exists()

    def test_disabled(self, monkeypatch):
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "false")
        assert adapter.check_requirements() is False
