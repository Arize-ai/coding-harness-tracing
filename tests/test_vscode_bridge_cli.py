"""Tests for core.vscode_bridge.cli."""

from __future__ import annotations

import json
from io import StringIO
from unittest import mock

from core.vscode_bridge.cli import main

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(argv, monkeypatch):
    """Run the CLI with *argv* and return (exit_code, stdout_lines, stderr)."""
    stdout = StringIO()
    stderr = StringIO()
    monkeypatch.setattr("sys.stdout", stdout)
    monkeypatch.setattr("sys.stderr", stderr)
    code = main(argv)
    out_lines = [line for line in stdout.getvalue().splitlines() if line]
    return code, out_lines, stderr.getvalue()


def _parse_ndjson(lines):
    """Parse each line as JSON and return a list of dicts."""
    return [json.loads(line) for line in lines]


def _install_argv(**overrides):
    """Build a minimal install argv list."""
    defaults = {
        "--harness": "claude-code",
        "--target": "arize",
        "--endpoint": "https://otlp.arize.com",
        "--api-key": "test-key",
        "--space-id": "space-123",
        "--project-name": "my-project",
    }
    defaults.update(overrides)
    argv = ["install"]
    for k, v in defaults.items():
        if v is not None:
            argv.extend([k, v])
    return argv


# ---------------------------------------------------------------------------
# Argv parsing failures → exit 2
# ---------------------------------------------------------------------------


class TestArgvParsing:
    def test_no_command(self, monkeypatch):
        code, lines, stderr = _run([], monkeypatch)
        assert code == 2

    def test_unknown_command(self, monkeypatch):
        code, lines, stderr = _run(["bogus"], monkeypatch)
        assert code == 2

    def test_unknown_flag(self, monkeypatch):
        code, lines, stderr = _run(["status", "--bogus"], monkeypatch)
        assert code == 2

    def test_install_missing_required(self, monkeypatch):
        code, lines, stderr = _run(["install", "--harness", "codex"], monkeypatch)
        assert code == 2

    def test_unknown_harness(self, monkeypatch):
        code, lines, stderr = _run(
            [
                "install",
                "--harness",
                "unknown",
                "--target",
                "arize",
                "--endpoint",
                "x",
                "--api-key",
                "k",
                "--project-name",
                "p",
            ],
            monkeypatch,
        )
        assert code == 2


# ---------------------------------------------------------------------------
# Status subcommand
# ---------------------------------------------------------------------------


class TestStatus:
    @mock.patch("core.vscode_bridge.status.load_status")
    def test_happy_path(self, mock_load, monkeypatch):
        mock_load.return_value = {
            "success": True,
            "error": None,
            "user_id": None,
            "harnesses": [],
            "logging": None,
            "codex_buffer": None,
        }
        code, lines, stderr = _run(["status"], monkeypatch)
        assert code == 0
        events = _parse_ndjson(lines)
        assert len(events) == 1
        assert events[0]["event"] == "result"
        assert events[0]["payload"]["success"] is True

    @mock.patch("core.vscode_bridge.status.load_status")
    def test_failure(self, mock_load, monkeypatch):
        mock_load.return_value = {
            "success": False,
            "error": "config_malformed",
            "user_id": None,
            "harnesses": [],
            "logging": None,
            "codex_buffer": None,
        }
        code, lines, stderr = _run(["status"], monkeypatch)
        assert code == 1
        events = _parse_ndjson(lines)
        assert events[-1]["payload"]["success"] is False


# ---------------------------------------------------------------------------
# Install subcommand
# ---------------------------------------------------------------------------


class TestInstall:
    @mock.patch("core.vscode_bridge.install.install")
    def test_happy_path(self, mock_install, monkeypatch):
        mock_install.return_value = {
            "success": True,
            "error": None,
            "harness": "claude-code",
            "logs": ["Setting up claude-code", "Done"],
        }
        code, lines, stderr = _run(_install_argv(), monkeypatch)
        assert code == 0
        events = _parse_ndjson(lines)
        # Two log events + one result event
        assert len(events) == 3
        assert events[0]["event"] == "log"
        assert events[1]["event"] == "log"
        assert events[2]["event"] == "result"
        assert events[2]["payload"]["success"] is True

    @mock.patch("core.vscode_bridge.install.install")
    def test_logs_drained_before_result(self, mock_install, monkeypatch):
        mock_install.return_value = {
            "success": True,
            "error": None,
            "harness": "codex",
            "logs": ["line1", "line2", "line3"],
        }
        code, lines, stderr = _run(_install_argv(**{"--harness": "codex"}), monkeypatch)
        events = _parse_ndjson(lines)
        log_events = [e for e in events if e["event"] == "log"]
        result_events = [e for e in events if e["event"] == "result"]
        assert len(log_events) == 3
        assert len(result_events) == 1
        # Result is always the last line
        assert events[-1]["event"] == "result"

    def test_arize_missing_space_id(self, monkeypatch):
        """Install with --target=arize but no --space-id → exit 1, error=missing_credentials."""
        argv = _install_argv()
        # Remove --space-id
        argv = [
            "install",
            "--harness",
            "claude-code",
            "--target",
            "arize",
            "--endpoint",
            "https://otlp.arize.com",
            "--api-key",
            "test-key",
            "--project-name",
            "my-project",
        ]
        code, lines, stderr = _run(argv, monkeypatch)
        assert code == 1
        events = _parse_ndjson(lines)
        result = events[-1]
        assert result["event"] == "result"
        assert result["payload"]["success"] is False
        assert result["payload"]["error"] == "missing_credentials"

    @mock.patch("core.vscode_bridge.install.install")
    def test_phoenix_empty_api_key(self, mock_install, monkeypatch):
        """--api-key '' is valid for phoenix."""
        mock_install.return_value = {
            "success": True,
            "error": None,
            "harness": "cursor",
            "logs": [],
        }
        argv = [
            "install",
            "--harness",
            "cursor",
            "--target",
            "phoenix",
            "--endpoint",
            "http://localhost:6006",
            "--api-key",
            "",
            "--project-name",
            "my-project",
        ]
        code, lines, stderr = _run(argv, monkeypatch)
        assert code == 0

    @mock.patch("core.vscode_bridge.install.install")
    def test_install_failure(self, mock_install, monkeypatch):
        mock_install.return_value = {
            "success": False,
            "error": "install_failed",
            "harness": "copilot",
            "logs": ["traceback..."],
        }
        code, lines, stderr = _run(_install_argv(**{"--harness": "copilot"}), monkeypatch)
        assert code == 1
        events = _parse_ndjson(lines)
        assert events[-1]["payload"]["success"] is False

    @mock.patch("core.vscode_bridge.install.install")
    def test_with_logging_flags(self, mock_install, monkeypatch):
        mock_install.return_value = {
            "success": True,
            "error": None,
            "harness": "gemini",
            "logs": [],
        }
        argv = _install_argv(**{"--harness": "gemini"})
        argv.extend(["--log-prompts", "false", "--log-tool-details", "true", "--log-tool-content", "false"])
        code, lines, stderr = _run(argv, monkeypatch)
        assert code == 0
        # Verify the install function received the right logging block
        call_args = mock_install.call_args[0][0]
        assert call_args["logging"] == {
            "prompts": False,
            "tool_details": True,
            "tool_content": False,
        }

    @mock.patch("core.vscode_bridge.install.install")
    def test_with_skills_flag(self, mock_install, monkeypatch):
        mock_install.return_value = {
            "success": True,
            "error": None,
            "harness": "claude-code",
            "logs": [],
        }
        argv = _install_argv()
        argv.append("--with-skills")
        code, lines, stderr = _run(argv, monkeypatch)
        assert code == 0
        call_args = mock_install.call_args[0][0]
        assert call_args["with_skills"] is True


# ---------------------------------------------------------------------------
# Install subcommand — Kiro-specific flags
# ---------------------------------------------------------------------------


class TestInstallKiroFlags:
    @mock.patch("core.vscode_bridge.install.install")
    def test_install_kiro_with_agent_name_flag(self, mock_install, monkeypatch):
        mock_install.return_value = {
            "success": True,
            "error": None,
            "harness": "kiro",
            "logs": [],
        }
        argv = [
            "install",
            "--harness",
            "kiro",
            "--target",
            "phoenix",
            "--endpoint",
            "http://x",
            "--api-key",
            "",
            "--project-name",
            "p",
            "--agent-name",
            "my-agent",
        ]
        code, lines, stderr = _run(argv, monkeypatch)
        assert code == 0
        call_args = mock_install.call_args[0][0]
        assert call_args["kiro_options"] is not None
        assert call_args["kiro_options"]["agent_name"] == "my-agent"
        assert call_args["kiro_options"]["set_default"] is False

    @mock.patch("core.vscode_bridge.install.install")
    def test_install_kiro_with_set_default_flag(self, mock_install, monkeypatch):
        mock_install.return_value = {
            "success": True,
            "error": None,
            "harness": "kiro",
            "logs": [],
        }
        argv = [
            "install",
            "--harness",
            "kiro",
            "--target",
            "phoenix",
            "--endpoint",
            "http://x",
            "--api-key",
            "",
            "--project-name",
            "p",
            "--set-default",
        ]
        code, lines, stderr = _run(argv, monkeypatch)
        assert code == 0
        call_args = mock_install.call_args[0][0]
        assert call_args["kiro_options"] is not None
        assert call_args["kiro_options"]["set_default"] is True
        assert call_args["kiro_options"]["agent_name"] == "arize-traced"

    @mock.patch("core.vscode_bridge.install.install")
    def test_install_kiro_without_flags_omits_options(self, mock_install, monkeypatch):
        mock_install.return_value = {
            "success": True,
            "error": None,
            "harness": "kiro",
            "logs": [],
        }
        argv = [
            "install",
            "--harness",
            "kiro",
            "--target",
            "phoenix",
            "--endpoint",
            "http://x",
            "--api-key",
            "",
            "--project-name",
            "p",
        ]
        code, lines, stderr = _run(argv, monkeypatch)
        assert code == 0
        call_args = mock_install.call_args[0][0]
        assert call_args["kiro_options"] is None

    @mock.patch("core.vscode_bridge.install.install")
    def test_install_non_kiro_ignores_kiro_flags(self, mock_install, monkeypatch):
        mock_install.return_value = {
            "success": True,
            "error": None,
            "harness": "codex",
            "logs": [],
        }
        argv = [
            "install",
            "--harness",
            "codex",
            "--target",
            "phoenix",
            "--endpoint",
            "http://x",
            "--api-key",
            "",
            "--project-name",
            "p",
            "--agent-name",
            "my-agent",
        ]
        code, lines, stderr = _run(argv, monkeypatch)
        assert code == 0
        call_args = mock_install.call_args[0][0]
        assert call_args["kiro_options"] is None


# ---------------------------------------------------------------------------
# Install subcommand — Copilot-specific --repo-path flag
# ---------------------------------------------------------------------------


class TestInstallRepoPath:
    @mock.patch("core.vscode_bridge.install.install")
    def test_install_copilot_with_repo_path_flag(self, mock_install, monkeypatch):
        mock_install.return_value = {
            "success": True,
            "error": None,
            "harness": "copilot",
            "logs": [],
        }
        argv = [
            "install",
            "--harness",
            "copilot",
            "--target",
            "arize",
            "--endpoint",
            "https://otlp.arize.com",
            "--api-key",
            "k",
            "--space-id",
            "s",
            "--project-name",
            "copilot",
            "--repo-path",
            "/repo/a",
        ]
        code, lines, stderr = _run(argv, monkeypatch)
        assert code == 0
        mock_install.assert_called_once()
        call_args = mock_install.call_args[0][0]
        assert call_args["repo_path"] == "/repo/a"

    @mock.patch("core.vscode_bridge.install.install")
    def test_install_without_repo_path_flag_defaults_to_none(self, mock_install, monkeypatch):
        mock_install.return_value = {
            "success": True,
            "error": None,
            "harness": "copilot",
            "logs": [],
        }
        argv = [
            "install",
            "--harness",
            "copilot",
            "--target",
            "arize",
            "--endpoint",
            "https://otlp.arize.com",
            "--api-key",
            "k",
            "--space-id",
            "s",
            "--project-name",
            "copilot",
        ]
        code, lines, stderr = _run(argv, monkeypatch)
        assert code == 0
        mock_install.assert_called_once()
        call_args = mock_install.call_args[0][0]
        assert call_args["repo_path"] is None

    @mock.patch("core.vscode_bridge.install.install")
    def test_install_repo_path_on_non_copilot_rejected_by_build_install_request(self, mock_install, monkeypatch):
        argv = [
            "install",
            "--harness",
            "claude-code",
            "--target",
            "arize",
            "--endpoint",
            "https://otlp.arize.com",
            "--api-key",
            "k",
            "--space-id",
            "s",
            "--project-name",
            "p",
            "--repo-path",
            "/repo/a",
        ]
        code, lines, stderr = _run(argv, monkeypatch)
        assert code == 1
        events = _parse_ndjson(lines)
        result = events[-1]
        assert result["event"] == "result"
        assert result["payload"]["success"] is False
        mock_install.assert_not_called()


# ---------------------------------------------------------------------------
# Uninstall subcommand
# ---------------------------------------------------------------------------


class TestUninstall:
    @mock.patch("core.vscode_bridge.install.uninstall")
    def test_happy_path(self, mock_uninstall, monkeypatch):
        mock_uninstall.return_value = {
            "success": True,
            "error": None,
            "harness": "codex",
            "logs": ["removed"],
        }
        code, lines, stderr = _run(["uninstall", "--harness", "codex"], monkeypatch)
        assert code == 0
        events = _parse_ndjson(lines)
        assert events[-1]["event"] == "result"
        assert events[-1]["payload"]["success"] is True
        mock_uninstall.assert_called_once_with("codex")

    @mock.patch("core.vscode_bridge.install.uninstall")
    def test_failure(self, mock_uninstall, monkeypatch):
        mock_uninstall.return_value = {
            "success": False,
            "error": "install_failed",
            "harness": "cursor",
            "logs": ["error detail"],
        }
        code, lines, stderr = _run(["uninstall", "--harness", "cursor"], monkeypatch)
        assert code == 1


class TestSetUserId:
    @mock.patch("core.vscode_bridge.install.set_user_id")
    def test_happy_path(self, mock_set, monkeypatch):
        mock_set.return_value = {
            "success": True,
            "error": None,
            "harness": None,
            "logs": [],
        }
        code, lines, stderr = _run(["set-user-id", "--user-id", "alice"], monkeypatch)
        assert code == 0
        events = _parse_ndjson(lines)
        assert events[-1]["event"] == "result"
        assert events[-1]["payload"]["success"] is True
        mock_set.assert_called_once_with("alice")

    @mock.patch("core.vscode_bridge.install.set_user_id")
    def test_clear_when_user_id_empty(self, mock_set, monkeypatch):
        mock_set.return_value = {
            "success": True,
            "error": None,
            "harness": None,
            "logs": [],
        }
        code, _, _ = _run(["set-user-id", "--user-id", ""], monkeypatch)
        assert code == 0
        mock_set.assert_called_once_with("")

    @mock.patch("core.vscode_bridge.install.set_user_id")
    def test_user_id_default_is_empty(self, mock_set, monkeypatch):
        mock_set.return_value = {
            "success": True,
            "error": None,
            "harness": None,
            "logs": [],
        }
        code, _, _ = _run(["set-user-id"], monkeypatch)
        assert code == 0
        mock_set.assert_called_once_with("")


# ---------------------------------------------------------------------------
# Codex buffer subcommands
# ---------------------------------------------------------------------------


class TestCodexBuffer:
    @mock.patch("core.vscode_bridge.codex.buffer_status")
    def test_status(self, mock_fn, monkeypatch):
        mock_fn.return_value = {
            "success": True,
            "error": None,
            "state": "running",
            "host": "127.0.0.1",
            "port": 9999,
            "pid": 42,
        }
        code, lines, stderr = _run(["codex-buffer-status"], monkeypatch)
        assert code == 0
        events = _parse_ndjson(lines)
        assert events[0]["payload"]["state"] == "running"

    @mock.patch("core.vscode_bridge.codex.buffer_start")
    def test_start(self, mock_fn, monkeypatch):
        mock_fn.return_value = {
            "success": True,
            "error": None,
            "state": "running",
            "host": "127.0.0.1",
            "port": 9999,
            "pid": 100,
        }
        code, lines, stderr = _run(["codex-buffer-start"], monkeypatch)
        assert code == 0

    @mock.patch("core.vscode_bridge.codex.buffer_stop")
    def test_stop(self, mock_fn, monkeypatch):
        mock_fn.return_value = {
            "success": True,
            "error": None,
            "state": "stopped",
            "host": "127.0.0.1",
            "port": 9999,
            "pid": None,
        }
        code, lines, stderr = _run(["codex-buffer-stop"], monkeypatch)
        assert code == 0

    @mock.patch("core.vscode_bridge.codex.buffer_status")
    def test_buffer_failure(self, mock_fn, monkeypatch):
        mock_fn.return_value = {
            "success": False,
            "error": "buffer_unreachable",
            "state": "unknown",
            "host": None,
            "port": None,
            "pid": None,
        }
        code, lines, stderr = _run(["codex-buffer-status"], monkeypatch)
        assert code == 1


# ---------------------------------------------------------------------------
# NDJSON framing invariants
# ---------------------------------------------------------------------------


class TestNDJSONFraming:
    @mock.patch("core.vscode_bridge.status.load_status")
    def test_stdout_is_valid_ndjson(self, mock_load, monkeypatch):
        mock_load.return_value = {
            "success": True,
            "error": None,
            "user_id": None,
            "harnesses": [],
            "logging": None,
            "codex_buffer": None,
        }
        code, lines, stderr = _run(["status"], monkeypatch)
        for line in lines:
            parsed = json.loads(line)
            assert "event" in parsed

    @mock.patch("core.vscode_bridge.install.install")
    def test_result_always_last(self, mock_install, monkeypatch):
        mock_install.return_value = {
            "success": True,
            "error": None,
            "harness": "claude-code",
            "logs": ["a", "b", "c"],
        }
        code, lines, stderr = _run(_install_argv(), monkeypatch)
        events = _parse_ndjson(lines)
        result_indices = [i for i, e in enumerate(events) if e["event"] == "result"]
        assert len(result_indices) == 1
        assert result_indices[0] == len(events) - 1

    @mock.patch("core.vscode_bridge.install.install")
    def test_exactly_one_result_event(self, mock_install, monkeypatch):
        mock_install.return_value = {
            "success": True,
            "error": None,
            "harness": "codex",
            "logs": [],
        }
        code, lines, stderr = _run(_install_argv(**{"--harness": "codex"}), monkeypatch)
        events = _parse_ndjson(lines)
        result_count = sum(1 for e in events if e["event"] == "result")
        assert result_count == 1
