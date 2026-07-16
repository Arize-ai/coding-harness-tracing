"""Tests for tracing.devin.install — SessionEnd hook registration in Devin's config."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

# The hook command string install.py should write into config.json.
FAKE_VENV_BIN = Path("/fake/venv/bin/arize-hook-devin")
HOOK_CMD = str(FAKE_VENV_BIN)


@pytest.fixture(autouse=True)
def _mock_venv_bin(monkeypatch):
    """Make venv_bin() always return our fake path."""
    monkeypatch.setattr("tracing.devin.install.venv_bin", lambda name: FAKE_VENV_BIN)


@pytest.fixture(autouse=True)
def _no_dry_run(monkeypatch):
    """Default: dry-run is off."""
    monkeypatch.delenv("ARIZE_DRY_RUN", raising=False)


@pytest.fixture()
def config_file(tmp_path, monkeypatch):
    """Point CONFIG_FILE at a temp path (not yet created)."""
    path = tmp_path / "config" / "devin" / "config.json"
    monkeypatch.setattr("tracing.devin.install.CONFIG_FILE", path)
    return path


def _session_end_commands(config: dict) -> list:
    """Collect all hook commands registered under hooks.SessionEnd."""
    cmds = []
    for matcher in config.get("hooks", {}).get("SessionEnd", []):
        for h in matcher.get("hooks", []):
            cmds.append(h.get("command"))
    return cmds


class TestRegisterHooks:
    def test_preserves_unrelated_keys(self, config_file):
        from tracing.devin.install import _register_hooks

        config_file.parent.mkdir(parents=True, exist_ok=True)
        config_file.write_text(json.dumps({"version": 1, "theme_mode": "dark"}))

        _register_hooks()

        data = json.loads(config_file.read_text())
        assert data["version"] == 1
        assert data["theme_mode"] == "dark"
        assert _session_end_commands(data) == [HOOK_CMD]

    def test_hook_entry_shape(self, config_file):
        from tracing.devin.install import HOOK_TIMEOUT, _register_hooks

        _register_hooks()

        data = json.loads(config_file.read_text())
        matcher = data["hooks"]["SessionEnd"][0]
        assert matcher["hooks"][0] == {
            "type": "command",
            "command": HOOK_CMD,
            "timeout": HOOK_TIMEOUT,
        }

    def test_idempotent(self, config_file):
        from tracing.devin.install import _register_hooks

        _register_hooks()
        _register_hooks()

        data = json.loads(config_file.read_text())
        assert _session_end_commands(data) == [HOOK_CMD]

    def test_creates_missing_config(self, config_file):
        from tracing.devin.install import _register_hooks

        assert not config_file.exists()
        _register_hooks()

        assert config_file.exists()
        data = json.loads(config_file.read_text())
        # Only the hooks block — nothing else invented.
        assert list(data.keys()) == ["hooks"]
        assert _session_end_commands(data) == [HOOK_CMD]

    def test_rebuilds_on_malformed_config(self, config_file):
        from tracing.devin.install import _register_hooks

        config_file.parent.mkdir(parents=True, exist_ok=True)
        config_file.write_text("{not valid json!!!")

        _register_hooks()

        data = json.loads(config_file.read_text())
        assert _session_end_commands(data) == [HOOK_CMD]

    def test_dry_run_no_write(self, config_file, monkeypatch):
        from tracing.devin.install import _register_hooks

        monkeypatch.setenv("ARIZE_DRY_RUN", "true")
        _register_hooks()

        assert not config_file.exists()

    def test_preserves_other_events(self, config_file):
        from tracing.devin.install import _register_hooks

        config_file.parent.mkdir(parents=True, exist_ok=True)
        config_file.write_text(
            json.dumps(
                {
                    "hooks": {
                        "PreToolUse": [{"hooks": [{"type": "command", "command": "/usr/bin/other"}]}],
                    }
                }
            )
        )

        _register_hooks()

        data = json.loads(config_file.read_text())
        assert data["hooks"]["PreToolUse"][0]["hooks"][0]["command"] == "/usr/bin/other"
        assert _session_end_commands(data) == [HOOK_CMD]


class TestUnregisterHooks:
    def test_removes_command_and_drops_empty_hooks(self, config_file):
        from tracing.devin.install import _register_hooks, _unregister_hooks

        config_file.parent.mkdir(parents=True, exist_ok=True)
        config_file.write_text(json.dumps({"version": 1, "theme_mode": "dark"}))

        _register_hooks()
        _unregister_hooks()

        data = json.loads(config_file.read_text())
        # Our command gone; empty hooks key dropped; unrelated keys intact.
        assert "hooks" not in data
        assert data["version"] == 1
        assert data["theme_mode"] == "dark"

    def test_preserves_other_commands_in_event(self, config_file):
        from tracing.devin.install import _register_hooks, _unregister_hooks

        config_file.parent.mkdir(parents=True, exist_ok=True)
        config_file.write_text(
            json.dumps(
                {
                    "hooks": {
                        "SessionEnd": [{"hooks": [{"type": "command", "command": "/usr/bin/other"}]}],
                    }
                }
            )
        )

        _register_hooks()
        _unregister_hooks()

        data = json.loads(config_file.read_text())
        assert _session_end_commands(data) == ["/usr/bin/other"]

    def test_preserves_other_events(self, config_file):
        from tracing.devin.install import _register_hooks, _unregister_hooks

        config_file.parent.mkdir(parents=True, exist_ok=True)
        config_file.write_text(
            json.dumps(
                {
                    "hooks": {
                        "PreToolUse": [{"hooks": [{"type": "command", "command": "/usr/bin/other"}]}],
                    }
                }
            )
        )

        _register_hooks()
        _unregister_hooks()

        data = json.loads(config_file.read_text())
        assert "SessionEnd" not in data["hooks"]
        assert data["hooks"]["PreToolUse"][0]["hooks"][0]["command"] == "/usr/bin/other"

    def test_no_op_when_config_missing(self, config_file):
        from tracing.devin.install import _unregister_hooks

        # Should not raise or create the file.
        _unregister_hooks()
        assert not config_file.exists()

    def test_no_op_on_malformed_config(self, config_file):
        from tracing.devin.install import _unregister_hooks

        config_file.parent.mkdir(parents=True, exist_ok=True)
        config_file.write_text("{not valid json!!!")
        original = config_file.read_text()

        _unregister_hooks()

        assert config_file.read_text() == original

    def test_dry_run_no_write(self, config_file, monkeypatch):
        from tracing.devin.install import _register_hooks, _unregister_hooks

        _register_hooks()
        before = config_file.read_text()

        monkeypatch.setenv("ARIZE_DRY_RUN", "true")
        _unregister_hooks()

        assert config_file.read_text() == before
