"""Contract tests for schema-grounded Codex rollout reconstruction."""

from pathlib import Path

from tracing.codex.hooks.transcript import _extract_turn_from_rollout

_FIXTURES = Path(__file__).parents[2] / "fixtures" / "codex"


def test_parse_codex_rollout_reconstructs_chain_llm_tool_llm():
    turn = _extract_turn_from_rollout(_FIXTURES / "rollout_two_responses.jsonl", "turn-fixture")

    assert turn is not None
    assert turn["user_prompt"] == "Inspect the repository"
    assert turn["assistant_output"] == "Repository inspected."
    assert len(turn["llm_calls"]) == 2

    first, second = turn["llm_calls"]
    assert first["input_value"] == "Inspect the repository"
    assert first["assistant_output"] == ""
    assert [tool["call_id"] for tool in first["tool_calls"]] == ["call-fixture-1"]
    assert first["tool_calls"][0]["output"] == "README.md"
    assert first["token_usage"]["prompt_tokens"] == 10
    assert first["token_usage"]["cached_input_tokens"] == 8

    assert second["input_value"] == "README.md"
    assert second["assistant_output"] == "Repository inspected."
    assert second["tool_calls"] == []
    assert second["token_usage"]["prompt_tokens"] == 20
    assert second["token_usage"]["cached_input_tokens"] == 15

    # Codex input_tokens already includes cached input. Cache is a subset of
    # prompt usage, not an additional bucket to add to it.
    assert turn["token_usage"]["prompt_tokens"] == 30
    assert turn["token_usage"]["cached_input_tokens"] == 23
    assert turn["token_usage"]["total_tokens"] == 37
