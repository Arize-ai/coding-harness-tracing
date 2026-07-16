"""Live ``sessions.db`` reader for the Devin CLI harness.

Devin persists conversation state to a SQLite DB at
``~/.local/share/devin/cli/sessions.db`` *during* a session (not just at end),
so it is the right source for per-turn, ``Stop``-triggered emission. The
transcript JSON is only written at session end and is therefore useless for
live tracing.

Two facts shape this module:

* **The message forest is branched.** Devin duplicates the system/user prefix
  as the conversation grows ("MessageChain tree duplication"), so the same
  logical generation appears under multiple ``node_id`` values. Every real LLM
  generation carries a stable ``metadata.request_id``; we dedupe on that. A
  ``node_id`` watermark alone would re-emit copies.
* **Tokens/model/timing are live** on each assistant node's
  ``metadata.metrics`` — no need to wait for session end.

Everything here is defensive: malformed / missing fields fall back to safe
defaults and DB errors surface as an empty result, never an exception, so a
hook can never crash Devin.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class ToolCall:
    """A tool call issued within an assistant generation."""

    tool_call_id: str
    name: str
    arguments: dict
    result: str = ""  # best-effort; often unavailable live


@dataclass
class LlmStep:
    """One real LLM generation, keyed by ``request_id`` (deduped across copies)."""

    request_id: str
    content: str  # assistant visible text ("" if none)
    thinking: str  # reasoning ("" if none)
    model_name: str
    prompt_tokens: int  # inclusive input total (0 if absent)
    completion_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    tool_calls: list[ToolCall] = field(default_factory=list)
    node_id: int = 0  # min node_id of the deduped copies (for ordering)
    start_ms: int = 0
    end_ms: int = 0


def _as_str(value: object) -> str:
    return value if isinstance(value, str) else ""


def _as_dict(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _as_list(value: object) -> list:
    return value if isinstance(value, list) else []


def _as_int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    return 0


def _iso_to_ms(ts: object) -> int:
    """Convert an ISO8601 timestamp to epoch milliseconds, 0 on failure.

    Accepts any value (None / wrong types coerce to "" and yield 0).
    """
    ts = _as_str(ts)
    if not ts:
        return 0
    # Python's fromisoformat rejects a trailing 'Z' before 3.11; normalize it.
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    try:
        return int(datetime.fromisoformat(ts).timestamp() * 1000)
    except (ValueError, TypeError, OverflowError, OSError):
        return 0


def connect_readonly(db_path, retries: int = 3, delay: float = 0.1):
    """Open ``db_path`` read-only, honoring the WAL (so the newest, not-yet-
    checkpointed rows are visible).

    We deliberately do NOT use ``immutable=1``: it tells SQLite the file cannot
    change and to ignore the ``-wal`` sidecar, which would hide the very rows a
    just-fired ``Stop`` cares about. ``mode=ro`` respects the WAL. Retries a few
    times on transient lock errors. Returns ``None`` on failure.
    """
    uri = f"file:{db_path}?mode=ro"
    last: Exception | None = None
    for attempt in range(max(retries, 1)):
        try:
            return sqlite3.connect(uri, uri=True, timeout=1.0)
        except sqlite3.Error as exc:  # pragma: no cover - timing dependent
            last = exc
            if attempt + 1 < retries:
                time.sleep(delay)
    if last is not None:
        raise last
    return None


def resolve_session_id(con: sqlite3.Connection, project_dir: str) -> str | None:
    """Most recent session id whose ``working_directory`` matches ``project_dir``."""
    if not project_dir:
        return None
    try:
        row = con.execute(
            "SELECT id FROM sessions WHERE working_directory = ? ORDER BY last_activity_at DESC LIMIT 1",
            (project_dir,),
        ).fetchone()
    except sqlite3.Error:
        return None
    if row and row[0]:
        return str(row[0])
    return None


def read_session_meta(con: sqlite3.Connection, session_id: str) -> dict:
    """Return ``{"backend": ..., "model": ...}`` for the session (empty on error)."""
    if not session_id:
        return {}
    try:
        row = con.execute(
            "SELECT backend_type, model FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
    except sqlite3.Error:
        return {}
    if not row:
        return {}
    return {"backend": _as_str(row[0]), "model": _as_str(row[1])}


def _build_tool_calls(msg: dict) -> list[ToolCall]:
    calls: list[ToolCall] = []
    for raw in _as_list(msg.get("tool_calls")):
        raw = _as_dict(raw)
        calls.append(
            ToolCall(
                tool_call_id=_as_str(raw.get("id")),
                name=_as_str(raw.get("name")),
                arguments=_as_dict(raw.get("arguments")),
            )
        )
    return calls


def read_steps(con: sqlite3.Connection, session_id: str) -> list[LlmStep]:
    """Return the session's LLM generations, deduped by ``request_id`` and
    ordered by first appearance (``node_id``).

    Only assistant nodes with a ``metadata.metrics`` block are real generations;
    duplicated copies share a ``request_id`` and collapse to one step (we keep
    the copy with the lowest ``node_id`` for stable ordering, preferring one that
    actually carries content).
    """
    if not session_id:
        return []
    try:
        rows = con.execute(
            "SELECT node_id, chat_message FROM message_nodes WHERE session_id = ? ORDER BY node_id",
            (session_id,),
        ).fetchall()
    except sqlite3.Error:
        return []

    by_request: dict[str, LlmStep] = {}
    for node_id, chat_message in rows:
        try:
            msg = json.loads(chat_message)
        except (TypeError, ValueError):
            continue
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        meta = _as_dict(msg.get("metadata"))
        metrics = _as_dict(meta.get("metrics"))
        if not metrics:
            continue  # not a real generation (e.g. a copied prefix stub)
        request_id = _as_str(meta.get("request_id")) or f"node-{_as_int(node_id)}"

        step = LlmStep(
            request_id=request_id,
            content=_as_str(msg.get("content")),
            thinking=_as_str(msg.get("thinking")),
            model_name=_as_str(meta.get("generation_model")),
            prompt_tokens=_as_int(metrics.get("input_tokens")),
            completion_tokens=_as_int(metrics.get("output_tokens")),
            cache_read_tokens=_as_int(metrics.get("cache_read_tokens")),
            cache_write_tokens=_as_int(metrics.get("cache_creation_tokens")),
            tool_calls=_build_tool_calls(msg),
            node_id=_as_int(node_id),
            start_ms=_iso_to_ms(meta.get("started_generation_at")),
            end_ms=_iso_to_ms(meta.get("created_at")),
        )

        existing = by_request.get(request_id)
        # Keep the first (lowest node_id) copy, but prefer a copy that has content.
        if existing is None:
            by_request[request_id] = step
        elif not existing.content and step.content:
            step.node_id = existing.node_id  # preserve original ordering anchor
            by_request[request_id] = step

    return sorted(by_request.values(), key=lambda s: s.node_id)


def latest_user_prompt(con: sqlite3.Connection, session_id: str) -> str:
    """Best-effort: the most recent real user prompt for the session.

    Prefers ``prompt_history`` (clean, one row per submitted prompt); falls back
    to the newest ``role == "user"`` message node.
    """
    if not session_id:
        return ""
    try:
        row = con.execute(
            "SELECT content FROM prompt_history WHERE session_id = ? AND is_shell = 0 ORDER BY timestamp DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        if row and row[0]:
            return _as_str(row[0])
    except sqlite3.Error:
        pass
    try:
        rows = con.execute(
            "SELECT chat_message FROM message_nodes WHERE session_id = ? ORDER BY node_id DESC",
            (session_id,),
        ).fetchall()
    except sqlite3.Error:
        return ""
    for (chat_message,) in rows:
        try:
            msg = json.loads(chat_message)
        except (TypeError, ValueError):
            continue
        if isinstance(msg, dict) and msg.get("role") == "user" and msg.get("content"):
            return _as_str(msg.get("content"))
    return ""
