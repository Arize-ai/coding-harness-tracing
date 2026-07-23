"""Durable buffering for Claude tool hook observations.

Claude invokes PreToolUse and PostToolUse hooks in separate processes.  This
module stores their partial observations in session state until a later Stop
hook can correlate them with transcript tool-use blocks.
"""

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from core.common import StateManager

_VALID_STATUSES = {"pending", "success", "error"}


def _json_safe(value: Any) -> Any:
    """Return a deterministic, JSON-serializable copy of a hook value."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    return str(value)


@dataclass
class ToolObservation:
    """The merged PreToolUse/PostToolUse data for one Claude tool call."""

    tool_use_id: str
    tool_name: Optional[str] = None
    tool_input: Any = None
    tool_response: Any = None
    error: Any = None
    started_at_ms: Optional[int] = None
    ended_at_ms: Optional[int] = None
    status: str = "pending"
    hook_event_metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Convert the observation to values accepted by ``json.dumps``."""
        return _json_safe(asdict(self))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ToolObservation":
        """Build an observation from persisted JSON data."""
        tool_use_id = _validate_tool_use_id(value.get("tool_use_id"))
        status = value.get("status", "pending")
        if status not in _VALID_STATUSES:
            raise ValueError("invalid tool observation status")
        metadata = value.get("hook_event_metadata") or {}
        if not isinstance(metadata, Mapping):
            metadata = {}
        return cls(
            tool_use_id=tool_use_id,
            tool_name=value.get("tool_name"),
            tool_input=_json_safe(value.get("tool_input")),
            tool_response=_json_safe(value.get("tool_response")),
            error=_json_safe(value.get("error")),
            started_at_ms=value.get("started_at_ms"),
            ended_at_ms=value.get("ended_at_ms"),
            status=status,
            hook_event_metadata=_json_safe(metadata),
        )


def _validate_tool_use_id(tool_use_id: Any) -> str:
    if not isinstance(tool_use_id, str) or not tool_use_id.strip():
        raise ValueError("tool_use_id must be a non-empty string")
    return tool_use_id


class ToolBuffer:
    """A process-safe tool observation buffer backed by one state key."""

    STATE_KEY = "claude.tool_observation_buffer"

    def __init__(self, state: StateManager) -> None:
        self._state = state

    def record_start(
        self,
        tool_use_id: str,
        *,
        tool_name: Optional[str] = None,
        tool_input: Any = None,
        started_at_ms: Optional[int] = None,
        hook_event_metadata: Optional[Mapping[str, Any]] = None,
    ) -> ToolObservation:
        """Record or merge a PreToolUse observation."""
        _validate_tool_use_id(tool_use_id)

        def update(items: Dict[str, ToolObservation]) -> Tuple[ToolObservation, bool]:
            observation = items.get(tool_use_id) or ToolObservation(tool_use_id=tool_use_id)
            observation.tool_name = tool_name
            observation.tool_input = _json_safe(tool_input)
            observation.started_at_ms = started_at_ms
            if hook_event_metadata is not None:
                observation.hook_event_metadata = _json_safe(hook_event_metadata)
            items[tool_use_id] = observation
            return observation, True

        return self._mutate(update)

    def record_result(
        self,
        tool_use_id: str,
        *,
        status: str,
        tool_response: Any = None,
        error: Any = None,
        ended_at_ms: Optional[int] = None,
        hook_event_metadata: Optional[Mapping[str, Any]] = None,
    ) -> ToolObservation:
        """Record or merge a successful or failed PostToolUse observation."""
        _validate_tool_use_id(tool_use_id)
        if status not in {"success", "error"}:
            raise ValueError("status must be 'success' or 'error'")

        def update(items: Dict[str, ToolObservation]) -> Tuple[ToolObservation, bool]:
            observation = items.get(tool_use_id) or ToolObservation(tool_use_id=tool_use_id)
            observation.status = status
            observation.tool_response = _json_safe(tool_response)
            observation.error = _json_safe(error)
            observation.ended_at_ms = ended_at_ms
            if hook_event_metadata is not None:
                observation.hook_event_metadata = _json_safe(hook_event_metadata)
            items[tool_use_id] = observation
            return observation, True

        return self._mutate(update)

    def get(self, tool_use_id: str) -> Optional[ToolObservation]:
        """Return one buffered observation, if present."""
        _validate_tool_use_id(tool_use_id)
        return self._load().get(tool_use_id)

    def all(self) -> List[ToolObservation]:
        """Return all valid buffered observations."""
        return list(self._load().values())

    def remove(self, tool_use_id: str) -> bool:
        """Remove one observation and report whether it existed."""
        _validate_tool_use_id(tool_use_id)

        def update(items: Dict[str, ToolObservation]) -> Tuple[bool, bool]:
            existed = tool_use_id in items
            items.pop(tool_use_id, None)
            return existed, existed

        return self._mutate(update)

    def remove_many(self, observations: List[ToolObservation]) -> set[str]:
        """Acknowledge only snapshot entries that have not changed since export."""
        expected = {item.tool_use_id: item.to_dict() for item in observations}

        def update(items: Dict[str, ToolObservation]) -> Tuple[set[str], bool]:
            removed = {
                tool_use_id
                for tool_use_id, value in expected.items()
                if tool_use_id in items and items[tool_use_id].to_dict() == value
            }
            for tool_use_id in removed:
                items.pop(tool_use_id, None)
            return removed, bool(removed)

        return self._mutate(update)

    def clear(self) -> None:
        """Remove all buffered observations while retaining the namespace key."""

        def update(items: Dict[str, ToolObservation]) -> Tuple[None, bool]:
            items.clear()
            return None, True

        self._mutate(update)

    def _load(self) -> Dict[str, ToolObservation]:
        return self._decode(self._state.get(self.STATE_KEY))

    @staticmethod
    def _decode(raw: Optional[str]) -> Dict[str, ToolObservation]:
        if not raw:
            return {}
        try:
            decoded = json.loads(raw)
        except (TypeError, ValueError):
            return {}
        if not isinstance(decoded, dict):
            return {}

        observations: Dict[str, ToolObservation] = {}
        for tool_use_id, value in decoded.items():
            if not isinstance(value, Mapping):
                continue
            try:
                observation = ToolObservation.from_dict(value)
            except (TypeError, ValueError):
                continue
            if observation.tool_use_id != tool_use_id:
                continue
            observations[tool_use_id] = observation
        return observations

    @staticmethod
    def _encode(items: Mapping[str, ToolObservation]) -> str:
        return json.dumps(
            {tool_use_id: observation.to_dict() for tool_use_id, observation in items.items()},
            separators=(",", ":"),
        )

    def _mutate(self, operation: Callable[[Dict[str, ToolObservation]], Tuple[Any, bool]]) -> Any:
        """Apply a read/modify/write transaction under StateManager's lock."""
        if self._state.state_file is None:
            items = self._load()
            result, _ = operation(items)
            return result

        # StateManager deliberately exposes only scalar set/get operations.  A
        # buffer merge must hold the same lock across read and write to avoid
        # losing observations produced by concurrent hook processes.
        with self._state._lock():
            state_data = self._state._read_safe()
            items = self._decode(state_data.get(self.STATE_KEY))
            result, changed = operation(items)
            if changed:
                state_data[self.STATE_KEY] = self._encode(items)
                self._state._write(state_data)
            return result
