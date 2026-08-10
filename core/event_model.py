"""Harness-neutral typed events for coding-agent traces.

The model deliberately contains no parser, renderer, or persistence concerns.  It is
small enough for hook adapters to populate and safe to serialize into future hook
state.
"""

import math
from dataclasses import dataclass, field, fields, is_dataclass
from enum import Enum
from typing import Any, ClassVar, Dict, Iterable, List, Mapping, Optional


class EventStatus(str, Enum):
    """Lifecycle status shared by every event kind."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


@dataclass
class Usage:
    """Model token usage using cache-neutral totals by default.

    Provider input token counts generally already describe their own cache
    semantics, so cache reads/writes are exposed separately and are not added to
    ``total_tokens``.  ``reported_total_tokens`` preserves a provider total when
    that provider explicitly supplies one.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reported_total_tokens: Optional[int] = None

    @property
    def total_tokens(self) -> int:
        if self.reported_total_tokens is not None:
            return self.reported_total_tokens
        return self.input_tokens + self.output_tokens

    @property
    def cache_read_input_tokens(self) -> int:
        """Claude-compatible name for cache read tokens."""

        return self.cache_read_tokens

    @property
    def cache_creation_input_tokens(self) -> int:
        """Claude-compatible name for cache write tokens."""

        return self.cache_write_tokens

    def to_dict(self) -> Dict[str, Any]:
        payload = _json_safe_dataclass(self)
        payload["total_tokens"] = self.total_tokens
        return payload


@dataclass
class BaseEvent:
    """Fields shared by all trace event kinds."""

    event_id: str
    session_id: str
    turn_id: str
    sequence: int
    started_at_ms: Optional[int]
    ended_at_ms: Optional[int]
    status: EventStatus
    parent_event_id: Optional[str] = None
    error: Optional[str] = None
    input: Any = None
    output: Any = None

    event_type: ClassVar[str] = "event"

    def to_dict(self) -> Dict[str, Any]:
        payload = _json_safe_dataclass(self)
        payload["event_type"] = self.event_type
        return payload


@dataclass
class TurnEvent(BaseEvent):
    """A user-visible coding-agent turn."""

    event_type: ClassVar[str] = "turn"


@dataclass
class AgentEvent(BaseEvent):
    """An agent or subagent invocation within a turn."""

    agent_id: Optional[str] = None
    source_id: Optional[str] = None
    model: Optional[str] = None
    usage: Optional[Usage] = None

    event_type: ClassVar[str] = "agent"


@dataclass
class ModelCallEvent(BaseEvent):
    """One request/response exchange with a model."""

    agent_id: Optional[str] = None
    source_id: Optional[str] = None
    model: Optional[str] = None
    usage: Optional[Usage] = None

    event_type: ClassVar[str] = "model_call"


@dataclass
class ToolEvent(BaseEvent):
    """A tool invocation and its result."""

    agent_id: Optional[str] = None
    source_id: Optional[str] = None
    tool_call_id: Optional[str] = None
    tool_name: Optional[str] = None

    event_type: ClassVar[str] = "tool"


@dataclass(frozen=True)
class GraphDiagnostic:
    """A non-destructive validation finding for an event graph."""

    code: str
    message: str
    event_id: Optional[str] = None
    related_event_id: Optional[str] = None
    severity: str = "error"

    def to_dict(self) -> Dict[str, Any]:
        return _json_safe_dataclass(self)


@dataclass
class EventGraph:
    """An ordered collection that retains events even when validation fails."""

    events: List[BaseEvent] = field(default_factory=list)
    diagnostics: List[GraphDiagnostic] = field(default_factory=list, init=False)
    _validation_diagnostics: List[GraphDiagnostic] = field(default_factory=list, init=False, repr=False)

    def __init__(self, events: Iterable[BaseEvent] = ()) -> None:
        self.events = list(events)
        self.diagnostics = []
        self._validation_diagnostics = []

    def validate(self) -> List[GraphDiagnostic]:
        diagnostics: List[GraphDiagnostic] = []
        first_by_id: Dict[str, BaseEvent] = {}

        for event in self.events:
            if event.event_id in first_by_id:
                diagnostics.append(
                    GraphDiagnostic(
                        code="duplicate_event_id",
                        message="event_id is used by more than one event",
                        event_id=event.event_id,
                        related_event_id=first_by_id[event.event_id].event_id,
                    )
                )
            else:
                first_by_id[event.event_id] = event

        known_ids = set(first_by_id)
        parent_by_id = {
            event.event_id: event.parent_event_id
            for event in first_by_id.values()
            if event.parent_event_id in known_ids
        }
        cycle_ids = set()
        for event_id in known_ids:
            path: List[str] = []
            positions: Dict[str, int] = {}
            current: Optional[str] = event_id
            while current in parent_by_id:
                if current in positions:
                    cycle_ids.update(path[positions[current] :])
                    break
                positions[current] = len(path)
                path.append(current)
                current = parent_by_id[current]
        for event_id in sorted(cycle_ids):
            diagnostics.append(
                GraphDiagnostic(
                    code="parent_cycle",
                    message="parent_event_id relationships must not contain cycles",
                    event_id=event_id,
                    related_event_id=parent_by_id.get(event_id),
                )
            )

        first_tool_by_call_id: Dict[str, ToolEvent] = {}
        for event in self.events:
            if event.parent_event_id and event.parent_event_id not in known_ids:
                diagnostics.append(
                    GraphDiagnostic(
                        code="missing_parent",
                        message="parent_event_id does not identify an event in the graph",
                        event_id=event.event_id,
                        related_event_id=event.parent_event_id,
                    )
                )

            if not _valid_timestamp_range(event.started_at_ms, event.ended_at_ms):
                diagnostics.append(
                    GraphDiagnostic(
                        code="invalid_timestamp_range",
                        message="timestamps must be non-negative numbers and end must not precede start",
                        event_id=event.event_id,
                    )
                )

            if isinstance(event, ToolEvent) and event.tool_call_id:
                first_tool = first_tool_by_call_id.get(event.tool_call_id)
                if first_tool is None:
                    first_tool_by_call_id[event.tool_call_id] = event
                elif first_tool is not event:
                    diagnostics.append(
                        GraphDiagnostic(
                            code="duplicate_tool_call_id",
                            message="tool_call_id is assigned to more than one tool event",
                            event_id=event.event_id,
                            related_event_id=first_tool.event_id,
                        )
                    )

        root_events = [event for event in self.events if not event.parent_event_id]
        if len(root_events) > 1:
            first_root = root_events[0]
            for event in root_events[1:]:
                diagnostics.append(
                    GraphDiagnostic(
                        code="multiple_roots",
                        message="graph contains more than one root event",
                        event_id=event.event_id,
                        related_event_id=first_root.event_id,
                    )
                )

        previous_validation_ids = {id(item) for item in self._validation_diagnostics}
        retained = [item for item in self.diagnostics if id(item) not in previous_validation_ids]
        self._validation_diagnostics = diagnostics
        self.diagnostics = retained + diagnostics
        return diagnostics

    def to_dict(self) -> Dict[str, Any]:
        return {
            "events": [event.to_dict() for event in self.events],
            "diagnostics": [diagnostic.to_dict() for diagnostic in self.diagnostics],
        }


def _valid_timestamp_range(started_at_ms: Any, ended_at_ms: Any) -> bool:
    def valid_value(value: Any) -> bool:
        return value is None or (
            not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value) and value >= 0
        )

    if not valid_value(started_at_ms) or not valid_value(ended_at_ms):
        return False
    return started_at_ms is None or ended_at_ms is None or ended_at_ms >= started_at_ms


def _json_safe_dataclass(value: Any) -> Dict[str, Any]:
    return {item.name: _json_safe(getattr(value, item.name)) for item in fields(value)}


def _json_safe(value: Any) -> Any:
    if isinstance(value, Enum):
        return _json_safe(value.value)
    if is_dataclass(value) and not isinstance(value, type):
        return _json_safe_dataclass(value)
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(item) for item in value]
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


__all__ = [
    "AgentEvent",
    "EventGraph",
    "EventStatus",
    "GraphDiagnostic",
    "ModelCallEvent",
    "ToolEvent",
    "TurnEvent",
    "Usage",
]
