"""Tests for tracing.cursor/install.py — install/uninstall module."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

import tracing.cursor.constants
import tracing.cursor.install


def _load_cursor_module(name: str):
    """Import a module from tracing.cursor package by name."""
    if name == "constants":
        return tracing.cursor.constants
    elif name == "install":
        return tracing.cursor.install
    raise ValueError(f"Unknown tracing.cursor module: {name}")


@pytest.fixture()
def fake_home(tmp_path, monkeypatch):
    """Redirect Path.home() to tmp_path so all file writes land in a temp dir.

    Also patches the module-level constants in install.py and core.setup that
    derive from Path.home() so they point at the temp tree. Cursor project-hook
    paths are intentionally relative, so run each test from tmp_path to keep
    install/uninstall helpers from touching the repository's real .cursor files.
    """
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.chdir(tmp_path)

    # Patch INSTALL_DIR / VENV_DIR / CONFIG_FILE in core.setup
    import core.setup as setup_mod

    install_dir = tmp_path / ".arize" / "harness"
    venv_dir = install_dir / "venv"
    config_file = install_dir / "config.yaml"

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

    # Patch HOOKS_FILE in the install module
    hooks_file = tmp_path / ".cursor" / "hooks.json"

    cursor_install = _load_cursor_module("install")

    monkeypatch.setattr(cursor_install, "HOOKS_FILE", hooks_file)

    # Patch INSTALL_DIR in the install module so state dir lands in tmp
    monkeypatch.setattr(cursor_install, "INSTALL_DIR", install_dir)

    # Create the harness plugin dir so harness_dir() resolves
    plugin_dir = install_dir / "tracing" / "cursor"
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
    cursor_install = _load_cursor_module("install")

    if backend is None:
        backend = PHOENIX_BACKEND

    monkeypatch.setattr(
        cursor_install,
        "prompt_backend",
        lambda existing_harnesses=None: backend,
    )
    monkeypatch.setattr(cursor_install, "prompt_project_name", lambda default: default)
    monkeypatch.setattr(cursor_install, "prompt_user_id", lambda: "")
    monkeypatch.setattr(
        cursor_install,
        "prompt_content_logging",
        lambda: {"prompts": True, "tool_details": True, "tool_content": True},
    )
    monkeypatch.setattr(cursor_install, "write_logging_config", lambda block, config_path=None: None)
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
    def test_install_fresh_writes_flat_harness_entry(self, fake_home, monkeypatch, backend, expected_target):
        """With no existing config, install() prompts and writes a flat harness entry."""
        cursor_install = _load_cursor_module("install")

        _mock_prompts(monkeypatch, backend=backend)

        cursor_install.install(with_skills=False)

        # Check config.yaml was written with flat schema
        config_file = fake_home / ".arize" / "harness" / "config.yaml"
        assert config_file.exists()
        config = yaml.safe_load(config_file.read_text())

        # Flat entry under harnesses.cursor
        entry = config["harnesses"]["cursor"]
        assert entry["target"] == expected_target
        assert entry["project_name"] == "cursor"
        assert entry["endpoint"] == backend[1]["endpoint"]
        assert entry["api_key"] == backend[1]["api_key"]
        if expected_target == "arize":
            assert entry["space_id"] == backend[1]["space_id"]

        # No top-level backend or collector blocks
        assert "backend" not in config
        assert "collector" not in config

        # Check hooks.json has 15 events (12 IDE + 3 CLI)
        hooks_file = fake_home / ".cursor" / "hooks.json"
        assert hooks_file.exists()
        hooks_data = json.loads(hooks_file.read_text())

        assert hooks_data["version"] == 1
        hooks = hooks_data.get("hooks", {})
        assert len(hooks) == 15

        # sessionStart, sessionEnd, and postToolUse are present and use the same hook command
        assert "sessionStart" in hooks
        assert "sessionEnd" in hooks
        assert "postToolUse" in hooks

        # sessionEnd uses the same hook command as sessionStart
        session_start_cmd = hooks["sessionStart"][0]["command"]
        session_end_cmd = hooks["sessionEnd"][0]["command"]
        assert session_start_cmd == session_end_cmd

        # Each event should have exactly one entry
        for event, entries in hooks.items():
            assert len(entries) == 1
            assert "command" in entries[0]

    def test_state_dir_created(self, fake_home, monkeypatch):
        """Install creates the cursor state directory."""
        cursor_install = _load_cursor_module("install")

        _mock_prompts(monkeypatch)
        cursor_install.install(with_skills=False)

        state_dir = fake_home / ".arize" / "harness" / "state" / "cursor"
        assert state_dir.is_dir()


class TestCopyFrom:
    """Copy-from flow when another harness is already installed."""

    def test_install_second_harness_offers_copy_from(self, fake_home, monkeypatch):
        """When another harness exists with matching target, prompt_backend receives existing_harnesses."""
        cursor_install = _load_cursor_module("install")

        # Pre-seed config with an existing claude-code harness entry
        config_file = fake_home / ".arize" / "harness" / "config.yaml"
        config_file.parent.mkdir(parents=True, exist_ok=True)
        existing_config = {
            "harnesses": {
                "claude-code": {
                    "project_name": "claude-code",
                    "target": "arize",
                    "endpoint": "otlp.arize.com:443",
                    "api_key": "existing-key",
                    "space_id": "existing-space",
                },
            },
        }
        config_file.write_text(yaml.dump(existing_config))

        # Track what prompt_backend receives
        received_harnesses = {}

        def fake_prompt_backend(existing_harnesses=None):
            received_harnesses["value"] = existing_harnesses
            return ARIZE_BACKEND

        monkeypatch.setattr(cursor_install, "prompt_backend", fake_prompt_backend)
        monkeypatch.setattr(cursor_install, "prompt_project_name", lambda default: default)
        monkeypatch.setattr(cursor_install, "prompt_user_id", lambda: "")
        monkeypatch.setattr(
            cursor_install,
            "prompt_content_logging",
            lambda: {"prompts": True, "tool_details": True, "tool_content": True},
        )
        monkeypatch.setattr(cursor_install, "write_logging_config", lambda block, config_path=None: None)
        monkeypatch.setattr("sys.stdout", _fake_stdout())

        cursor_install.install(with_skills=False)

        # prompt_backend should have received the existing harnesses dict
        assert "claude-code" in received_harnesses["value"]
        assert received_harnesses["value"]["claude-code"]["target"] == "arize"

        # Both harnesses should exist in config
        config = yaml.safe_load(config_file.read_text())
        assert "claude-code" in config["harnesses"]
        assert "cursor" in config["harnesses"]


class TestExistingEntry:
    """Re-install when cursor already has an entry."""

    def test_install_existing_cursor_entry_only_updates_project_name(self, fake_home, monkeypatch):
        """When cursor entry exists, install() only updates project_name via merge."""
        cursor_install = _load_cursor_module("install")

        # Pre-seed config with an existing cursor entry
        config_file = fake_home / ".arize" / "harness" / "config.yaml"
        config_file.parent.mkdir(parents=True, exist_ok=True)
        existing_config = {
            "harnesses": {
                "cursor": {
                    "project_name": "cursor",
                    "target": "phoenix",
                    "endpoint": "http://localhost:6006",
                    "api_key": "",
                },
            },
        }
        config_file.write_text(yaml.dump(existing_config))

        # prompt_backend should NOT be called
        prompt_backend_called = {"called": False}

        def fail_prompt_backend(existing_harnesses=None):
            prompt_backend_called["called"] = True
            return PHOENIX_BACKEND

        monkeypatch.setattr(cursor_install, "prompt_backend", fail_prompt_backend)
        monkeypatch.setattr(cursor_install, "prompt_project_name", lambda default: "my-cursor")
        monkeypatch.setattr(cursor_install, "prompt_user_id", lambda: "")
        monkeypatch.setattr(
            cursor_install,
            "prompt_content_logging",
            lambda: {"prompts": True, "tool_details": True, "tool_content": True},
        )
        monkeypatch.setattr(cursor_install, "write_logging_config", lambda block, config_path=None: None)
        monkeypatch.setattr("sys.stdout", _fake_stdout())

        cursor_install.install(with_skills=False)

        # prompt_backend should not have been called
        assert not prompt_backend_called["called"]

        # project_name should be updated, other fields preserved
        config = yaml.safe_load(config_file.read_text())
        entry = config["harnesses"]["cursor"]
        assert entry["project_name"] == "my-cursor"
        assert entry["target"] == "phoenix"
        assert entry["endpoint"] == "http://localhost:6006"


class TestIdempotent:
    """Re-install is idempotent — no duplicate hooks."""

    def test_double_install_no_duplicates(self, fake_home, monkeypatch):
        """Running install() twice does not duplicate hooks."""
        cursor_install = _load_cursor_module("install")

        _mock_prompts(monkeypatch)

        cursor_install.install(with_skills=False)
        cursor_install.install(with_skills=False)

        hooks_file = fake_home / ".cursor" / "hooks.json"
        hooks_data = json.loads(hooks_file.read_text())

        # Still exactly 15 events with 1 entry each
        hooks = hooks_data["hooks"]
        assert len(hooks) == 15
        for event, entries in hooks.items():
            assert len(entries) == 1, f"Event {event} has {len(entries)} entries"


class TestProjectHooks:
    """Repo-local Cursor hooks for Cloud/Background Agent checkouts."""

    def test_project_hooks_write_repo_hooks_and_wrapper(self, fake_home, monkeypatch):
        cursor_install = _load_cursor_module("install")
        repo = fake_home / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _mock_prompts(monkeypatch)

        cursor_install.install(with_skills=False, project_hooks=True)

        hooks_file = repo / ".cursor" / "hooks.json"
        wrapper = repo / ".cursor" / "hooks" / "arize-hook-cursor.sh"
        assert hooks_file.is_file()
        assert wrapper.is_file()
        assert wrapper.stat().st_mode & 0o111

        hooks = json.loads(hooks_file.read_text())["hooks"]
        assert len(hooks) == 15
        for entries in hooks.values():
            assert entries == [{"command": "bash .cursor/hooks/arize-hook-cursor.sh"}]

        text = wrapper.read_text()
        assert "ARIZE_HOOK_CURSOR" in text
        assert "arize-hook-cursor" in text

    def test_project_hooks_bypass_local_cursor_presence_check(self, fake_home, monkeypatch):
        cursor_install = _load_cursor_module("install")
        repo = fake_home / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _mock_prompts(monkeypatch)
        monkeypatch.setattr(cursor_install, "ensure_harness_installed", lambda *args, **kwargs: False)

        cursor_install.install(with_skills=False, project_hooks=True)

        assert (repo / ".cursor" / "hooks.json").is_file()

    def test_project_uninstall_removes_only_arize_hook_entries(self, fake_home, monkeypatch):
        cursor_install = _load_cursor_module("install")
        repo = fake_home / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        _mock_prompts(monkeypatch)

        cursor_install.install(with_skills=False, project_hooks=True)
        hooks_file = repo / ".cursor" / "hooks.json"
        data = json.loads(hooks_file.read_text())
        data["hooks"]["beforeSubmitPrompt"].append({"command": "/usr/local/bin/other-hook"})
        hooks_file.write_text(json.dumps(data, indent=2) + "\n")

        cursor_install.uninstall()

        hooks = json.loads(hooks_file.read_text()).get("hooks", {})
        assert "postToolUse" not in hooks
        assert hooks["beforeSubmitPrompt"] == [{"command": "/usr/local/bin/other-hook"}]


class TestCloudAgentInstall:
    """Cursor Cloud Agent mode writes project bootstrap files without prompting for secrets."""

    def test_cloud_agent_writes_bootstrap_and_environment_without_config_prompt(self, fake_home, monkeypatch):
        cursor_install = _load_cursor_module("install")
        repo = fake_home / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        monkeypatch.setattr(cursor_install, "ensure_harness_installed", lambda *args, **kwargs: False)
        monkeypatch.setattr(cursor_install, "prompt_backend", lambda *args, **kwargs: pytest.fail("prompted backend"))
        monkeypatch.setattr(
            cursor_install, "prompt_project_name", lambda *args, **kwargs: pytest.fail("prompted project")
        )
        monkeypatch.setattr(cursor_install, "prompt_user_id", lambda: pytest.fail("prompted user"))
        monkeypatch.setattr(cursor_install, "prompt_content_logging", lambda: pytest.fail("prompted logging"))
        monkeypatch.setattr("sys.stdout", _fake_stdout())

        cursor_install.install(with_skills=False, cloud_agent=True)

        assert (repo / ".cursor" / "hooks.json").is_file()
        assert (repo / ".cursor" / "hooks" / "arize-hook-cursor.sh").is_file()
        setup_script = repo / ".cursor" / "hooks" / "arize-cursor-cloud-setup.sh"
        assert setup_script.is_file()
        assert setup_script.stat().st_mode & 0o111
        setup_text = setup_script.read_text()
        assert "ARIZE_API_KEY" in setup_text
        assert "ensure_python_venv_support" in setup_text
        assert "python3-venv" in setup_text
        assert "remove_incomplete_harness_venv" in setup_text
        env_example = repo / ".cursor" / "hooks" / "arize-cloud-env.example"
        assert env_example.is_file()
        env_example_text = env_example.read_text()
        assert "ARIZE_API_KEY=" in env_example_text
        assert "ARIZE_INSTALL_BRANCH=main" in env_example_text
        assert "ARIZE_INSTALL_URL=" in env_example_text

        environment = json.loads((repo / ".cursor" / "environment.json").read_text())
        assert environment["install"] == cursor_install.CLOUD_SETUP_COMMAND
        assert "ARIZE_SPACE_ID" in environment["install"]
        assert "PHOENIX_ENDPOINT" in environment["install"]
        assert "ARIZE_INSTALL_BRANCH" in environment["install"]
        assert "ARIZE_INSTALL_URL" in environment["install"]
        assert not (fake_home / ".arize" / "harness" / "config.yaml").exists()

    def test_existing_environment_install_is_prefixed(self, fake_home, monkeypatch):
        cursor_install = _load_cursor_module("install")
        repo = fake_home / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        env_file = repo / ".cursor" / "environment.json"
        env_file.parent.mkdir()
        env_file.write_text(json.dumps({"install": "npm install", "terminals": []}) + "\n")
        monkeypatch.setattr("sys.stdout", _fake_stdout())

        cursor_install._ensure_cloud_environment_bootstrap()

        data = json.loads(env_file.read_text())
        assert data["install"] == f"{cursor_install.CLOUD_SETUP_COMMAND} && npm install"
        assert data["terminals"] == []

    def test_existing_environment_install_is_not_duplicated(self, fake_home, monkeypatch):
        cursor_install = _load_cursor_module("install")
        repo = fake_home / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        env_file = repo / ".cursor" / "environment.json"
        env_file.parent.mkdir()
        env_file.write_text(
            json.dumps({"install": "bash .cursor/hooks/arize-cursor-cloud-setup.sh && npm install"}) + "\n"
        )
        monkeypatch.setattr("sys.stdout", _fake_stdout())

        cursor_install._ensure_cloud_environment_bootstrap()

        data = json.loads(env_file.read_text())
        assert data["install"].count("arize-cursor-cloud-setup.sh") == 1


class TestUninstall:
    """Uninstall removes hooks and harness entry."""

    def test_uninstall_removes_harness_entry(self, fake_home, monkeypatch):
        """Uninstall removes hooks and harness entry from config.yaml."""
        cursor_install = _load_cursor_module("install")

        _mock_prompts(monkeypatch)

        cursor_install.install(with_skills=False)
        cursor_install.uninstall()

        # hooks.json should have no hooks (all events removed)
        hooks_file = fake_home / ".cursor" / "hooks.json"
        hooks_data = json.loads(hooks_file.read_text())
        assert hooks_data.get("hooks", {}) == {}

        # config.yaml should have no cursor entry
        config_file = fake_home / ".arize" / "harness" / "config.yaml"
        config = yaml.safe_load(config_file.read_text())
        harnesses = config.get("harnesses", {})
        assert "cursor" not in harnesses

    def test_uninstall_preserves_third_party_hooks(self, fake_home, monkeypatch):
        """Uninstall keeps hooks that don't belong to us."""
        cursor_install = _load_cursor_module("install")

        _mock_prompts(monkeypatch)

        cursor_install.install(with_skills=False)

        # Inject a third-party hook into beforeSubmitPrompt
        hooks_file = fake_home / ".cursor" / "hooks.json"
        hooks_data = json.loads(hooks_file.read_text())
        third_party = {"command": "/usr/local/bin/my-hook"}
        hooks_data["hooks"]["beforeSubmitPrompt"].append(third_party)
        # Also add a completely separate event
        hooks_data["hooks"]["CustomEvent"] = [{"command": "/usr/local/bin/other"}]
        hooks_file.write_text(json.dumps(hooks_data, indent=2) + "\n")

        cursor_install.uninstall()

        hooks_data = json.loads(hooks_file.read_text())
        hooks = hooks_data.get("hooks", {})

        # Third-party hook in beforeSubmitPrompt survives
        assert "beforeSubmitPrompt" in hooks
        assert len(hooks["beforeSubmitPrompt"]) == 1
        assert hooks["beforeSubmitPrompt"][0]["command"] == "/usr/local/bin/my-hook"

        # CustomEvent survives
        assert "CustomEvent" in hooks
        assert hooks["CustomEvent"][0]["command"] == "/usr/local/bin/other"

    def test_uninstall_removes_cli_events(self, fake_home, monkeypatch):
        """Uninstall removes sessionStart, sessionEnd, and postToolUse while preserving foreign hooks."""
        cursor_install = _load_cursor_module("install")

        _mock_prompts(monkeypatch)
        cursor_install.install(with_skills=False)

        # Inject a third-party hook into sessionStart
        hooks_file = fake_home / ".cursor" / "hooks.json"
        hooks_data = json.loads(hooks_file.read_text())
        hooks_data["hooks"]["sessionStart"].append({"command": "/usr/local/bin/my-session-hook"})
        hooks_file.write_text(json.dumps(hooks_data, indent=2) + "\n")

        cursor_install.uninstall()

        hooks_data = json.loads(hooks_file.read_text())
        hooks = hooks_data.get("hooks", {})

        # Our postToolUse entry is gone entirely (was only ours)
        assert "postToolUse" not in hooks

        # Our sessionEnd entry is gone entirely (was only ours)
        assert "sessionEnd" not in hooks

        # Third-party hook in sessionStart survives
        assert "sessionStart" in hooks
        assert len(hooks["sessionStart"]) == 1
        assert hooks["sessionStart"][0]["command"] == "/usr/local/bin/my-session-hook"

    def test_uninstall_removes_session_end_preserves_foreign(self, fake_home, monkeypatch):
        """Uninstall removes sessionEnd entry while preserving foreign hooks."""
        cursor_install = _load_cursor_module("install")

        _mock_prompts(monkeypatch)
        cursor_install.install(with_skills=False)

        # Inject a third-party hook into sessionEnd
        hooks_file = fake_home / ".cursor" / "hooks.json"
        hooks_data = json.loads(hooks_file.read_text())
        hooks_data["hooks"]["sessionEnd"].append({"command": "/usr/local/bin/my-end-hook"})
        hooks_file.write_text(json.dumps(hooks_data, indent=2) + "\n")

        cursor_install.uninstall()

        hooks_data = json.loads(hooks_file.read_text())
        hooks = hooks_data.get("hooks", {})

        # Third-party hook in sessionEnd survives
        assert "sessionEnd" in hooks
        assert len(hooks["sessionEnd"]) == 1
        assert hooks["sessionEnd"][0]["command"] == "/usr/local/bin/my-end-hook"

    def test_uninstall_is_idempotent(self, fake_home, monkeypatch):
        """Running uninstall twice succeeds and is a no-op the second time."""
        cursor_install = _load_cursor_module("install")

        _mock_prompts(monkeypatch)

        cursor_install.install(with_skills=False)
        cursor_install.uninstall()
        # Second uninstall should not raise
        cursor_install.uninstall()

        # config.yaml should still have no cursor entry
        config_file = fake_home / ".arize" / "harness" / "config.yaml"
        config = yaml.safe_load(config_file.read_text())
        harnesses = config.get("harnesses", {})
        assert "cursor" not in harnesses


class TestDryRun:
    """Dry-run mode should not write files."""

    def test_install_dry_run_writes_nothing(self, fake_home, monkeypatch):
        """With ARIZE_DRY_RUN=true, install() logs but does not write files."""
        cursor_install = _load_cursor_module("install")

        monkeypatch.setenv("ARIZE_DRY_RUN", "true")
        _mock_prompts(monkeypatch)

        cursor_install.install(with_skills=False)

        hooks_file = fake_home / ".cursor" / "hooks.json"
        assert not hooks_file.exists()

        config_file = fake_home / ".arize" / "harness" / "config.yaml"
        assert not config_file.exists()


class TestMainDispatch:
    def test_install_passes_project_and_cloud_flags(self, monkeypatch):
        cursor_install = _load_cursor_module("install")
        calls = []
        monkeypatch.setattr(
            cursor_install,
            "install",
            lambda with_skills=False, project_hooks=False, cloud_agent=False: calls.append(
                (with_skills, project_hooks, cloud_agent)
            ),
        )
        monkeypatch.setattr("sys.argv", ["install.py", "install", "--with-skills", "--project-hooks", "--cloud-agent"])

        cursor_install.main()

        assert calls == [(True, True, True)]
