"""Exercise the real shell installer with an isolated home and synthetic traces."""

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from tracing.codex._toml import _toml_load_strict, _toml_write


@pytest.mark.slow
def test_local_source_shell_install_dispatch_reinstall_uninstall(tmp_path):
    repo = Path(__file__).resolve().parents[3]
    home = tmp_path / "isolated home"
    codex_home = home / ".codex"
    harness_home = home / ".arize" / "harness"
    codex_home.mkdir(parents=True)
    harness_home.mkdir(parents=True)
    received = []

    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append((self.path, self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    # Fake only the desktop helper. The installed Arize entry point and export
    # run for real, and no production helper or user rollout is accessed.
    desktop = tmp_path / "Computer Use.app" / "SkyComputerUseClient"
    desktop.parent.mkdir()
    desktop.write_text(
        f"#!{sys.executable}\n"
        "import json, subprocess, sys\n"
        "assert sys.argv[1:3] == ['turn-ended', '--previous-notify']\n"
        "subprocess.run(json.loads(sys.argv[3]) + [sys.argv[4]], check=True)\n"
    )
    desktop.chmod(0o700)
    callback = tmp_path / "previous callback.py"
    forwarded = tmp_path / "forwarded.json"
    callback.write_text("import pathlib, sys\npathlib.Path(sys.argv[1]).write_text(sys.argv[2])\n")
    original = [
        str(desktop),
        "turn-ended",
        "--previous-notify",
        json.dumps([sys.executable, str(callback), str(forwarded)]),
    ]
    config_path = codex_home / "config.toml"
    _toml_write({"notify": original, "model": "preserve-me"}, config_path)
    (harness_home / "config.json").write_text(
        json.dumps(
            {
                "harnesses": {
                    "codex": {
                        "target": "phoenix",
                        "endpoint": f"http://127.0.0.1:{server.server_port}",
                        "api_key": "",
                        "project_name": "synthetic-installer-test",
                    }
                },
                "logging": {"prompts": True, "tool_details": True, "tool_content": True},
            }
        )
    )
    # A stale installed source file must not win over the freshly built package.
    stale = harness_home / "tracing" / "codex" / "install.py"
    stale.parent.mkdir(parents=True)
    stale.write_text("raise RuntimeError('stale source must not run')\n")
    env = {key: value for key, value in os.environ.items() if not key.startswith(("ARIZE_", "PHOENIX_", "CODEX_"))}
    env.update(
        HOME=str(home), CODEX_HOME=str(codex_home), ARIZE_SOURCE_DIR=str(repo), ARIZE_NONINTERACTIVE="1", NO_COLOR="1"
    )

    def run_installer(*args):
        result = subprocess.run(
            ["bash", str(repo / "install.sh"), *args],
            cwd=tmp_path,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=180,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "Syncing with" not in result.stdout
        assert "Downloading coding-harness" not in result.stdout
        return result

    try:
        run_installer("codex")
        command = _toml_load_strict(config_path)["notify"]
        hook = str(harness_home / "venv" / "bin" / "arize-hook-codex-notify")
        assert command[:3] == original[:3]
        assert json.loads(command[3]) == [hook, "--previous-notify", original[3]]
        payload = json.dumps(
            {
                "type": "agent-turn-complete",
                "thread-id": "synthetic-only",
                "turn-id": "t1",
                "input-messages": ["INSTALLER_TEST_42"],
                "last-assistant-message": "42",
            }
        )
        subprocess.run(command + [payload], env=env, check=True, timeout=30)
        assert forwarded.read_text() == payload
        assert len(received) == 1 and received[0][0] == "/v1/traces"
        assert b"INSTALLER_TEST_42" in received[0][1]  # protobuf contains this string
        records = [
            ("event_msg", {"type": "task_started", "turn_id": "t1"}),
            (
                "response_item",
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "INSTALLER_ROLLOUT_42"}],
                    "internal_chat_message_metadata_passthrough": {"content_item_kinds": ["user.text"]},
                },
            ),
            (
                "response_item",
                {
                    "type": "function_call",
                    "name": "exec_command",
                    "call_id": "c1",
                    "arguments": json.dumps({"cmd": 'python3 -c "print(6 * 7)"'}),
                },
            ),
            ("response_item", {"type": "function_call_output", "call_id": "c1", "output": "42"}),
            ("event_msg", {"type": "task_complete", "turn_id": "t1", "last_agent_message": "42"}),
        ]
        folder = codex_home / "sessions" / "2026" / "09" / "30"
        folder.mkdir(parents=True)
        rollout = folder / "rollout-2026-09-30T00-00-00-synthetic-only.jsonl"
        rollout.write_text(
            "\n".join(
                json.dumps({"type": kind, "payload": item, "timestamp": f"2026-09-30T00:00:0{index}.000Z"})
                for index, (kind, item) in enumerate(records)
            )
        )
        subprocess.run(command + [payload], env=env, check=True, timeout=30)
        assert len(received) == 2
        assert b"INSTALLER_ROLLOUT_42" in received[1][1]
        assert b"exec_command" in received[1][1] and b"print(6 * 7)" in received[1][1]
        assert b"codex.notify_fallback" not in received[1][1]
        check = subprocess.run(
            [str(harness_home / "venv" / "bin" / "python"), "-I", str(repo / "scripts" / "check-desktop-notify.py")],
            env=env,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        report = json.loads(check.stdout)
        assert report["chain_valid"] and report["desktop_helper_outermost"]
        assert report["project"] == "synthetic-installer-test"
        assert "api_key" not in report
        run_installer("codex")
        assert _toml_load_strict(config_path)["notify"] == command
        run_installer("update")
        assert _toml_load_strict(config_path)["notify"] == command
        run_installer("uninstall", "codex")
        assert _toml_load_strict(config_path) == {"notify": original, "model": "preserve-me"}
        assert stale.read_text() == "raise RuntimeError('stale source must not run')\n"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
