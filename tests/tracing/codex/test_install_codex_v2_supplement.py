#!/usr/bin/env python3
"""Supplementary tests for the v2-hooks installer rewrite.

These tests cover gaps in the existing test_install_codex.py suite:

- Per-entry hook metadata (type="command", timeout=30)
- v1 otel-exporter block stripping during install (migration path)
- install() / uninstall() integration with cleanup_legacy_install
- _entry_targets_cmd helper unit tests
- Full hook-event coverage matches the spec list
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

import tracing.codex.install as codex_install
from tracing.codex.constants import NOTIFY_BIN_NAME, SESSION_BIN_NAME, STOP_BIN_NAME, TOOL_BIN_NAME

PHOENIX_BACKEND = ("phoenix", {"endpoint": "http://localhost:6006", "api_key": ""})

EXPECTED_HOOK_EVENTS = (
    "SessionStart",
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "PermissionRequest",
    "Stop",
)


@pytest.fixture()
def fake_home(tmp_path, monkeypatch):
    """Redirect all paths to a temp directory — mirrors the main test fixture."""
    install_dir = tmp_path / ".arize" / "harness"
    install_dir.mkdir(parents=True)
    config_file = install_dir / "config.yaml"
    codex_dir = tmp_path / ".codex"
    venv_bin_dir = install_dir / "venv" / "bin"
    venv_bin_dir.mkdir(parents=True)

    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))

    monkeypatch.setattr("core.setup.INSTALL_DIR", install_dir)
    monkeypatch.setattr("core.setup.CONFIG_FILE", config_file)
    monkeypatch.setattr("core.setup.VENV_DIR", install_dir / "venv")
    monkeypatch.setattr("core.setup.BIN_DIR", install_dir / "bin")
    monkeypatch.setattr("core.setup.RUN_DIR", install_dir / "run")
    monkeypatch.setattr("core.setup.LOG_DIR", install_dir / "logs")
    monkeypatch.setattr("core.setup.STATE_DIR", install_dir / "state")

    monkeypatch.setattr("core.constants.CONFIG_FILE", config_file)
    monkeypatch.setattr("core.config.CONFIG_FILE", config_file)

    monkeypatch.setattr(codex_install, "CODEX_CONFIG_DIR", codex_dir)
    monkeypatch.setattr(codex_install, "CODEX_CONFIG_FILE", codex_dir / "config.toml")
    monkeypatch.setattr(codex_install, "CODEX_ENV_FILE", codex_dir / "arize-env.sh")
    monkeypatch.setattr(codex_install, "CONFIG_FILE", config_file)

    return tmp_path


@pytest.fixture(autouse=True)
def _stub_logging_prompts(monkeypatch):
    monkeypatch.setattr(
        codex_install,
        "prompt_content_logging",
        lambda: {"prompts": True, "tool_details": True, "tool_content": True},
    )
    monkeypatch.setattr(codex_install, "write_logging_config", lambda block, config_path=None: None)


@pytest.fixture()
def mock_prompts(monkeypatch):
    monkeypatch.setattr(codex_install, "prompt_project_name", lambda default: default)
    monkeypatch.setattr(codex_install, "prompt_user_id", lambda: "")
    monkeypatch.setattr(
        codex_install,
        "prompt_backend",
        lambda existing_harnesses=None: PHOENIX_BACKEND,
    )


def _venv_bin(fake_home: Path, name: str) -> str:
    return str(fake_home / ".arize" / "harness" / "venv" / "bin" / name)


def _all_hook_entries(data: dict, event: str) -> list[dict]:
    """All `{type, command, timeout}` dicts under [[hooks.<event>]]."""
    out: list[dict] = []
    for entry in data.get("hooks", {}).get(event, []):
        for h in entry.get("hooks", []):
            if isinstance(h, dict):
                out.append(h)
    return out


# ---------------------------------------------------------------------------
# Hook entry metadata
# ---------------------------------------------------------------------------


class TestHookEntryMetadata:
    """Each [[hooks.<Event>]] entry must declare type='command' and timeout=30."""

    def test_every_hook_entry_has_command_type_and_30s_timeout(self, fake_home, mock_prompts):
        codex_install.install()
        data = codex_install._toml_load(fake_home / ".codex" / "config.toml")

        for event in EXPECTED_HOOK_EVENTS:
            entries = _all_hook_entries(data, event)
            assert entries, f"missing hook entries for {event}"
            for h in entries:
                assert h["type"] == "command", f"{event}: expected type=command, got {h.get('type')!r}"
                assert h["timeout"] == 30, f"{event}: expected timeout=30, got {h.get('timeout')!r}"
                assert isinstance(h["command"], str) and h["command"], f"{event}: missing command string"

    def test_session_and_userpromptsubmit_share_same_command(self, fake_home, mock_prompts):
        codex_install.install()
        data = codex_install._toml_load(fake_home / ".codex" / "config.toml")

        session_cmd = _all_hook_entries(data, "SessionStart")[0]["command"]
        prompt_cmd = _all_hook_entries(data, "UserPromptSubmit")[0]["command"]
        assert session_cmd == prompt_cmd == _venv_bin(fake_home, SESSION_BIN_NAME)

    def test_pre_post_permission_share_tool_command(self, fake_home, mock_prompts):
        codex_install.install()
        data = codex_install._toml_load(fake_home / ".codex" / "config.toml")

        expected = _venv_bin(fake_home, TOOL_BIN_NAME)
        for event in ("PreToolUse", "PostToolUse", "PermissionRequest"):
            cmd = _all_hook_entries(data, event)[0]["command"]
            assert cmd == expected, f"{event}: expected {expected}, got {cmd}"

    def test_stop_uses_stop_command(self, fake_home, mock_prompts):
        codex_install.install()
        data = codex_install._toml_load(fake_home / ".codex" / "config.toml")
        assert _all_hook_entries(data, "Stop")[0]["command"] == _venv_bin(fake_home, STOP_BIN_NAME)

    def test_no_unexpected_hook_events_written(self, fake_home, mock_prompts):
        codex_install.install()
        data = codex_install._toml_load(fake_home / ".codex" / "config.toml")
        written_events = set(data.get("hooks", {}).keys())
        # We may want this to be a subset/equality — the installer should only write
        # the six events listed in the spec.
        assert written_events == set(
            EXPECTED_HOOK_EVENTS
        ), f"unexpected hook events: {written_events.symmetric_difference(set(EXPECTED_HOOK_EVENTS))}"


# ---------------------------------------------------------------------------
# v1 -> v2 migration: otel block stripping inside _codex_toml_apply
# ---------------------------------------------------------------------------


class TestV1OtelStripping:
    """When the existing config.toml has a v1 otel exporter block pointing
    at the local buffer, install() must strip it (the buffer is gone in v2)."""

    def test_install_strips_v1_otel_block_pointing_at_buffer(self, fake_home, mock_prompts):
        toml_path = fake_home / ".codex" / "config.toml"
        toml_path.parent.mkdir(parents=True, exist_ok=True)
        toml_path.write_text(
            "[otel.exporter.otlp-http]\n" 'endpoint = "http://127.0.0.1:4318/v1/logs"\n' 'protocol = "json"\n'
        )

        codex_install.install()

        data = codex_install._toml_load(toml_path)
        assert "otel" not in data, "v1 otel exporter block was not stripped"

    def test_install_strips_v1_otel_block_with_custom_port(self, fake_home, mock_prompts):
        """The regex matches any 127.0.0.1:<port>/v1/logs endpoint."""
        toml_path = fake_home / ".codex" / "config.toml"
        toml_path.parent.mkdir(parents=True, exist_ok=True)
        toml_path.write_text(
            "[otel.exporter.otlp-http]\n" 'endpoint = "http://127.0.0.1:4319/v1/logs"\n' 'protocol = "json"\n'
        )

        codex_install.install()
        data = codex_install._toml_load(toml_path)
        assert "otel" not in data

    def test_install_preserves_foreign_otel_block(self, fake_home, mock_prompts):
        """An otel block pointing at a non-buffer endpoint must survive."""
        toml_path = fake_home / ".codex" / "config.toml"
        toml_path.parent.mkdir(parents=True, exist_ok=True)
        toml_path.write_text(
            "[otel.exporter.otlp-http]\n"
            'endpoint = "https://my-otel-collector.example.com/v1/logs"\n'
            'protocol = "json"\n'
        )

        codex_install.install()
        data = codex_install._toml_load(toml_path)
        assert data["otel"]["exporter"]["otlp-http"]["endpoint"] == ("https://my-otel-collector.example.com/v1/logs")

    def test_install_strips_only_otlp_http_under_otel(self, fake_home, mock_prompts):
        """An unrelated key under [otel] should not be deleted."""
        toml_path = fake_home / ".codex" / "config.toml"
        toml_path.parent.mkdir(parents=True, exist_ok=True)
        toml_path.write_text(
            "[otel]\n"
            'some_other_key = "value"\n'
            "\n"
            "[otel.exporter.otlp-http]\n"
            'endpoint = "http://127.0.0.1:4318/v1/logs"\n'
            'protocol = "json"\n'
        )

        codex_install.install()
        data = codex_install._toml_load(toml_path)
        # The otlp-http exporter is gone but the sibling key survives.
        assert "otlp-http" not in data.get("otel", {}).get("exporter", {})
        assert data["otel"]["some_other_key"] == "value"

    def test_uninstall_strips_v1_otel_block_when_v2_was_never_installed(self, fake_home, mock_prompts):
        """A v1 user who never ran v2 install still has the legacy [otel.exporter.otlp-http]
        block. Running uninstall must strip it via cleanup_legacy_install — otherwise the
        config is left in a half-uninstalled state pointing at a defunct buffer service."""
        toml_path = fake_home / ".codex" / "config.toml"
        toml_path.parent.mkdir(parents=True, exist_ok=True)
        notify_cmd = _venv_bin(fake_home, NOTIFY_BIN_NAME)
        toml_path.write_text(
            f"notify = [{notify_cmd!r}]\n"
            "\n"
            "[otel.exporter.otlp-http]\n"
            'endpoint = "http://127.0.0.1:4318/v1/logs"\n'
            'protocol = "json"\n'
        )

        codex_install.uninstall()

        data = codex_install._toml_load(toml_path)
        assert "otel" not in data, "v1 otel block survived uninstall"
        assert "notify" not in data, "v1 notify entry should also be removed on uninstall"


# ---------------------------------------------------------------------------
# install/uninstall integration with cleanup_legacy_install
# ---------------------------------------------------------------------------


class TestLegacyCleanupIntegration:
    """Verify the install path calls cleanup_legacy_install() before writing
    the new layout (and uninstall calls it first too)."""

    def test_install_invokes_cleanup_legacy(self, fake_home, mock_prompts):
        with patch.object(codex_install, "cleanup_legacy_install") as m:
            codex_install.install()
            m.assert_called_once_with(codex_install.CODEX_CONFIG_FILE)

    def test_uninstall_invokes_cleanup_legacy(self, fake_home, mock_prompts):
        codex_install.install()
        with patch.object(codex_install, "cleanup_legacy_install") as m:
            codex_install.uninstall()
            m.assert_called_once_with(codex_install.CODEX_CONFIG_FILE)

    def test_cleanup_legacy_runs_before_toml_apply(self, fake_home, mock_prompts):
        """Order matters: legacy cleanup must happen BEFORE writing the new
        TOML — otherwise the new entries could be clobbered."""
        call_order: list[str] = []

        original_cleanup = codex_install.cleanup_legacy_install
        original_apply = codex_install._codex_toml_apply

        def trace_cleanup(*args, **kwargs):
            call_order.append("cleanup")
            return original_cleanup(*args, **kwargs)

        def trace_apply(*args, **kwargs):
            call_order.append("apply")
            return original_apply(*args, **kwargs)

        with patch.object(codex_install, "cleanup_legacy_install", side_effect=trace_cleanup):
            with patch.object(codex_install, "_codex_toml_apply", side_effect=trace_apply):
                codex_install.install()

        assert call_order.index("cleanup") < call_order.index(
            "apply"
        ), f"cleanup must precede toml apply, got order: {call_order}"


# ---------------------------------------------------------------------------
# _entry_targets_cmd helper unit tests
# ---------------------------------------------------------------------------


class TestEntryTargetsCmd:
    """Unit tests for the inner-command matcher used by apply / remove_v2."""

    def test_matches_when_command_present(self):
        entry = {"hooks": [{"type": "command", "command": "/venv/bin/x", "timeout": 30}]}
        assert codex_install._entry_targets_cmd(entry, "/venv/bin/x") is True

    def test_no_match_when_command_differs(self):
        entry = {"hooks": [{"type": "command", "command": "/venv/bin/x", "timeout": 30}]}
        assert codex_install._entry_targets_cmd(entry, "/venv/bin/other") is False

    def test_no_match_when_entry_not_dict(self):
        assert codex_install._entry_targets_cmd("not-a-dict", "/venv/bin/x") is False
        assert codex_install._entry_targets_cmd(None, "/venv/bin/x") is False
        assert codex_install._entry_targets_cmd(["list"], "/venv/bin/x") is False

    def test_no_match_when_hooks_not_list(self):
        entry = {"hooks": "not-a-list"}
        assert codex_install._entry_targets_cmd(entry, "/venv/bin/x") is False

    def test_no_match_when_inner_not_dict(self):
        entry = {"hooks": ["not-a-dict"]}
        assert codex_install._entry_targets_cmd(entry, "/venv/bin/x") is False

    def test_matches_when_multiple_inner_hooks(self):
        entry = {
            "hooks": [
                {"type": "command", "command": "/other", "timeout": 30},
                {"type": "command", "command": "/venv/bin/x", "timeout": 30},
            ]
        }
        assert codex_install._entry_targets_cmd(entry, "/venv/bin/x") is True


# ---------------------------------------------------------------------------
# Trust prompt content
# ---------------------------------------------------------------------------


class TestTrustPromptContent:
    """The trust-prompt message must mention /hooks AND arize-hook-codex-*."""

    def test_install_prints_hooks_command_in_trust_message(self, fake_home, mock_prompts, capsys):
        codex_install.install()
        out = capsys.readouterr().out
        # The plan literal: "open codex, run `/hooks`, and approve the arize-hook-codex-*"
        assert "/hooks" in out
        assert "arize-hook-codex-" in out


# ---------------------------------------------------------------------------
# Re-install handles a pre-existing v1 + v2 mixed config gracefully
# ---------------------------------------------------------------------------


class TestMixedConfigUpgrade:
    """A user upgrading from v1 may have BOTH the v1 notify and an otel block
    in their config. install() should produce a clean v2 layout."""

    def test_upgrade_from_v1_notify_only(self, fake_home, mock_prompts):
        """v1 user had only the notify entry — installer should preserve it
        once (since it points at the same notify binary) and add hooks."""
        toml_path = fake_home / ".codex" / "config.toml"
        toml_path.parent.mkdir(parents=True, exist_ok=True)
        # Use the same path that the installer would compute
        notify_cmd = _venv_bin(fake_home, NOTIFY_BIN_NAME)
        toml_path.write_text(f"notify = [{notify_cmd!r}]\n")

        codex_install.install()
        data = codex_install._toml_load(toml_path)
        # Notify must remain exactly once (no duplicates).
        assert data["notify"].count(notify_cmd) == 1
        # All six hooks must be present.
        assert set(data["hooks"].keys()) == set(EXPECTED_HOOK_EVENTS)

    def test_upgrade_from_v1_full_layout(self, fake_home, mock_prompts):
        """Simulate a complete v1 install: notify + otel.exporter.otlp-http."""
        toml_path = fake_home / ".codex" / "config.toml"
        toml_path.parent.mkdir(parents=True, exist_ok=True)
        notify_cmd = _venv_bin(fake_home, NOTIFY_BIN_NAME)
        toml_path.write_text(
            f"notify = [{notify_cmd!r}]\n"
            "\n"
            "[otel.exporter.otlp-http]\n"
            'endpoint = "http://127.0.0.1:4318/v1/logs"\n'
            'protocol = "json"\n'
        )

        codex_install.install()
        data = codex_install._toml_load(toml_path)
        # The v1 otel block is stripped; notify survives (deduped); hooks are present.
        assert "otel" not in data
        assert data["notify"] == [notify_cmd]
        assert set(data["hooks"].keys()) == set(EXPECTED_HOOK_EVENTS)


# ---------------------------------------------------------------------------
# install_legacy module surface
# ---------------------------------------------------------------------------


class TestInstallLegacyModuleSurface:
    """The legacy module's public API must be importable from the install module.

    These are not behavior tests — they protect the install/install_legacy
    split from accidental regressions like dropping the cleanup_legacy_install
    name."""

    def test_install_module_imports_cleanup_legacy(self):
        # Imported at module top so it's available as an attribute.
        assert hasattr(codex_install, "cleanup_legacy_install")
        assert callable(codex_install.cleanup_legacy_install)

    def test_install_legacy_module_exposes_cleanup(self):
        from tracing.codex import install_legacy

        assert hasattr(install_legacy, "cleanup_legacy_install")
        assert callable(install_legacy.cleanup_legacy_install)


# ---------------------------------------------------------------------------
# config.yaml has no collector entry under harnesses.codex
# ---------------------------------------------------------------------------


class TestNoCollectorEntry:
    """v2 install must not write a collector sub-block under harnesses.codex
    (the buffer service is gone)."""

    def test_fresh_install_omits_collector(self, fake_home, mock_prompts):
        codex_install.install()
        config_file = fake_home / ".arize" / "harness" / "config.yaml"
        config = yaml.safe_load(config_file.read_text())
        assert "collector" not in config["harnesses"]["codex"]
