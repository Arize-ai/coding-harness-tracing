"""Tests for tracing.opencode.install: install/uninstall of the opencode plugin shim.

Mirrors tests/tracing/gemini/test_install_gemini.py but adapted for the file-drop
delivery: instead of merging hooks into a JSON settings file, opencode's
install copies the shipped TS plugin into ``~/.config/opencode/plugin/`` and
uninstall deletes it (only when it's our file — header-marker guard).
"""

from __future__ import annotations

import json

import pytest

import tracing.opencode.constants as _oc
import tracing.opencode.install as _install

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
    """Set cwd to tmp_path and patch core.setup + tracing.opencode.constants paths for isolation."""
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

    # Redirect opencode plugin paths into the temp tree (mirror gemini's SETTINGS_FILE redirect)
    plugin_dir = tmp_path / ".config" / "opencode" / "plugin"
    monkeypatch.setattr(_oc, "OPENCODE_CONFIG_DIR", tmp_path / ".config" / "opencode")
    monkeypatch.setattr(_oc, "PLUGIN_DIR", plugin_dir)
    monkeypatch.setattr(_oc, "PLUGIN_FILE", plugin_dir / "arize-tracing.ts")

    return tmp_path


@pytest.fixture
def plugin_file(cwd_tmp):
    """Return the path to the redirected opencode plugin file."""
    return _oc.PLUGIN_FILE


@pytest.fixture
def plugin_source_text(cwd_tmp):
    """Return the text contents of the shipped plugin source asset."""
    return _oc.PLUGIN_SOURCE.read_text(encoding="utf-8")


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
        entry = config["harnesses"]["opencode"]
        assert entry["target"] == expected_target
        assert entry["project_name"] == "opencode"
        assert entry["endpoint"] == backend[1]["endpoint"]
        assert entry["api_key"] == backend[1]["api_key"]

        if expected_target == "arize":
            assert entry["space_id"] == backend[1]["space_id"]

        # No collector for opencode
        assert "collector" not in entry

    def test_plugin_file_created(self, plugin_file, monkeypatch):
        _mock_prompts(monkeypatch)
        install()
        assert plugin_file.is_file()

    def test_plugin_dir_created(self, plugin_file, monkeypatch):
        """install should create the plugin parent directory if missing."""
        _mock_prompts(monkeypatch)
        # Sanity check — plugin dir doesn't exist before install
        assert not plugin_file.parent.exists()
        install()
        assert plugin_file.parent.is_dir()

    def test_plugin_file_content_matches_source(self, plugin_file, plugin_source_text, monkeypatch):
        """The installed plugin file content must equal the shipped source asset."""
        _mock_prompts(monkeypatch)
        install()
        assert plugin_file.read_text(encoding="utf-8") == plugin_source_text

    def test_plugin_file_has_arize_header_marker(self, plugin_file, monkeypatch):
        """The shim's first line must contain the Arize header marker comment.

        This marker is the basis for the uninstall guard, so we assert it lives in the
        installed file.
        """
        _mock_prompts(monkeypatch)
        install()
        text = plugin_file.read_text(encoding="utf-8")
        assert text.startswith("// Arize opencode tracing plugin (shim).")


# ---------------------------------------------------------------------------
# Plugin source resolution (regression for site-packages FileNotFoundError)
# ---------------------------------------------------------------------------


class TestPluginSourceResolution:
    """The installer must locate its bundled .ts relative to install.py itself.

    Regression for the real-install crash: the shell router runs install.py from
    ~/.arize/harness/, but ``tracing.opencode.constants`` is imported from the
    venv site-packages copy, which does not ship the .ts data asset. Resolving
    the source via constants.PLUGIN_SOURCE therefore pointed at a nonexistent
    site-packages path and raised FileNotFoundError.
    """

    def test_source_resolved_independently_of_constants(self, cwd_tmp, plugin_file, monkeypatch):
        _mock_prompts(monkeypatch)
        # Simulate constants imported from a tree that lacks the data asset
        # (the venv site-packages situation).
        monkeypatch.setattr(_oc, "PLUGIN_SOURCE", cwd_tmp / "no-such-tree" / "arize-tracing.ts")

        install()

        assert plugin_file.is_file()
        assert plugin_file.read_text(encoding="utf-8").startswith("// Arize opencode tracing plugin (shim).")

    def test_plugin_source_points_at_existing_asset(self):
        """_plugin_source() resolves to a file that actually exists on disk."""
        src = _install._plugin_source()
        assert src.is_file()
        assert src.name == "arize-tracing.ts"


class TestPluginChildSessionContract:
    def test_fetches_task_child_session_with_exact_call_id(self, plugin_source_text):
        assert "fetchChildSessions" in plugin_source_text
        assert "parentCallID" in plugin_source_text
        assert "metadata?.sessionId" in plugin_source_text
        assert "client.session.get" in plugin_source_text
        assert "client.session.messages" in plugin_source_text

    def test_forwards_child_sessions_in_snapshot(self, plugin_source_text):
        assert "childSessions" in plugin_source_text
        assert "forward({ type, sessionID, messages, childSessions })" in plugin_source_text

    def test_suppresses_independent_child_session_snapshots(self, plugin_source_text):
        assert "const sessionInfoRes = await client.session.get" in plugin_source_text
        assert "const sessionInfo = successfulSession(sessionInfoRes, sessionID)" in plugin_source_text
        assert "if (!sessionInfo || sessionInfo.parentID) return" in plugin_source_text


# ---------------------------------------------------------------------------
# Install tests — second harness (copy-from)
# ---------------------------------------------------------------------------


class TestInstallSecondHarnessOffersCopyFrom:
    """When another harness exists with the same target, copy-from is offered."""

    def test_copy_from_populates_credentials(self, cwd_tmp, monkeypatch):
        """Pre-seed a claude-code entry; opencode install should receive it in prompt_backend."""
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

        assert captured["existing_harnesses"] is not None
        assert "claude-code" in captured["existing_harnesses"]
        assert captured["existing_harnesses"]["claude-code"]["target"] == "arize"

        config = json.loads(config_path.read_text())
        entry = config["harnesses"]["opencode"]
        assert entry["target"] == "arize"
        assert entry["endpoint"] == ARIZE_BACKEND[1]["endpoint"]
        assert entry["api_key"] == ARIZE_BACKEND[1]["api_key"]
        assert entry["space_id"] == ARIZE_BACKEND[1]["space_id"]
        assert entry["project_name"] == "opencode"


# ---------------------------------------------------------------------------
# Install tests — existing opencode entry (re-install)
# ---------------------------------------------------------------------------


class TestInstallExistingOpencodeEntryOnlyUpdatesProjectName:
    """Re-install with existing opencode config only updates project_name."""

    def test_existing_entry_preserves_target(self, cwd_tmp, monkeypatch):
        config_dir = cwd_tmp / ".arize" / "harness"
        config_dir.mkdir(parents=True, exist_ok=True)
        config_path = config_dir / "config.json"

        seed_config = {
            "harnesses": {
                "opencode": {
                    "project_name": "opencode",
                    "target": "arize",
                    "endpoint": "otlp.arize.com:443",
                    "api_key": "ak-existing",
                    "space_id": "space-existing",
                },
            },
        }
        config_path.write_text(json.dumps(seed_config, indent=2))

        monkeypatch.setattr(_install, "prompt_project_name", lambda default: "my-opencode")
        monkeypatch.setattr(
            _install,
            "prompt_content_logging",
            lambda: {"prompts": True, "tool_details": True, "tool_content": True},
        )
        monkeypatch.setattr(_install, "write_logging_config", lambda block, config_path=None: None)
        monkeypatch.setattr("sys.stdout", _fake_stdout())

        install()

        config = json.loads(config_path.read_text())
        entry = config["harnesses"]["opencode"]
        assert entry["project_name"] == "my-opencode"
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
    """Re-install is idempotent — running twice leaves a single, correct plugin file."""

    def test_no_duplicate_plugin_files(self, plugin_file, monkeypatch):
        _mock_prompts(monkeypatch)
        install()
        install()
        # The plugin dir should still contain exactly one *.ts file with our name
        assert plugin_file.is_file()
        siblings = list(plugin_file.parent.glob("arize-tracing*.ts"))
        assert siblings == [plugin_file]

    def test_reinstall_content_matches_source(self, plugin_file, plugin_source_text, monkeypatch):
        """Even after two installs, content remains equal to the shipped source."""
        _mock_prompts(monkeypatch)
        install()
        install()
        assert plugin_file.read_text(encoding="utf-8") == plugin_source_text


# ---------------------------------------------------------------------------
# Preserve user files in the plugin dir
# ---------------------------------------------------------------------------


class TestInstallPreservesUnrelatedPlugins:
    """Install must not touch other files in the opencode plugin dir."""

    def test_preserves_other_plugin_files(self, plugin_file, monkeypatch):
        """A user's own plugin file should survive install."""
        plugin_file.parent.mkdir(parents=True, exist_ok=True)
        other = plugin_file.parent / "user-plugin.ts"
        other_text = "// user's own plugin\nexport const X = 1;\n"
        other.write_text(other_text, encoding="utf-8")

        _mock_prompts(monkeypatch)
        install()

        assert other.is_file()
        assert other.read_text(encoding="utf-8") == other_text
        # Our file is also present
        assert plugin_file.is_file()


# ---------------------------------------------------------------------------
# Missing plugin dir
# ---------------------------------------------------------------------------


class TestInstallHandlesMissingPluginDir:
    """Install creates ~/.config/opencode/plugin/ if it doesn't exist."""

    def test_creates_plugin_dir_and_file(self, cwd_tmp, monkeypatch):
        """When ~/.config/opencode/plugin doesn't exist, install creates it."""
        _mock_prompts(monkeypatch)
        install()
        assert _oc.PLUGIN_DIR.is_dir()
        assert _oc.PLUGIN_FILE.is_file()


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
            harnesses = (config or {}).get("harnesses", {})
            assert "opencode" not in harnesses

    def test_plugin_file_removed(self, plugin_file, monkeypatch):
        _mock_prompts(monkeypatch)
        install()
        assert plugin_file.is_file()
        uninstall()
        assert not plugin_file.is_file()

    def test_uninstall_is_idempotent(self, cwd_tmp, monkeypatch):
        """Running uninstall twice succeeds without error."""
        _mock_prompts(monkeypatch)
        install()
        uninstall()
        uninstall()  # second call — no exception
        config_path = cwd_tmp / ".arize" / "harness" / "config.json"
        if config_path.is_file():
            config = json.loads(config_path.read_text())
            harnesses = (config or {}).get("harnesses", {})
            assert "opencode" not in harnesses

    def test_uninstall_when_no_plugin_file(self, cwd_tmp, monkeypatch):
        """Uninstall with no plugin file installed does not raise."""
        monkeypatch.setattr("sys.stdout", _fake_stdout())
        assert not _oc.PLUGIN_FILE.is_file()
        uninstall()  # should not raise

    def test_uninstall_when_no_plugin_dir(self, cwd_tmp, monkeypatch):
        """Uninstall with no plugin parent dir does not raise."""
        monkeypatch.setattr("sys.stdout", _fake_stdout())
        assert not _oc.PLUGIN_DIR.exists()
        uninstall()  # should not raise


class TestUninstallGuardsForeignPluginFile:
    """Uninstall must NOT delete a plugin file that lacks our header marker."""

    def test_non_arize_file_not_deleted(self, plugin_file, monkeypatch):
        """A file at PLUGIN_FILE without our header marker is preserved."""
        plugin_file.parent.mkdir(parents=True, exist_ok=True)
        foreign_text = "// SomeoneElse's plugin\nexport const X = 1;\n"
        plugin_file.write_text(foreign_text, encoding="utf-8")

        monkeypatch.setattr("sys.stdout", _fake_stdout())
        uninstall()

        # Foreign file remains untouched
        assert plugin_file.is_file()
        assert plugin_file.read_text(encoding="utf-8") == foreign_text

    def test_preserves_other_user_plugin_files(self, plugin_file, monkeypatch):
        """Uninstall must not touch unrelated *.ts files in the same plugin dir."""
        _mock_prompts(monkeypatch)
        install()

        other = plugin_file.parent / "user-plugin.ts"
        other_text = "// user's own plugin\nexport const X = 1;\n"
        other.write_text(other_text, encoding="utf-8")

        uninstall()

        # Our file is gone…
        assert not plugin_file.is_file()
        # …but the user's file survives
        assert other.is_file()
        assert other.read_text(encoding="utf-8") == other_text


# ---------------------------------------------------------------------------
# Dry-run tests
# ---------------------------------------------------------------------------


class TestInstallDryRunWritesNothing:
    """Dry-run mode writes nothing."""

    def test_dry_run_no_plugin_file(self, plugin_file, monkeypatch):
        monkeypatch.setenv("ARIZE_DRY_RUN", "true")
        _mock_prompts(monkeypatch)
        install()
        assert not plugin_file.is_file()

    def test_dry_run_no_plugin_dir(self, cwd_tmp, monkeypatch):
        """Dry-run install should not create the plugin dir."""
        monkeypatch.setenv("ARIZE_DRY_RUN", "true")
        _mock_prompts(monkeypatch)
        install()
        assert not _oc.PLUGIN_DIR.exists()

    def test_dry_run_no_config(self, cwd_tmp, monkeypatch):
        monkeypatch.setenv("ARIZE_DRY_RUN", "true")
        _mock_prompts(monkeypatch)
        install()
        config_path = cwd_tmp / ".arize" / "harness" / "config.json"
        assert not config_path.is_file()


class TestUninstallDryRunWritesNothing:
    """Dry-run uninstall preserves all on-disk state."""

    def test_dry_run_uninstall_preserves_plugin_file(self, plugin_file, monkeypatch):
        _mock_prompts(monkeypatch)
        install()
        original = plugin_file.read_text(encoding="utf-8")

        monkeypatch.setenv("ARIZE_DRY_RUN", "true")
        uninstall()

        assert plugin_file.is_file()
        assert plugin_file.read_text(encoding="utf-8") == original

    def test_dry_run_uninstall_no_plugin_no_error(self, cwd_tmp, monkeypatch):
        """Dry-run uninstall with no plugin file should not error."""
        monkeypatch.setenv("ARIZE_DRY_RUN", "true")
        monkeypatch.setattr("sys.stdout", _fake_stdout())
        uninstall()  # should not raise


# ---------------------------------------------------------------------------
# CLI main() dispatch tests
# ---------------------------------------------------------------------------


class TestMainDispatch:
    """main() dispatches install/uninstall based on argv."""

    def test_bad_args_exits_1(self, monkeypatch):
        monkeypatch.setattr("sys.argv", ["tracing.opencode.install", "bogus"])
        with pytest.raises(SystemExit) as exc_info:
            _install.main()
        assert exc_info.value.code == 1

    def test_no_args_exits_1(self, monkeypatch):
        monkeypatch.setattr("sys.argv", ["tracing.opencode.install"])
        with pytest.raises(SystemExit) as exc_info:
            _install.main()
        assert exc_info.value.code == 1

    def test_install_arg_calls_install(self, cwd_tmp, monkeypatch):
        _mock_prompts(monkeypatch)
        monkeypatch.setattr("sys.argv", ["tracing.opencode.install", "install"])
        called = []
        monkeypatch.setattr(_install, "install", lambda with_skills=False: called.append("install"))
        _install.main()
        assert called == ["install"]

    def test_uninstall_arg_calls_uninstall(self, cwd_tmp, monkeypatch):
        monkeypatch.setattr("sys.argv", ["tracing.opencode.install", "uninstall"])
        called = []
        monkeypatch.setattr(_install, "uninstall", lambda: called.append("uninstall"))
        _install.main()
        assert called == ["uninstall"]


# ---------------------------------------------------------------------------
# Constants verification
# ---------------------------------------------------------------------------


class TestConstants:
    """Verify opencode constants are well-formed."""

    def test_harness_name_is_opencode(self):
        assert _oc.HARNESS_NAME == "opencode"

    def test_plugin_file_under_plugin_dir(self):
        assert _oc.PLUGIN_FILE.parent == _oc.PLUGIN_DIR

    def test_plugin_file_name(self):
        assert _oc.PLUGIN_FILE.name == "arize-tracing.ts"

    def test_plugin_source_exists_in_repo(self):
        assert _oc.PLUGIN_SOURCE.is_file()

    def test_plugin_source_has_header_marker(self):
        text = _oc.PLUGIN_SOURCE.read_text(encoding="utf-8")
        assert text.startswith("// Arize opencode tracing plugin (shim).")


# ---------------------------------------------------------------------------
# Install calls prompts for logging when no block exists
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Skills wiring (--with-skills)
# ---------------------------------------------------------------------------


class TestInstallSkillsWiring:
    """install() honors with_skills and symlinks the management skill."""

    def test_install_with_skills_true_calls_symlink(self, cwd_tmp, monkeypatch):
        _mock_prompts(monkeypatch)
        calls = []
        monkeypatch.setattr(_install, "symlink_skills", lambda harness: calls.append(harness))
        install(with_skills=True)
        assert calls == ["opencode"]

    def test_install_default_does_not_symlink(self, cwd_tmp, monkeypatch):
        _mock_prompts(monkeypatch)
        calls = []
        monkeypatch.setattr(_install, "symlink_skills", lambda harness: calls.append(harness))
        install()
        assert calls == []

    def test_install_with_skills_false_does_not_symlink(self, cwd_tmp, monkeypatch):
        _mock_prompts(monkeypatch)
        calls = []
        monkeypatch.setattr(_install, "symlink_skills", lambda harness: calls.append(harness))
        install(with_skills=False)
        assert calls == []

    def test_round_trip_creates_then_removes_symlink(self, cwd_tmp, monkeypatch):
        """install --with-skills creates the symlink; uninstall removes it."""
        _mock_prompts(monkeypatch)
        # Stage a fake skills source under the temp INSTALL_DIR that
        # harness_dir("opencode") resolves to.
        skills_src = cwd_tmp / ".arize" / "harness" / "tracing" / "opencode" / "skills"
        skill = skills_src / "manage-opencode-tracing"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("# manage-opencode-tracing\n")

        install(with_skills=True)
        link = cwd_tmp / ".agents" / "skills" / "manage-opencode-tracing"
        assert link.is_symlink()

        uninstall()
        assert not link.exists()


class TestMainDispatchSkillsFlag:
    """main() parses --with-skills and forwards it to install()."""

    def test_main_install_with_skills_flag(self, cwd_tmp, monkeypatch):
        monkeypatch.setattr("sys.argv", ["tracing.opencode.install", "install", "--with-skills"])
        captured = {}
        monkeypatch.setattr(_install, "install", lambda with_skills=False: captured.update(with_skills=with_skills))
        _install.main()
        assert captured == {"with_skills": True}

    def test_main_install_without_skills_flag(self, cwd_tmp, monkeypatch):
        monkeypatch.setattr("sys.argv", ["tracing.opencode.install", "install"])
        captured = {}
        monkeypatch.setattr(_install, "install", lambda with_skills=False: captured.update(with_skills=with_skills))
        _install.main()
        assert captured == {"with_skills": False}


# ---------------------------------------------------------------------------
# Setup wizard delegation
# ---------------------------------------------------------------------------


class TestSetupOpencodeModule:
    """core/setup/opencode.py delegates to tracing.opencode.install."""

    def test_setup_module_importable(self):
        from core.setup.opencode import install, main, uninstall

        assert callable(install)
        assert callable(uninstall)
        assert callable(main)

    def test_setup_install_delegates(self, cwd_tmp, monkeypatch):
        """core.setup.opencode.install() should call tracing.opencode.install.install()."""
        import core.setup.opencode as setup_opencode

        called = []
        monkeypatch.setattr(_install, "install", lambda: called.append("install"))
        setup_opencode.install()
        assert called == ["install"]

    def test_setup_uninstall_delegates(self, cwd_tmp, monkeypatch):
        """core.setup.opencode.uninstall() should call tracing.opencode.install.uninstall()."""
        import core.setup.opencode as setup_opencode

        called = []
        monkeypatch.setattr(_install, "uninstall", lambda: called.append("uninstall"))
        setup_opencode.uninstall()
        assert called == ["uninstall"]

    def test_setup_main_delegates_to_install(self, cwd_tmp, monkeypatch):
        """core.setup.opencode.main() should call tracing.opencode.install.install()."""
        import core.setup.opencode as setup_opencode

        called = []
        monkeypatch.setattr(_install, "install", lambda: called.append("install"))
        setup_opencode.main()
        assert called == ["install"]
