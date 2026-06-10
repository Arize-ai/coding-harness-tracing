"""Tests for tracing.gemini/install.py: install and uninstall of Gemini hooks."""

from __future__ import annotations

import json

import pytest

import tracing.gemini.constants as _gc
import tracing.gemini.install as _install

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
    """Set cwd to tmp_path and patch core.setup + tracing.gemini.constants paths for isolation."""
    monkeypatch.chdir(tmp_path)

    import core.setup as setup_mod

    monkeypatch.setattr(setup_mod, "INSTALL_DIR", tmp_path / ".arize" / "harness")
    monkeypatch.setattr(setup_mod, "VENV_DIR", tmp_path / ".arize" / "harness" / "venv")
    monkeypatch.setattr(setup_mod, "CONFIG_FILE", tmp_path / ".arize" / "harness" / "config.json")
    monkeypatch.setattr(setup_mod, "BIN_DIR", tmp_path / ".arize" / "harness" / "bin")
    monkeypatch.setattr(setup_mod, "RUN_DIR", tmp_path / ".arize" / "harness" / "run")
    monkeypatch.setattr(setup_mod, "LOG_DIR", tmp_path / ".arize" / "harness" / "logs")
    monkeypatch.setattr(setup_mod, "STATE_DIR", tmp_path / ".arize" / "harness" / "state")

    import core.constants as c

    monkeypatch.setattr(c, "BASE_DIR", tmp_path / ".arize" / "harness")
    monkeypatch.setattr(c, "CONFIG_FILE", tmp_path / ".arize" / "harness" / "config.json")

    import core.config as config_mod

    monkeypatch.setattr(config_mod, "CONFIG_FILE", str(tmp_path / ".arize" / "harness" / "config.json"))

    # Redirect gemini settings to temp dir (the fixture does NOT do this automatically)
    gemini_settings_dir = tmp_path / ".gemini"
    monkeypatch.setattr(_gc, "SETTINGS_DIR", gemini_settings_dir)
    monkeypatch.setattr(_gc, "SETTINGS_FILE", gemini_settings_dir / "settings.json")

    return tmp_path


@pytest.fixture
def settings_file(cwd_tmp):
    """Return the path to the redirected Gemini settings.json."""
    return _gc.SETTINGS_FILE


# ---------------------------------------------------------------------------
# Install tests — fresh install
# ---------------------------------------------------------------------------


class TestInstallFreshWritesFlatHarnessEntry:
    """Fresh install writes flat harness entry to config.json."""

    @pytest.mark.parametrize(
        "backend,expected_target",
        [
            (PHOENIX_BACKEND, "phoenix"),
            (ARIZE_BACKEND, "arize"),
        ],
        ids=["phoenix", "arize"],
    )
    def test_fresh_install_creates_config(self, cwd_tmp, monkeypatch, backend, expected_target):
        _mock_prompts(monkeypatch, backend=backend)
        install()

        config_path = cwd_tmp / ".arize" / "harness" / "config.json"
        assert config_path.is_file()
        config = json.loads(config_path.read_text())
        entry = config["harnesses"]["gemini"]
        assert entry["target"] == expected_target
        assert entry["project_name"] == "gemini"
        assert entry["endpoint"] == backend[1]["endpoint"]
        assert entry["api_key"] == backend[1]["api_key"]

        if expected_target == "arize":
            assert entry["space_id"] == backend[1]["space_id"]

        # No collector for gemini
        assert "collector" not in entry

    def test_settings_json_created(self, settings_file, monkeypatch):
        _mock_prompts(monkeypatch)
        install()
        assert settings_file.is_file()

    def test_settings_json_has_all_8_events(self, settings_file, monkeypatch):
        _mock_prompts(monkeypatch)
        install()
        data = json.loads(settings_file.read_text())
        assert "hooks" in data
        expected_events = set(_gc.EVENTS.keys())
        assert set(data["hooks"].keys()) == expected_events

    def test_settings_json_hook_structure(self, settings_file, monkeypatch):
        """Each event should have the correct matcher/hooks/type/name/command/timeout structure."""
        _mock_prompts(monkeypatch)
        install()
        data = json.loads(settings_file.read_text())
        for event, entry_point in _gc.EVENTS.items():
            event_list = data["hooks"][event]
            assert isinstance(event_list, list)
            assert len(event_list) == 1
            block = event_list[0]
            assert block["matcher"] == ""
            assert isinstance(block["hooks"], list)
            assert len(block["hooks"]) == 1
            inner = block["hooks"][0]
            assert inner["type"] == "command"
            assert inner["name"] == _gc.HOOK_NAME
            assert entry_point in inner["command"]
            assert inner["timeout"] == _gc.HOOK_TIMEOUT_MS

    def test_settings_json_pretty_printed_with_trailing_newline(self, settings_file, monkeypatch):
        _mock_prompts(monkeypatch)
        install()
        text = settings_file.read_text()
        # Pretty-printed means indented
        assert "\n  " in text or "\n    " in text
        # Trailing newline
        assert text.endswith("\n")

    def test_settings_json_command_uses_venv_bin(self, cwd_tmp, settings_file, monkeypatch):
        """Commands should reference the venv bin path."""
        _mock_prompts(monkeypatch)
        install()
        data = json.loads(settings_file.read_text())
        # Pick one event to check
        first_event = list(_gc.EVENTS.keys())[0]
        cmd = data["hooks"][first_event][0]["hooks"][0]["command"]
        # Should contain venv path component
        assert "venv" in cmd or ".arize" in cmd


# ---------------------------------------------------------------------------
# Install tests — second harness (copy-from)
# ---------------------------------------------------------------------------


class TestInstallSecondHarnessOffersCopyFrom:
    """When another harness exists with the same target, copy-from is offered."""

    def test_copy_from_populates_credentials(self, cwd_tmp, monkeypatch):
        """Pre-seed a claude-code entry; gemini install should receive it in prompt_backend."""
        config_dir = cwd_tmp / ".arize" / "harness"
        config_dir.mkdir(parents=True, exist_ok=True)
        config_path = config_dir / "config.json"

        seed_config = {
            "harnesses": {
                "claude-code": {
                    "project_name": "claude-code",
                    "target": "arize",
                    "endpoint": "otlp.arize.com:443",
                    "api_key": "ak-existing",
                    "space_id": "space-existing",
                },
            },
        }
        config_path.write_text(json.dumps(seed_config, indent=2))

        captured = {}

        def fake_prompt_backend(existing_harnesses=None):
            captured["existing_harnesses"] = existing_harnesses
            return ARIZE_BACKEND

        monkeypatch.setattr(_install, "prompt_backend", fake_prompt_backend)
        monkeypatch.setattr(_install, "prompt_project_name", lambda default: default)
        monkeypatch.setattr(_install, "prompt_user_id", lambda: "")
        monkeypatch.setattr(
            _install,
            "prompt_content_logging",
            lambda: {"prompts": True, "tool_details": True, "tool_content": True},
        )
        monkeypatch.setattr(_install, "write_logging_config", lambda block, config_path=None: None)
        monkeypatch.setattr("sys.stdout", _fake_stdout())

        install()

        # prompt_backend should have received the existing harnesses dict
        assert captured["existing_harnesses"] is not None
        assert "claude-code" in captured["existing_harnesses"]
        assert captured["existing_harnesses"]["claude-code"]["target"] == "arize"

        # Verify the gemini entry was actually written
        config = json.loads(config_path.read_text())
        entry = config["harnesses"]["gemini"]
        assert entry["target"] == "arize"
        assert entry["endpoint"] == ARIZE_BACKEND[1]["endpoint"]
        assert entry["api_key"] == ARIZE_BACKEND[1]["api_key"]
        assert entry["space_id"] == ARIZE_BACKEND[1]["space_id"]
        assert entry["project_name"] == "gemini"


# ---------------------------------------------------------------------------
# Install tests — existing gemini entry (re-install)
# ---------------------------------------------------------------------------


class TestInstallExistingGeminiEntryOnlyUpdatesProjectName:
    """Re-install with existing gemini config only updates project_name."""

    def test_existing_entry_preserves_target(self, cwd_tmp, monkeypatch):
        config_dir = cwd_tmp / ".arize" / "harness"
        config_dir.mkdir(parents=True, exist_ok=True)
        config_path = config_dir / "config.json"

        seed_config = {
            "harnesses": {
                "gemini": {
                    "project_name": "gemini",
                    "target": "arize",
                    "endpoint": "otlp.arize.com:443",
                    "api_key": "ak-existing",
                    "space_id": "space-existing",
                },
            },
        }
        config_path.write_text(json.dumps(seed_config, indent=2))

        # prompt_project_name returns a new name
        monkeypatch.setattr(_install, "prompt_project_name", lambda default: "my-gemini")
        monkeypatch.setattr(
            _install,
            "prompt_content_logging",
            lambda: {"prompts": True, "tool_details": True, "tool_content": True},
        )
        monkeypatch.setattr(_install, "write_logging_config", lambda block, config_path=None: None)
        monkeypatch.setattr("sys.stdout", _fake_stdout())

        install()

        config = json.loads(config_path.read_text())
        entry = config["harnesses"]["gemini"]
        assert entry["project_name"] == "my-gemini"
        # Other fields preserved
        assert entry["target"] == "arize"
        assert entry["endpoint"] == "otlp.arize.com:443"
        assert entry["api_key"] == "ak-existing"
        assert entry["space_id"] == "space-existing"


# ---------------------------------------------------------------------------
# Install tests — existing logging block
# ---------------------------------------------------------------------------


class TestInstallExistingLoggingBlockSkipsPrompt:
    """When config.json already has a logging block, skip the logging prompt."""

    def test_existing_logging_not_reprompted(self, cwd_tmp, monkeypatch):
        config_dir = cwd_tmp / ".arize" / "harness"
        config_dir.mkdir(parents=True, exist_ok=True)
        config_path = config_dir / "config.json"

        seed_config = {
            "logging": {"prompts": False, "tool_details": True, "tool_content": False},
        }
        config_path.write_text(json.dumps(seed_config, indent=2))

        _mock_prompts(monkeypatch)

        prompt_logging_called = []
        monkeypatch.setattr(
            _install,
            "prompt_content_logging",
            lambda: prompt_logging_called.append(True) or {"prompts": True, "tool_details": True, "tool_content": True},
        )

        install()

        assert len(prompt_logging_called) == 0, "prompt_content_logging should not be called"


# ---------------------------------------------------------------------------
# Idempotency tests
# ---------------------------------------------------------------------------


class TestIdempotent:
    """Re-install is idempotent — no duplicate entries."""

    def test_no_duplicate_hooks(self, settings_file, monkeypatch):
        _mock_prompts(monkeypatch)
        install()
        install()
        data = json.loads(settings_file.read_text())
        for event in _gc.EVENTS:
            event_list = data["hooks"][event]
            assert len(event_list) == 1, f"Duplicate matcher-blocks for event {event}"

    def test_no_duplicate_inner_hooks(self, settings_file, monkeypatch):
        _mock_prompts(monkeypatch)
        install()
        install()
        data = json.loads(settings_file.read_text())
        for event in _gc.EVENTS:
            inner_hooks = data["hooks"][event][0]["hooks"]
            names = [h["name"] for h in inner_hooks]
            assert names.count(_gc.HOOK_NAME) == 1, f"Duplicate inner hooks for event {event}"


# ---------------------------------------------------------------------------
# Preserve user settings
# ---------------------------------------------------------------------------


class TestInstallPreservesUserSettings:
    """Install must not clobber unrelated keys in settings.json."""

    def test_preserves_telemetry_key(self, settings_file, monkeypatch):
        """Existing telemetry settings should be untouched."""
        settings_file.parent.mkdir(parents=True, exist_ok=True)
        existing = {"telemetry": {"enabled": False}, "mcpServers": {"my-server": {}}}
        settings_file.write_text(json.dumps(existing, indent=2) + "\n")

        _mock_prompts(monkeypatch)
        install()

        data = json.loads(settings_file.read_text())
        assert data["telemetry"] == {"enabled": False}
        assert data["mcpServers"] == {"my-server": {}}
        assert "hooks" in data

    def test_preserves_user_hooks_for_other_events(self, settings_file, monkeypatch):
        """User hooks for events we don't use should survive."""
        settings_file.parent.mkdir(parents=True, exist_ok=True)
        existing = {
            "hooks": {
                "CustomEvent": [
                    {"matcher": "", "hooks": [{"type": "command", "name": "user-hook", "command": "/usr/bin/custom"}]}
                ],
            }
        }
        settings_file.write_text(json.dumps(existing, indent=2) + "\n")

        _mock_prompts(monkeypatch)
        install()

        data = json.loads(settings_file.read_text())
        assert "CustomEvent" in data["hooks"]
        assert data["hooks"]["CustomEvent"][0]["hooks"][0]["name"] == "user-hook"

    def test_preserves_user_hooks_within_our_events(self, settings_file, monkeypatch):
        """User matcher-blocks within our event arrays should survive install."""
        settings_file.parent.mkdir(parents=True, exist_ok=True)
        existing = {
            "hooks": {
                "BeforeTool": [
                    {
                        "matcher": "grep",
                        "hooks": [{"type": "command", "name": "user-grep-hook", "command": "/usr/bin/grep-hook"}],
                    },
                ],
            }
        }
        settings_file.write_text(json.dumps(existing, indent=2) + "\n")

        _mock_prompts(monkeypatch)
        install()

        data = json.loads(settings_file.read_text())
        before_tool = data["hooks"]["BeforeTool"]
        # User block + our block
        assert len(before_tool) >= 2
        user_blocks = [b for b in before_tool if any(h.get("name") == "user-grep-hook" for h in b.get("hooks", []))]
        assert len(user_blocks) == 1


# ---------------------------------------------------------------------------
# Missing / empty settings.json
# ---------------------------------------------------------------------------


class TestInstallHandlesMissingSettings:
    """Install creates settings.json if missing or handles empty file."""

    def test_creates_settings_dir_and_file(self, cwd_tmp, monkeypatch):
        """When ~/.gemini doesn't exist, install creates it."""
        _mock_prompts(monkeypatch)
        install()
        assert _gc.SETTINGS_FILE.is_file()

    def test_handles_empty_settings_file(self, settings_file, monkeypatch):
        """An empty settings.json should be treated as {}."""
        settings_file.parent.mkdir(parents=True, exist_ok=True)
        settings_file.write_text("")

        _mock_prompts(monkeypatch)
        install()

        data = json.loads(settings_file.read_text())
        assert "hooks" in data
        assert len(data["hooks"]) == 8


# ---------------------------------------------------------------------------
# Uninstall tests
# ---------------------------------------------------------------------------


class TestUninstallRemovesHarnessEntry:
    """Uninstall removes harness entry from config.json."""

    def test_config_entry_removed(self, cwd_tmp, monkeypatch):
        _mock_prompts(monkeypatch)
        install()
        uninstall()
        config_path = cwd_tmp / ".arize" / "harness" / "config.json"
        if config_path.is_file():
            config = json.loads(config_path.read_text())
            harnesses = config.get("harnesses", {})
            assert "gemini" not in harnesses

    def test_hooks_removed_from_settings_json(self, settings_file, monkeypatch):
        _mock_prompts(monkeypatch)
        install()
        assert settings_file.is_file()
        uninstall()
        # Either file is deleted (if it became empty) or our hooks are gone
        if settings_file.is_file():
            data = json.loads(settings_file.read_text())
            hooks = data.get("hooks", {})
            for event in _gc.EVENTS:
                if event in hooks:
                    for block in hooks[event]:
                        for h in block.get("hooks", []):
                            assert h.get("name") != _gc.HOOK_NAME

    def test_uninstall_is_idempotent(self, cwd_tmp, monkeypatch):
        """Running uninstall twice succeeds without error."""
        _mock_prompts(monkeypatch)
        install()
        uninstall()
        # Second uninstall should be a no-op, no exception
        uninstall()
        config_path = cwd_tmp / ".arize" / "harness" / "config.json"
        if config_path.is_file():
            config = json.loads(config_path.read_text())
            harnesses = config.get("harnesses", {})
            assert "gemini" not in harnesses


class TestUninstallPreservesUserHooks:
    """Uninstall preserves unrelated user hooks in settings.json."""

    def test_preserves_user_hooks_in_our_events(self, settings_file, monkeypatch):
        """User matcher-blocks within our event arrays survive uninstall."""
        _mock_prompts(monkeypatch)
        install()

        # Add a user hook to BeforeTool
        data = json.loads(settings_file.read_text())
        data["hooks"]["BeforeTool"].append(
            {
                "matcher": "grep",
                "hooks": [{"type": "command", "name": "user-grep-hook", "command": "/usr/bin/grep-hook"}],
            }
        )
        settings_file.write_text(json.dumps(data, indent=2) + "\n")

        uninstall()

        assert settings_file.is_file()
        remaining = json.loads(settings_file.read_text())
        bt = remaining["hooks"]["BeforeTool"]
        assert len(bt) == 1
        assert bt[0]["hooks"][0]["name"] == "user-grep-hook"

    def test_preserves_custom_events(self, settings_file, monkeypatch):
        """Events not in our EVENTS map survive uninstall."""
        _mock_prompts(monkeypatch)
        install()

        data = json.loads(settings_file.read_text())
        data["hooks"]["CustomEvent"] = [
            {"matcher": "", "hooks": [{"type": "command", "name": "custom", "command": "/usr/bin/custom"}]}
        ]
        settings_file.write_text(json.dumps(data, indent=2) + "\n")

        uninstall()

        assert settings_file.is_file()
        remaining = json.loads(settings_file.read_text())
        assert "CustomEvent" in remaining["hooks"]

    def test_preserves_non_hooks_keys(self, settings_file, monkeypatch):
        """Top-level keys like telemetry, mcpServers survive uninstall."""
        _mock_prompts(monkeypatch)
        install()

        data = json.loads(settings_file.read_text())
        data["telemetry"] = {"enabled": False}
        data["mcpServers"] = {"my-server": {}}
        settings_file.write_text(json.dumps(data, indent=2) + "\n")

        uninstall()

        assert settings_file.is_file()
        remaining = json.loads(settings_file.read_text())
        assert remaining["telemetry"] == {"enabled": False}
        assert remaining["mcpServers"] == {"my-server": {}}


class TestUninstallDeletesFileWhenEmpty:
    """When uninstall leaves an empty settings.json, the file is deleted."""

    def test_file_deleted_when_only_our_hooks(self, settings_file, monkeypatch):
        _mock_prompts(monkeypatch)
        install()
        uninstall()
        # File should be deleted since it only contained our hooks
        assert not settings_file.is_file()


class TestUninstallPrunesEmptyEvents:
    """Empty event arrays are pruned; empty hooks dict is pruned."""

    def test_empty_events_removed(self, settings_file, monkeypatch):
        _mock_prompts(monkeypatch)
        install()

        # Add a custom event so file isn't entirely deleted
        data = json.loads(settings_file.read_text())
        data["other_key"] = "preserve_me"
        settings_file.write_text(json.dumps(data, indent=2) + "\n")

        uninstall()

        remaining = json.loads(settings_file.read_text())
        # Our events should not appear as empty arrays
        for event in _gc.EVENTS:
            assert event not in remaining.get("hooks", {})

    def test_hooks_key_removed_if_empty(self, settings_file, monkeypatch):
        _mock_prompts(monkeypatch)
        install()

        # Add a non-hooks key so file isn't deleted
        data = json.loads(settings_file.read_text())
        data["telemetry"] = {"enabled": True}
        settings_file.write_text(json.dumps(data, indent=2) + "\n")

        uninstall()

        remaining = json.loads(settings_file.read_text())
        assert "hooks" not in remaining


# ---------------------------------------------------------------------------
# Dry-run tests
# ---------------------------------------------------------------------------


class TestInstallDryRunWritesNothing:
    """Dry-run mode writes nothing."""

    def test_dry_run_no_settings_file(self, settings_file, monkeypatch):
        monkeypatch.setenv("ARIZE_DRY_RUN", "true")
        _mock_prompts(monkeypatch)
        install()
        assert not settings_file.is_file()

    def test_dry_run_no_config(self, cwd_tmp, monkeypatch):
        monkeypatch.setenv("ARIZE_DRY_RUN", "true")
        _mock_prompts(monkeypatch)
        install()
        config_path = cwd_tmp / ".arize" / "harness" / "config.json"
        assert not config_path.is_file()

    def test_dry_run_does_not_modify_existing_settings(self, settings_file, monkeypatch):
        """Dry run with existing settings.json should leave it untouched."""
        settings_file.parent.mkdir(parents=True, exist_ok=True)
        original = {"telemetry": {"enabled": True}}
        settings_file.write_text(json.dumps(original, indent=2) + "\n")

        monkeypatch.setenv("ARIZE_DRY_RUN", "true")
        _mock_prompts(monkeypatch)
        install()

        data = json.loads(settings_file.read_text())
        assert data == original


# ---------------------------------------------------------------------------
# CLI main() dispatch tests
# ---------------------------------------------------------------------------


class TestMainDispatch:
    """main() dispatches install/uninstall based on argv."""

    def test_bad_args_exits_1(self, monkeypatch):
        monkeypatch.setattr("sys.argv", ["tracing.gemini.install", "bogus"])
        with pytest.raises(SystemExit) as exc_info:
            _install.main()
        assert exc_info.value.code == 1

    def test_no_args_exits_1(self, monkeypatch):
        monkeypatch.setattr("sys.argv", ["tracing.gemini.install"])
        with pytest.raises(SystemExit) as exc_info:
            _install.main()
        assert exc_info.value.code == 1

    def test_install_arg_calls_install(self, cwd_tmp, monkeypatch):
        _mock_prompts(monkeypatch)
        monkeypatch.setattr("sys.argv", ["tracing.gemini.install", "install"])
        called = []
        monkeypatch.setattr(_install, "install", lambda: called.append("install"))
        _install.main()
        assert called == ["install"]

    def test_uninstall_arg_calls_uninstall(self, cwd_tmp, monkeypatch):
        monkeypatch.setattr("sys.argv", ["tracing.gemini.install", "uninstall"])
        called = []
        monkeypatch.setattr(_install, "uninstall", lambda: called.append("uninstall"))
        _install.main()
        assert called == ["uninstall"]


# ---------------------------------------------------------------------------
# Dedupe by hook name (not command string)
# ---------------------------------------------------------------------------


class TestDedupeByHookName:
    """Deduplication should use HOOK_NAME, not the command path."""

    def test_reinstall_with_different_venv_path_dedupes(self, settings_file, monkeypatch):
        """If a prior install used a different venv path, dedupe by name should still work."""
        settings_file.parent.mkdir(parents=True, exist_ok=True)
        # Seed with an old install that used a different venv path
        old_data = {
            "hooks": {
                "SessionStart": [
                    {
                        "matcher": "",
                        "hooks": [
                            {
                                "type": "command",
                                "name": _gc.HOOK_NAME,
                                "command": "/old/path/venv/bin/arize-hook-gemini-session-start",
                                "timeout": _gc.HOOK_TIMEOUT_MS,
                            }
                        ],
                    }
                ],
            }
        }
        settings_file.write_text(json.dumps(old_data, indent=2) + "\n")

        _mock_prompts(monkeypatch)
        install()

        data = json.loads(settings_file.read_text())
        # Should still have exactly 1 block for SessionStart
        assert len(data["hooks"]["SessionStart"]) == 1


# ---------------------------------------------------------------------------
# Malformed settings.json
# ---------------------------------------------------------------------------


class TestInstallHandlesMalformedSettings:
    """Install aborts on malformed settings.json to avoid overwriting user data."""

    def test_malformed_json_aborts_with_exit_1(self, settings_file, monkeypatch):
        """Malformed JSON should abort with sys.exit(1), not silently overwrite."""
        settings_file.parent.mkdir(parents=True, exist_ok=True)
        original_content = "{this is not valid json!!!"
        settings_file.write_text(original_content)

        _mock_prompts(monkeypatch)

        with pytest.raises(SystemExit) as exc_info:
            install()
        assert exc_info.value.code == 1

        # The malformed file must not be overwritten
        assert settings_file.read_text() == original_content


# ---------------------------------------------------------------------------
# Uninstall dry-run
# ---------------------------------------------------------------------------


class TestUninstallDryRunWritesNothing:
    """Dry-run uninstall should not modify anything."""

    def test_dry_run_uninstall_preserves_settings(self, settings_file, monkeypatch):
        """Dry-run uninstall should not remove hooks from settings.json."""
        _mock_prompts(monkeypatch)
        install()

        original = settings_file.read_text()

        monkeypatch.setenv("ARIZE_DRY_RUN", "true")
        uninstall()

        assert settings_file.is_file()
        assert settings_file.read_text() == original

    def test_dry_run_uninstall_no_settings_no_error(self, cwd_tmp, monkeypatch):
        """Dry-run uninstall with no settings.json should not error."""
        monkeypatch.setenv("ARIZE_DRY_RUN", "true")
        monkeypatch.setattr("sys.stdout", _fake_stdout())
        # Should not raise
        uninstall()


# ---------------------------------------------------------------------------
# JSON output validity
# ---------------------------------------------------------------------------


class TestSettingsJsonOutputValidity:
    """Verify that settings.json output is always valid JSON."""

    def test_output_is_valid_json(self, settings_file, monkeypatch):
        """Output should always be parseable JSON."""
        _mock_prompts(monkeypatch)
        install()
        text = settings_file.read_text()
        # Should not raise
        data = json.loads(text)
        assert isinstance(data, dict)

    def test_output_has_no_duplicate_keys(self, settings_file, monkeypatch):
        """No duplicate event keys in the hooks dict."""
        _mock_prompts(monkeypatch)
        install()
        data = json.loads(settings_file.read_text())
        event_keys = list(data["hooks"].keys())
        assert len(event_keys) == len(set(event_keys))


# ---------------------------------------------------------------------------
# Constants verification
# ---------------------------------------------------------------------------


class TestConstants:
    """Verify constants are well-formed."""

    def test_events_has_8_entries(self):
        assert len(_gc.EVENTS) == 8

    def test_harness_name_is_gemini(self):
        assert _gc.HARNESS_NAME == "gemini"

    def test_hook_name_is_arize_tracing(self):
        assert _gc.HOOK_NAME == "arize-tracing"

    def test_settings_file_is_under_settings_dir(self):
        assert _gc.SETTINGS_FILE.parent == _gc.SETTINGS_DIR

    def test_settings_file_name(self):
        assert _gc.SETTINGS_FILE.name == "settings.json"

    def test_hook_timeout_is_positive_int(self):
        assert isinstance(_gc.HOOK_TIMEOUT_MS, int)
        assert _gc.HOOK_TIMEOUT_MS > 0

    def test_all_entry_points_have_gemini_prefix(self):
        """All entry point names should start with arize-hook-gemini-."""
        for ep in _gc.EVENTS.values():
            assert ep.startswith("arize-hook-gemini-"), f"Unexpected entry point: {ep}"

    def test_events_keys_are_camelcase(self):
        """Event names should be CamelCase as per Gemini spec."""
        for event in _gc.EVENTS:
            assert event[0].isupper(), f"Event {event} should start with uppercase"


# ---------------------------------------------------------------------------
# Handlers importability
# ---------------------------------------------------------------------------


class TestHandlersImportable:
    """All 8 handler functions should be importable and callable."""

    def test_all_handlers_importable(self):
        from tracing.gemini.hooks.handlers import (
            after_agent,
            after_model,
            after_tool,
            before_agent,
            before_model,
            before_tool,
            session_end,
            session_start,
        )

        for fn in [
            session_start,
            session_end,
            before_agent,
            after_agent,
            before_model,
            after_model,
            before_tool,
            after_tool,
        ]:
            assert callable(fn)


# ---------------------------------------------------------------------------
# Setup wizard delegation
# ---------------------------------------------------------------------------


class TestSetupGeminiModule:
    """core/setup/gemini.py delegates to tracing.gemini/install.py."""

    def test_setup_module_importable(self):
        from core.setup.gemini import install, main, uninstall

        assert callable(install)
        assert callable(uninstall)
        assert callable(main)

    def test_setup_install_delegates(self, cwd_tmp, monkeypatch):
        """core.setup.gemini.install() should call tracing.gemini.install.install()."""
        import core.setup.gemini as setup_gemini

        called = []
        monkeypatch.setattr(_install, "install", lambda: called.append("install"))
        setup_gemini.install()
        assert called == ["install"]

    def test_setup_uninstall_delegates(self, cwd_tmp, monkeypatch):
        """core.setup.gemini.uninstall() should call tracing.gemini.install.uninstall()."""
        import core.setup.gemini as setup_gemini

        called = []
        monkeypatch.setattr(_install, "uninstall", lambda: called.append("uninstall"))
        setup_gemini.uninstall()
        assert called == ["uninstall"]

    def test_setup_main_delegates_to_install(self, cwd_tmp, monkeypatch):
        """core.setup.gemini.main() should call tracing.gemini.install.install()."""
        import core.setup.gemini as setup_gemini

        called = []
        monkeypatch.setattr(_install, "install", lambda: called.append("install"))
        setup_gemini.main()
        assert called == ["install"]


# ---------------------------------------------------------------------------
# Missing tests from task spec
# ---------------------------------------------------------------------------


class TestUninstallWhenNoSettingsFile:
    """Calling uninstall() with no ~/.gemini/settings.json doesn't raise."""

    def test_uninstall_when_no_settings_file(self, cwd_tmp, monkeypatch):
        monkeypatch.setattr("sys.stdout", _fake_stdout())
        # settings.json does not exist (cwd_tmp fixture redirects but never creates it)
        assert not _gc.SETTINGS_FILE.is_file()
        uninstall()  # should not raise


class TestUninstallWhenSettingsMalformed:
    """Uninstall with malformed settings.json should exit cleanly without overwriting."""

    def test_uninstall_when_settings_malformed(self, settings_file, monkeypatch):
        settings_file.parent.mkdir(parents=True, exist_ok=True)
        original_content = "{not json!!!"
        settings_file.write_text(original_content)

        monkeypatch.setattr("sys.stdout", _fake_stdout())
        # Current implementation sys.exit(1) on malformed JSON (same as install path)
        with pytest.raises(SystemExit) as exc_info:
            uninstall()
        assert exc_info.value.code == 1

        # The malformed file must not be overwritten
        assert settings_file.read_text() == original_content


class TestInstallPromptsForLogging:
    """Install prompts for logging settings when logging block is missing."""

    def test_install_prompts_for_logging_when_block_missing(self, cwd_tmp, monkeypatch):
        """Fresh state: install() calls prompt_content_logging() exactly once
        and write_logging_config() exactly once with the returned dict."""
        from unittest.mock import MagicMock

        logging_result = {"prompts": True, "tool_details": True, "tool_content": True}
        mock_prompt_logging = MagicMock(return_value=logging_result)
        mock_write_logging = MagicMock()

        monkeypatch.setattr(
            _install,
            "prompt_backend",
            lambda existing_harnesses=None: PHOENIX_BACKEND,
        )
        monkeypatch.setattr(_install, "prompt_project_name", lambda default: default)
        monkeypatch.setattr(_install, "prompt_user_id", lambda: "")
        monkeypatch.setattr(_install, "prompt_content_logging", mock_prompt_logging)
        monkeypatch.setattr(_install, "write_logging_config", mock_write_logging)
        monkeypatch.setattr("sys.stdout", _fake_stdout())

        install()

        mock_prompt_logging.assert_called_once()
        mock_write_logging.assert_called_once_with(logging_result)
