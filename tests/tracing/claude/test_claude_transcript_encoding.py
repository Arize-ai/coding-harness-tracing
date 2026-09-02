"""Tests for reading claude transcripts as UTF-8"""

import builtins
import json
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

import pytest

from core.common import StateManager
from core.event_model import EventStatus, ModelCallEvent, TurnEvent
from tracing.claude_code.hooks.handlers import _handle_user_prompt_submit, _scan_transcript_for_usage
from tracing.claude_code.hooks.transcript import parse_claude_transcript

NON_ASCII_TEXT = "Done ✓ — Привет 한글 العربية 😏"


@contextmanager
def _non_utf8_locale():
    """Make implicit-encoding opens decode as ASCII, like a non-UTF-8 Windows codepage."""
    real_open = builtins.open

    def locale_open(file, mode="r", buffering=-1, encoding=None, errors=None, newline=None, closefd=True, opener=None):
        if "b" not in mode and encoding in (None, "locale"):
            encoding = "ascii"
        return real_open(file, mode, buffering, encoding, errors, newline, closefd, opener)

    real_read_text = Path.read_text

    def locale_read_text(self, encoding=None, *args, **kwargs):
        return real_read_text(self, encoding or "ascii", *args, **kwargs)

    # ``open()`` in handlers.py resolves to builtins.open. pathlib reaches
    # io.open through version-specific bindings (3.10 snapshots it at import),
    # so patch ``Path.read_text`` itself rather than io.open.
    with (
        mock.patch.object(builtins, "open", locale_open),
        mock.patch.object(Path, "read_text", locale_read_text),
    ):
        yield


def _write_transcript(path: Path) -> None:
    rows = [
        {"type": "user", "uuid": "user-1", "message": {"role": "user", "content": NON_ASCII_TEXT}},
        {
            "type": "assistant",
            "uuid": "assistant-1",
            "timestamp": "2026-01-01T12:00:01.000Z",
            "requestId": "req-1",
            "message": {
                "id": "msg-1",
                "role": "assistant",
                "model": "claude-test",
                "content": [{"type": "text", "text": NON_ASCII_TEXT}],
                "usage": {"input_tokens": 11, "output_tokens": 7},
            },
        },
    ]
    path.write_bytes(("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n").encode("utf-8"))


@pytest.fixture
def transcript(tmp_path: Path) -> Path:
    path = tmp_path / "session.jsonl"
    _write_transcript(path)
    return path


@pytest.fixture
def state(tmp_path: Path) -> StateManager:
    sm = StateManager(state_dir=tmp_path, state_file=tmp_path / "state.json", lock_path=tmp_path / ".lock")
    sm.init_state()
    sm.set("session_id", "test-session")
    sm.set("project_name", "test-project")
    sm.set("trace_count", "0")
    return sm


def test_simulated_locale_rejects_non_ascii_transcript(transcript: Path):
    """Sanity check: the simulation really fails on an implicit-encoding read."""
    with _non_utf8_locale(), pytest.raises(UnicodeDecodeError):
        with open(transcript) as f:
            f.read()
    with _non_utf8_locale(), pytest.raises(UnicodeDecodeError):
        transcript.read_text()


def test_user_prompt_submit_counts_lines_under_non_utf8_locale(transcript: Path, state: StateManager):
    """The trace-start line is recorded even when the transcript holds non-ASCII text."""
    with (
        _non_utf8_locale(),
        mock.patch("tracing.claude_code.hooks.handlers.resolve_session", return_value=state),
    ):
        _handle_user_prompt_submit({"prompt": NON_ASCII_TEXT, "transcript_path": str(transcript)})

    assert state.get("trace_start_line") == "2"


def test_scan_transcript_for_usage_under_non_utf8_locale(transcript: Path):
    """Token usage and output text are extracted regardless of host locale."""
    with _non_utf8_locale():
        output, usage, model = _scan_transcript_for_usage(transcript, 0)

    assert model == "claude-test"
    assert usage.prompt == 11
    assert usage.completion == 7
    assert NON_ASCII_TEXT in output


def test_parse_claude_transcript_under_non_utf8_locale(transcript: Path):
    """The high-fidelity parser returns model children, not a root-only graph."""
    root = TurnEvent(
        event_id="turn-1",
        session_id="test-session",
        turn_id="turn-1",
        sequence=0,
        started_at_ms=1_767_268_800_000,
        ended_at_ms=None,
        status=EventStatus.RUNNING,
        input="prompt",
    )

    with _non_utf8_locale():
        graph = parse_claude_transcript(transcript, root)

    assert [d.code for d in graph.diagnostics if d.code == "transcript_read_error"] == []
    models = [e for e in graph.events if isinstance(e, ModelCallEvent)]
    assert len(models) == 1
    assert models[0].output == NON_ASCII_TEXT
