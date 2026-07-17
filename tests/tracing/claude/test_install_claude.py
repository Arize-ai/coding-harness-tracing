"""Tests for claude-code-tracing/install.py — install/uninstall module."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture()
def fake_home(tmp_path, monkeypatch):
    """Redirect Path.home() to tmp_path so all file writes land in a temp dir.

    Also patches the module-level constants in install.py and core.setup that
    derive from Path.home() so they point at the temp tree.
    """
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))

    # Patch INSTALL_DIR / VENV_DIR / CONFIG_FILE in core.setup
    import core.setup as setup_mod

    install_dir = tmp_path / ".arize" / "harness"
    venv_dir = install_dir / "venv"
    config_file = install_dir / "config.json"

    monkeypatch.setattr(setup_mod, "INSTALL_DIR", install_dir)
    monkeypatch.setattr(setup_mod, "VENV_DIR", venv_dir)
    monkeypatch.setattr(setup_mod, "CONFIG_FILE", config_file)
    monkeypatch.setattr(setup_mod, "BIN_DIR", install_dir / "bin")
    monkeypatch.setattr(setup_mod, "RUN_DIR", install_dir / "run")
    monkeypatch.setattr(setup_mod, "LOG_DIR", install_dir / "logs")
    monkeypatch.setattr(setup_mod, "STATE_DIR", install_dir / "state")

    # Patch CONFIG_FILE in core.config so load_config/save_config use tmp
    import core.config as config_mod

    monkeypatch.setattr(config_mod, "CONFIG_FILE", str(config_file))

    # Patch SETTINGS_FILE in the install module's constants
    settings_file = tmp_path / ".claude" / "settings.json"

    # We need to patch both the constants module and the install module's reference
    import tracing.claude_code.constants as claude_constants
    import tracing.claude_code.install as claude_install

    monkeypatch.setattr(claude_constants, "SETTINGS_FILE", settings_file)
    monkeypatch.setattr(claude_install, "SETTINGS_FILE", settings_file)

    # Create the harness plugin dir so harness_dir() resolves
    plugin_dir = install_dir / "tracing" / "claude_code"
    plugin_dir.mkdir(parents=True, exist_ok=True)

    return tmp_path


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


PHOENIX_BACKEND = ("phoenix", {"endpoint": "http://localhost:6006", "api_key": ""})
ARIZE_BACKEND = (
    "arize",
    {"endpoint": "otlp.arize.com:443", "api_key": "test-key", "space_id": "test-space"},
)


def _mock_prompts(monkeypatch, backend=None):
    """Patch prompt functions on the install module (where they're bound after import)."""
    import tracing.claude_code.install as claude_install

    if backend is None:
        backend = PHOENIX_BACKEND

    monkeypatch.setattr(
        claude_install,
        "prompt_backend",
        lambda existing_harnesses=None: backend,
    )
    monkeypatch.setattr(claude_install, "prompt_project_name", lambda default: default)
    monkeypatch.setattr(claude_install, "prompt_user_id", lambda: "")
    monkeypatch.setattr(
        claude_install, "prompt_content_logging", lambda: {"prompts": True, "tool_details": True, "tool_content": True}
    )
    monkeypatch.setattr(claude_install, "write_logging_config", lambda block, config_path=None: None)
    monkeypatch.setattr("sys.stdout", _fake_stdout())


class TestFreshInstall:
    """Fresh install with no existing config."""

    @pytest.mark.parametrize(
        "backend,expected_target",
        [
            (PHOENIX_BACKEND, "phoenix"),
            (ARIZE_BACKEND, "arize"),
        ],
        ids=["phoenix", "arize"],
    )
    def test_fresh_install_creates_config_and_hooks(self, fake_home, monkeypatch, backend, expected_target):
        """With no existing config, install() prompts and writes config.json + settings.json."""
        import tracing.claude_code.install as claude_install

        _mock_prompts(monkeypatch, backend=backend)

        claude_install.install(with_skills=False)

        # Check config.json was written
        config_file = fake_home / ".arize" / "harness" / "config.json"
        assert config_file.exists()
        config = json.loads(config_file.read_text())
        assert config["harnesses"]["claude-code"]["target"] == expected_target
        assert config["harnesses"]["claude-code"]["project_name"] == "claude-code"

        # Check settings.json has plugin + all hook events
        settings_file = fake_home / ".claude" / "settings.json"
        assert settings_file.exists()
        settings = json.loads(settings_file.read_text())

        assert len(settings.get("plugins", [])) == 1
        assert settings["plugins"][0]["type"] == "local"

        hooks = settings.get("hooks", {})
        assert len(hooks) == 16

        env = settings.get("env", {})
        assert env.get("ARIZE_TRACE_ENABLED") == "true"
        # project_name lives in config.json only; baking ARIZE_PROJECT_NAME here
        # used to shadow config.json edits and leak into the Phoenix backend (#74).
        assert "ARIZE_PROJECT_NAME" not in env

    def test_install_fresh_writes_flat_harness_entry(self, fake_home, monkeypatch):
        """Fresh install writes all backend fields directly under harnesses.claude-code."""
        import tracing.claude_code.install as claude_install

        _mock_prompts(monkeypatch, backend=ARIZE_BACKEND)

        claude_install.install(with_skills=False)

        config_file = fake_home / ".arize" / "harness" / "config.json"
        config = json.loads(config_file.read_text())
        entry = config["harnesses"]["claude-code"]

        # All fields at the same level — no nested backend block
        assert entry["project_name"] == "claude-code"
        assert entry["target"] == "arize"
        assert entry["endpoint"] == "otlp.arize.com:443"
        assert entry["api_key"] == "test-key"
        assert entry["space_id"] == "test-space"

        # No legacy top-level backend block
        assert "backend" not in config
        # No nested backend under the harness entry
        assert "backend" not in entry


class TestIdempotent:
    """Re-install is idempotent — no duplicate hooks."""

    def test_double_install_no_duplicates(self, fake_home, monkeypatch):
        """Running install() twice does not duplicate hooks or plugins."""
        import tracing.claude_code.install as claude_install

        _mock_prompts(monkeypatch)

        claude_install.install(with_skills=False)
        claude_install.install(with_skills=False)

        settings_file = fake_home / ".claude" / "settings.json"
        settings = json.loads(settings_file.read_text())

        # Still exactly 1 plugin
        assert len(settings["plugins"]) == 1

        # Still exactly 1 hook entry per event
        for event, entries in settings["hooks"].items():
            assert len(entries) == 1, f"Event {event} has {len(entries)} entries"


class TestExistingEntry:
    """Re-install with an existing harness entry only updates project_name."""

    def test_install_existing_claude_entry_only_updates_project_name(self, fake_home, monkeypatch):
        """When harnesses.claude-code already exists, re-install updates only project_name."""
        import tracing.claude_code.install as claude_install

        _mock_prompts(monkeypatch, backend=ARIZE_BACKEND)

        # Pre-populate config with an existing claude-code entry
        config_file = fake_home / ".arize" / "harness" / "config.json"
        config_file.parent.mkdir(parents=True, exist_ok=True)
        original_entry = {
            "project_name": "old-name",
            "target": "arize",
            "endpoint": "otlp.arize.com:443",
            "api_key": "original-key",
            "space_id": "original-space",
        }
        config_file.write_text(json.dumps({"harnesses": {"claude-code": original_entry}}, indent=2))

        # Mock prompt_project_name to return a new name
        monkeypatch.setattr(claude_install, "prompt_project_name", lambda default: "new-project-name")

        claude_install.install(with_skills=False)

        config = json.loads(config_file.read_text())
        entry = config["harnesses"]["claude-code"]

        # project_name updated
        assert entry["project_name"] == "new-project-name"
        # credentials preserved
        assert entry["target"] == "arize"
        assert entry["endpoint"] == "otlp.arize.com:443"
        assert entry["api_key"] == "original-key"
        assert entry["space_id"] == "original-space"


class TestCopyFrom:
    """Copy-from: install offers to reuse credentials from another harness."""

    def test_install_second_harness_offers_copy_from(self, fake_home, monkeypatch):
        """Pre-populate codex with arize creds; verify claude-code gets them via copy-from."""
        import tracing.claude_code.install as claude_install

        # Pre-populate config with a codex entry using arize
        config_file = fake_home / ".arize" / "harness" / "config.json"
        config_file.parent.mkdir(parents=True, exist_ok=True)
        codex_entry = {
            "project_name": "codex",
            "target": "arize",
            "endpoint": "otlp.arize.com:443",
            "api_key": "codex-key",
            "space_id": "codex-space",
        }
        config_file.write_text(json.dumps({"harnesses": {"codex": codex_entry}}, indent=2))

        # Mock prompt_backend to return arize target with codex's credentials (simulating copy-from)
        copied_creds = {
            "endpoint": "otlp.arize.com:443",
            "api_key": "codex-key",
            "space_id": "codex-space",
        }
        monkeypatch.setattr(
            claude_install,
            "prompt_backend",
            lambda existing_harnesses=None: ("arize", copied_creds),
        )
        monkeypatch.setattr(claude_install, "prompt_project_name", lambda default: default)
        monkeypatch.setattr(claude_install, "prompt_user_id", lambda: "")
        monkeypatch.setattr(
            claude_install,
            "prompt_content_logging",
            lambda: {"prompts": True, "tool_details": True, "tool_content": True},
        )
        monkeypatch.setattr(claude_install, "write_logging_config", lambda block, config_path=None: None)
        monkeypatch.setattr("sys.stdout", _fake_stdout())

        claude_install.install(with_skills=False)

        config = json.loads(config_file.read_text())
        entry = config["harnesses"]["claude-code"]

        # claude-code got codex's credentials
        assert entry["target"] == "arize"
        assert entry["endpoint"] == "otlp.arize.com:443"
        assert entry["api_key"] == "codex-key"
        assert entry["space_id"] == "codex-space"
        assert entry["project_name"] == "claude-code"

        # codex entry is preserved
        assert config["harnesses"]["codex"]["api_key"] == "codex-key"


class TestUninstall:
    """Uninstall removes hooks and harness entry."""

    def test_uninstall_removes_hooks_and_config(self, fake_home, monkeypatch):
        """Uninstall removes hooks, plugin, and harness entry from config.json."""
        import tracing.claude_code.install as claude_install

        _mock_prompts(monkeypatch)

        claude_install.install(with_skills=False)
        claude_install.uninstall()

        # settings.json should have no hooks and no plugins
        settings_file = fake_home / ".claude" / "settings.json"
        settings = json.loads(settings_file.read_text())
        assert "hooks" not in settings
        assert "plugins" not in settings

        # config.json should have no claude-code entry
        config_file = fake_home / ".arize" / "harness" / "config.json"
        config = json.loads(config_file.read_text())
        harnesses = config.get("harnesses", {})
        assert "claude-code" not in harnesses

    def test_uninstall_removes_harness_entry_preserves_others(self, fake_home, monkeypatch):
        """Uninstall removes claude-code but preserves other harness entries."""
        import tracing.claude_code.install as claude_install

        _mock_prompts(monkeypatch)

        # Install claude-code
        claude_install.install(with_skills=False)

        # Add another harness entry to config
        config_file = fake_home / ".arize" / "harness" / "config.json"
        config = json.loads(config_file.read_text())
        config["harnesses"]["copilot"] = {
            "project_name": "copilot",
            "target": "arize",
            "endpoint": "otlp.arize.com:443",
            "api_key": "copilot-key",
            "space_id": "copilot-space",
        }
        config_file.write_text(json.dumps(config, indent=2))

        claude_install.uninstall()

        config = json.loads(config_file.read_text())
        assert "claude-code" not in config["harnesses"]
        assert config["harnesses"]["copilot"]["api_key"] == "copilot-key"

    def test_uninstall_idempotent(self, fake_home, monkeypatch):
        """Calling uninstall twice does not error."""
        import tracing.claude_code.install as claude_install

        _mock_prompts(monkeypatch)

        claude_install.install(with_skills=False)
        claude_install.uninstall()
        # Second uninstall should be a no-op, not raise
        claude_install.uninstall()

    def test_uninstall_clears_arize_env_keys(self, fake_home, monkeypatch):
        """Uninstall pops Arize env vars from settings.json.

        Regression guard: the previous bash installer cleaned
        ARIZE_PROJECT_NAME / ARIZE_TRACE_ENABLED / etc. out of
        settings.json on uninstall. The Python port only removed hooks
        and plugins, leaving stale env entries.
        """
        import tracing.claude_code.install as claude_install

        _mock_prompts(monkeypatch)

        claude_install.install(with_skills=False)

        # Inject extra Arize env keys + a non-Arize key that must be preserved.
        settings_file = fake_home / ".claude" / "settings.json"
        settings = json.loads(settings_file.read_text())
        settings["env"].update(
            {
                "ARIZE_USER_ID": "user-42",
                "ARIZE_API_KEY": "ak-secret",
                "ARIZE_SPACE_ID": "sp-abc",
                "PHOENIX_ENDPOINT": "http://localhost:6006",
                "UNRELATED_VAR": "keep-me",
            }
        )
        settings_file.write_text(json.dumps(settings, indent=2) + "\n")

        claude_install.uninstall()

        settings = json.loads(settings_file.read_text())
        env = settings.get("env", {})

        # All Arize keys are gone
        for key in (
            "ARIZE_PROJECT_NAME",
            "ARIZE_TRACE_ENABLED",
            "ARIZE_USER_ID",
            "ARIZE_API_KEY",
            "ARIZE_SPACE_ID",
            "PHOENIX_ENDPOINT",
        ):
            assert key not in env, f"{key} should have been removed from env"

        # Non-Arize env survives
        assert env.get("UNRELATED_VAR") == "keep-me"

    def test_uninstall_drops_env_block_when_emptied(self, fake_home, monkeypatch):
        """If removing Arize keys leaves env empty, the env block is dropped."""
        import tracing.claude_code.install as claude_install

        _mock_prompts(monkeypatch)
        claude_install.install(with_skills=False)
        claude_install.uninstall()

        settings_file = fake_home / ".claude" / "settings.json"
        settings = json.loads(settings_file.read_text())
        # install only set ARIZE_* keys, so removing them leaves env empty
        assert "env" not in settings

    def test_uninstall_preserves_third_party_hooks(self, fake_home, monkeypatch):
        """Uninstall keeps hooks that don't belong to us."""
        import tracing.claude_code.install as claude_install

        _mock_prompts(monkeypatch)

        claude_install.install(with_skills=False)

        # Inject a third-party hook into SessionStart
        settings_file = fake_home / ".claude" / "settings.json"
        settings = json.loads(settings_file.read_text())
        third_party = {"hooks": [{"type": "command", "command": "/usr/local/bin/my-hook"}]}
        settings["hooks"]["SessionStart"].append(third_party)
        # Also add a completely separate event
        settings["hooks"]["CustomEvent"] = [{"hooks": [{"type": "command", "command": "/usr/local/bin/other"}]}]
        settings_file.write_text(json.dumps(settings, indent=2) + "\n")

        claude_install.uninstall()

        settings = json.loads(settings_file.read_text())
        hooks = settings.get("hooks", {})

        # Third-party hook in SessionStart survives
        assert "SessionStart" in hooks
        assert len(hooks["SessionStart"]) == 1
        assert hooks["SessionStart"][0]["hooks"][0]["command"] == "/usr/local/bin/my-hook"

        # CustomEvent survives
        assert "CustomEvent" in hooks
        assert hooks["CustomEvent"][0]["hooks"][0]["command"] == "/usr/local/bin/other"


class TestDryRun:
    """Dry-run mode should not write files."""

    def test_install_dry_run_writes_nothing(self, fake_home, monkeypatch):
        """With ARIZE_DRY_RUN=true, install() logs but does not write files."""
        import tracing.claude_code.install as claude_install

        monkeypatch.setenv("ARIZE_DRY_RUN", "true")
        _mock_prompts(monkeypatch)

        claude_install.install(with_skills=False)

        settings_file = fake_home / ".claude" / "settings.json"
        assert not settings_file.exists()

        config_file = fake_home / ".arize" / "harness" / "config.json"
        assert not config_file.exists()
