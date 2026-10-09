"""Windows integration test for registered Claude hook commands."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Iterator

Received = list[tuple[str, dict, dict[str, str]]]


@contextmanager
def _collector(harness_names: list[str]) -> Iterator[Received]:
    """Run a local OTLP collector and point each harness's config entry at it.

    The developer's config.json is restored afterwards.
    """
    from core.constants import CONFIG_FILE

    received: Received = []

    class CollectorHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
            try:
                payload = json.loads(body.decode("utf-8"))
                headers = {key.lower(): value for key, value in self.headers.items()}
                received.append((self.path, payload, headers))
                self.send_response(200)
            except (UnicodeDecodeError, json.JSONDecodeError):
                self.send_response(400)
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), CollectorHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    config_path = Path(CONFIG_FILE)
    original_config = config_path.read_bytes() if config_path.exists() else None
    try:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(
            json.dumps(
                {
                    "harnesses": {
                        name: {
                            "project_name": "windows-hook-test",
                            "target": "arize",
                            "endpoint": f"http://127.0.0.1:{server.server_address[1]}",
                            "api_key": "windows-test-key",
                            "space_id": "windows-test-space",
                        }
                        for name in harness_names
                    }
                }
            ),
            encoding="utf-8",
        )
        yield received
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        if original_config is None:
            config_path.unlink(missing_ok=True)
        else:
            config_path.write_bytes(original_config)


def _hook_environment(extra: dict[str, str]) -> dict[str, str]:
    """The test's environment without tracing overrides, plus ``extra``."""
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith(("ARIZE_", "PHOENIX_", "OTEL_"))
    }
    environment.update(extra)
    environment.update(
        {
            "ARIZE_DRY_RUN": "false",
            "ARIZE_LOG_PROMPTS": "true",
            "ARIZE_LOG_TOOL_DETAILS": "true",
            "ARIZE_LOG_TOOL_CONTENT": "true",
        }
    )
    return environment


def _span_attributes(span: dict) -> dict:
    return {attribute["key"]: next(iter(attribute["value"].values())) for attribute in span["attributes"]}


def _opted_in_git_bash(test: unittest.TestCase) -> str:
    """Skip unless the Windows hook tests are opted in; return the Git Bash path."""
    if not os.environ.get("WINDOWS_HOOK_TEST_HOME"):
        test.skipTest("set WINDOWS_HOOK_TEST_HOME to opt into the registered-command test")
    git_bash = os.environ.get("WINDOWS_GIT_BASH")
    test.assertTrue(git_bash, "WINDOWS_GIT_BASH must be set when Windows hook tests are opted in")
    test.assertTrue(Path(git_bash).is_file(), f"WINDOWS_GIT_BASH does not name a file: {git_bash}")
    test_home = os.environ.get("WINDOWS_HOOK_TEST_HOME")
    expected_home = Path.home()
    test.assertEqual(Path(test_home), expected_home)
    test.assertEqual(sys.prefix, str(expected_home / ".arize" / "harness" / "venv"))
    return git_bash


@unittest.skipUnless(os.name == "nt", "Windows-only integration test")
class TestClaudeHookDelivery(unittest.TestCase):
    """Verify Claude hook commands deliver OTLP/JSON.

    The registered path invokes Git Bash directly across Claude's command shell
    boundary described at https://code.claude.com/docs/en/hooks#exec-form-and-shell-form;
    it does not invoke the Claude CLI.
    """

    def test_registered_commands_deliver_span_via_git_bash(self) -> None:
        git_bash = _opted_in_git_bash(self)

        from tracing.claude_code.constants import SETTINGS_FILE
        from tracing.claude_code.install import _register_claude_hooks

        _register_claude_hooks()
        settings = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        settings_env = settings.get("env", {})
        self.assertIsInstance(settings_env, dict)
        self.assertEqual(settings_env.get("ARIZE_TRACE_ENABLED"), "true")

        session_id = f"windows-test-{uuid.uuid4().hex}"
        hook_env = {
            key: value
            for key, value in settings_env.items()
            if isinstance(key, str) and isinstance(value, str) and not key.startswith(("PHOENIX_", "OTEL_"))
        }

        with _collector(["claude-code"]) as received:
            environment = _hook_environment(hook_env)

            with tempfile.TemporaryDirectory() as project_dir:
                events = [
                    {
                        "session_id": session_id,
                        "cwd": project_dir,
                        "hook_event_name": "SessionStart",
                    },
                    {
                        "session_id": session_id,
                        "cwd": project_dir,
                        "prompt": "Windows hook test",
                        "hook_event_name": "UserPromptSubmit",
                    },
                    {
                        "session_id": session_id,
                        "cwd": project_dir,
                        "last_assistant_message": "Windows hook response",
                        "hook_event_name": "Stop",
                    },
                ]
                failures = []
                for payload in events:
                    event = payload["hook_event_name"]
                    command = [git_bash, "-c", settings["hooks"][event][0]["hooks"][0]["command"]]
                    result = subprocess.run(
                        command,
                        input=json.dumps(payload),
                        text=True,
                        capture_output=True,
                        cwd=project_dir,
                        env=environment,
                        timeout=30,
                        check=False,
                    )
                    print(f"{event}: command={command!r} returncode={result.returncode} stderr={result.stderr!r}")
                    if result.returncode:
                        failures.append((event, result.returncode, result.stderr))
                print(f"received spans: {len(received)}")
                self.assertEqual(failures, [])

            self.assertEqual(len(received), 1)
            path, payload, headers = received[0]
            self.assertEqual(path, "/v1/traces")
            self.assertEqual(headers.get("content-type"), "application/json")
            span = payload["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
            self.assertTrue(span["traceId"])
            self.assertTrue(span["spanId"])
            attributes = _span_attributes(span)
            self.assertEqual(span["name"], "Turn 1")
            self.assertEqual(attributes["session.id"], session_id)
            self.assertEqual(attributes["project.name"], "windows-hook-test")
            self.assertEqual(attributes["arize.project.name"], "windows-hook-test")
            self.assertEqual(attributes["input.value"], "Windows hook test")
            self.assertEqual(attributes["output.value"], "Windows hook response")

    def test_registered_dispatcher_delivers_claude_and_codex_spans(self) -> None:
        """``arize-harness.exe hook …`` passes Claude's stdin and Codex's argv payload through.

        Claude Code runs hook commands through Git Bash; Codex execs its notify
        command directly with the event JSON as the last argument.
        """
        git_bash = _opted_in_git_bash(self)
        scripts_dir = Path(sys.prefix) / ("Scripts" if os.name == "nt" else "bin")
        dispatcher = scripts_dir / ("arize-harness.exe" if os.name == "nt" else "arize-harness")
        self.assertTrue(dispatcher.is_file(), f"arize-harness was not installed: {dispatcher}")

        session_id = f"windows-dispatch-{uuid.uuid4().hex}"
        thread_id = f"windows-codex-{uuid.uuid4().hex}"

        with _collector(["claude-code", "codex"]) as received, tempfile.TemporaryDirectory() as project_dir:
            codex_home = Path(project_dir) / "codex-home"
            codex_home.mkdir()
            environment = _hook_environment({"ARIZE_TRACE_ENABLED": "true", "CODEX_HOME": str(codex_home)})
            failures = []

            for event, payload in [
                ("session-start", {"session_id": session_id, "cwd": project_dir, "hook_event_name": "SessionStart"}),
                (
                    "user-prompt-submit",
                    {
                        "session_id": session_id,
                        "cwd": project_dir,
                        "prompt": "Windows dispatcher test",
                        "hook_event_name": "UserPromptSubmit",
                    },
                ),
                (
                    "stop",
                    {
                        "session_id": session_id,
                        "cwd": project_dir,
                        "last_assistant_message": "Windows dispatcher response",
                        "hook_event_name": "Stop",
                    },
                ),
            ]:
                command = [git_bash, "-c", f'"{dispatcher.as_posix()}" hook claude {event}']
                result = subprocess.run(
                    command,
                    input=json.dumps(payload),
                    text=True,
                    capture_output=True,
                    cwd=project_dir,
                    env=environment,
                    timeout=30,
                    check=False,
                )
                print(f"claude {event}: command={command!r} returncode={result.returncode} stderr={result.stderr!r}")
                if result.returncode:
                    failures.append((event, result.returncode, result.stderr))

            notify_payload = {
                "type": "agent-turn-complete",
                "thread-id": thread_id,
                "turn-id": "turn-1",
                "input-messages": ['Windows "codex" prompt'],
                "last-assistant-message": "Windows codex response",
            }
            command = [str(dispatcher), "hook", "codex", "notify", json.dumps(notify_payload)]
            result = subprocess.run(
                command,
                text=True,
                capture_output=True,
                cwd=project_dir,
                env=environment,
                timeout=30,
                check=False,
            )
            print(f"codex notify: command={command!r} returncode={result.returncode} stderr={result.stderr!r}")
            if result.returncode:
                failures.append(("codex notify", result.returncode, result.stderr))
            print(f"received spans: {len(received)}")
            self.assertEqual(failures, [])

        spans = [payload["resourceSpans"][0]["scopeSpans"][0]["spans"][0] for _, payload, _ in received]
        by_session = {_span_attributes(span)["session.id"]: _span_attributes(span) for span in spans}
        self.assertEqual(set(by_session), {session_id, thread_id})
        self.assertEqual(by_session[session_id]["input.value"], "Windows dispatcher test")
        self.assertEqual(by_session[session_id]["output.value"], "Windows dispatcher response")
        self.assertEqual(by_session[thread_id]["input.value"], 'Windows "codex" prompt')
        self.assertEqual(by_session[thread_id]["output.value"], "Windows codex response")
