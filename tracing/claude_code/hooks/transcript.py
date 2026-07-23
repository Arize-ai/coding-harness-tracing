"""Parse Claude Code transcript JSONL into harness-neutral typed events."""

from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any

from core.event_model import (
    AgentEvent,
    BaseEvent,
    EventGraph,
    EventStatus,
    GraphDiagnostic,
    ModelCallEvent,
    ToolEvent,
    Usage,
)


def parse_claude_transcript(
    transcript: Path,
    root_event: BaseEvent,
    *,
    start_line: int = 0,
) -> EventGraph:
    """Return a typed event graph for one main-agent or subagent transcript.

    ``start_line`` is a zero-based physical JSONL line offset, matching the value
    recorded by Claude's ``UserPromptSubmit`` hook. Unknown records and malformed
    lines are ignored with diagnostics instead of aborting the whole turn.
    """

    graph = EventGraph([root_event])
    parser_diagnostics: list[GraphDiagnostic] = []
    tools_by_call_id: dict[str, ToolEvent] = {}
    models_by_message_id: dict[str, ModelCallEvent] = {}
    agent_id = root_event.agent_id if isinstance(root_event, AgentEvent) else None
    sequence = root_event.sequence + 1

    try:
        lines = transcript.read_text().splitlines()
    except (OSError, UnicodeError) as exc:
        graph.diagnostics = [
            GraphDiagnostic(
                code="transcript_read_error",
                message=str(exc),
                event_id=root_event.event_id,
            )
        ]
        return graph

    for line_index, raw_line in enumerate(lines):
        if line_index < max(0, start_line) or not raw_line.strip():
            continue
        try:
            entry = json.loads(raw_line)
        except (json.JSONDecodeError, TypeError) as exc:
            parser_diagnostics.append(
                GraphDiagnostic(
                    code="malformed_json",
                    message=f"line {line_index + 1}: {exc}",
                    event_id=root_event.event_id,
                    severity="warning",
                )
            )
            continue
        if not isinstance(entry, dict):
            continue

        message = entry.get("message")
        if not isinstance(message, dict):
            continue
        role = message.get("role")

        if role == "assistant":
            event_id = _string(entry.get("uuid"))
            if not event_id:
                event_id = f"assistant-line-{line_index + 1}"
                parser_diagnostics.append(
                    GraphDiagnostic(
                        code="missing_source_id",
                        message=f"assistant record on line {line_index + 1} has no uuid",
                        event_id=event_id,
                        severity="warning",
                    )
                )

            timestamp_ms = _timestamp_ms(entry.get("timestamp"))
            if "timestamp" in entry and timestamp_ms is None:
                parser_diagnostics.append(
                    GraphDiagnostic(
                        code="invalid_timestamp",
                        message=f"assistant record on line {line_index + 1} has an invalid timestamp",
                        event_id=event_id,
                        severity="warning",
                    )
                )

            content = message.get("content")
            message_id = _string(message.get("id"))
            # Claude Code v2 writes one assistant response (one message.id) as
            # several records -- thinking / text / each tool_use on its own line,
            # each with a distinct record uuid. Fold those back into a single
            # ModelCallEvent keyed by message.id so one response is one LLM span,
            # not one per block. Records with no message.id can't be grouped and
            # stay one-event-per-record (also preserves assistant-line-* ids).
            model_event = models_by_message_id.get(message_id) if message_id else None
            if model_event is None:
                model_event = ModelCallEvent(
                    event_id=event_id,
                    parent_event_id=root_event.event_id,
                    session_id=_string(entry.get("sessionId")) or root_event.session_id,
                    turn_id=root_event.turn_id,
                    sequence=sequence,
                    started_at_ms=timestamp_ms,
                    ended_at_ms=timestamp_ms,
                    status=EventStatus.COMPLETED,
                    input=None,
                    output=_assistant_text(content),
                    agent_id=agent_id,
                    source_id=message_id or event_id,
                    model=_string(message.get("model")) or None,
                    usage=_usage(message.get("usage")),
                )
                graph.events.append(model_event)
                sequence += 1
                if message_id:
                    models_by_message_id[message_id] = model_event
            else:
                # Merge this continuation record into the existing call: append
                # any text, extend the time window, keep the fullest usage/model.
                extra_text = _assistant_text(content)
                if extra_text:
                    model_event.output = f"{model_event.output}\n{extra_text}" if model_event.output else extra_text
                if timestamp_ms is not None:
                    if model_event.started_at_ms is None:
                        model_event.started_at_ms = timestamp_ms
                    model_event.ended_at_ms = timestamp_ms
                if not model_event.model:
                    model_event.model = _string(message.get("model")) or None
                record_usage = _usage(message.get("usage"))
                existing_total = model_event.usage.total_tokens if model_event.usage else -1
                if record_usage.total_tokens > existing_total:
                    model_event.usage = record_usage

            for block in _content_blocks(content):
                if block.get("type") != "tool_use":
                    continue
                call_id = _string(block.get("id"))
                if not call_id:
                    parser_diagnostics.append(
                        GraphDiagnostic(
                            code="missing_tool_call_id",
                            message=f"tool_use on line {line_index + 1} has no id",
                            event_id=event_id,
                            severity="warning",
                        )
                    )
                    continue
                duplicate = call_id in tools_by_call_id
                tool_event_id = f"tool:{call_id}:duplicate:{sequence}" if duplicate else f"tool:{call_id}"
                tool = ToolEvent(
                    event_id=tool_event_id,
                    parent_event_id=model_event.event_id,
                    session_id=model_event.session_id,
                    turn_id=root_event.turn_id,
                    sequence=sequence,
                    started_at_ms=timestamp_ms,
                    ended_at_ms=None,
                    status=EventStatus.PENDING,
                    input=block.get("input"),
                    output=None,
                    agent_id=agent_id,
                    source_id=call_id,
                    tool_call_id=call_id,
                    tool_name=_string(block.get("name")) or None,
                )
                graph.events.append(tool)
                tools_by_call_id.setdefault(call_id, tool)
                sequence += 1

        elif role == "user":
            result_timestamp_ms = _timestamp_ms(entry.get("timestamp"))
            for block in _content_blocks(message.get("content")):
                if block.get("type") != "tool_result":
                    continue
                call_id = _string(block.get("tool_use_id"))
                if not call_id:
                    continue
                correlated_tool = tools_by_call_id.get(call_id)
                if correlated_tool is None:
                    correlated_tool = ToolEvent(
                        event_id=f"tool:{call_id}",
                        parent_event_id=root_event.event_id,
                        session_id=_string(entry.get("sessionId")) or root_event.session_id,
                        turn_id=root_event.turn_id,
                        sequence=sequence,
                        started_at_ms=None,
                        ended_at_ms=result_timestamp_ms,
                        status=EventStatus.UNKNOWN,
                        input=None,
                        output=None,
                        agent_id=agent_id,
                        source_id=call_id,
                        tool_call_id=call_id,
                        tool_name=None,
                    )
                    graph.events.append(correlated_tool)
                    tools_by_call_id[call_id] = correlated_tool
                    sequence += 1
                    parser_diagnostics.append(
                        GraphDiagnostic(
                            code="orphan_tool_result",
                            message=f"tool result {call_id!r} has no matching tool_use",
                            event_id=correlated_tool.event_id,
                            severity="warning",
                        )
                    )

                failed = bool(block.get("is_error")) or _tool_result_failed(entry.get("toolUseResult"))
                content = block.get("content")
                correlated_tool.output = _tool_output(entry, content)
                correlated_tool.ended_at_ms = result_timestamp_ms
                correlated_tool.status = EventStatus.FAILED if failed else EventStatus.COMPLETED
                if "timestamp" in entry and result_timestamp_ms is None:
                    parser_diagnostics.append(
                        GraphDiagnostic(
                            code="invalid_timestamp",
                            message=f"tool result on line {line_index + 1} has an invalid timestamp",
                            event_id=correlated_tool.event_id,
                            severity="warning",
                        )
                    )
                if failed:
                    correlated_tool.error = _content_text(content) or "Tool call failed"

    validation_diagnostics = graph.validate()
    graph.diagnostics = parser_diagnostics + validation_diagnostics
    return graph


def _content_blocks(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, list):
        return [item for item in content if isinstance(item, dict)]
    return []


def _assistant_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    return "\n".join(
        _string(block.get("text"))
        for block in _content_blocks(content)
        if block.get("type") == "text" and _string(block.get("text"))
    )


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            _string(item.get("text")) if isinstance(item, dict) else _string(item)
            for item in content
            if (_string(item.get("text")) if isinstance(item, dict) else _string(item))
        )
    if content is None:
        return ""
    return _string(content)


def _tool_output(entry: dict[str, Any], content: Any) -> Any:
    tool_use_result = entry.get("toolUseResult")
    if isinstance(tool_use_result, dict) and tool_use_result.get("agentId"):
        return {"content": content, "toolUseResult": tool_use_result}
    return content


def _tool_result_failed(result: Any) -> bool:
    if not isinstance(result, dict):
        return False
    return result.get("status") in {"failed", "error"} or result.get("is_error") is True


def _usage(raw: Any) -> Usage:
    data = raw if isinstance(raw, dict) else {}
    return Usage(
        input_tokens=_nonnegative_int(data.get("input_tokens")),
        output_tokens=_nonnegative_int(data.get("output_tokens")),
        cache_read_tokens=_nonnegative_int(data.get("cache_read_input_tokens")),
        cache_write_tokens=_nonnegative_int(data.get("cache_creation_input_tokens")),
        reported_total_tokens=_optional_nonnegative_int(data.get("total_tokens")),
    )


def _nonnegative_int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _optional_nonnegative_int(value: Any) -> int | None:
    if value is None:
        return None
    return _nonnegative_int(value)


def _timestamp_ms(value: Any) -> int | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value) if math.isfinite(value) and value >= 0 else None
    if not isinstance(value, str) or not value:
        return None
    try:
        timestamp = int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)
        return timestamp if timestamp >= 0 else None
    except (OSError, OverflowError, ValueError):
        return None


def _string(value: Any) -> str:
    return value if isinstance(value, str) else ""


__all__ = ["parse_claude_transcript"]
