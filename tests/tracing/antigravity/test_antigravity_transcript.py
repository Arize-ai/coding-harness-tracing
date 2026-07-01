"""Tests for tracing.antigravity.hooks.transcript.parse_transcript."""

from __future__ import annotations

import json
from pathlib import Path

from tracing.antigravity.hooks.transcript import parse_transcript

FIXTURE_DIR = Path(__file__).parent / "fixtures"


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")


class TestParseTranscriptRealFixture:
    def test_parses_full_fixture_into_single_turn(self):
        turns = parse_transcript(FIXTURE_DIR / "transcript_full.jsonl")
        assert len(turns) == 1

    def test_user_input_strips_metadata_blocks(self):
        turns = parse_transcript(FIXTURE_DIR / "transcript_full.jsonl")
        user_input = turns[0]["user_input"]
        assert "codecov.yml" in user_input
        assert "<" not in user_input

    def test_extracts_model_name(self):
        turns = parse_transcript(FIXTURE_DIR / "transcript_full.jsonl")
        assert turns[0]["model_name"] == "Gemini 3.5 Flash (Medium)"

    def test_tool_steps_match_fixture_calls(self):
        turns = parse_transcript(FIXTURE_DIR / "transcript_full.jsonl")
        tool_steps = turns[0]["tool_steps"]
        assert len(tool_steps) == 5
        names = [t["name"] for t in tool_steps]
        assert names == [
            "grep_search",
            "list_dir",
            "view_file",
            "search_web",
            "run_command",
        ]

    def test_tool_step_end_ms_after_or_equal_start_ms(self):
        turns = parse_transcript(FIXTURE_DIR / "transcript_full.jsonl")
        for step in turns[0]["tool_steps"]:
            assert step["end_ms"] >= step["start_ms"]
            assert step["start_ms"] > 0

    def test_tool_step_run_command_uses_created_completed_at(self):
        turns = parse_transcript(FIXTURE_DIR / "transcript_full.jsonl")
        run_cmd = next(t for t in turns[0]["tool_steps"] if t["name"] == "run_command")
        # Run command spans 16:00:20Z → 16:00:25Z = 5s difference per fixture
        assert run_cmd["end_ms"] - run_cmd["start_ms"] == 5000

    def test_final_response_non_empty(self):
        turns = parse_transcript(FIXTURE_DIR / "transcript_full.jsonl")
        assert turns[0]["final_response"]
        assert "codecov.yml" in turns[0]["final_response"]

    def test_max_step_index(self):
        turns = parse_transcript(FIXTURE_DIR / "transcript_full.jsonl")
        assert turns[0]["max_step_index"] == 13

    def test_llm_steps_count_matches_planner_responses(self):
        turns = parse_transcript(FIXTURE_DIR / "transcript_full.jsonl")
        # Fixture has 6 PLANNER_RESPONSE records (steps 2,5,7,9,11,13).
        assert len(turns[0]["llm_steps"]) == 6

    def test_last_llm_step_carries_thinking(self):
        turns = parse_transcript(FIXTURE_DIR / "transcript_full.jsonl")
        last_step = turns[0]["llm_steps"][-1]
        assert last_step["thinking"]
        assert "Validating" in last_step["thinking"]

    def test_turn_start_and_end_ms_set(self):
        turns = parse_transcript(FIXTURE_DIR / "transcript_full.jsonl")
        t = turns[0]
        assert t["start_ms"] > 0
        assert t["end_ms"] >= t["start_ms"]


class TestParseTranscriptFullPreferred:
    def test_sibling_full_preferred_over_truncated(self, tmp_path):
        truncated = tmp_path / "transcript.jsonl"
        full = tmp_path / "transcript_full.jsonl"
        _write_jsonl(
            truncated,
            [
                {
                    "step_index": 0,
                    "source": "USER_EXPLICIT",
                    "type": "USER_INPUT",
                    "status": "DONE",
                    "created_at": "2026-06-09T16:00:00Z",
                    "content": "<USER_REQUEST>truncated</USER_REQUEST>",
                },
            ],
        )
        _write_jsonl(
            full,
            [
                {
                    "step_index": 0,
                    "source": "USER_EXPLICIT",
                    "type": "USER_INPUT",
                    "status": "DONE",
                    "created_at": "2026-06-09T16:00:00Z",
                    "content": "<USER_REQUEST>full content</USER_REQUEST>",
                },
            ],
        )

        turns = parse_transcript(truncated)
        assert len(turns) == 1
        assert turns[0]["user_input"] == "full content"


class TestParseTranscriptMultiTurn:
    def test_two_user_inputs_yield_two_turns(self, tmp_path):
        f = tmp_path / "transcript.jsonl"
        _write_jsonl(
            f,
            [
                {
                    "step_index": 0,
                    "type": "USER_INPUT",
                    "created_at": "2026-06-09T16:00:00Z",
                    "content": "<USER_REQUEST>first</USER_REQUEST>",
                },
                {
                    "step_index": 1,
                    "type": "PLANNER_RESPONSE",
                    "created_at": "2026-06-09T16:00:01Z",
                    "content": "first answer",
                },
                {
                    "step_index": 2,
                    "type": "USER_INPUT",
                    "created_at": "2026-06-09T16:00:10Z",
                    "content": "<USER_REQUEST>second</USER_REQUEST>",
                },
                {
                    "step_index": 3,
                    "type": "PLANNER_RESPONSE",
                    "created_at": "2026-06-09T16:00:11Z",
                    "content": "second answer",
                },
                {
                    "step_index": 4,
                    "type": "PLANNER_RESPONSE",
                    "created_at": "2026-06-09T16:00:12Z",
                    "content": "final second answer",
                },
            ],
        )

        turns = parse_transcript(f)
        assert len(turns) == 2
        assert turns[0]["user_input"] == "first"
        assert turns[0]["final_response"] == "first answer"
        assert turns[0]["max_step_index"] == 1
        assert turns[1]["user_input"] == "second"
        assert turns[1]["final_response"] == "final second answer"
        assert turns[1]["max_step_index"] == 4


class TestParseTranscriptDefensive:
    def test_missing_file_returns_empty_list(self, tmp_path):
        assert parse_transcript(tmp_path / "nope.jsonl") == []

    def test_blank_file_returns_empty_list(self, tmp_path):
        f = tmp_path / "transcript.jsonl"
        f.write_text("", encoding="utf-8")
        assert parse_transcript(f) == []

    def test_malformed_lines_are_skipped(self, tmp_path):
        f = tmp_path / "transcript.jsonl"
        f.write_text(
            "not json\n"
            + json.dumps(
                {
                    "step_index": 0,
                    "type": "USER_INPUT",
                    "created_at": "2026-06-09T16:00:00Z",
                    "content": "<USER_REQUEST>hi</USER_REQUEST>",
                }
            )
            + "\n{bad json\n",
            encoding="utf-8",
        )
        turns = parse_transcript(f)
        assert len(turns) == 1
        assert turns[0]["user_input"] == "hi"

    def test_conversation_history_records_are_skipped(self, tmp_path):
        f = tmp_path / "transcript.jsonl"
        _write_jsonl(
            f,
            [
                {
                    "step_index": 0,
                    "type": "USER_INPUT",
                    "created_at": "2026-06-09T16:00:00Z",
                    "content": "<USER_REQUEST>hi</USER_REQUEST>",
                },
                {
                    "step_index": 1,
                    "type": "CONVERSATION_HISTORY",
                    "created_at": "2026-06-09T16:00:00Z",
                },
                {
                    "step_index": 2,
                    "type": "PLANNER_RESPONSE",
                    "created_at": "2026-06-09T16:00:01Z",
                    "content": "hello",
                },
            ],
        )
        turns = parse_transcript(f)
        assert len(turns) == 1
        assert len(turns[0]["llm_steps"]) == 1
        assert turns[0]["final_response"] == "hello"

    def test_records_before_first_user_input_are_ignored(self, tmp_path):
        f = tmp_path / "transcript.jsonl"
        _write_jsonl(
            f,
            [
                {
                    "step_index": 0,
                    "type": "PLANNER_RESPONSE",
                    "created_at": "2026-06-09T16:00:00Z",
                    "content": "orphan",
                },
                {
                    "step_index": 1,
                    "type": "USER_INPUT",
                    "created_at": "2026-06-09T16:00:01Z",
                    "content": "<USER_REQUEST>hi</USER_REQUEST>",
                },
            ],
        )
        turns = parse_transcript(f)
        assert len(turns) == 1
        assert turns[0]["user_input"] == "hi"
        assert turns[0]["llm_steps"] == []


class TestParseTranscriptMetadataExtraction:
    def test_user_request_block_extracted(self, tmp_path):
        f = tmp_path / "transcript.jsonl"
        _write_jsonl(
            f,
            [
                {
                    "step_index": 0,
                    "type": "USER_INPUT",
                    "created_at": "2026-06-09T16:00:00Z",
                    "content": (
                        "<USER_REQUEST>hi</USER_REQUEST>\n"
                        "<USER_SETTINGS_CHANGE>\n"
                        "The user changed setting `Model Selection` from None to "
                        "Gemini 3.5 Flash (Medium). Other text.\n"
                        "</USER_SETTINGS_CHANGE>"
                    ),
                }
            ],
        )
        turns = parse_transcript(f)
        assert turns[0]["user_input"] == "hi"
        assert turns[0]["model_name"] == "Gemini 3.5 Flash (Medium)"

    def test_metadata_blocks_stripped_when_no_user_request_wrapper(self, tmp_path):
        f = tmp_path / "transcript.jsonl"
        _write_jsonl(
            f,
            [
                {
                    "step_index": 0,
                    "type": "USER_INPUT",
                    "created_at": "2026-06-09T16:00:00Z",
                    "content": ("bare prompt text\n" "<ADDITIONAL_METADATA>\nremove me\n</ADDITIONAL_METADATA>"),
                }
            ],
        )
        turns = parse_transcript(f)
        assert turns[0]["user_input"] == "bare prompt text"

    def test_missing_model_setting_returns_empty_string(self, tmp_path):
        f = tmp_path / "transcript.jsonl"
        _write_jsonl(
            f,
            [
                {
                    "step_index": 0,
                    "type": "USER_INPUT",
                    "created_at": "2026-06-09T16:00:00Z",
                    "content": "<USER_REQUEST>hi</USER_REQUEST>",
                }
            ],
        )
        turns = parse_transcript(f)
        assert turns[0]["model_name"] == ""


class TestParseTranscriptToolPairing:
    def test_planner_without_tool_calls_emits_no_tool_step(self, tmp_path):
        f = tmp_path / "transcript.jsonl"
        _write_jsonl(
            f,
            [
                {
                    "step_index": 0,
                    "type": "USER_INPUT",
                    "created_at": "2026-06-09T16:00:00Z",
                    "content": "<USER_REQUEST>hi</USER_REQUEST>",
                },
                {
                    "step_index": 1,
                    "type": "PLANNER_RESPONSE",
                    "created_at": "2026-06-09T16:00:01Z",
                    "content": "no tools here",
                },
            ],
        )
        turns = parse_transcript(f)
        assert turns[0]["tool_steps"] == []
        assert len(turns[0]["llm_steps"]) == 1

    def test_extra_tool_calls_without_results_are_dropped(self, tmp_path):
        f = tmp_path / "transcript.jsonl"
        _write_jsonl(
            f,
            [
                {
                    "step_index": 0,
                    "type": "USER_INPUT",
                    "created_at": "2026-06-09T16:00:00Z",
                    "content": "<USER_REQUEST>hi</USER_REQUEST>",
                },
                {
                    "step_index": 1,
                    "type": "PLANNER_RESPONSE",
                    "created_at": "2026-06-09T16:00:01Z",
                    "content": "calling",
                    "tool_calls": [
                        {"name": "tool_a", "args": {"x": 1}},
                        {"name": "tool_b", "args": {"y": 2}},
                    ],
                },
                {
                    "step_index": 2,
                    "type": "TOOL_A",
                    "created_at": "2026-06-09T16:00:02Z",
                    "content": "Created At: 2026-06-09T16:00:02Z\nCompleted At: 2026-06-09T16:00:03Z\nok",
                },
            ],
        )
        turns = parse_transcript(f)
        assert len(turns[0]["tool_steps"]) == 1
        assert turns[0]["tool_steps"][0]["name"] == "tool_a"
        assert turns[0]["tool_steps"][0]["args"] == {"x": 1}
