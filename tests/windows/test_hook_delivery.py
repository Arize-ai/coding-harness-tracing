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
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path


@unittest.skipUnless(os.name == "nt", "Windows-only integration test")
class TestClaudeHookDelivery(unittest.TestCase):
    """Verify Claude hook commands deliver OTLP/JSON.

    The registered path invokes Git Bash directly across Claude's command shell
    boundary described at https://code.claude.com/docs/en/hooks#exec-form-and-shell-form;
    it does not invoke the Claude CLI.
    """

    def test_registered_commands_deliver_span_via_git_bash(self) -> None:
        if not os.environ.get("WINDOWS_HOOK_TEST_HOME"):
            self.skipTest("set WINDOWS_HOOK_TEST_HOME to opt into the registered-command test")
        git_bash = os.environ.get("WINDOWS_GIT_BASH")
        self.assertTrue(git_bash, "WINDOWS_GIT_BASH must be set when Windows hook tests are opted in")
        self.assertTrue(Path(git_bash).is_file(), f"WINDOWS_GIT_BASH does not name a file: {git_bash}")
        test_home = os.environ.get("WINDOWS_HOOK_TEST_HOME")
        expected_home = Path.home()
        self.assertEqual(Path(test_home), expected_home)
        self.assertEqual(sys.prefix, str(expected_home / ".arize" / "harness" / "venv"))

        from tracing.claude_code.constants import SETTINGS_FILE
        from tracing.claude_code.install import _register_claude_hooks

        _register_claude_hooks()
        settings = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        settings_env = settings.get("env", {})
        self.assertIsInstance(settings_env, dict)
        self.assertEqual(settings_env.get("ARIZE_TRACE_ENABLED"), "true")

        from core.constants import CONFIG_FILE

        received: list[tuple[str, dict, dict[str, str]]] = []

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
        session_id = f"windows-test-{uuid.uuid4().hex}"

        try:
            config_path.parent.mkdir(parents=True, exist_ok=True)
            config_path.write_text(
                json.dumps(
                    {
                        "harnesses": {
                            "claude-code": {
                                "project_name": "windows-hook-test",
                                "target": "arize",
                                "endpoint": f"http://127.0.0.1:{server.server_address[1]}",
                                "api_key": "windows-test-key",
                                "space_id": "windows-test-space",
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )

            environment = {
                key: value for key, value in os.environ.items() if not key.startswith(("ARIZE_", "PHOENIX_", "OTEL_"))
            }
            environment.update(
                {
                    key: value
                    for key, value in settings_env.items()
                    if isinstance(key, str) and isinstance(value, str) and not key.startswith(("PHOENIX_", "OTEL_"))
                }
            )
            environment.update(
                {
                    "ARIZE_DRY_RUN": "false",
                    "ARIZE_LOG_PROMPTS": "true",
                    "ARIZE_LOG_TOOL_DETAILS": "true",
                    "ARIZE_LOG_TOOL_CONTENT": "true",
                }
            )

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
            attributes = {attribute["key"]: next(iter(attribute["value"].values())) for attribute in span["attributes"]}
            self.assertEqual(span["name"], "Turn 1")
            self.assertEqual(attributes["session.id"], session_id)
            self.assertEqual(attributes["project.name"], "windows-hook-test")
            self.assertEqual(attributes["arize.project.name"], "windows-hook-test")
            self.assertEqual(attributes["input.value"], "Windows hook test")
            self.assertEqual(attributes["output.value"], "Windows hook response")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            if original_config is None:
                config_path.unlink(missing_ok=True)
            else:
                config_path.write_bytes(original_config)
