#!/usr/bin/env python3
"""Regression test: Cursor hook dispatch never writes to stdout.

Cursor interprets hook stdout as control JSON (permission/continue/user_message),
so any stray tracing output could deny a shell execution or file read. Every
event handler must keep stdout silent — diagnostics belong on stderr (via
``log()`` / ``error()`` in ``core.common``) and the only legitimate stdout
write is the final protocol response in ``main()._print_permissive`` (which is
not exercised here).
"""
from unittest import mock

import pytest

from tracing.cursor.hooks import adapter
from tracing.cursor.hooks.handlers import _dispatch


@pytest.fixture(autouse=True)
def _mock_sleep(monkeypatch):
    """Mock time.sleep to prevent real delays."""
    monkeypatch.setattr("time.sleep", lambda s: None)


@pytest.fixture(autouse=True)
def _patch_cursor_state(tmp_path, monkeypatch):
    """Redirect cursor adapter STATE_DIR to temp so disk writes stay hermetic."""
    state_dir = tmp_path / "state" / "cursor"
    state_dir.mkdir(parents=True)
    monkeypatch.setattr(adapter, "STATE_DIR", state_dir)
    return state_dir


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Backend absent: hooks fail open, no network."""
    monkeypatch.delenv("ARIZE_API_KEY", raising=False)
    monkeypatch.delenv("ARIZE_SPACE_ID", raising=False)
    monkeypatch.delenv("PHOENIX_ENDPOINT", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)


# Representative events covering every dispatch arm that produces a span,
# pushes/pops state, or runs through the unknown-event path.
_EVENT_PAYLOADS = [
    (
        "sessionStart",
        {
            "hookEventName": "sessionStart",
            "conversation_id": "conv-1",
            "generation_id": "gen-1",
            "cwd": "/tmp/project",
        },
    ),
    (
        "beforeSubmitPrompt",
        {
            "hook_event_name": "beforeSubmitPrompt",
            "conversation_id": "conv-1",
            "generation_id": "gen-1",
            "prompt": "do the thing",
            "model_name": "claude-4",
        },
    ),
    (
        "beforeShellExecution",
        {
            "hook_event_name": "beforeShellExecution",
            "conversation_id": "conv-1",
            "generation_id": "gen-1",
            "command": "ls -la",
            "cwd": "/tmp",
        },
    ),
    (
        "afterShellExecution",
        {
            "hook_event_name": "afterShellExecution",
            "conversation_id": "conv-1",
            "generation_id": "gen-1",
            "output": "total 0",
            "exit_code": "0",
        },
    ),
    (
        "beforeReadFile",
        {
            "hook_event_name": "beforeReadFile",
            "conversation_id": "conv-1",
            "generation_id": "gen-1",
            "file_path": "/foo/bar.py",
        },
    ),
    (
        "afterFileEdit",
        {
            "hook_event_name": "afterFileEdit",
            "conversation_id": "conv-1",
            "generation_id": "gen-1",
            "file_path": "/foo/bar.py",
            "diff": "+added line",
        },
    ),
    (
        "afterAgentResponse",
        {
            "hook_event_name": "afterAgentResponse",
            "conversation_id": "conv-1",
            "generation_id": "gen-1",
            "response": "did the thing",
            "model_name": "claude-4",
        },
    ),
    (
        "afterAgentThought",
        {
            "hook_event_name": "afterAgentThought",
            "conversation_id": "conv-1",
            "generation_id": "gen-1",
            "thought": "thinking",
        },
    ),
    (
        "postToolUse",
        {
            "hookEventName": "postToolUse",
            "conversation_id": "conv-1",
            "generation_id": "gen-1",
            "tool_name": "grep",
            "tool_input": "pattern",
            "result": "matches",
        },
    ),
    (
        "stop",
        {
            "hook_event_name": "stop",
            "conversation_id": "conv-1",
            "generation_id": "gen-1",
            "status": "completed",
        },
    ),
    (
        "sessionEnd",
        {
            "hookEventName": "sessionEnd",
            "conversation_id": "conv-1",
            "generation_id": "gen-1",
            "final_status": "ok",
        },
    ),
    (
        "unknownEvent",
        {
            "hook_event_name": "unknownEvent",
            "conversation_id": "conv-1",
            "generation_id": "gen-1",
        },
    ),
]


@pytest.mark.parametrize("event,payload", _EVENT_PAYLOADS, ids=[e for e, _ in _EVENT_PAYLOADS])
def test_dispatch_writes_nothing_to_stdout(event, payload, capsys, monkeypatch):
    """Every Cursor hook event must leave stdout untouched.

    Cursor reads hook stdout as control JSON. Stray output from a tracing hook
    would be parsed as a permission/continue directive and could block the
    user's shell command or file read.
    """
    monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
    # Drain any output capsys may have buffered before this test's tool patches.
    capsys.readouterr()

    with mock.patch("tracing.cursor.hooks.handlers.send_span", return_value=True):
        _dispatch(event, payload)

    captured = capsys.readouterr()
    assert captured.out == "", f"event {event!r} wrote to stdout: {captured.out!r}"


def test_full_turn_writes_nothing_to_stdout(capsys, monkeypatch):
    """End-to-end IDE turn (submit → response → tool spans → stop) keeps stdout silent."""
    monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
    capsys.readouterr()

    base = {"conversation_id": "conv-X", "generation_id": "gen-X"}
    sequence = [
        ("beforeSubmitPrompt", {"hook_event_name": "beforeSubmitPrompt", "prompt": "p", **base}),
        (
            "beforeShellExecution",
            {"hook_event_name": "beforeShellExecution", "command": "echo hi", **base},
        ),
        (
            "afterShellExecution",
            {"hook_event_name": "afterShellExecution", "output": "hi", "exit_code": "0", **base},
        ),
        ("beforeReadFile", {"hook_event_name": "beforeReadFile", "file_path": "/x", **base}),
        (
            "afterAgentResponse",
            {"hook_event_name": "afterAgentResponse", "response": "done", **base},
        ),
        ("stop", {"hook_event_name": "stop", "status": "completed", **base}),
    ]

    with mock.patch("tracing.cursor.hooks.handlers.send_span", return_value=True):
        for event, payload in sequence:
            _dispatch(event, payload)

    captured = capsys.readouterr()
    assert captured.out == "", f"full turn wrote to stdout: {captured.out!r}"
