"""Tests for tracing.antigravity.install: install and uninstall of Antigravity hooks.

Mirrors tests/tracing/gemini/test_install_gemini.py but accounts for Antigravity's
inverted schema (top-level hook name -> { event -> [handlers] }), the seconds
timeout unit, and the dedicated hooks.json file path.
"""

from __future__ import annotations

import json

import pytest
import yaml

import tracing.antigravity.constants as _ac
import tracing.antigravity.install as _install

install = _install.install
uninstall = _install.uninstall


# ---------------------------------------------------------------------------
# Test backend tuples
# ---------------------------------------------------------------------------

PHOENIX_BACKEND = ("phoenix", {"endpoint": "http://localhost:6006", "api_key": ""})
ARIZE_BACKEND = (
    "arize",
    {"endpoint": "otlp.arize.com:443", "api_key": "test-key", "space_id": "test-space"},
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _fake_stdout():
    """Non-tty stdout to suppress ANSI codes."""
    return type(
        "FakeOut",
        (),
        {
            "isatty": lambda self: False,
            "write": lambda self, s: None,
            "flush": lambda self: None,
        },
    )()


def _mock_prompts(monkeypatch, backend=None):
    """Patch prompt functions on the install module (where they're bound after import)."""
    if backend is None:
        backend = PHOENIX_BACKEND

    monkeypatch.setattr(
        _install,
        "prompt_backend",
        lambda existing_harnesses=None: backend,
    )
    monkeypatch.setattr(_install, "prompt_project_name", lambda default: default)
    monkeypatch.setattr(_install, "prompt_user_id", lambda: "")
    monkeypatch.setattr(
        _install,
        "prompt_content_logging",
        lambda: {"prompts": True, "tool_details": True, "tool_content": True},
    )
    monkeypatch.setattr(_install, "write_logging_config", lambda block, config_path=None: None)
    monkeypatch.setattr("sys.stdout", _fake_stdout())


@pytest.fixture
def cwd_tmp(tmp_path, monkeypatch):
    """Set cwd to tmp_path and patch core.setup + tracing.antigravity.constants paths."""
    monkeypatch.chdir(tmp_path)

    import core.setup as setup_mod

    monkeypatch.setattr(setup_mod, "INSTALL_DIR", tmp_path / ".arize" / "harness")
    monkeypatch.setattr(setup_mod, "VENV_DIR", tmp_path / ".arize" / "harness" / "venv")
    monkeypatch.setattr(setup_mod, "CONFIG_FILE", tmp_path / ".arize" / "harness" / "config.yaml")
    monkeypatch.setattr(setup_mod, "BIN_DIR", tmp_path / ".arize" / "harness" / "bin")
    monkeypatch.setattr(setup_mod, "RUN_DIR", tmp_path / ".arize" / "harness" / "run")
    monkeypatch.setattr(setup_mod, "LOG_DIR", tmp_path / ".arize" / "harness" / "logs")
    monkeypatch.setattr(setup_mod, "STATE_DIR", tmp_path / ".arize" / "harness" / "state")

    import core.constants as c

    monkeypatch.setattr(c, "BASE_DIR", tmp_path / ".arize" / "harness")
    monkeypatch.setattr(c, "CONFIG_FILE", tmp_path / ".arize" / "harness" / "config.yaml")

    import core.config as config_mod

    monkeypatch.setattr(config_mod, "CONFIG_FILE", str(tmp_path / ".arize" / "harness" / "config.yaml"))

    # Redirect Antigravity hooks file to temp dir
    antigravity_settings_dir = tmp_path / ".gemini" / "config"
    monkeypatch.setattr(_ac, "SETTINGS_DIR", antigravity_settings_dir)
    monkeypatch.setattr(_ac, "SETTINGS_FILE", antigravity_settings_dir / "hooks.json")

    # Mock venv_bin so commands are deterministic and don't depend on a real venv
    def _fake_venv_bin(name):
        return tmp_path / ".arize" / "harness" / "venv" / "bin" / name

    monkeypatch.setattr(_install, "venv_bin", _fake_venv_bin)

    return tmp_path


@pytest.fixture
def settings_file(cwd_tmp):
    """Return the path to the redirected Antigravity hooks.json."""
    return _ac.SETTINGS_FILE


# ---------------------------------------------------------------------------
# Install tests — fresh install writes the expected structure
# ---------------------------------------------------------------------------


class TestInstallFreshStructure:
    """A fresh install writes the inverted hooks.json schema correctly."""

    def test_hooks_file_created(self, settings_file, monkeypatch):
        _mock_prompts(monkeypatch)
        install()
        assert settings_file.is_file()

    def test_top_level_contains_arize_tracing(self, settings_file, monkeypatch):
        _mock_prompts(monkeypatch)
        install()
        data = json.loads(settings_file.read_text())
        assert _ac.HOOK_NAME in data
        assert _ac.HOOK_NAME == "arize-tracing"

    def test_arize_tracing_has_both_events(self, settings_file, monkeypatch):
        _mock_prompts(monkeypatch)
        install()
        data = json.loads(settings_file.read_text())
        block = data[_ac.HOOK_NAME]
        assert set(block.keys()) == set(_ac.EVENTS.keys())
        assert "PreInvocation" in block
        assert "Stop" in block

    def test_each_event_is_flat_handler_list(self, settings_file, monkeypatch):
        """Each event's value is a flat list of handler dicts — no matcher wrapper."""
        _mock_prompts(monkeypatch)
        install()
        data = json.loads(settings_file.read_text())
        for event, entry_point in _ac.EVENTS.items():
            handlers = data[_ac.HOOK_NAME][event]
            assert isinstance(handlers, list)
            assert len(handlers) == 1
            h = handlers[0]
            # Flat handler — must not have a 'matcher' or nested 'hooks' key
            assert "matcher" not in h
            assert "hooks" not in h
            assert h["type"] == "command"
            assert entry_point in h["command"]

    def test_timeout_is_30_seconds_int(self, settings_file, monkeypatch):
        """Timeout MUST be 30 (seconds), never 30000 (ms)."""
        _mock_prompts(monkeypatch)
        install()
        data = json.loads(settings_file.read_text())
        for event in _ac.EVENTS:
            handler = data[_ac.HOOK_NAME][event][0]
            assert handler["timeout"] == 30
            assert handler["timeout"] == _ac.HOOK_TIMEOUT_SECONDS
            assert isinstance(handler["timeout"], int)

    def test_command_uses_venv_bin(self, cwd_tmp, settings_file, monkeypatch):
        _mock_prompts(monkeypatch)
        install()
        data = json.loads(settings_file.read_text())
        for event, entry_point in _ac.EVENTS.items():
            cmd = data[_ac.HOOK_NAME][event][0]["command"]
            assert "venv" in cmd
            assert cmd.endswith(entry_point)

    def test_pretty_printed_with_trailing_newline(self, settings_file, monkeypatch):
        _mock_prompts(monkeypatch)
        install()
        text = settings_file.read_text()
        assert "\n  " in text or "\n    " in text
        assert text.endswith("\n")


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


class TestIdempotent:
    """Running install twice yields a single arize-tracing block."""

    def test_no_duplicate_block(self, settings_file, monkeypatch):
        _mock_prompts(monkeypatch)
        install()
        install()
        data = json.loads(settings_file.read_text())
        # Only one arize-tracing block, with each event present exactly once
        assert _ac.HOOK_NAME in data
        for event in _ac.EVENTS:
            handlers = data[_ac.HOOK_NAME][event]
            assert len(handlers) == 1

    def test_event_set_unchanged_after_reinstall(self, settings_file, monkeypatch):
        _mock_prompts(monkeypatch)
        install()
        install()
        data = json.loads(settings_file.read_text())
        assert set(data[_ac.HOOK_NAME].keys()) == set(_ac.EVENTS.keys())


# ---------------------------------------------------------------------------
# Foreign top-level keys are preserved across install and uninstall
# ---------------------------------------------------------------------------


class TestForeignKeysPreserved:
    """A pre-existing foreign top-level key survives install and uninstall."""

    def test_foreign_key_preserved_on_install(self, settings_file, monkeypatch):
        settings_file.parent.mkdir(parents=True, exist_ok=True)
        existing = {"my-hook": {"PreInvocation": [{"type": "command", "command": "/usr/bin/foo", "timeout": 10}]}}
        settings_file.write_text(json.dumps(existing, indent=2) + "\n")

        _mock_prompts(monkeypatch)
        install()

        data = json.loads(settings_file.read_text())
        assert "my-hook" in data
        assert data["my-hook"] == existing["my-hook"]
        assert _ac.HOOK_NAME in data

    def test_foreign_key_preserved_on_uninstall(self, settings_file, monkeypatch):
        settings_file.parent.mkdir(parents=True, exist_ok=True)
        existing = {"my-hook": {"PreInvocation": [{"type": "command", "command": "/usr/bin/foo", "timeout": 10}]}}
        settings_file.write_text(json.dumps(existing, indent=2) + "\n")

        _mock_prompts(monkeypatch)
        install()
        uninstall()

        # File should still exist with the foreign key intact
        assert settings_file.is_file()
        data = json.loads(settings_file.read_text())
        assert "my-hook" in data
        assert data["my-hook"] == existing["my-hook"]
        assert _ac.HOOK_NAME not in data


# ---------------------------------------------------------------------------
# Uninstall removes our key; deletes file if it was the only key
# ---------------------------------------------------------------------------


class TestUninstall:
    def test_uninstall_removes_only_arize_tracing(self, settings_file, monkeypatch):
        settings_file.parent.mkdir(parents=True, exist_ok=True)
        existing = {"other-hook": {"Stop": [{"type": "command", "command": "/usr/bin/bar", "timeout": 5}]}}
        settings_file.write_text(json.dumps(existing, indent=2) + "\n")

        _mock_prompts(monkeypatch)
        install()
        uninstall()

        data = json.loads(settings_file.read_text())
        assert _ac.HOOK_NAME not in data
        assert data["other-hook"] == existing["other-hook"]

    def test_uninstall_deletes_file_when_empty(self, settings_file, monkeypatch):
        _mock_prompts(monkeypatch)
        install()
        uninstall()
        assert not settings_file.is_file()

    def test_uninstall_when_no_settings_file(self, cwd_tmp, monkeypatch):
        monkeypatch.setattr("sys.stdout", _fake_stdout())
        assert not _ac.SETTINGS_FILE.is_file()
        uninstall()  # should not raise

    def test_uninstall_is_idempotent(self, settings_file, monkeypatch):
        _mock_prompts(monkeypatch)
        install()
        uninstall()
        # Second uninstall should be a no-op, no exception
        uninstall()


# ---------------------------------------------------------------------------
# Malformed JSON aborts without overwriting
# ---------------------------------------------------------------------------


class TestMalformedJson:
    def test_install_raises_systemexit_on_malformed(self, settings_file, monkeypatch):
        settings_file.parent.mkdir(parents=True, exist_ok=True)
        original = "{this is not valid json!!!"
        settings_file.write_text(original)

        _mock_prompts(monkeypatch)

        with pytest.raises(SystemExit) as exc_info:
            install()
        assert exc_info.value.code == 1

        # File must not be overwritten
        assert settings_file.read_text() == original

    def test_uninstall_raises_systemexit_on_malformed(self, settings_file, monkeypatch):
        settings_file.parent.mkdir(parents=True, exist_ok=True)
        original = "{also not json"
        settings_file.write_text(original)

        monkeypatch.setattr("sys.stdout", _fake_stdout())
        with pytest.raises(SystemExit) as exc_info:
            uninstall()
        assert exc_info.value.code == 1

        assert settings_file.read_text() == original


# ---------------------------------------------------------------------------
# Empty file is treated as {}
# ---------------------------------------------------------------------------


class TestEmptyHooksFile:
    def test_empty_file_treated_as_empty_dict(self, settings_file, monkeypatch):
        settings_file.parent.mkdir(parents=True, exist_ok=True)
        settings_file.write_text("")

        _mock_prompts(monkeypatch)
        install()

        data = json.loads(settings_file.read_text())
        assert _ac.HOOK_NAME in data
        assert set(data[_ac.HOOK_NAME].keys()) == set(_ac.EVENTS.keys())


# ---------------------------------------------------------------------------
# Dry-run writes nothing
# ---------------------------------------------------------------------------


class TestDryRun:
    def test_dry_run_no_hooks_file(self, settings_file, monkeypatch):
        monkeypatch.setenv("ARIZE_DRY_RUN", "true")
        _mock_prompts(monkeypatch)
        install()
        assert not settings_file.is_file()

    def test_dry_run_uninstall_preserves(self, settings_file, monkeypatch):
        _mock_prompts(monkeypatch)
        install()
        original = settings_file.read_text()

        monkeypatch.setenv("ARIZE_DRY_RUN", "true")
        uninstall()

        assert settings_file.is_file()
        assert settings_file.read_text() == original


# ---------------------------------------------------------------------------
# Config.yaml integration
# ---------------------------------------------------------------------------


class TestConfigYamlIntegration:
    def test_fresh_install_writes_harness_entry(self, cwd_tmp, monkeypatch):
        _mock_prompts(monkeypatch, backend=PHOENIX_BACKEND)
        install()

        config_path = cwd_tmp / ".arize" / "harness" / "config.yaml"
        assert config_path.is_file()
        config = yaml.safe_load(config_path.read_text())
        entry = config["harnesses"]["antigravity"]
        assert entry["target"] == "phoenix"
        assert entry["project_name"] == "antigravity"

    def test_uninstall_removes_harness_entry(self, cwd_tmp, monkeypatch):
        _mock_prompts(monkeypatch)
        install()
        uninstall()
        config_path = cwd_tmp / ".arize" / "harness" / "config.yaml"
        if config_path.is_file():
            config = yaml.safe_load(config_path.read_text())
            harnesses = config.get("harnesses", {}) if config else {}
            assert "antigravity" not in harnesses


# ---------------------------------------------------------------------------
# CLI main() dispatch
# ---------------------------------------------------------------------------


class TestMainDispatch:
    def test_bad_args_exits_1(self, monkeypatch):
        monkeypatch.setattr("sys.argv", ["tracing.antigravity.install", "bogus"])
        with pytest.raises(SystemExit) as exc_info:
            _install.main()
        assert exc_info.value.code == 1

    def test_no_args_exits_1(self, monkeypatch):
        monkeypatch.setattr("sys.argv", ["tracing.antigravity.install"])
        with pytest.raises(SystemExit) as exc_info:
            _install.main()
        assert exc_info.value.code == 1

    def test_install_arg_calls_install(self, cwd_tmp, monkeypatch):
        _mock_prompts(monkeypatch)
        monkeypatch.setattr("sys.argv", ["tracing.antigravity.install", "install"])
        called = []
        monkeypatch.setattr(_install, "install", lambda: called.append("install"))
        _install.main()
        assert called == ["install"]

    def test_uninstall_arg_calls_uninstall(self, cwd_tmp, monkeypatch):
        monkeypatch.setattr("sys.argv", ["tracing.antigravity.install", "uninstall"])
        called = []
        monkeypatch.setattr(_install, "uninstall", lambda: called.append("uninstall"))
        _install.main()
        assert called == ["uninstall"]
