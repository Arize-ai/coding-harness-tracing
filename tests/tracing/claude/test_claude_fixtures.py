"""Integrity and sanitization checks for captured Claude transcript fixtures."""

import json
from pathlib import Path

import pytest

FIXTURE_DIR = Path(__file__).parent / "fixtures"
FIXTURES = tuple(sorted(FIXTURE_DIR.glob("*.jsonl")))
FORBIDDEN_MARKERS = (
    "/root/",
    "034a75b0-5d61-4aba-9462-8d0ce21a13a9",
    "75733acb-f1c3-4b9f-bb31-916ebfa60895",
    "af064ee28f107fa97",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "OLLAMA_API_KEY",
)


def test_fixture_set_is_present():
    assert {path.name for path in FIXTURES} == {
        "main_tool_cycle.jsonl",
        "subagent_agent.jsonl",
        "subagent_main.jsonl",
    }


@pytest.mark.parametrize("fixture_path", FIXTURES, ids=lambda path: path.name)
def test_fixture_is_valid_sanitized_jsonl(fixture_path: Path):
    raw = fixture_path.read_text()

    for marker in FORBIDDEN_MARKERS:
        assert marker not in raw

    records = [json.loads(line) for line in raw.splitlines() if line.strip()]
    assert records
    assert all(isinstance(record, dict) for record in records)
    assert all(record.get("sessionId", "").startswith("session-") for record in records)
