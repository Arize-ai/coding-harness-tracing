"""Tests for tracing.devin.transcript — the pure ATIF-v1.7 parser."""

from __future__ import annotations

import json
from pathlib import Path

from tracing.devin.transcript import ParsedSession, parse_transcript

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _load_fixture() -> ParsedSession:
    data = json.loads((FIXTURES_DIR / "session.json").read_text())
    return parse_transcript(data)


def test_session_metadata():
    parsed = _load_fixture()
    assert parsed.session_id == "test-session"
    assert parsed.model_name == "SWE-1.6 Slow"
    assert parsed.backend == "Windsurf"
    assert parsed.agent_version == "3000.1.27"


def test_user_prompts():
    parsed = _load_fixture()
    assert parsed.user_prompts == ["Can you get me the weather in Seattle today"]


def test_agent_steps_and_tokens():
    parsed = _load_fixture()
    assert len(parsed.steps) == 2
    assert parsed.steps[0].prompt_tokens == 15093
    assert parsed.steps[0].completion_tokens == 101
    assert parsed.steps[1].prompt_tokens == 16527


def test_tool_calls_matched_to_observation():
    parsed = _load_fixture()
    calls = parsed.steps[0].tool_calls
    assert len(calls) == 1
    assert calls[0].name == "web_search"
    assert calls[0].arguments == {"query": "Seattle weather today"}
    assert calls[0].result.startswith("# Web Search Results")


def test_step_without_tool_calls():
    parsed = _load_fixture()
    assert parsed.steps[1].tool_calls == []


def test_totals():
    parsed = _load_fixture()
    assert parsed.total_prompt_tokens == 31620
    assert parsed.total_completion_tokens == 429
    assert parsed.total_cached_tokens == 0


def test_timestamps():
    parsed = _load_fixture()
    assert parsed.steps[0].start_ms > 0
    assert parsed.steps[0].end_ms >= parsed.steps[0].start_ms


def test_empty_dict_is_safe():
    parsed = parse_transcript({})
    assert isinstance(parsed, ParsedSession)
    assert parsed.user_prompts == []
    assert parsed.steps == []
    assert parsed.total_prompt_tokens == 0
    assert parsed.total_completion_tokens == 0
    assert parsed.total_cached_tokens == 0
    assert parsed.start_ms == 0
    assert parsed.end_ms == 0
