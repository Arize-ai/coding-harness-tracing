"""Edge-case / defensive-parsing tests for tracing.devin.transcript.

The parser must never raise on missing, null, or wrong-typed fields and must
implement the timestamp-chaining rules exactly. These tests exercise the
defensive branches and the cross-step timestamp logic that the fixture-based
happy-path tests do not fully cover.
"""

from __future__ import annotations

import json
from pathlib import Path

from tracing.devin.transcript import AgentStep, ParsedSession, ToolCallInfo, _iso_to_ms, parse_transcript

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _load_fixture() -> ParsedSession:
    data = json.loads((FIXTURES_DIR / "session.json").read_text())
    return parse_transcript(data)


# --------------------------------------------------------------------------
# _iso_to_ms
# --------------------------------------------------------------------------


def test_iso_to_ms_valid():
    # 2026-07-16T19:00:47.614156+00:00 -> epoch ms
    ms = _iso_to_ms("2026-07-16T19:00:47.614156+00:00")
    assert ms == 1784228447614


def test_iso_to_ms_empty_string_is_zero():
    assert _iso_to_ms("") == 0


def test_iso_to_ms_garbage_is_zero():
    assert _iso_to_ms("not-a-timestamp") == 0


def test_iso_to_ms_none_is_zero():
    # Wrong type must not raise.
    assert _iso_to_ms(None) == 0  # type: ignore[arg-type]


def test_iso_to_ms_non_string_is_zero():
    assert _iso_to_ms(12345) == 0  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# Timestamp chaining
# --------------------------------------------------------------------------


def test_end_ms_points_to_next_step_regardless_of_source():
    parsed = _load_fixture()
    # First agent step (step_id 9) is followed by the next agent step
    # (step_id 10) in the raw step list, so its end_ms is that step's start.
    assert parsed.steps[0].end_ms == parsed.steps[1].start_ms
    assert parsed.steps[0].end_ms > parsed.steps[0].start_ms


def test_last_step_end_ms_equals_its_start_ms():
    parsed = _load_fixture()
    last = parsed.steps[-1]
    assert last.end_ms == last.start_ms


def test_session_span_covers_first_and_last_step_overall():
    parsed = _load_fixture()
    # start_ms = first step overall (a system step), end_ms = last step overall.
    assert parsed.start_ms == _iso_to_ms("2026-07-16T18:58:47.538048+00:00")
    assert parsed.end_ms == _iso_to_ms("2026-07-16T19:00:49.219635+00:00")
    # First agent step starts after the session start (system step precedes it).
    assert parsed.steps[0].start_ms > parsed.start_ms


def test_end_ms_uses_next_step_even_when_next_is_non_agent():
    data = {
        "steps": [
            {"source": "agent", "timestamp": "2026-01-01T00:00:00+00:00"},
            {"source": "tool", "timestamp": "2026-01-01T00:00:05+00:00"},
        ]
    }
    parsed = parse_transcript(data)
    assert len(parsed.steps) == 1
    # end_ms is the *next* raw step (the tool step), not skipped.
    assert parsed.steps[0].end_ms == _iso_to_ms("2026-01-01T00:00:05+00:00")
    assert parsed.steps[0].end_ms > parsed.steps[0].start_ms


# --------------------------------------------------------------------------
# Defensive parsing — must not raise
# --------------------------------------------------------------------------


def test_none_input_is_safe():
    parsed = parse_transcript(None)  # type: ignore[arg-type]
    assert isinstance(parsed, ParsedSession)
    assert parsed.steps == []
    assert parsed.user_prompts == []


def test_wrong_typed_top_level_fields_do_not_raise():
    data = {
        "session_id": 123,  # not a str
        "agent": "nope",  # not a dict
        "steps": "nope",  # not a list
        "final_metrics": [1, 2, 3],  # not a dict
    }
    parsed = parse_transcript(data)
    assert parsed.session_id == ""
    assert parsed.model_name == ""
    assert parsed.backend == ""
    assert parsed.agent_version == ""
    assert parsed.steps == []
    assert parsed.total_prompt_tokens == 0


def test_agent_step_with_missing_optional_fields():
    data = {"steps": [{"source": "agent"}]}
    parsed = parse_transcript(data)
    assert len(parsed.steps) == 1
    step = parsed.steps[0]
    assert step.step_id == 0
    assert step.assistant_text == ""
    assert step.reasoning == ""
    assert step.model_name == ""
    assert step.prompt_tokens == 0
    assert step.completion_tokens == 0
    assert step.tool_calls == []
    assert step.start_ms == 0
    assert step.end_ms == 0


def test_null_fields_fall_back_to_defaults():
    data = {
        "session_id": None,
        "agent": {"version": None, "model_name": None, "extra": None},
        "steps": [
            {
                "source": "agent",
                "timestamp": None,
                "message": None,
                "reasoning_content": None,
                "model_name": None,
                "metrics": None,
                "tool_calls": None,
                "observation": None,
            }
        ],
        "final_metrics": None,
    }
    parsed = parse_transcript(data)
    assert parsed.session_id == ""
    assert parsed.agent_version == ""
    assert parsed.backend == ""
    assert len(parsed.steps) == 1
    assert parsed.steps[0].tool_calls == []
    assert parsed.total_prompt_tokens == 0


def test_bool_metrics_coerced_to_zero():
    # JSON booleans must not be treated as ints (True == 1).
    data = {"steps": [{"source": "agent", "metrics": {"prompt_tokens": True}}]}
    parsed = parse_transcript(data)
    assert parsed.steps[0].prompt_tokens == 0


def test_float_metrics_coerced_to_int():
    data = {"steps": [{"source": "agent", "metrics": {"prompt_tokens": 12.9}}]}
    parsed = parse_transcript(data)
    assert parsed.steps[0].prompt_tokens == 12


# --------------------------------------------------------------------------
# User prompts / step filtering
# --------------------------------------------------------------------------


def test_multiple_user_prompts_preserved_in_order():
    data = {
        "steps": [
            {"source": "user", "message": "first"},
            {"source": "system", "message": "ignored"},
            {"source": "user", "message": "second"},
            {"source": "agent", "message": "reply"},
            {"source": "tool", "message": "ignored"},
            {"source": "user", "message": "third"},
        ]
    }
    parsed = parse_transcript(data)
    assert parsed.user_prompts == ["first", "second", "third"]
    assert len(parsed.steps) == 1  # only the agent step


def test_system_and_tool_steps_excluded_from_steps():
    data = {
        "steps": [
            {"source": "system", "message": "sys"},
            {"source": "tool", "message": "tool"},
        ]
    }
    parsed = parse_transcript(data)
    assert parsed.steps == []
    assert parsed.user_prompts == []


# --------------------------------------------------------------------------
# Tool-call / observation matching
# --------------------------------------------------------------------------


def test_tool_call_without_matching_observation_has_empty_result():
    data = {
        "steps": [
            {
                "source": "agent",
                "tool_calls": [{"tool_call_id": "c1", "function_name": "f", "arguments": {}}],
                "observation": {"results": [{"source_call_id": "OTHER", "content": "x"}]},
            }
        ]
    }
    parsed = parse_transcript(data)
    call = parsed.steps[0].tool_calls[0]
    assert isinstance(call, ToolCallInfo)
    assert call.tool_call_id == "c1"
    assert call.name == "f"
    assert call.result == ""


def test_tool_call_with_no_observation_key():
    data = {
        "steps": [
            {
                "source": "agent",
                "tool_calls": [{"tool_call_id": "c1", "function_name": "f"}],
            }
        ]
    }
    parsed = parse_transcript(data)
    call = parsed.steps[0].tool_calls[0]
    assert call.result == ""
    assert call.arguments == {}  # missing arguments -> {}


def test_multiple_tool_calls_matched_by_id():
    data = {
        "steps": [
            {
                "source": "agent",
                "tool_calls": [
                    {"tool_call_id": "a", "function_name": "fa", "arguments": {"x": 1}},
                    {"tool_call_id": "b", "function_name": "fb", "arguments": {"y": 2}},
                ],
                "observation": {
                    "results": [
                        {"source_call_id": "b", "content": "RESULT-B"},
                        {"source_call_id": "a", "content": "RESULT-A"},
                    ]
                },
            }
        ]
    }
    parsed = parse_transcript(data)
    calls = parsed.steps[0].tool_calls
    assert len(calls) == 2
    assert calls[0].name == "fa"
    assert calls[0].result == "RESULT-A"
    assert calls[1].name == "fb"
    assert calls[1].result == "RESULT-B"


def test_wrong_typed_tool_calls_do_not_raise():
    data = {
        "steps": [
            {
                "source": "agent",
                "tool_calls": [None, "nope", {"tool_call_id": "ok"}],
                "observation": {"results": ["bad", None]},
            }
        ]
    }
    parsed = parse_transcript(data)
    calls = parsed.steps[0].tool_calls
    assert len(calls) == 3
    # None / "nope" become empty ToolCallInfo objects, not exceptions.
    assert calls[0].name == ""
    assert calls[2].tool_call_id == "ok"


# --------------------------------------------------------------------------
# Totals
# --------------------------------------------------------------------------


def test_totals_from_final_metrics_with_cached():
    data = {
        "final_metrics": {
            "total_prompt_tokens": 100,
            "total_completion_tokens": 20,
            "total_cached_tokens": 40,
        }
    }
    parsed = parse_transcript(data)
    assert parsed.total_prompt_tokens == 100
    assert parsed.total_completion_tokens == 20
    assert parsed.total_cached_tokens == 40


def test_agentstep_dataclass_shape():
    # Guard the public dataclass contract the span builder depends on.
    step = AgentStep(
        step_id=1,
        assistant_text="a",
        reasoning="r",
        model_name="m",
        prompt_tokens=1,
        completion_tokens=2,
        tool_calls=[],
        start_ms=10,
        end_ms=20,
    )
    assert step.step_id == 1
    assert step.tool_calls == []
