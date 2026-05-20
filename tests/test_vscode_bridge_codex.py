"""Tests for core.vscode_bridge.codex — Codex buffer bridge helpers.

All tests mock codex_buffer_ctl so no real buffer process is needed.
"""

from __future__ import annotations

from unittest.mock import patch

# All payload keys that must be present in every CodexBufferPayload.
_PAYLOAD_KEYS = {"success", "error", "state", "host", "port", "pid"}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_CTL = "tracing.codex.codex_buffer_ctl"
_MOD = "core.vscode_bridge.codex"


def _assert_payload(payload: dict) -> None:
    """Assert *payload* contains every documented CodexBufferPayload key."""
    assert isinstance(payload, dict)
    assert _PAYLOAD_KEYS <= payload.keys(), f"missing keys: {_PAYLOAD_KEYS - payload.keys()}"


# ---------------------------------------------------------------------------
# buffer_status
# ---------------------------------------------------------------------------


class TestBufferStatus:
    """buffer_status() scenarios."""

    def test_running(self):
        """Health check passes and identity matches — state='running'."""
        with (
            patch(f"{_CTL}._resolve_host_port", return_value=("127.0.0.1", 9009)),
            patch(f"{_CTL}._health_check", return_value=True),
            patch(
                f"{_CTL}._health_identity",
                return_value={"pid": 42, "build_path": "/fake/codex_buffer.py"},
            ),
            patch(f"{_CTL}._expected_build_path", return_value="/fake/codex_buffer.py"),
            patch(f"{_CTL}._listener_pid", return_value=42),
        ):
            from core.vscode_bridge.codex import buffer_status

            result = buffer_status()

        _assert_payload(result)
        assert result["success"] is True
        assert result["state"] == "running"
        assert result["host"] == "127.0.0.1"
        assert result["port"] == 9009
        assert result["pid"] == 42

    def test_stopped(self):
        """Health check fails — state='stopped'."""
        with (
            patch(f"{_CTL}._resolve_host_port", return_value=("127.0.0.1", 9009)),
            patch(f"{_CTL}._health_check", return_value=False),
        ):
            from core.vscode_bridge.codex import buffer_status

            result = buffer_status()

        _assert_payload(result)
        assert result["success"] is True
        assert result["state"] == "stopped"

    def test_stale(self):
        """Health check passes but identity mismatches — state='stale'."""
        with (
            patch(f"{_CTL}._resolve_host_port", return_value=("127.0.0.1", 9009)),
            patch(f"{_CTL}._health_check", return_value=True),
            patch(
                f"{_CTL}._health_identity",
                return_value={"pid": 99, "build_path": "/other/codex_buffer.py"},
            ),
            patch(f"{_CTL}._expected_build_path", return_value="/fake/codex_buffer.py"),
            patch(f"{_CTL}._listener_pid", return_value=99),
        ):
            from core.vscode_bridge.codex import buffer_status

            result = buffer_status()

        _assert_payload(result)
        assert result["success"] is True
        assert result["state"] == "stale"
        assert result["pid"] == 99

    def test_unreachable(self):
        """resolve_host_port raises — success=False, state='unknown'."""
        with patch(
            f"{_CTL}._resolve_host_port",
            side_effect=RuntimeError("boom"),
        ):
            from core.vscode_bridge.codex import buffer_status

            result = buffer_status()

        _assert_payload(result)
        assert result["success"] is False
        assert result["state"] == "unknown"
        assert result["error"] == "buffer_unreachable"


# ---------------------------------------------------------------------------
# buffer_start
# ---------------------------------------------------------------------------


class TestBufferStart:
    """buffer_start() scenarios."""

    def test_start_success(self):
        """ctl.buffer_start returns True — success=True."""
        with (
            patch(f"{_CTL}._resolve_host_port", return_value=("127.0.0.1", 9009)),
            patch(f"{_CTL}.buffer_start", return_value=True),
            patch(f"{_CTL}._health_check", return_value=True),
            patch(
                f"{_CTL}._health_identity",
                return_value={"pid": 55, "build_path": "/fake/codex_buffer.py"},
            ),
            patch(f"{_CTL}._expected_build_path", return_value="/fake/codex_buffer.py"),
            patch(f"{_CTL}._listener_pid", return_value=55),
        ):
            from core.vscode_bridge.codex import buffer_start

            result = buffer_start()

        _assert_payload(result)
        assert result["success"] is True
        assert result["state"] == "running"
        assert result["host"] == "127.0.0.1"
        assert result["port"] == 9009

    def test_start_failure(self):
        """ctl.buffer_start returns False — success=False."""
        with (
            patch(f"{_CTL}._resolve_host_port", return_value=("127.0.0.1", 9009)),
            patch(f"{_CTL}.buffer_start", return_value=False),
            patch(f"{_CTL}._health_check", return_value=False),
        ):
            from core.vscode_bridge.codex import buffer_start

            result = buffer_start()

        _assert_payload(result)
        assert result["success"] is False
        assert result["error"] == "buffer_start_failed"

    def test_start_exception(self):
        """ctl.buffer_start raises — success=False, host/port still populated."""
        with (
            patch(f"{_CTL}._resolve_host_port", return_value=("127.0.0.1", 9009)),
            patch(f"{_CTL}.buffer_start", side_effect=RuntimeError("boom")),
        ):
            from core.vscode_bridge.codex import buffer_start

            result = buffer_start()

        _assert_payload(result)
        assert result["success"] is False
        assert result["error"] == "buffer_start_failed"
        assert result["host"] == "127.0.0.1"
        assert result["port"] == 9009


# ---------------------------------------------------------------------------
# buffer_stop
# ---------------------------------------------------------------------------


class TestBufferStop:
    """buffer_stop() scenarios."""

    def test_stop_success(self):
        """ctl.buffer_stop returns 'stopped' — success=True."""
        with (
            patch(f"{_CTL}._resolve_host_port", return_value=("127.0.0.1", 9009)),
            patch(f"{_CTL}.buffer_stop", return_value="stopped"),
        ):
            from core.vscode_bridge.codex import buffer_stop

            result = buffer_stop()

        _assert_payload(result)
        assert result["success"] is True
        assert result["state"] == "stopped"

    def test_stop_failure(self):
        """ctl.buffer_stop returns 'refused' — success=False."""
        with (
            patch(f"{_CTL}._resolve_host_port", return_value=("127.0.0.1", 9009)),
            patch(f"{_CTL}.buffer_stop", return_value="refused"),
            patch(f"{_CTL}._health_check", return_value=True),
            patch(
                f"{_CTL}._health_identity",
                return_value={"pid": 77, "build_path": "/other/codex_buffer.py"},
            ),
            patch(f"{_CTL}._expected_build_path", return_value="/fake/codex_buffer.py"),
            patch(f"{_CTL}._listener_pid", return_value=77),
        ):
            from core.vscode_bridge.codex import buffer_stop

            result = buffer_stop()

        _assert_payload(result)
        assert result["success"] is False
        assert result["error"] == "buffer_stop_failed"

    def test_stop_exception(self):
        """ctl.buffer_stop raises — success=False, host/port still populated."""
        with (
            patch(f"{_CTL}._resolve_host_port", return_value=("127.0.0.1", 9009)),
            patch(f"{_CTL}.buffer_stop", side_effect=RuntimeError("boom")),
        ):
            from core.vscode_bridge.codex import buffer_stop

            result = buffer_stop()

        _assert_payload(result)
        assert result["success"] is False
        assert result["error"] == "buffer_stop_failed"
        assert result["host"] == "127.0.0.1"
        assert result["port"] == 9009
