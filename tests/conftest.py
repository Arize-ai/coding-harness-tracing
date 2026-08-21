"""Shared pytest fixtures for coding-harness-tracing tests."""

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

# Ensure repo root is importable
REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT))


# Env-var prefixes that steer backend/target/project resolution (core.common).
# Any of these leaking in from the developer's shell can flip a test's resolved
# backend or project name (e.g. an exported ARIZE_API_KEY+ARIZE_SPACE_ID forces
# target="arize" and shadows a config's phoenix target). Cleared per test so the
# baseline is hermetic; a test that needs one sets it explicitly via monkeypatch.
_ISOLATED_ENV_PREFIXES = ("ARIZE_", "PHOENIX_", "OTEL_")
# ARIZE_LOG_FILE is not shell input — each adapter installs it via os.environ
# .setdefault() at import time (a per-harness log path), and TestLogFileEnv
# asserts that side effect. Keep it so the strip doesn't erase import artifacts.
_ISOLATED_ENV_EXEMPT = frozenset({"ARIZE_LOG_FILE"})


@pytest.fixture(autouse=True)
def isolate_config(tmp_path, monkeypatch):
    """Isolate every test from the developer's real ~/.arize/harness/config.json.

    Three independent leaks are closed here:

    1. ``core.config.load_config`` binds its path via ``from core.constants
       import CONFIG_FILE``, so patching ``core.constants.CONFIG_FILE`` (as
       ``tmp_harness_dir`` does) does NOT redirect it — the name ``load_config``
       actually reads is ``core.config.CONFIG_FILE``. Point that at a
       nonexistent path so config defaults to ``{}`` unless a test opts in
       (tests that need real config, like ``test_config.py``, re-patch it).
    2. ``core.common.env`` is a module-level singleton whose ``cached_property``
       config reads survive across tests. Clear them before and after each test
       so the next access re-reads the isolated config rather than a stale value
       cached from a sibling test.
    3. ``ARIZE_*`` / ``PHOENIX_*`` / ``OTEL_*`` env vars exported in the
       developer's shell steer backend, target, and project-name resolution.
       Strip them so a test's result never depends on who is running it; tests
       that exercise env precedence opt back in with ``monkeypatch.setenv``.
    """
    import os

    from core.common import env

    for key in [k for k in os.environ if k.startswith(_ISOLATED_ENV_PREFIXES) and k not in _ISOLATED_ENV_EXEMPT]:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr("core.config.CONFIG_FILE", tmp_path / "no-such-config.json")
    env.invalidate_caches()
    yield
    env.invalidate_caches()


@pytest.fixture
def tmp_harness_dir(tmp_path, monkeypatch):
    """Create the full ~/.arize/harness directory tree in a temp location.

    Monkeypatches core.constants so all code sees the temp paths.
    Returns the base directory Path.
    """
    base = tmp_path / ".arize" / "harness"
    for subdir in ["bin", "run", "logs", "state/claude-code", "state/codex", "state/cursor"]:
        (base / subdir).mkdir(parents=True)

    import core.constants as c

    monkeypatch.setattr(c, "BASE_DIR", base)
    monkeypatch.setattr(c, "CONFIG_FILE", base / "config.json")
    monkeypatch.setattr(c, "PID_DIR", base / "run")
    monkeypatch.setattr(c, "LOG_DIR", base / "logs")
    monkeypatch.setattr(c, "BIN_DIR", base / "bin")
    monkeypatch.setattr(c, "VENV_DIR", base / "venv")
    monkeypatch.setattr(c, "STATE_BASE_DIR", base / "state")
    return base


@pytest.fixture
def sample_config(tmp_harness_dir):
    """Write a known-good config.json into the temp harness dir.

    Returns the config dict.
    """
    config = {
        "harnesses": {
            "claude-code": {
                "project_name": "claude-code",
                "target": "phoenix",
                "endpoint": "http://localhost:6006",
                "api_key": "",
            },
            "codex": {
                "project_name": "codex",
                "target": "phoenix",
                "endpoint": "http://localhost:6006",
                "api_key": "",
                "collector": {"host": "127.0.0.1", "port": 4318},
            },
            "cursor": {
                "project_name": "cursor",
                "target": "phoenix",
                "endpoint": "http://localhost:6006",
                "api_key": "",
            },
        },
    }
    config_path = tmp_harness_dir / "config.json"
    with open(config_path, "w") as f:
        json.dump(config, f, indent=2)
    return config


class _CollectorHandler(BaseHTTPRequestHandler):
    """Minimal mock HTTP handler that records POSTed spans."""

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        if self.headers.get("Content-Type", "").startswith("application/json"):
            # Arize path: OTLP/JSON
            self.server._received.append(json.loads(body))
        else:
            # Phoenix path: OTLP binary protobuf — record raw bytes
            self.server._received.append(body)
        self.send_response(200)
        self.end_headers()

    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass  # silence request logging in test output


@pytest.fixture
def mock_collector():
    """Start a real HTTP server on a random port.

    Accepts POST /v1/traces (records the body: parsed JSON for
    application/json, raw bytes for protobuf) and GET /health (returns 200).
    Yields dict: {"url": "http://127.0.0.1:{port}", "received": [...], "port": int}
    Server is torn down after the test.
    """
    server = HTTPServer(("127.0.0.1", 0), _CollectorHandler)
    server._received = []
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield {"url": f"http://127.0.0.1:{port}", "received": server._received, "port": port}
    server.shutdown()


@pytest.fixture
def capture_log(tmp_path):
    """Provide a temp log file and a reader function.

    Returns (log_file_path, read_log_fn). read_log_fn() returns list of lines.
    """
    log_file = tmp_path / "test.log"

    def read_log():
        return log_file.read_text().splitlines() if log_file.exists() else []

    return log_file, read_log


# ---------------------------------------------------------------------------
# Inline fixture data (replaces tests/fixtures/*.json)
# ---------------------------------------------------------------------------


@pytest.fixture
def claude_session_start_input():
    """Claude Code session_start hook input."""
    return {"session_id": "sess-abc123", "cwd": "/home/user/project"}


@pytest.fixture
def claude_stop_input():
    """Claude Code stop hook input."""
    return {"session_id": "sess-abc123", "transcript_path": "/tmp/transcript.jsonl"}


@pytest.fixture
def cursor_before_submit_input():
    """Cursor beforeSubmitPrompt hook input."""
    return {
        "hook_event_name": "beforeSubmitPrompt",
        "conversation_id": "conv-1",
        "generation_id": "gen-1",
        "prompt": "fix the bug",
    }


@pytest.fixture
def cursor_after_shell_input():
    """Cursor afterShellExecution hook input."""
    return {
        "hook_event_name": "afterShellExecution",
        "conversation_id": "conv-1",
        "generation_id": "gen-1",
        "command": "ls -la",
        "output": "total 0",
        "exit_code": "0",
    }


@pytest.fixture
def golden_span():
    """Expected OTLP span structure for golden/snapshot tests."""
    return {
        "resourceSpans": [
            {
                "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "test-service"}}]},
                "scopeSpans": [
                    {
                        "scope": {"name": "test-scope"},
                        "spans": [
                            {
                                "traceId": "0123456789abcdef0123456789abcdef",
                                "spanId": "abcdef1234567890",
                                "name": "Turn 1",
                                "kind": 1,
                                "startTimeUnixNano": "1711987200000000000",
                                "endTimeUnixNano": "1711987201000000000",
                                "attributes": [
                                    {"key": "session.id", "value": {"stringValue": "sess-1"}},
                                    {"key": "input.value", "value": {"stringValue": "hello"}},
                                ],
                                "status": {"code": 1},
                            }
                        ],
                    }
                ],
            }
        ],
    }


SAMPLE_TRANSCRIPT_LINES = [
    '{"type": "user", "message": {"role": "user", "content": "fix the bug"}}',
    '{"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "I found the issue."}], "model": "claude-sonnet-4-20250514", "usage": {"input_tokens": 100, "output_tokens": 50, "cache_read_input_tokens": 10, "cache_creation_input_tokens": 5}}}',
    '{"type": "tool_use", "message": {"role": "assistant", "content": [{"type": "tool_use", "name": "Edit", "input": {"file": "main.py"}}]}}',
]


@pytest.fixture
def transcript_file(tmp_path):
    """Write the sample transcript to a temp file and return its path."""
    tf = tmp_path / "transcript.jsonl"
    tf.write_text("\n".join(SAMPLE_TRANSCRIPT_LINES) + "\n")
    return str(tf)
