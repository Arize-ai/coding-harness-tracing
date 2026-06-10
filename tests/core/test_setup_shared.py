"""Tests for core.setup shared helpers and core.setup.wipe."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_install(tmp_path, monkeypatch):
    """Redirect all core.setup path constants to a temp directory.

    Returns the install dir Path.
    """
    install_dir = tmp_path / ".arize" / "harness"
    install_dir.mkdir(parents=True)

    import core.setup as setup_mod

    monkeypatch.setattr(setup_mod, "INSTALL_DIR", install_dir)
    monkeypatch.setattr(setup_mod, "VENV_DIR", install_dir / "venv")
    monkeypatch.setattr(setup_mod, "CONFIG_FILE", install_dir / "config.json")
    monkeypatch.setattr(setup_mod, "BIN_DIR", install_dir / "bin")
    monkeypatch.setattr(setup_mod, "RUN_DIR", install_dir / "run")
    monkeypatch.setattr(setup_mod, "LOG_DIR", install_dir / "logs")
    monkeypatch.setattr(setup_mod, "STATE_DIR", install_dir / "state")

    # Also patch core.constants so load_config/save_config can use default paths
    import core.constants as c

    monkeypatch.setattr(c, "BASE_DIR", install_dir)
    monkeypatch.setattr(c, "CONFIG_FILE", install_dir / "config.json")

    return install_dir


@pytest.fixture
def populated_config(fake_install):
    """Write a config.json with one harness entry in the flat schema."""
    config = {
        "harnesses": {
            "claude-code": {
                "project_name": "claude-code",
                "target": "phoenix",
                "endpoint": "http://localhost:6006",
                "api_key": "",
            },
        },
    }
    config_path = fake_install / "config.json"
    fd = os.open(str(config_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(config, f, indent=2)
    return config


# ---------------------------------------------------------------------------
# dry_run()
# ---------------------------------------------------------------------------


class TestDryRun:
    @pytest.mark.parametrize("val", ["1", "true", "yes", "TRUE", "True", "YES"])
    def test_truthy_values(self, monkeypatch, val):
        monkeypatch.setenv("ARIZE_DRY_RUN", val)
        from core.setup import dry_run

        assert dry_run() is True

    @pytest.mark.parametrize("val", ["0", "false", "", "no", "FALSE"])
    def test_falsy_values(self, monkeypatch, val):
        monkeypatch.setenv("ARIZE_DRY_RUN", val)
        from core.setup import dry_run

        assert dry_run() is False

    def test_unset(self, monkeypatch):
        monkeypatch.delenv("ARIZE_DRY_RUN", raising=False)
        from core.setup import dry_run

        assert dry_run() is False


# ---------------------------------------------------------------------------
# ensure_shared_runtime()
# ---------------------------------------------------------------------------


class TestEnsureSharedRuntime:
    def test_creates_subdirs(self, fake_install):
        from core.setup import ensure_shared_runtime

        ensure_shared_runtime()

        assert (fake_install / "bin").is_dir()
        assert (fake_install / "run").is_dir()
        assert (fake_install / "logs").is_dir()
        assert (fake_install / "state").is_dir()

    def test_idempotent(self, fake_install):
        from core.setup import ensure_shared_runtime

        ensure_shared_runtime()
        ensure_shared_runtime()  # should not raise

        assert (fake_install / "bin").is_dir()

    def test_removes_legacy_artefacts(self, fake_install):
        from core.setup import ensure_shared_runtime

        # Create subdirs and legacy files
        for d in ("bin", "run", "logs"):
            (fake_install / d).mkdir(parents=True, exist_ok=True)
        (fake_install / "bin" / "arize-collector").write_text("legacy")
        (fake_install / "run" / "collector.pid").write_text("123")
        (fake_install / "logs" / "collector.log").write_text("log")

        ensure_shared_runtime()

        assert not (fake_install / "bin" / "arize-collector").exists()
        assert not (fake_install / "run" / "collector.pid").exists()
        assert not (fake_install / "logs" / "collector.log").exists()

    def test_dry_run_does_not_create(self, fake_install, monkeypatch):
        monkeypatch.setenv("ARIZE_DRY_RUN", "true")
        from core.setup import ensure_shared_runtime

        ensure_shared_runtime()

        assert not (fake_install / "bin").exists()
        assert not (fake_install / "run").exists()


# ---------------------------------------------------------------------------
# venv_bin()
# ---------------------------------------------------------------------------


class TestVenvBin:
    def test_posix(self, fake_install, monkeypatch):
        monkeypatch.setattr(os, "name", "posix")
        from core.setup import VENV_DIR, venv_bin

        result = venv_bin("foo")
        assert result == VENV_DIR / "bin" / "foo"

    def test_windows(self, fake_install, monkeypatch):
        monkeypatch.setattr(os, "name", "nt")
        from core.setup import VENV_DIR, venv_bin

        result = venv_bin("foo")
        assert result == VENV_DIR / "Scripts" / "foo.exe"


# ---------------------------------------------------------------------------
# merge_harness_entry()
# ---------------------------------------------------------------------------


class TestMergeHarnessEntry:
    def test_creates_on_fresh_config(self, fake_install):
        from core.setup import merge_harness_entry

        merge_harness_entry("copilot", "my-copilot")

        config_path = fake_install / "config.json"
        assert config_path.exists()
        with open(config_path) as f:
            config = json.load(f)
        assert config["harnesses"]["copilot"]["project_name"] == "my-copilot"

    def test_preserves_existing_harness(self, fake_install, populated_config):
        from core.setup import merge_harness_entry

        merge_harness_entry("copilot", "my-copilot")

        with open(fake_install / "config.json") as f:
            config = json.load(f)
        # Original harness preserved
        assert config["harnesses"]["claude-code"]["project_name"] == "claude-code"
        assert config["harnesses"]["claude-code"]["target"] == "phoenix"
        # New harness added
        assert config["harnesses"]["copilot"]["project_name"] == "my-copilot"

    def test_full_update_with_credentials(self, fake_install):
        from core.setup import merge_harness_entry

        merge_harness_entry(
            "copilot",
            "my-copilot",
            target="arize",
            credentials={"endpoint": "otlp.arize.com:443", "api_key": "ak-xxx", "space_id": "sp-1"},
        )

        with open(fake_install / "config.json") as f:
            config = json.load(f)
        entry = config["harnesses"]["copilot"]
        assert entry["target"] == "arize"
        assert entry["endpoint"] == "otlp.arize.com:443"
        assert entry["api_key"] == "ak-xxx"
        assert entry["space_id"] == "sp-1"
        assert entry["project_name"] == "my-copilot"

    def test_dry_run_no_write(self, fake_install, monkeypatch):
        monkeypatch.setenv("ARIZE_DRY_RUN", "true")
        from core.setup import merge_harness_entry

        merge_harness_entry("copilot", "my-copilot")

        assert not (fake_install / "config.json").exists()


# ---------------------------------------------------------------------------
# remove_harness_entry()
# ---------------------------------------------------------------------------


class TestRemoveHarnessEntry:
    def test_noop_missing_config(self, fake_install):
        from core.setup import remove_harness_entry

        # Should not raise
        remove_harness_entry("copilot")

    def test_removes_entry(self, fake_install, populated_config):
        from core.setup import remove_harness_entry

        remove_harness_entry("claude-code")

        with open(fake_install / "config.json") as f:
            config = json.load(f)
        assert "claude-code" not in config.get("harnesses", {})

    def test_noop_missing_key(self, fake_install, populated_config):
        from core.setup import remove_harness_entry

        remove_harness_entry("nonexistent")

        with open(fake_install / "config.json") as f:
            config = json.load(f)
        # Original entry still present
        assert "claude-code" in config["harnesses"]

    def test_dry_run_no_write(self, fake_install, populated_config, monkeypatch):
        monkeypatch.setenv("ARIZE_DRY_RUN", "true")
        from core.setup import remove_harness_entry

        remove_harness_entry("claude-code")

        with open(fake_install / "config.json") as f:
            config = json.load(f)
        # Entry should still be there
        assert "claude-code" in config["harnesses"]


# ---------------------------------------------------------------------------
# list_installed_harnesses()
# ---------------------------------------------------------------------------


class TestListInstalledHarnesses:
    def test_empty_on_missing_config(self, fake_install):
        from core.setup import list_installed_harnesses

        assert list_installed_harnesses() == []

    def test_returns_keys(self, fake_install, populated_config):
        from core.setup import list_installed_harnesses

        result = list_installed_harnesses()
        assert result == ["claude-code"]


# ---------------------------------------------------------------------------
# harness_dir()
# ---------------------------------------------------------------------------


class TestHarnessDir:
    def test_primary_path(self, fake_install):
        from core.setup import harness_dir

        assert harness_dir("copilot") == fake_install / "tracing" / "copilot"

    def test_hyphenated_alias_normalized(self, fake_install):
        from core.setup import harness_dir

        assert harness_dir("claude-code") == fake_install / "tracing" / "claude_code"


# ---------------------------------------------------------------------------
# symlink_skills() / unlink_skills()
# ---------------------------------------------------------------------------


class TestSymlinkSkills:
    def test_creates_symlink(self, fake_install, tmp_path):
        from core.setup import symlink_skills

        # Set up a harness with a skills dir
        hdir = fake_install / "tracing" / "copilot" / "skills"
        hdir.mkdir(parents=True)
        (hdir / "my-skill.md").write_text("skill content")

        target = tmp_path / "project"
        target.mkdir()

        symlink_skills("copilot", target_dir=target)

        link = target / ".agents" / "skills" / "my-skill.md"
        assert link.is_symlink()
        assert link.read_text() == "skill content"

    def test_idempotent(self, fake_install, tmp_path):
        from core.setup import symlink_skills

        hdir = fake_install / "tracing" / "copilot" / "skills"
        hdir.mkdir(parents=True)
        (hdir / "my-skill.md").write_text("skill content")

        target = tmp_path / "project"
        target.mkdir()

        symlink_skills("copilot", target_dir=target)
        symlink_skills("copilot", target_dir=target)  # should not raise

        link = target / ".agents" / "skills" / "my-skill.md"
        assert link.is_symlink()

    def test_no_skills_dir_noop(self, fake_install, tmp_path):
        from core.setup import symlink_skills

        (fake_install / "tracing" / "copilot").mkdir(parents=True)
        target = tmp_path / "project"
        target.mkdir()

        symlink_skills("copilot", target_dir=target)

        assert not (target / ".agents").exists()


class TestUnlinkSkills:
    def test_removes_symlink(self, fake_install, tmp_path):
        from core.setup import symlink_skills, unlink_skills

        hdir = fake_install / "tracing" / "copilot" / "skills"
        hdir.mkdir(parents=True)
        (hdir / "my-skill.md").write_text("skill content")

        target = tmp_path / "project"
        target.mkdir()

        symlink_skills("copilot", target_dir=target)
        unlink_skills("copilot", target_dir=target)

        link = target / ".agents" / "skills" / "my-skill.md"
        assert not link.exists()

    def test_preserves_regular_file(self, fake_install, tmp_path):
        from core.setup import unlink_skills

        hdir = fake_install / "tracing" / "copilot" / "skills"
        hdir.mkdir(parents=True)
        (hdir / "my-skill.md").write_text("source skill")

        target = tmp_path / "project"
        dest = target / ".agents" / "skills"
        dest.mkdir(parents=True)
        # Create a regular file with the same name
        (dest / "my-skill.md").write_text("user file")

        unlink_skills("copilot", target_dir=target)

        # Regular file should survive
        assert (dest / "my-skill.md").exists()
        assert not (dest / "my-skill.md").is_symlink()
        assert (dest / "my-skill.md").read_text() == "user file"

    def test_idempotent(self, fake_install, tmp_path):
        from core.setup import unlink_skills

        hdir = fake_install / "tracing" / "copilot" / "skills"
        hdir.mkdir(parents=True)
        (hdir / "my-skill.md").write_text("skill")

        target = tmp_path / "project"
        target.mkdir()

        # No links to remove — should not raise
        unlink_skills("copilot", target_dir=target)


# ---------------------------------------------------------------------------
# wipe_shared_runtime()
# ---------------------------------------------------------------------------


class TestWipeSharedRuntime:
    def test_removes_directory(self, fake_install):
        from core.setup.wipe import wipe_shared_runtime

        # Create some content
        (fake_install / "bin").mkdir(exist_ok=True)
        (fake_install / "config.json").write_text('{"test": true}')

        wipe_shared_runtime()

        assert not fake_install.exists()

    def test_idempotent_missing_dir(self, fake_install):
        import shutil

        from core.setup.wipe import wipe_shared_runtime

        shutil.rmtree(fake_install)

        # Should not raise
        wipe_shared_runtime()

    def test_dry_run_preserves(self, fake_install, monkeypatch):
        monkeypatch.setenv("ARIZE_DRY_RUN", "true")
        from core.setup.wipe import wipe_shared_runtime

        (fake_install / "config.json").write_text('{"test": true}')

        wipe_shared_runtime()

        assert fake_install.exists()
        assert (fake_install / "config.json").exists()


# ---------------------------------------------------------------------------
# Harness presence check helpers
# ---------------------------------------------------------------------------


class TestIsHarnessInstalled:
    def test_true_when_home_subdir_exists(self, tmp_path, monkeypatch):
        from core.setup import is_harness_installed

        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        (tmp_path / ".claude").mkdir()

        assert is_harness_installed(home_subdir=".claude") is True

    def test_true_when_bin_on_path(self, tmp_path, monkeypatch):
        import core.setup as setup_mod

        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setattr(
            setup_mod.shutil, "which", lambda name: "/usr/local/bin/claude" if name == "claude" else None
        )

        assert setup_mod.is_harness_installed(home_subdir=".claude", bin_name="claude") is True

    def test_false_when_neither(self, tmp_path, monkeypatch):
        import core.setup as setup_mod

        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setattr(setup_mod.shutil, "which", lambda name: None)

        assert setup_mod.is_harness_installed(home_subdir=".claude", bin_name="claude") is False

    def test_false_with_no_args(self):
        from core.setup import is_harness_installed

        assert is_harness_installed() is False


class TestEnsureHarnessInstalled:
    def test_proceeds_when_installed(self, tmp_path, monkeypatch, capsys):
        from core.setup import ensure_harness_installed

        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        (tmp_path / ".claude").mkdir()

        assert ensure_harness_installed("Claude Code", home_subdir=".claude", bin_name="claude") is True
        # no warning when installed
        assert "warning" not in capsys.readouterr().out.lower()

    def test_non_interactive_proceeds_with_warning(self, tmp_path, monkeypatch, capsys):
        import core.setup as setup_mod

        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setattr(setup_mod.shutil, "which", lambda name: None)
        # pytest default stdout isn't a tty but be explicit
        monkeypatch.setattr(sys.stdout, "isatty", lambda: False)

        assert setup_mod.ensure_harness_installed("Claude Code", home_subdir=".claude", bin_name="claude") is True
        out = capsys.readouterr().out
        assert "warning" in out.lower()
        assert "non-interactive" in out.lower()

    def test_interactive_yes_proceeds(self, tmp_path, monkeypatch):
        import core.setup as setup_mod

        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setattr(setup_mod.shutil, "which", lambda name: None)
        monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda prompt="": "y")

        assert setup_mod.ensure_harness_installed("Claude Code", home_subdir=".claude") is True

    def test_interactive_no_aborts(self, tmp_path, monkeypatch):
        import core.setup as setup_mod

        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setattr(setup_mod.shutil, "which", lambda name: None)
        monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda prompt="": "n")

        assert setup_mod.ensure_harness_installed("Claude Code", home_subdir=".claude") is False

    def test_interactive_default_empty_aborts(self, tmp_path, monkeypatch):
        import core.setup as setup_mod

        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setattr(setup_mod.shutil, "which", lambda name: None)
        monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda prompt="": "")

        assert setup_mod.ensure_harness_installed("Claude Code", home_subdir=".claude") is False

    def test_keyboard_interrupt_aborts(self, tmp_path, monkeypatch):
        import core.setup as setup_mod

        def _raise_kbd(prompt=""):
            raise KeyboardInterrupt

        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setattr(setup_mod.shutil, "which", lambda name: None)
        monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
        monkeypatch.setattr("builtins.input", _raise_kbd)

        assert setup_mod.ensure_harness_installed("Claude Code", home_subdir=".claude") is False


# ---------------------------------------------------------------------------
# write_config() — flat schema tests
# ---------------------------------------------------------------------------


class TestWriteConfigFlat:
    def test_write_config_writes_flat_arize_entry(self, fake_install):
        from core.setup import write_config

        config_path = str(fake_install / "config.json")
        write_config(
            "arize",
            {"endpoint": "otlp.arize.com:443", "api_key": "ak-1", "space_id": "sp-1"},
            "claude-code",
            "claude-code",
            config_path=config_path,
        )

        with open(config_path) as f:
            cfg = json.load(f)
        entry = cfg["harnesses"]["claude-code"]
        assert entry == {
            "project_name": "claude-code",
            "target": "arize",
            "endpoint": "otlp.arize.com:443",
            "api_key": "ak-1",
            "space_id": "sp-1",
        }
        assert "backend" not in cfg
        assert "collector" not in cfg

    def test_write_config_writes_flat_phoenix_entry(self, fake_install):
        from core.setup import write_config

        config_path = str(fake_install / "config.json")
        write_config(
            "phoenix",
            {"endpoint": "http://localhost:6006", "api_key": ""},
            "cursor",
            "cursor",
            config_path=config_path,
        )

        with open(config_path) as f:
            cfg = json.load(f)
        entry = cfg["harnesses"]["cursor"]
        assert entry == {
            "project_name": "cursor",
            "target": "phoenix",
            "endpoint": "http://localhost:6006",
            "api_key": "",
        }
        assert "space_id" not in entry
        assert "backend" not in cfg

    def test_write_config_writes_collector_for_codex(self, fake_install):
        from core.setup import write_config

        config_path = str(fake_install / "config.json")
        write_config(
            "phoenix",
            {"endpoint": "http://localhost:6006", "api_key": ""},
            "codex",
            "codex",
            collector={"host": "127.0.0.1", "port": 4318},
            config_path=config_path,
        )

        with open(config_path) as f:
            cfg = json.load(f)
        assert cfg["harnesses"]["codex"]["collector"] == {"host": "127.0.0.1", "port": 4318}
        # collector must NOT be at top level
        assert "collector" not in cfg

    def test_write_config_preserves_existing_harnesses(self, fake_install):
        from core.setup import write_config

        config_path = str(fake_install / "config.json")
        write_config(
            "phoenix",
            {"endpoint": "http://localhost:6006", "api_key": ""},
            "claude-code",
            "claude-code",
            config_path=config_path,
        )
        write_config(
            "arize",
            {"endpoint": "otlp.arize.com:443", "api_key": "ak-1", "space_id": "sp-1"},
            "copilot",
            "copilot",
            config_path=config_path,
        )

        with open(config_path) as f:
            cfg = json.load(f)
        assert "claude-code" in cfg["harnesses"]
        assert "copilot" in cfg["harnesses"]
        assert cfg["harnesses"]["claude-code"]["target"] == "phoenix"
        assert cfg["harnesses"]["copilot"]["target"] == "arize"

    def test_write_config_strips_top_level_backend_and_collector(self, fake_install):
        from core.setup import write_config

        config_path = str(fake_install / "config.json")
        # Pre-write a config with legacy top-level keys
        legacy = {
            "backend": {"target": "phoenix"},
            "collector": {"host": "127.0.0.1", "port": 4318},
            "harnesses": {},
        }
        with open(config_path, "w") as f:
            json.dump(legacy, f, indent=2)

        write_config(
            "phoenix",
            {"endpoint": "http://localhost:6006", "api_key": ""},
            "cursor",
            "cursor",
            config_path=config_path,
        )

        with open(config_path) as f:
            cfg = json.load(f)
        assert "backend" not in cfg
        assert "collector" not in cfg
        assert "cursor" in cfg["harnesses"]


# ---------------------------------------------------------------------------
# merge_harness_entry() — additional flat schema tests
# ---------------------------------------------------------------------------


class TestMergeHarnessEntryFlat:
    def test_project_name_only(self, fake_install, populated_config):
        """Updates project_name without touching other fields."""
        from core.setup import merge_harness_entry

        merge_harness_entry("claude-code", "renamed-project")

        with open(fake_install / "config.json") as f:
            cfg = json.load(f)
        entry = cfg["harnesses"]["claude-code"]
        assert entry["project_name"] == "renamed-project"
        # Other fields preserved
        assert entry["target"] == "phoenix"
        assert entry["endpoint"] == "http://localhost:6006"

    def test_full_update(self, fake_install, populated_config):
        """credentials param replaces target, endpoint, api_key, space_id."""
        from core.setup import merge_harness_entry

        merge_harness_entry(
            "claude-code",
            "claude-code",
            target="arize",
            credentials={"endpoint": "otlp.arize.com:443", "api_key": "ak-new", "space_id": "sp-new"},
        )

        with open(fake_install / "config.json") as f:
            cfg = json.load(f)
        entry = cfg["harnesses"]["claude-code"]
        assert entry["target"] == "arize"
        assert entry["api_key"] == "ak-new"
        assert entry["space_id"] == "sp-new"

    def test_creates_file(self, fake_install):
        """From nothing, writes minimal {harnesses: {name: {project_name: ...}}}."""
        from core.setup import merge_harness_entry

        merge_harness_entry("copilot", "my-copilot")

        config_path = fake_install / "config.json"
        assert config_path.exists()
        with open(config_path) as f:
            cfg = json.load(f)
        assert cfg == {"harnesses": {"copilot": {"project_name": "my-copilot"}}}


# ---------------------------------------------------------------------------
# remove / list — flat schema
# ---------------------------------------------------------------------------


class TestRemoveHarnessEntryFlat:
    def test_removes_flat_entry(self, fake_install, populated_config):
        from core.setup import remove_harness_entry

        remove_harness_entry("claude-code")

        with open(fake_install / "config.json") as f:
            cfg = json.load(f)
        assert "claude-code" not in cfg.get("harnesses", {})


class TestListInstalledHarnessesFlat:
    def test_returns_names(self, fake_install, populated_config):
        from core.setup import list_installed_harnesses

        result = list_installed_harnesses()
        assert result == ["claude-code"]


# ---------------------------------------------------------------------------
# prompt_backend() — copy-from and masking tests
# ---------------------------------------------------------------------------


class TestPromptBackendCopyFrom:
    def test_copy_from_matching_target(self, monkeypatch):
        from core.setup import prompt_backend

        existing = {
            "claude-code": {
                "project_name": "claude-code",
                "target": "arize",
                "endpoint": "otlp.arize.com:443",
                "api_key": "ak-1",
                "space_id": "sp-1",
            },
        }
        # Choose arize (2), then copy from entry 1
        inputs = iter(["2", "1"])
        monkeypatch.setattr("builtins.input", lambda prompt="": next(inputs))

        target, creds = prompt_backend(existing_harnesses=existing)
        assert target == "arize"
        assert creds["api_key"] == "ak-1"
        assert creds["space_id"] == "sp-1"
        assert creds["endpoint"] == "otlp.arize.com:443"

    def test_no_copy_when_no_matching_target(self, monkeypatch):
        """Only phoenix harnesses installed, user picks arize — no menu shown."""
        from core.setup import prompt_backend

        existing = {
            "claude-code": {
                "project_name": "claude-code",
                "target": "phoenix",
                "endpoint": "http://localhost:6006",
                "api_key": "",
            },
        }
        # Choose arize (2), then provide fresh credentials
        getpass_calls = []

        def mock_getpass(prompt=""):
            getpass_calls.append(prompt)
            return "my-key"

        inputs = iter(["2", "my-space", ""])
        monkeypatch.setattr("builtins.input", lambda prompt="": next(inputs))
        monkeypatch.setattr("core.setup.getpass", mock_getpass)
        monkeypatch.setattr(sys.stdout, "isatty", lambda: False)

        target, creds = prompt_backend(existing_harnesses=existing)
        assert target == "arize"
        assert creds["api_key"] == "my-key"

    def test_enter_new_from_menu(self, monkeypatch):
        """When copy-from menu is shown and user picks 'Enter new credentials'."""
        from core.setup import prompt_backend

        existing = {
            "claude-code": {
                "project_name": "claude-code",
                "target": "phoenix",
                "endpoint": "http://localhost:6006",
                "api_key": "",
            },
        }
        # Choose phoenix (1), pick "Enter new credentials" (2), then provide endpoint + key
        getpass_calls = []

        def mock_getpass(prompt=""):
            getpass_calls.append(prompt)
            return ""

        inputs = iter(["1", "2", "http://custom:9999"])
        monkeypatch.setattr("builtins.input", lambda prompt="": next(inputs))
        monkeypatch.setattr("core.setup.getpass", mock_getpass)

        target, creds = prompt_backend(existing_harnesses=existing)
        assert target == "phoenix"
        assert creds["endpoint"] == "http://custom:9999"
        # Should have gone through fresh prompts
        assert len(getpass_calls) == 1  # api_key prompt


class TestPromptBackendMasking:
    def test_arize_masks_api_key(self, monkeypatch):
        """api_key prompt routes through getpass, space_id/endpoint through input."""
        from core.setup import prompt_backend

        getpass_calls = []
        input_calls = []

        def mock_getpass(prompt=""):
            getpass_calls.append(prompt)
            return "secret-key"

        real_inputs = iter(["2", "my-space", ""])

        def mock_input(prompt=""):
            input_calls.append(prompt)
            return next(real_inputs)

        monkeypatch.setattr("builtins.input", mock_input)
        monkeypatch.setattr("core.setup.getpass", mock_getpass)
        monkeypatch.setattr(sys.stdout, "isatty", lambda: False)

        target, creds = prompt_backend()
        assert target == "arize"
        assert creds["api_key"] == "secret-key"
        # getpass was called for api_key
        assert len(getpass_calls) == 1
        assert "API Key" in getpass_calls[0]
        # space_id went through input
        assert any("Space ID" in p for p in input_calls)

    def test_phoenix_masks_api_key(self, monkeypatch):
        """Phoenix api_key prompt routes through getpass."""
        from core.setup import prompt_backend

        getpass_calls = []
        input_calls = []

        def mock_getpass(prompt=""):
            getpass_calls.append(prompt)
            return ""

        real_inputs = iter(["1", ""])

        def mock_input(prompt=""):
            input_calls.append(prompt)
            return next(real_inputs)

        monkeypatch.setattr("builtins.input", mock_input)
        monkeypatch.setattr("core.setup.getpass", mock_getpass)

        target, creds = prompt_backend()
        assert target == "phoenix"
        # getpass was called for api_key
        assert len(getpass_calls) == 1
        assert "API Key" in getpass_calls[0]
