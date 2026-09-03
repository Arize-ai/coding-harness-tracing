"""Parse persisted Codex rollout JSONL into one reconstructed turn.

Standard rollout files do not expose exact provider request boundaries. Model
response cycles are therefore inferred from persisted response items and closed
primarily by ``event_msg.token_count`` records. Parsing is fail-soft: malformed
lines and unknown item variants are skipped, while an open final response is
retained at turn completion.
"""

from __future__ import annotations

import json
import os
import stat
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator

from core.common import get_timestamp_ms

# Reading is streamed, but hard budgets keep notify latency and memory bounded
# for malformed, very large, or concurrently growing rollout files.
_MAX_ROLLOUT_BYTES = 64 << 20  # 64 MiB
_MAX_ROLLOUT_LINE_BYTES = 1 << 20  # 1 MiB
_MAX_ROLLOUT_RECORDS = 200_000


# ---------------------------------------------------------------------------
# Rollout extraction
# ---------------------------------------------------------------------------


@contextmanager
def _open_regular_text(path: Path) -> Iterator[Iterator[str]]:
    """Yield bounded UTF-8 lines from one verified regular-file descriptor."""
    descriptor = -1
    try:
        flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            raise OSError("rollout candidate is not a regular file")
        if file_stat.st_size > _MAX_ROLLOUT_BYTES:
            raise OSError("rollout exceeds safe byte limit")

        handle = os.fdopen(descriptor, "rb")
        descriptor = -1  # fdopen owns and closes it from here.

        def bounded_lines() -> Iterator[str]:
            total_bytes = 0
            records = 0
            while True:
                raw_line = handle.readline(_MAX_ROLLOUT_LINE_BYTES + 1)
                if not raw_line:
                    return
                total_bytes += len(raw_line)
                records += 1
                if len(raw_line) > _MAX_ROLLOUT_LINE_BYTES:
                    raise OSError("rollout line exceeds safe byte limit")
                if total_bytes > _MAX_ROLLOUT_BYTES:
                    raise OSError("rollout exceeds safe byte limit")
                if records > _MAX_ROLLOUT_RECORDS:
                    raise OSError("rollout exceeds safe record limit")
                yield raw_line.decode("utf-8")

        with handle:
            yield bounded_lines()
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _iso_to_ms(ts: object) -> int:
    """Convert an ISO-8601 timestamp string to milliseconds, or return zero."""
    if not isinstance(ts, str) or not ts:
        return 0
    try:
        normalized = ts.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
        return int(dt.timestamp() * 1000)
    except (ValueError, TypeError, OverflowError, OSError):
        return 0


def _extract_turn_from_rollout(rollout_path: Path, turn_id: str) -> "dict | None":
    """Walk the rollout JSONL and extract everything for one turn.

    Returns a dict with: ``trace_count``, ``turn_start_ms``, ``turn_end_ms``,
    ``duration_ms``, ``user_prompt``, ``assistant_output``, ``model``, ``cwd``,
    ``permission_mode``, ``sandbox_mode``, ``token_usage`` (or None), and
    ``tool_calls`` (a list of ``{tool, args, output, call_id, start_ts, end_ts}``).

    Returns None if the turn isn't found.
    """
    if not turn_id:
        return None

    fields = (
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "cached_input_tokens",
        "non_cached_input_tokens",
        "reasoning_output_tokens",
    )
    token_sums: dict = {k: 0 for k in fields}
    saw_tokens = False

    in_turn = False
    trace_count = 0
    turn_start_ms = 0
    turn_end_ms = 0
    last_in_turn_ts = 0
    duration_ms: "int | None" = None
    user_prompt = ""
    assistant_output = ""
    model = ""
    cwd = ""
    permission_mode = ""
    sandbox_mode = ""

    llm_calls: list = []
    active_llm_call: "dict | None" = None
    pending_model_inputs: list[dict[str, str]] = []
    saw_turn_evidence = False

    def valid_count(value: object) -> "int | None":
        return value if type(value) is int and value >= 0 else None

    def metadata_text(value: object, limit: int = 4096) -> str:
        return value[:limit] if isinstance(value, str) else ""

    def response_message_text(payload: dict) -> str:
        content = payload.get("content") or []
        if isinstance(content, str):
            return content
        if not isinstance(content, list):
            return ""
        return "\n".join(
            item["text"] for item in content if isinstance(item, dict) and isinstance(item.get("text"), str)
        )

    def append_model_input(kind: str, value: str) -> None:
        """Collapse duplicate durable/event representations of one input."""
        part = {"kind": kind, "value": value}
        if not pending_model_inputs or pending_model_inputs[-1] != part:
            pending_model_inputs.append(part)

    def ensure_llm_call(start_ms: int) -> dict:
        nonlocal active_llm_call, saw_turn_evidence
        saw_turn_evidence = True
        if active_llm_call is None:
            input_parts = list(pending_model_inputs)
            active_llm_call = {
                "start_ts": start_ms,
                "end_ts": start_ms,
                "input_value": "\n".join(part["value"] for part in input_parts),
                "input_parts": input_parts,
                "assistant_output": "",
                "token_usage": None,
                "tool_calls": [],
            }
            pending_model_inputs.clear()
        return active_llm_call

    def per_response_usage(raw: dict) -> dict:
        return {
            "prompt_tokens": valid_count(raw.get("input_tokens")),
            "completion_tokens": valid_count(raw.get("output_tokens")),
            "total_tokens": valid_count(raw.get("total_tokens")),
            "cached_input_tokens": valid_count(raw.get("cached_input_tokens")) or 0,
            "non_cached_input_tokens": valid_count(raw.get("non_cached_input_tokens")) or 0,
            "reasoning_output_tokens": valid_count(raw.get("reasoning_output_tokens")) or 0,
            "model": model or "",
        }

    tool_calls: list = []
    pending_func: dict[str, list[dict]] = {}  # call_id -> unmatched entries in arrival order
    pending_search_end: "dict | None" = None  # most recent unmatched web_search_end

    def value_text(value: object) -> str:
        if isinstance(value, str):
            return value
        if value is None:
            return ""
        try:
            return json.dumps(value, separators=(",", ":"), ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            return str(value)

    def record_tool(tool: object, args: object, call_id: object, ts_ms: int, output: str = "") -> dict:
        normalized_call_id = metadata_text(call_id)
        entry = {
            "tool": metadata_text(tool) or "unknown_tool",
            "args": value_text(args),
            "output": output,
            "call_id": normalized_call_id,
            "start_ts": ts_ms,
            "end_ts": ts_ms,
            "decision": None,
        }
        tool_calls.append(entry)
        ensure_llm_call(ts_ms)["tool_calls"].append(entry)
        if normalized_call_id:
            pending_func.setdefault(normalized_call_id, []).append(entry)
        return entry

    def record_tool_output(call_id: str, output_value: object, ts_ms: int) -> None:
        call_id = metadata_text(call_id)
        output = value_text(output_value)
        queue = pending_func.get(call_id) if call_id else None
        pending = queue.pop(0) if queue else None
        if queue == []:
            pending_func.pop(call_id, None)
        if pending is not None:
            pending["output"] = output
            pending["end_ts"] = max(pending["start_ts"], ts_ms or pending["end_ts"])
        if output:
            append_model_input("tool_output", output)

    try:
        with _open_regular_text(rollout_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if not isinstance(obj, dict):
                    continue

                outer = obj.get("type")
                payload = obj.get("payload")
                if not isinstance(payload, dict):
                    continue
                ptype = payload.get("type")
                ts_ms = _iso_to_ms(obj.get("timestamp"))

                # turn_context records carry per-turn settings (model, cwd, sandbox).
                # They live at the outer level (no payload.type).
                if outer == "turn_context":
                    if payload.get("turn_id") == turn_id:
                        model = metadata_text(payload.get("model")) or model
                        cwd = metadata_text(payload.get("cwd")) or cwd
                        permission_mode = metadata_text(payload.get("approval_policy")) or permission_mode
                        sandbox_policy = payload.get("sandbox_policy")
                        if isinstance(sandbox_policy, dict):
                            sandbox_mode = metadata_text(sandbox_policy.get("type")) or sandbox_mode
                    continue

                if outer != "event_msg" and outer != "response_item":
                    continue

                # Turn boundaries
                if outer == "event_msg" and ptype == "task_started":
                    if in_turn:
                        # Next turn started after ours -- stop walking.
                        break
                    trace_count += 1
                    if payload.get("turn_id") == turn_id:
                        in_turn = True
                        # started_at is unix seconds; convert to ms.
                        started_at = valid_count(payload.get("started_at"))
                        if started_at is not None:
                            turn_start_ms = started_at * 1000
                        elif ts_ms:
                            turn_start_ms = ts_ms
                    continue

                if not in_turn:
                    continue

                if ts_ms:
                    last_in_turn_ts = max(last_in_turn_ts, ts_ms)

                # Matching task_complete is the hard parsing boundary for this turn.
                if outer == "event_msg" and ptype == "task_complete":
                    completion_turn = payload.get("turn_id")
                    if completion_turn not in (None, "", turn_id):
                        continue
                    msg = payload.get("last_agent_message")
                    if isinstance(msg, str) and msg:
                        saw_turn_evidence = True
                        assistant_output = msg
                    completed_at = valid_count(payload.get("completed_at"))
                    if completed_at is not None:
                        turn_end_ms = completed_at * 1000
                    elif ts_ms:
                        turn_end_ms = ts_ms
                    d = valid_count(payload.get("duration_ms"))
                    if d is not None:
                        duration_ms = d
                    break

                # User prompt (legacy event form).
                if outer == "event_msg" and ptype == "user_message":
                    msg = payload.get("message")
                    if isinstance(msg, str) and msg:
                        saw_turn_evidence = True
                        user_prompt = msg
                        append_model_input("prompt", msg)
                    continue

                # User prompt (durable rollout response-item form).
                if outer == "response_item" and ptype == "message" and payload.get("role") == "user":
                    msg = response_message_text(payload)
                    if msg:
                        saw_turn_evidence = True
                        user_prompt = msg
                        append_model_input("prompt", msg)
                    continue

                # Intermediate assistant message (overwritten by task_complete if both present)
                if outer == "event_msg" and ptype == "agent_message":
                    msg = payload.get("message")
                    if isinstance(msg, str) and msg:
                        saw_turn_evidence = True
                        assistant_output = msg
                    continue

                # Token counts -- sum per-call deltas and close the active model response.
                if outer == "event_msg" and ptype == "token_count":
                    info = payload.get("info")
                    if not isinstance(info, dict):
                        continue
                    last = info.get("last_token_usage")
                    if not isinstance(last, dict):
                        continue
                    valid_usage = False
                    for k in fields:
                        v = valid_count(last.get(k))
                        if v is not None:
                            token_sums[k] += v
                            saw_tokens = True
                            saw_turn_evidence = True
                            valid_usage = True
                    if active_llm_call is not None and valid_usage:
                        active_llm_call["end_ts"] = max(
                            active_llm_call["start_ts"], ts_ms or active_llm_call["start_ts"]
                        )
                        active_llm_call["token_usage"] = per_response_usage(last)
                        llm_calls.append(active_llm_call)
                        active_llm_call = None
                    continue

                if outer == "response_item" and ptype == "reasoning":
                    ensure_llm_call(ts_ms)
                    continue

                if outer == "response_item" and ptype == "message" and payload.get("role") == "assistant":
                    call = ensure_llm_call(ts_ms)
                    msg = response_message_text(payload)
                    if msg:
                        existing = call["assistant_output"]
                        call["assistant_output"] = f"{existing}\n{msg}" if existing else msg
                        if assistant_output != msg:
                            assistant_output = f"{assistant_output}\n{msg}" if assistant_output else msg
                    continue

                # Structured function call.
                if outer == "response_item" and ptype == "function_call":
                    record_tool(
                        payload.get("name") or "function_call",
                        payload.get("arguments") or "",
                        payload.get("call_id") or "",
                        ts_ms,
                    )
                    continue

                if outer == "response_item" and ptype == "function_call_output":
                    record_tool_output(payload.get("call_id") or "", payload.get("output"), ts_ms)
                    continue

                # Free-form custom tool call; its input is plain text rather than JSON arguments.
                if outer == "response_item" and ptype == "custom_tool_call":
                    record_tool(
                        payload.get("name") or "custom_tool",
                        payload.get("input") or "",
                        payload.get("call_id") or "",
                        ts_ms,
                    )
                    continue

                if outer == "response_item" and ptype == "custom_tool_call_output":
                    record_tool_output(payload.get("call_id") or "", payload.get("output"), ts_ms)
                    continue

                # Responses API built-in local shell call. Preserve action.command as argv JSON.
                if outer == "response_item" and ptype == "local_shell_call":
                    action = payload.get("action") or {}
                    record_tool(
                        "local_shell",
                        value_text(action),
                        payload.get("call_id") or payload.get("id") or "",
                        ts_ms,
                        payload.get("status") or "",
                    )
                    continue

                # Dynamic tool discovery call and its persisted tool definitions.
                if outer == "response_item" and ptype == "tool_search_call":
                    search_args = value_text(
                        {
                            "arguments": payload.get("arguments"),
                            "execution": payload.get("execution") or "",
                        }
                    )
                    record_tool(
                        "tool_search",
                        search_args,
                        payload.get("call_id") or payload.get("id") or "",
                        ts_ms,
                    )
                    continue

                if outer == "response_item" and ptype == "tool_search_output":
                    record_tool_output(payload.get("call_id") or "", payload.get("tools"), ts_ms)
                    continue

                # Built-in image generation call. Result is gated like every other tool output.
                if outer == "response_item" and ptype == "image_generation_call":
                    image_result = value_text(payload.get("result"))
                    if image_result:
                        image_result = f"<image result omitted ({len(image_result)} chars)>"
                    record_tool(
                        "image_generation",
                        payload.get("revised_prompt") or "",
                        payload.get("id") or "",
                        ts_ms,
                        image_result or payload.get("status") or "",
                    )
                    continue

                # Web search: web_search_end (event_msg, has call_id) is paired with
                # the next web_search_call (response_item) by emission order.
                if outer == "event_msg" and ptype == "web_search_end":
                    pending_search_end = {
                        "call_id": payload.get("call_id") or "",
                        "ts_ms": ts_ms,
                    }
                    continue

                if outer == "response_item" and ptype == "web_search_call":
                    action_value = payload.get("action")
                    action = action_value if isinstance(action_value, dict) else {}
                    action_type = action.get("type") if isinstance(action.get("type"), str) else "other"
                    if action_type == "open_page":
                        tool_name = "open_page"
                        args = action.get("url") if isinstance(action.get("url"), str) else ""
                    elif action_type == "find_in_page":
                        tool_name = "find_in_page"
                        args = value_text(
                            {key: action[key] for key in ("pattern", "url") if isinstance(action.get(key), str)}
                        )
                    elif action_type == "search":
                        tool_name = "web_search"
                        if isinstance(action.get("query"), str):
                            args = action["query"]
                        else:
                            queries = action.get("queries")
                            args = value_text(queries) if isinstance(queries, list) else ""
                    else:
                        tool_name = "web_search_other"
                        args = value_text(action_value)
                    start_ts = ts_ms
                    call_id = ""
                    if pending_search_end is not None:
                        call_id = pending_search_end["call_id"]
                        start_ts = pending_search_end["ts_ms"] or start_ts
                        pending_search_end = None
                    entry = {
                        "tool": tool_name,
                        "args": args,
                        "output": payload.get("status") or "",
                        "call_id": call_id,
                        "start_ts": start_ts,
                        "end_ts": ts_ms,
                        "decision": None,
                    }
                    tool_calls.append(entry)
                    ensure_llm_call(ts_ms)["tool_calls"].append(entry)
                    continue
    except (OSError, UnicodeError):
        return None

    if not in_turn or not saw_turn_evidence:
        return None

    candidates = [turn_start_ms, turn_end_ms]
    candidates.extend(e["start_ts"] for e in tool_calls if e.get("start_ts"))
    candidates.extend(e["end_ts"] for e in tool_calls if e.get("end_ts"))
    if active_llm_call is not None:
        candidates.append(active_llm_call["start_ts"])
    if not turn_end_ms:
        candidates.append(last_in_turn_ts)
    turn_end_ms = max(candidates)

    if active_llm_call is not None:
        active_llm_call["end_ts"] = max(active_llm_call["start_ts"], turn_end_ms or active_llm_call["start_ts"])
        llm_calls.append(active_llm_call)

    token_usage: "dict | None" = None
    if saw_tokens:
        token_usage = {
            "prompt_tokens": token_sums["input_tokens"] or None,
            "completion_tokens": token_sums["output_tokens"] or None,
            "total_tokens": token_sums["total_tokens"] or None,
            "cached_input_tokens": token_sums["cached_input_tokens"],
            "non_cached_input_tokens": token_sums["non_cached_input_tokens"],
            "reasoning_output_tokens": token_sums["reasoning_output_tokens"],
            "model": model or "",
        }

    return {
        "trace_count": trace_count,
        "turn_start_ms": turn_start_ms or get_timestamp_ms(),
        "turn_end_ms": turn_end_ms or get_timestamp_ms(),
        "duration_ms": duration_ms,
        "user_prompt": user_prompt,
        "assistant_output": assistant_output,
        "model": model,
        "cwd": cwd,
        "permission_mode": permission_mode,
        "sandbox_mode": sandbox_mode,
        "token_usage": token_usage,
        "tool_calls": tool_calls,
        "llm_calls": llm_calls,
    }
