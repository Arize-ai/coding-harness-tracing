"""On-disk append-only buffer of tool-span rows keyed by thread_id."""

from __future__ import annotations

import json
from pathlib import Path

from tracing.codex.hooks.adapter import STATE_DIR


def spans_path(thread_id: str) -> Path:
    """Return the JSONL path for a given thread_id. Caller is responsible for ensuring STATE_DIR exists."""
    return STATE_DIR / f"spans_{thread_id}.jsonl"


def _append_row(thread_id: str, row: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    line = json.dumps(row, separators=(",", ":")) + "\n"
    path = spans_path(thread_id)
    with open(path, "a", encoding="utf-8") as f:
        f.write(line)


def append_tool_start(thread_id: str, *, call_id: str, tool: str, args: str, ts_ms: int) -> None:
    _append_row(
        thread_id,
        {
            "kind": "tool_start",
            "call_id": call_id,
            "tool": tool,
            "args": args,
            "ts_ms": ts_ms,
        },
    )


def append_tool_end(thread_id: str, *, call_id: str, output: str, ts_ms: int) -> None:
    _append_row(
        thread_id,
        {
            "kind": "tool_end",
            "call_id": call_id,
            "output": output,
            "ts_ms": ts_ms,
        },
    )


def append_permission(thread_id: str, *, call_id: str, decision: str, ts_ms: int) -> None:
    _append_row(
        thread_id,
        {
            "kind": "permission",
            "call_id": call_id,
            "decision": decision,
            "ts_ms": ts_ms,
        },
    )


def read_all(thread_id: str) -> list[dict]:
    """Return all rows in append order. Returns [] if the file doesn't exist."""
    from core.common import log

    path = spans_path(thread_id)
    if not path.exists():
        return []

    rows: list[dict] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                obj = json.loads(stripped)
            except json.JSONDecodeError:
                log(f"skipping malformed spans row: {stripped!r}")
                continue
            rows.append(obj)
    return rows


def delete(thread_id: str) -> None:
    """Remove the JSONL file. No-op if it doesn't exist."""
    spans_path(thread_id).unlink(missing_ok=True)


def join_by_call_id(rows: list[dict]) -> list[dict]:
    """Group tool rows into per-call-id dicts ready for span construction.

    Returns a list of dicts, one per distinct call_id (in order of first appearance), with keys:
        call_id: str
        tool: str | None             # from tool_start
        args: str | None             # from tool_start
        output: str | None           # from tool_end
        decision: str | None         # from permission (most recent if multiple)
        start_ts_ms: int | None      # from tool_start
        end_ts_ms: int | None        # from tool_end

    Some Codex CLI hook payloads do not include a stable tool call id. In that
    case, pair rows by append order: a no-id end row attaches to the oldest
    unmatched no-id start row, and a no-id permission row attaches to the most
    recent unmatched no-id start row.
    """
    by_id: dict[str, dict] = {}
    order: list[str] = []
    pending_no_id: list[str] = []
    synthetic_count = 0

    def new_entry(key: str, call_id: str = "") -> dict:
        if key not in by_id:
            by_id[key] = {
                "call_id": call_id,
                "tool": None,
                "args": None,
                "output": None,
                "decision": None,
                "start_ts_ms": None,
                "end_ts_ms": None,
            }
            order.append(key)
        return by_id[key]

    def next_synthetic_key() -> str:
        nonlocal synthetic_count
        synthetic_count += 1
        return f"__codex_no_call_id_{synthetic_count}"

    def pending_without_end() -> list[str]:
        return [key for key in pending_no_id if by_id.get(key, {}).get("end_ts_ms") is None]

    for row in rows:
        raw_call_id = row.get("call_id") or ""
        kind = row.get("kind")

        if raw_call_id:
            key = raw_call_id
            entry = new_entry(key, raw_call_id)
        elif kind == "tool_start":
            key = next_synthetic_key()
            entry = new_entry(key)
            pending_no_id.append(key)
        elif kind == "tool_end":
            open_keys = pending_without_end()
            key = open_keys[0] if open_keys else next_synthetic_key()
            entry = new_entry(key)
        elif kind == "permission":
            open_keys = pending_without_end()
            key = open_keys[-1] if open_keys else next_synthetic_key()
            entry = new_entry(key)
        else:
            continue

        if kind == "tool_start":
            entry["tool"] = row.get("tool")
            entry["args"] = row.get("args")
            entry["start_ts_ms"] = row.get("ts_ms")
        elif kind == "tool_end":
            entry["output"] = row.get("output")
            entry["end_ts_ms"] = row.get("ts_ms")
        elif kind == "permission":
            entry["decision"] = row.get("decision")

    return [by_id[cid] for cid in order]
