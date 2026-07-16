"""Pure ATIF-v1.7 transcript parser for the Devin CLI harness.

This module contains the only genuinely new parsing logic in the Devin harness:
it turns a raw ATIF-v1.7 transcript ``dict`` (as read from
``~/.local/share/devin/cli/transcripts/<session_id>.json``) into structured,
easily-spannable dataclasses.

It performs no I/O, no network, and imports nothing from ``tracing.devin``
except (optionally) constants — everything here is fully unit-testable in
isolation. All parsing is defensive: any missing, null, or wrong-typed field
falls back to a safe default instead of raising, so even an empty or garbage
``dict`` yields a valid :class:`ParsedSession`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class ToolCallInfo:
    """A single tool call issued by an agent step, plus its matched result."""

    tool_call_id: str
    name: str  # function_name
    arguments: dict
    result: str  # matched observation content ("" if none)


@dataclass
class AgentStep:
    """One ``source == "agent"`` step from the transcript."""

    step_id: int
    assistant_text: str  # step "message" ("" if missing)
    reasoning: str  # "reasoning_content" ("" if missing)
    model_name: str
    prompt_tokens: int  # 0 if absent
    completion_tokens: int
    tool_calls: list[ToolCallInfo]
    start_ms: int  # from step "timestamp" (ISO8601 -> epoch ms)
    end_ms: int  # next step's timestamp, or start_ms if last


@dataclass
class ParsedSession:
    """Structured view of a whole ATIF-v1.7 transcript."""

    session_id: str = ""
    model_name: str = ""  # agent.model_name
    agent_version: str = ""
    backend: str = ""
    user_prompts: list[str] = field(default_factory=list)
    steps: list[AgentStep] = field(default_factory=list)  # source=="agent" only
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    total_cached_tokens: int = 0
    start_ms: int = 0  # first step timestamp
    end_ms: int = 0  # last step timestamp


def _iso_to_ms(ts: str) -> int:
    """Convert an ISO8601 timestamp to epoch milliseconds, 0 on failure."""
    if not ts or not isinstance(ts, str):
        return 0
    try:
        return int(datetime.fromisoformat(ts).timestamp() * 1000)
    except (ValueError, TypeError, OverflowError, OSError):
        return 0


def _as_int(value: object) -> int:
    """Coerce a value to int, returning 0 for anything non-numeric."""
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    return 0


def _as_str(value: object) -> str:
    """Return the value if it is a str, else "" (treats None/wrong types as absent)."""
    return value if isinstance(value, str) else ""


def _as_dict(value: object) -> dict:
    """Return the value if it is a dict, else an empty dict."""
    return value if isinstance(value, dict) else {}


def _as_list(value: object) -> list:
    """Return the value if it is a list, else an empty list."""
    return value if isinstance(value, list) else []


def _build_tool_calls(step: dict) -> list[ToolCallInfo]:
    """Build ToolCallInfo entries for an agent step, matching observations by call id."""
    tool_calls = _as_list(step.get("tool_calls"))
    if not tool_calls:
        return []

    # Index observation results by their source_call_id for O(1) matching.
    observation = _as_dict(step.get("observation"))
    results_by_call_id: dict = {}
    for result in _as_list(observation.get("results")):
        result = _as_dict(result)
        call_id = _as_str(result.get("source_call_id"))
        if call_id:
            results_by_call_id[call_id] = _as_str(result.get("content"))

    infos: list[ToolCallInfo] = []
    for call in tool_calls:
        call = _as_dict(call)
        call_id = _as_str(call.get("tool_call_id"))
        infos.append(
            ToolCallInfo(
                tool_call_id=call_id,
                name=_as_str(call.get("function_name")),
                arguments=_as_dict(call.get("arguments")),
                result=results_by_call_id.get(call_id, ""),
            )
        )
    return infos


def parse_transcript(data: dict) -> ParsedSession:
    """Parse an ATIF-v1.7 transcript dict into a :class:`ParsedSession`.

    Never raises: any missing/null/wrong-typed field falls back to a safe
    default. An empty or garbage ``dict`` yields a ParsedSession with empty
    lists and zeroed totals.
    """
    data = _as_dict(data)

    agent = _as_dict(data.get("agent"))
    extra = _as_dict(agent.get("extra"))
    final_metrics = _as_dict(data.get("final_metrics"))

    raw_steps = _as_list(data.get("steps"))

    # Pre-compute each step's start_ms so end_ms can reference the next step.
    starts = [_iso_to_ms(_as_str(_as_dict(s).get("timestamp"))) for s in raw_steps]

    user_prompts: list[str] = []
    steps: list[AgentStep] = []

    for i, raw in enumerate(raw_steps):
        raw = _as_dict(raw)
        source = _as_str(raw.get("source"))

        if source == "user":
            message = _as_str(raw.get("message"))
            user_prompts.append(message)
            continue

        if source != "agent":
            continue  # ignore system/tool steps for span construction

        metrics = _as_dict(raw.get("metrics"))
        start_ms = starts[i]
        end_ms = starts[i + 1] if i + 1 < len(starts) else start_ms

        steps.append(
            AgentStep(
                step_id=_as_int(raw.get("step_id")),
                assistant_text=_as_str(raw.get("message")),
                reasoning=_as_str(raw.get("reasoning_content")),
                model_name=_as_str(raw.get("model_name")),
                prompt_tokens=_as_int(metrics.get("prompt_tokens")),
                completion_tokens=_as_int(metrics.get("completion_tokens")),
                tool_calls=_build_tool_calls(raw),
                start_ms=start_ms,
                end_ms=end_ms,
            )
        )

    return ParsedSession(
        session_id=_as_str(data.get("session_id")),
        model_name=_as_str(agent.get("model_name")),
        agent_version=_as_str(agent.get("version")),
        backend=_as_str(extra.get("backend")),
        user_prompts=user_prompts,
        steps=steps,
        total_prompt_tokens=_as_int(final_metrics.get("total_prompt_tokens")),
        total_completion_tokens=_as_int(final_metrics.get("total_completion_tokens")),
        total_cached_tokens=_as_int(final_metrics.get("total_cached_tokens")),
        start_ms=starts[0] if starts else 0,
        end_ms=starts[-1] if starts else 0,
    )
