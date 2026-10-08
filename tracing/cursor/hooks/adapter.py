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
MAX_THOUGHT_HASHES = 256
MAX_RESPONSE_FRAGMENTS = 64
MAX_RESPONSE_FRAGMENT_CHARS = 10000
MAX_TERMINAL_MARKERS = 4096
MAX_GENERATION_ALIASES = 64

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


def conversation_state_key(conversation_id: str) -> str:
    """Collision-resistant filename key for one conversation.

    ``sanitize()`` maps many distinct conversation IDs (e.g. "a/b" and "a b")
    to the same sanitized text, which would let two unrelated conversations
    collide on the same active-turn or terminal-marker file. Appending a
    short stable hash of the raw ID keeps the key human-readable while
    remaining unique per conversation.
    """
    digest = hashlib.sha256(conversation_id.encode()).hexdigest()[:10]
    readable = sanitize(conversation_id)[:80]
    return f"{readable}_{digest}"


# --- Conversation-scoped active turn state ---


def _active_turn_paths(conversation_id: str):
    safe = conversation_state_key(conversation_id)
    return STATE_DIR / f"active_{safe}.json", STATE_DIR / f".lock_active_{safe}"


def _read_json_dict(path) -> "dict | None":
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def _write_json_atomic(path, value: dict) -> None:
    tmp = path.with_suffix(f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(value, indent=2))
    tmp.replace(path)


def active_turn_save(conversation_id: str, value: dict) -> bool:
    """Replace one conversation's active turn with an atomic, locked write."""
    if not conversation_id or not isinstance(value, dict):
        return False
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        path, lock_path = _active_turn_paths(conversation_id)
        with FileLock(lock_path):
            _write_json_atomic(path, value)
        return True
    except OSError:
        return False


def active_turn_get(conversation_id: str) -> "dict | None":
    """Read one conversation's active turn. Missing or corrupt state is absent."""
    if not conversation_id:
        return None
    try:
        path, lock_path = _active_turn_paths(conversation_id)
        with FileLock(lock_path):
            return _read_json_dict(path)
    except OSError:
        return None


def active_turn_update(conversation_id: str, updates: dict) -> "dict | None":
    """Merge fields into an active turn and return the new state."""
    if not conversation_id or not isinstance(updates, dict):
        return None
    try:
        path, lock_path = _active_turn_paths(conversation_id)
        with FileLock(lock_path):
            state = _read_json_dict(path)
            if state is None:
                return None
            state.update(updates)
            _write_json_atomic(path, state)
            return state
    except OSError:
        return None


def active_turn_add_response(
    conversation_id: str,
    text: str,
    observed_ms: int,
    model: str = "",
    user_id: str = "",
    expected_root_span_id: str = "",
) -> "dict | None":
    """Append one response fragment in observation order.

    Fragment count is bounded (``MAX_RESPONSE_FRAGMENTS``), dropping the
    oldest fragments first, so a long-running turn cannot grow the state file
    without bound or make each rewrite progressively more expensive.
    """
    if not conversation_id:
        return None
    try:
        path, lock_path = _active_turn_paths(conversation_id)
        with FileLock(lock_path):
            state = _read_json_dict(path)
            if state is None:
                return None
            if expected_root_span_id and state.get("root_span_id") != expected_root_span_id:
                return None
            responses = state.get("responses")
            if not isinstance(responses, list):
                responses = []
            response_start_ms = int(state.get("last_activity_ms") or state.get("start_ms") or observed_ms)
            responses.append(
                {
                    "text": truncate_attr(text, MAX_RESPONSE_FRAGMENT_CHARS),
                    "start_ms": response_start_ms,
                    "observed_ms": observed_ms,
                }
            )
            state["responses"] = responses[-MAX_RESPONSE_FRAGMENTS:]
            state["last_activity_ms"] = observed_ms
            if model:
                state["model"] = model
            # afterAgentResponse can supply a user identity Cursor had not yet
            # resolved at beforeSubmitPrompt time — copy it in when present.
            if user_id:
                state["user_id"] = user_id
            _write_json_atomic(path, state)
            return state
    except OSError:
        return None


def active_turn_add_thought_hash(
    conversation_id: str,
    thought_hash: str,
    observed_ms: int,
    expected_root_span_id: str = "",
) -> bool:
    """Record one exact thought hash. Return False when it already exists."""
    if not conversation_id or not thought_hash:
        return True
    try:
        path, lock_path = _active_turn_paths(conversation_id)
        with FileLock(lock_path):
            state = _read_json_dict(path)
            if state is None:
                return True
            if expected_root_span_id and state.get("root_span_id") != expected_root_span_id:
                return False
            hashes = state.get("thought_hashes")
            if not isinstance(hashes, list):
                hashes = []
            if thought_hash in hashes:
                return False
            hashes.append(thought_hash)
            state["thought_hashes"] = hashes[-MAX_THOUGHT_HASHES:]
            state["last_activity_ms"] = observed_ms
            _write_json_atomic(path, state)
            return True
    except OSError:
        return True


def active_turn_clear(conversation_id: str) -> "dict | None":
    """Atomically claim and remove one active turn, unconditionally.

    Used for the approved fallback-closure paths (next prompt, sessionEnd),
    which intentionally close whatever turn is pending regardless of its
    identity. For an identity-gated close, use ``active_turn_clear_if_matches``.
    """
    if not conversation_id:
        return None
    try:
        path, lock_path = _active_turn_paths(conversation_id)
        with FileLock(lock_path):
            state = _read_json_dict(path)
            path.unlink(missing_ok=True)
            return state
    except OSError:
        return None


def turn_matches_generation(turn: dict, generation_id: str) -> bool:
    """True when ``generation_id`` is empty, is the turn's canonical id, or
    is a previously recorded alias of it.

    An empty incoming generation id (no generation concept at all, e.g. some
    CLI events) always matches — there is nothing more specific to check
    against.
    """
    if not generation_id:
        return True
    if generation_id == turn.get("generation_id"):
        return True
    aliases = turn.get("generation_aliases")
    return isinstance(aliases, list) and generation_id in aliases


def active_turn_record_alias(
    conversation_id: str,
    generation_id: str,
    observed_ms: int,
    expected_root_span_id: str = "",
) -> "dict | None":
    """Record one additional generation id observed for the active turn.

    Cursor can rotate the generation id mid-turn (e.g. tool/shell sub-events
    may carry a different id than the one beforeSubmitPrompt started with)
    while conversation-level resolution still correctly attributes them to
    the same turn. Recording every id actually observed — bounded by
    ``MAX_GENERATION_ALIASES`` — lets a later event (notably ``stop``) verify
    its generation against the turn's full known identity instead of either
    rejecting a legitimate rotated id or blindly trusting an unrelated,
    never-before-seen (and possibly stale) one.
    """
    if not conversation_id or not generation_id:
        return None
    try:
        path, lock_path = _active_turn_paths(conversation_id)
        with FileLock(lock_path):
            state = _read_json_dict(path)
            if state is None:
                return None
            if expected_root_span_id and state.get("root_span_id") != expected_root_span_id:
                return None
            if generation_id == state.get("generation_id"):
                state["last_activity_ms"] = observed_ms
                _write_json_atomic(path, state)
                return state
            aliases = state.get("generation_aliases")
            if not isinstance(aliases, list):
                aliases = []
            if generation_id not in aliases:
                aliases.append(generation_id)
                state["generation_aliases"] = aliases[-MAX_GENERATION_ALIASES:]
            state["last_activity_ms"] = observed_ms
            _write_json_atomic(path, state)
            return state
    except OSError:
        return None


def active_turn_clear_if_matches(
    conversation_id: str,
    root_span_id: str = "",
    generation_id: str = "",
) -> "dict | None":
    """Atomically clear the active turn only if it still matches.

    A match is either an exact root span id match, or ``generation_id``
    matching the turn's *current* canonical id or one of its recorded
    aliases (re-read from disk under the lock, not from a possibly-stale
    caller-held copy). When neither matches — including when there is no
    active turn at all — nothing is cleared and ``None`` is returned.

    This is the guard a ``stop`` event must use before closing a turn: it
    must clear only the turn it actually resolved to, never whatever turn
    happens to be active right now. A stale or otherwise-mismatched
    generation leaves the active turn untouched.
    """
    if not conversation_id or not (root_span_id or generation_id):
        return None
    try:
        path, lock_path = _active_turn_paths(conversation_id)
        with FileLock(lock_path):
            state = _read_json_dict(path)
            if state is None:
                return None
            root_matches = bool(root_span_id) and state.get("root_span_id") == root_span_id
            gen_matches = bool(generation_id) and turn_matches_generation(state, generation_id)
            if not (root_matches or gen_matches):
                return None
            path.unlink(missing_ok=True)
            return state
    except OSError:
        return None


def _terminal_paths(conversation_id: str):
    safe = conversation_state_key(conversation_id)
    return STATE_DIR / f"terminal_{safe}", STATE_DIR / f".lock_terminal_{safe}"


def _merge_terminal_markers(existing, candidates: list) -> list:
    """Append new markers to the existing bounded list, de-duplicated, newest last."""
    merged = list(existing) if isinstance(existing, list) else []
    for value in candidates:
        if value not in merged:
            merged.append(value)
    return merged[-MAX_TERMINAL_MARKERS:]


def terminal_turn_claim_many(conversation_id: str, generation_ids) -> bool:
    """Atomically claim all generation aliases for one terminal event."""
    if not conversation_id:
        return True
    candidates = [value for value in dict.fromkeys(generation_ids) if value]
    if not candidates:
        return True
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        path, lock_path = _terminal_paths(conversation_id)
        with FileLock(lock_path):
            raw = path.read_text().strip() if path.exists() else "[]"
            try:
                existing = json.loads(raw)
            except json.JSONDecodeError:
                existing = [raw] if raw else []
            if isinstance(existing, list) and any(value in existing for value in candidates):
                return False
            tmp = path.with_suffix(f".tmp.{os.getpid()}")
            tmp.write_text(json.dumps(_merge_terminal_markers(existing, candidates)))
            tmp.replace(path)
            return True
    except OSError:
        return True


def terminal_turn_claim(
    conversation_id: str,
    generation_id: str,
    alternate_generation_id: str = "",
) -> bool:
    """Atomically claim terminal delivery for known generation aliases."""
    return terminal_turn_claim_many(conversation_id, (generation_id, alternate_generation_id))


def terminal_turn_mark_many(conversation_id: str, generation_ids) -> None:
    """Mark every given generation id terminal in one bounded, durable write.

    Used when a turn's full known identity (its canonical id plus every
    alias observed during its lifetime) must all be recorded at once — e.g.
    fallback closure at the next prompt or sessionEnd, or a successful
    ``stop`` close — so a late event for *any* of those ids is still
    recognized as a duplicate, not just the one id a 2-argument call would
    have captured.
    """
    if not conversation_id:
        return
    candidates = [value for value in dict.fromkeys(generation_ids) if value]
    if not candidates:
        return
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        path, lock_path = _terminal_paths(conversation_id)
        with FileLock(lock_path):
            raw = path.read_text().strip() if path.exists() else "[]"
            try:
                existing = json.loads(raw)
            except json.JSONDecodeError:
                existing = [raw] if raw else []
            merged = _merge_terminal_markers(existing, candidates)
            tmp = path.with_suffix(f".tmp.{os.getpid()}")
            tmp.write_text(json.dumps(merged))
            tmp.replace(path)
    except OSError:
        return


def terminal_turn_is_marked(conversation_id: str, generation_id: str) -> bool:
    """Return whether a generation already reached a terminal event."""
    if not conversation_id or not generation_id:
        return False
    try:
        path, lock_path = _terminal_paths(conversation_id)
        with FileLock(lock_path):
            raw = path.read_text().strip() if path.exists() else "[]"
            try:
                existing = json.loads(raw)
            except json.JSONDecodeError:
                existing = [raw] if raw else []
            return isinstance(existing, list) and generation_id in existing
    except OSError:
        return False


def terminal_turn_mark(
    conversation_id: str,
    generation_id: str,
    alternate_generation_id: str = "",
) -> None:
    """Remember terminal generation aliases for duplicate delivery checks.

    Appends to the same bounded, durable marker list as ``terminal_turn_claim``
    instead of overwriting it, for the same reason: a later overwrite must
    not erase an older generation's already-recorded terminal marker.
    """
    terminal_turn_mark_many(conversation_id, (generation_id, alternate_generation_id))


def _terminal_nogen_path(conversation_id: str):
    safe = conversation_state_key(conversation_id)
    return STATE_DIR / f"terminal_nogen_{safe}"


def terminal_nogen_claim(conversation_id: str) -> bool:
    """Atomically claim terminal delivery for a conversation's generation-less
    ``stop`` (no generation_id at all on either the turn or the event).

    This is a dedicated sentinel, separate from the per-generation marker
    list. A generation-less turn has no identity to key a durable marker on,
    so this sentinel must be resettable at the next generation-less prompt —
    but resetting *it* must never discard the per-generation marker list's
    history for real generations that happened in between.
    """
    if not conversation_id:
        return True
    path, lock_path = _terminal_paths(conversation_id)
    sentinel = _terminal_nogen_path(conversation_id)
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with FileLock(lock_path):
            if sentinel.exists():
                return False
            sentinel.write_text("1")
            return True
    except OSError:
        return True


def terminal_nogen_clear(conversation_id: str) -> None:
    """Reset only the generation-less dedup sentinel for a new turn.

    Deliberately leaves the per-generation terminal marker list (and its
    file) untouched — only this dedicated sentinel is specific to turns that
    never had a generation id at all.
    """
    if not conversation_id:
        return
    try:
        _, lock_path = _terminal_paths(conversation_id)
        with FileLock(lock_path):
            _terminal_nogen_path(conversation_id).unlink(missing_ok=True)
    except OSError:
        return


def conversation_cleanup(conversation_id: str) -> None:
    """Remove active state after a conversation ends.

    Terminal history remains bounded and durable so late duplicate stops cannot
    create duplicate spans. Lock files remain reusable because unlinking a lock
    inode can break mutual exclusion for a concurrent process.
    """
    if not conversation_id:
        return
    active_path, active_lock = _active_turn_paths(conversation_id)
    try:
        with FileLock(active_lock):
            active_path.unlink(missing_ok=True)
    except OSError:
        pass


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
