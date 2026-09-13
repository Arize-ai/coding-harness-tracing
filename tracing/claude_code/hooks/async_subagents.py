"""Export background (async) subagents whose SubagentStop arrives after the turn's Stop.

Claude Code 2.1.2xx launches every ``Agent`` tool call asynchronously: the tool
returns immediately, the main turn's ``Stop`` fires, ``SubagentStop`` arrives
later, and a synthetic ``<task-notification>`` user turn follows. The stock merge
path (``_merge_pending_subagents``) can only attach a subagent to the turn being
exported right now, so async subagents were silently dropped.

This module records, when a turn is exported, the trace/span ids of ``Agent`` tool
calls whose subagent has not stopped yet. When ``SubagentStop`` finally arrives it
renders the subagent transcript as an AGENT subtree parented to that earlier tool
span, inside the original turn's trace.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from core.common import error, generate_span_id, log, send_span
from core.event_model import AgentEvent, EventGraph, EventStatus, ToolEvent

from .adapter import SCOPE_NAME, SERVICE_NAME
from .span_renderer import render_event_graph
from .transcript import parse_claude_transcript

STATE_KEY = "async_agent_parents"
TASK_NOTIFICATION_PREFIX = "<task-notification>"
_TASK_ID_PATTERN = re.compile(r"<task-id>\s*([^<\s]+)\s*</task-id>")


def agent_id_from_tool(event: ToolEvent) -> str:
    """Return the subagent id an ``Agent`` tool result points at, or ``""``."""
    if not isinstance(event.output, dict):
        return ""
    result = event.output.get("toolUseResult")
    if not isinstance(result, dict):
        return ""
    agent_id = result.get("agentId")
    return agent_id if isinstance(agent_id, str) else ""


def decode_parents(raw: object) -> dict[str, dict]:
    """Decode the persisted ``async_agent_parents`` map; malformed input yields ``{}``."""
    if not isinstance(raw, str) or not raw:
        return {}
    try:
        decoded = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return {}
    if not isinstance(decoded, dict):
        return {}
    return {str(agent_id): parent for agent_id, parent in decoded.items() if isinstance(parent, dict)}


def collect_parents(
    events: list,
    *,
    trace_id: str,
    turn_id: str,
    span_id_overrides: Mapping[str, str],
    exclude: set[str],
) -> dict[str, dict]:
    """Describe every ``Agent`` tool event whose subagent is still running."""
    parents: dict[str, dict] = {}
    for event in events:
        if not isinstance(event, ToolEvent):
            continue
        agent_id = agent_id_from_tool(event)
        span_id = span_id_overrides.get(event.event_id)
        if not agent_id or agent_id in exclude or not span_id:
            continue
        prompt = event.input.get("prompt") if isinstance(event.input, dict) else None
        parents[agent_id] = {
            "trace_id": trace_id,
            "span_id": span_id,
            "turn_id": turn_id,
            "prompt": prompt if isinstance(prompt, str) else "",
        }
    return parents


def record_parents(state, parents: Mapping[str, dict]) -> bool:
    """Persist *parents* into the session state (merged with earlier entries)."""
    if not parents or state.state_file is None:
        return False
    try:
        with state._lock():
            data = state._read_safe()
            merged = {**decode_parents(data.get(STATE_KEY, "")), **parents}
            state._write({**data, STATE_KEY: json.dumps(merged, sort_keys=True)})
    except Exception as exc:  # noqa: BLE001 - hooks must never raise
        error(f"Failed to record async subagent parents: {exc}")
        return False
    return True


def pop_parent(state, agent_id: str) -> Optional[dict]:
    """Atomically remove and return the recorded parent for *agent_id*."""
    if not agent_id or state.state_file is None:
        return None
    try:
        with state._lock():
            data = state._read_safe()
            parents = decode_parents(data.get(STATE_KEY, ""))
            parent = parents.get(agent_id)
            if parent is None:
                return None
            remaining = {key: value for key, value in parents.items() if key != agent_id}
            updated = {key: value for key, value in data.items() if key != STATE_KEY}
            if remaining:
                updated[STATE_KEY] = json.dumps(remaining, sort_keys=True)
            state._write(updated)
            return parent
    except Exception as exc:  # noqa: BLE001
        error(f"Failed to read async subagent parent for {agent_id}: {exc}")
        return None


def task_notification_attrs(prompt: object) -> dict[str, str]:
    """Attributes for the synthetic turn Claude Code injects when a background agent finishes."""
    text = prompt.lstrip() if isinstance(prompt, str) else ""
    if not text.startswith(TASK_NOTIFICATION_PREFIX):
        return {}
    match = _TASK_ID_PATTERN.search(text)
    attrs = {"turn.trigger": "task-notification"}
    return {**attrs, "subagent.id": match.group(1)} if match else attrs


def _as_int(value: object) -> Optional[int]:
    text = str(value) if value is not None else ""
    return int(text) if text.isdigit() else None


def build_agent_event(state, input_json: dict, parent: Mapping[str, Any], *, ended_at_ms: int, session_id: str) -> AgentEvent:
    """Root event for the async subagent subtree."""
    agent_id = str(input_json.get("agent_id") or "")
    agent_type = str(input_json.get("agent_type") or "unknown")
    prompt = parent.get("prompt") or state.get(f"subagent_{agent_id}_prompt") or None
    return AgentEvent(
        event_id=f"agent:{agent_id}",
        parent_event_id=None,
        session_id=session_id,
        turn_id=str(parent.get("turn_id") or ""),
        sequence=0,
        started_at_ms=_as_int(state.get(f"subagent_{agent_id}_start_time")),
        ended_at_ms=ended_at_ms,
        status=EventStatus.COMPLETED,
        input=prompt,
        output=input_json.get("last_assistant_message") or "",
        agent_id=agent_id,
        source_id=agent_type,
    )


def reparent_root(payload: dict, root_span_id: str, parent_span_id: str) -> dict:
    """Return a copy of *payload* whose span *root_span_id* is parented to *parent_span_id*."""
    resource_spans = []
    for resource in payload.get("resourceSpans", []):
        scope_spans = []
        for scope in resource.get("scopeSpans", []):
            spans = [
                {**span, "parentSpanId": parent_span_id} if span.get("spanId") == root_span_id else span
                for span in scope.get("spans", [])
            ]
            scope_spans.append({**scope, "spans": spans})
        resource_spans.append({**resource, "scopeSpans": scope_spans})
    return {**payload, "resourceSpans": resource_spans}


def export_async_subagent(
    state,
    input_json: dict,
    parent: Mapping[str, Any],
    *,
    ended_at_ms: int,
    session_id: str,
    send: Callable[[dict], bool] = send_span,
) -> bool:
    """Render the finished subagent under its recorded ``Agent`` tool span and send it via *send*."""
    agent_event = build_agent_event(state, input_json, parent, ended_at_ms=ended_at_ms, session_id=session_id)
    transcript = input_json.get("agent_transcript_path")
    path = Path(transcript) if isinstance(transcript, str) and transcript else None
    graph = parse_claude_transcript(path, agent_event) if path is not None and path.is_file() else EventGraph([agent_event])
    if path is None or not path.is_file():
        log(f"async subagent {agent_event.agent_id}: no transcript, exporting AGENT span only")
    root_span_id = generate_span_id()
    payload = render_event_graph(
        graph,
        trace_id=str(parent.get("trace_id") or ""),
        service_name=SERVICE_NAME,
        scope_name=SCOPE_NAME,
        span_id_overrides={agent_event.event_id: root_span_id},
        extra_attributes={agent_event.event_id: {"subagent.async": "true"}},
    )
    sent = send(reparent_root(payload, root_span_id, str(parent.get("span_id") or ""))) is not False
    if not sent:
        error(f"async subagent {agent_event.agent_id}: export failed")
    state.delete(f"subagent_{agent_event.agent_id}_start_time")
    state.delete(f"subagent_{agent_event.agent_id}_prompt")
    return sent


__all__ = [
    "STATE_KEY",
    "agent_id_from_tool",
    "collect_parents",
    "decode_parents",
    "export_async_subagent",
    "pop_parent",
    "record_parents",
    "reparent_root",
    "task_notification_attrs",
]
