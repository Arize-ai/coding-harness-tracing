"""Tests for tracing.copilot/install.py: install and uninstall of Copilot hooks."""

from __future__ import annotations

import json

import pytest

import tracing.copilot.install as _install

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
    """Set cwd to tmp_path and patch core.setup paths for isolation."""
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

    return tmp_path


@pytest.fixture
def hooks_dir(cwd_tmp):
    """Return the .github/hooks directory under the temp cwd."""
    return cwd_tmp / ".github" / "hooks"


# ---------------------------------------------------------------------------
# Install tests
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
    def test_fresh_install_creates_config_and_hooks(self, cwd_tmp, monkeypatch, backend, expected_target):
        _mock_prompts(monkeypatch, backend=backend)
        install()

        config_path = cwd_tmp / ".arize" / "harness" / "config.json"
        assert config_path.is_file()
        config = json.loads(config_path.read_text())
        entry = config["harnesses"]["copilot"]
        assert entry["target"] == expected_target
        assert entry["project_name"] == "copilot"
        assert entry["endpoint"] == backend[1]["endpoint"]
        assert entry["api_key"] == backend[1]["api_key"]

        if expected_target == "arize":
            assert entry["space_id"] == backend[1]["space_id"]

        # No collector for copilot
        assert "collector" not in entry

    def test_hooks_json_created(self, hooks_dir, monkeypatch):
        _mock_prompts(monkeypatch)
        install()
        assert (hooks_dir / "hooks.json").is_file()

    def test_hooks_json_structure(self, hooks_dir, monkeypatch):
        """hooks.json must follow the VS Code Copilot Chat schema:
        {"hooks": {"<EventName>": [{"type": "command", "command": "<cmd>"}]}}
        """
        _mock_prompts(monkeypatch)
        install()
        data = json.loads((hooks_dir / "hooks.json").read_text())
        assert set(data["hooks"].keys()) == {
            "SessionStart",
            "UserPromptSubmit",
            "PreToolUse",
            "PostToolUse",
            "Stop",
            "SubagentStop",
        }
        for event, entries in data["hooks"].items():
            assert len(entries) == 1
            assert entries[0]["type"] == "command"
            assert "arize-hook-copilot-" in entries[0]["command"]

    def test_only_hooks_json_written(self, hooks_dir, monkeypatch):
        _mock_prompts(monkeypatch)
        install()
        json_files = list(hooks_dir.glob("*.json"))
        assert len(json_files) == 1
        assert json_files[0].name == "hooks.json"


class TestInstallSecondHarnessOffersCopyFrom:
    """When another harness exists with the same target, copy-from is offered."""

    def test_copy_from_populates_credentials(self, cwd_tmp, monkeypatch):
        """Pre-seed a claude-code entry; copilot install should receive it in prompt_backend."""
        config_dir = cwd_tmp / ".arize" / "harness"
        config_dir.mkdir(parents=True, exist_ok=True)
        config_path = config_dir / "config.json"

        # Pre-seed with claude-code arize entry
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

        # Verify the copilot entry was actually written with correct credentials
        config = json.loads(config_path.read_text())
        entry = config["harnesses"]["copilot"]
        assert entry["target"] == "arize"
        assert entry["endpoint"] == ARIZE_BACKEND[1]["endpoint"]
        assert entry["api_key"] == ARIZE_BACKEND[1]["api_key"]
        assert entry["space_id"] == ARIZE_BACKEND[1]["space_id"]
        assert entry["project_name"] == "copilot"


class TestInstallExistingCopilotEntryOnlyUpdatesProjectName:
    """Re-install with existing copilot config only updates project_name."""

    def test_existing_entry_preserves_target(self, cwd_tmp, monkeypatch):
        config_dir = cwd_tmp / ".arize" / "harness"
        config_dir.mkdir(parents=True, exist_ok=True)
        config_path = config_dir / "config.json"

        seed_config = {
            "harnesses": {
                "copilot": {
                    "project_name": "copilot",
                    "target": "arize",
                    "endpoint": "otlp.arize.com:443",
                    "api_key": "ak-existing",
                    "space_id": "space-existing",
                },
            },
        }
        config_path.write_text(json.dumps(seed_config, indent=2))

        # prompt_project_name returns a new name
        monkeypatch.setattr(_install, "prompt_project_name", lambda default: "my-copilot")
        monkeypatch.setattr(
            _install,
            "prompt_content_logging",
            lambda: {"prompts": True, "tool_details": True, "tool_content": True},
        )
        monkeypatch.setattr(_install, "write_logging_config", lambda block, config_path=None: None)
        monkeypatch.setattr("sys.stdout", _fake_stdout())

        install()

        config = json.loads(config_path.read_text())
        entry = config["harnesses"]["copilot"]
        assert entry["project_name"] == "my-copilot"
        # Other fields preserved
        assert entry["target"] == "arize"
        assert entry["endpoint"] == "otlp.arize.com:443"
        assert entry["api_key"] == "ak-existing"
        assert entry["space_id"] == "space-existing"


class TestIdempotent:
    """Re-install is idempotent — no duplicate entries."""

    def test_no_duplicate_entries(self, hooks_dir, monkeypatch):
        _mock_prompts(monkeypatch)
        install()
        install()
        data = json.loads((hooks_dir / "hooks.json").read_text())
        for event, entries in data["hooks"].items():
            assert len(entries) == 1, f"Duplicate entries for event {event}"


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
            assert "copilot" not in harnesses

    def test_hooks_json_removed(self, hooks_dir, monkeypatch):
        _mock_prompts(monkeypatch)
        install()
        assert (hooks_dir / "hooks.json").is_file()
        uninstall()
        assert not (hooks_dir / "hooks.json").exists()

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
            assert "copilot" not in harnesses


class TestUninstallPreservesUserHooks:
    """Uninstall on a pre-populated hooks.json preserves unrelated user hooks."""

    def test_preserves_user_hooks(self, hooks_dir, monkeypatch):
        _mock_prompts(monkeypatch)
        install()

        # Add user-defined entries: a brand-new event, plus an extra command
        # alongside ours under SessionStart.
        hf = hooks_dir / "hooks.json"
        data = json.loads(hf.read_text())
        data["hooks"]["CustomEvent"] = [{"type": "command", "command": "/usr/local/bin/my-hook"}]
        data["hooks"]["SessionStart"].append({"type": "command", "command": "/usr/local/bin/user-session"})
        hf.write_text(json.dumps(data, indent=2) + "\n")

        uninstall()

        assert hf.is_file()
        remaining = json.loads(hf.read_text())
        assert remaining["hooks"]["CustomEvent"] == [{"type": "command", "command": "/usr/local/bin/my-hook"}]
        assert remaining["hooks"]["SessionStart"] == [{"type": "command", "command": "/usr/local/bin/user-session"}]


# ---------------------------------------------------------------------------
# Dry-run tests
# ---------------------------------------------------------------------------


class TestInstallDryRunWritesNothing:
    """Dry-run mode writes nothing."""

    def test_dry_run_no_files(self, hooks_dir, monkeypatch):
        monkeypatch.setenv("ARIZE_DRY_RUN", "true")
        _mock_prompts(monkeypatch)
        install()
        json_files = list(hooks_dir.glob("*.json")) if hooks_dir.exists() else []
        assert len(json_files) == 0

    def test_dry_run_no_config(self, cwd_tmp, monkeypatch):
        monkeypatch.setenv("ARIZE_DRY_RUN", "true")
        _mock_prompts(monkeypatch)
        install()
        config_path = cwd_tmp / ".arize" / "harness" / "config.json"
        assert not config_path.is_file()
