#!/usr/bin/env python3
"""Tests for tracing.codex.hooks.stop -- the no-op Stop hook.

Span shipping moved to the notify handler under the deferred-parent
architecture. Stop is kept as a registered hook only so existing
``/hooks`` trust hashes stay valid; the body is a near no-op. These
tests verify it exits cleanly across the events Codex may fire it for.
"""

from __future__ import annotations

import json

import pytest

from tracing.codex.hooks.stop import main


@pytest.fixture(autouse=True)
def _enable_tracing(monkeypatch):
    monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")


class _StdinStub:
    def __init__(self, payload: str) -> None:
        self._payload = payload

    def read(self) -> str:
        return self._payload


def test_main_with_valid_payload_exits_clean(tmp_harness_dir, monkeypatch):
    payload = json.dumps({"hook_event_name": "Stop", "session_id": "thread-1"})
    monkeypatch.setattr("sys.stdin", _StdinStub(payload))
    # No span backend call should happen; the no-op Stop does not ship spans.
    main()  # must not raise


def test_main_with_empty_stdin_exits_clean(tmp_harness_dir, monkeypatch):
    monkeypatch.setattr("sys.stdin", _StdinStub(""))
    main()


def test_main_with_tracing_disabled_returns_early(tmp_harness_dir, monkeypatch):
    monkeypatch.setenv("ARIZE_TRACE_ENABLED", "false")
    payload = json.dumps({"session_id": "thread-1"})
    monkeypatch.setattr("sys.stdin", _StdinStub(payload))
    main()


def test_main_with_malformed_json_does_not_raise(tmp_harness_dir, monkeypatch):
    monkeypatch.setattr("sys.stdin", _StdinStub("not json"))
    main()  # caught by outer try/except, exits clean
