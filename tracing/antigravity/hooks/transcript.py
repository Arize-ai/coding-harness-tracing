"""Parser for Antigravity transcript JSONL.

Antigravity emits a transcript file (one JSON object per line) that captures the
ground-truth conversation: user inputs, planner responses (model turns), tool
calls, and tool results. The Stop / PreInvocation hooks only signal *when* to
read the transcript — the transcript itself is the source of truth.

`parse_transcript` is a pure function: no logging, no span building, no imports
from `core`. It splits the transcript into turns (one per ``USER_INPUT``) and
returns a list of structured dicts.
"""

from __future__ import annotations

import datetime
import json
import re
from pathlib import Path
from typing import Any

_USER_REQUEST_RE = re.compile(r"<USER_REQUEST>(.*?)</USER_REQUEST>", re.DOTALL)
_ADDITIONAL_METADATA_RE = re.compile(r"<ADDITIONAL_METADATA>.*?</ADDITIONAL_METADATA>", re.DOTALL)
_USER_SETTINGS_CHANGE_RE = re.compile(r"<USER_SETTINGS_CHANGE>.*?</USER_SETTINGS_CHANGE>", re.DOTALL)
_MODEL_SELECTION_RE = re.compile(
    r"changed setting `Model Selection` from .*? to (.+?)\.(?=\s+[A-Z]|\s*$)",
    re.DOTALL,
)
_CREATED_AT_RE = re.compile(r"^Created At:\s*(\S+)", re.MULTILINE)
_COMPLETED_AT_RE = re.compile(r"^Completed At:\s*(\S+)", re.MULTILINE)

_NON_TOOL_TYPES = {"USER_INPUT", "PLANNER_RESPONSE", "CONVERSATION_HISTORY"}


def _iso_to_ms(value: str) -> int:
    """Parse an ISO-8601 timestamp (e.g. ``2026-06-09T16:00:11Z``) to epoch ms.

    Returns ``0`` on any parse failure.
    """
    if not value:
        return 0
    try:
        normalized = value.strip()
        if normalized.endswith("Z"):
            normalized = normalized[:-1] + "+00:00"
        dt = datetime.datetime.fromisoformat(normalized)
        return int(dt.timestamp() * 1000)
    except (ValueError, TypeError):
        return 0


def _extract_user_input(content: str) -> str:
    """Pull the user prompt out of a ``USER_INPUT`` record's content."""
    match = _USER_REQUEST_RE.search(content)
    if match:
        return match.group(1).strip()
    stripped = _ADDITIONAL_METADATA_RE.sub("", content)
    stripped = _USER_SETTINGS_CHANGE_RE.sub("", stripped)
    return stripped.strip()


def _extract_model_name(content: str) -> str:
    """Best-effort extraction of the model name from the user-settings block."""
    match = _MODEL_SELECTION_RE.search(content)
    if match:
        return match.group(1).strip()
    return ""


def _build_turn(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Build a single Turn dict from an in-order list of records.

    The first record is expected to be the ``USER_INPUT`` record. Any
    ``CONVERSATION_HISTORY`` records have already been filtered out by the
    caller.
    """
    user_record = records[0]
    user_content = user_record.get("content", "") or ""

    turn: dict[str, Any] = {
        "user_input": _extract_user_input(user_content),
        "final_response": "",
        "model_name": _extract_model_name(user_content),
        "max_step_index": 0,
        "start_ms": 0,
        "end_ms": 0,
        "llm_steps": [],
        "tool_steps": [],
    }

    step_indices = [r.get("step_index", 0) for r in records if "step_index" in r]
    if step_indices:
        turn["max_step_index"] = max(step_indices)

    timestamps = [_iso_to_ms(r.get("created_at", "") or "") for r in records]
    timestamps = [t for t in timestamps if t > 0]
    if timestamps:
        turn["start_ms"] = timestamps[0]
        turn["end_ms"] = timestamps[-1]

    tool_calls: list[tuple[str, dict[str, Any]]] = []
    tool_results: list[dict[str, Any]] = []
    last_planner_content = ""

    for idx, rec in enumerate(records):
        rec_type = rec.get("type", "")
        if rec_type == "PLANNER_RESPONSE":
            start_ms = _iso_to_ms(rec.get("created_at", "") or "")
            if idx + 1 < len(records):
                end_ms = _iso_to_ms(records[idx + 1].get("created_at", "") or "")
                if end_ms == 0:
                    end_ms = start_ms
            else:
                end_ms = start_ms
            turn["llm_steps"].append(
                {
                    "content": rec.get("content", "") or "",
                    "thinking": rec.get("thinking", "") or "",
                    "step_index": rec.get("step_index", 0),
                    "start_ms": start_ms,
                    "end_ms": end_ms,
                }
            )
            content = rec.get("content", "") or ""
            if content:
                last_planner_content = content
            for call in rec.get("tool_calls", []) or []:
                if not isinstance(call, dict):
                    continue
                name = call.get("name", "") or ""
                args = call.get("args", {}) or {}
                if not isinstance(args, dict):
                    args = {}
                tool_calls.append((name, args))
        elif rec_type == "USER_INPUT":
            continue
        elif rec_type and rec_type not in _NON_TOOL_TYPES:
            tool_results.append(rec)

    turn["final_response"] = last_planner_content

    for (name, args), result in zip(tool_calls, tool_results):
        result_content = result.get("content", "") or ""
        created_match = _CREATED_AT_RE.search(result_content)
        completed_match = _COMPLETED_AT_RE.search(result_content)
        fallback_ms = _iso_to_ms(result.get("created_at", "") or "")
        start_ms = _iso_to_ms(created_match.group(1)) if created_match else 0
        end_ms = _iso_to_ms(completed_match.group(1)) if completed_match else 0
        if start_ms == 0:
            start_ms = fallback_ms
        if end_ms == 0:
            end_ms = fallback_ms
        turn["tool_steps"].append(
            {
                "name": name,
                "args": args,
                "output": result_content,
                "step_index": result.get("step_index", 0),
                "start_ms": start_ms,
                "end_ms": end_ms,
            }
        )

    return turn


def parse_transcript(path: str | Path) -> list[dict[str, Any]]:
    """Parse an Antigravity transcript JSONL into a list of Turn dicts.

    Prefers a sibling ``transcript_full.jsonl`` if present (it carries the full
    untruncated record content). Returns ``[]`` on any missing/unreadable file.
    """
    p = Path(path).expanduser()
    full = p.with_name("transcript_full.jsonl")
    if full.is_file():
        target = full
    elif p.is_file():
        target = p
    else:
        return []

    records: list[dict[str, Any]] = []
    try:
        with target.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(rec, dict):
                    continue
                if rec.get("type") == "CONVERSATION_HISTORY":
                    continue
                records.append(rec)
    except OSError:
        return []

    turns: list[dict[str, Any]] = []
    current: list[dict[str, Any]] = []
    for rec in records:
        if rec.get("type") == "USER_INPUT":
            if current:
                turns.append(_build_turn(current))
            current = [rec]
        else:
            if current:
                current.append(rec)
    if current:
        turns.append(_build_turn(current))

    return turns
