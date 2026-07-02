"""Tests for tracing.omp.install: install/uninstall of the omp tracing shim.

omp is a **hybrid** of two existing installers:

* The config-prompt flow + ``.ts`` file-drop come from
  ``tracing/opencode/install.py`` (opencode loads plugins in-process inside its
  Bun runtime, so delivery is a file drop, not a settings merge).
* The **JSON settings.json read-merge-write** comes from
  ``tracing/gemini/install.py`` — unlike opencode, omp does NOT auto-discover a
  plugin dir. We must register the shim's absolute path in the ``extensions``
  array of ``~/.omp/agent/settings.json``.

These tests therefore mirror ``tests/tracing/opencode/test_install_opencode.py``
for the file-drop side and borrow the settings-merge assertions from
``tests/tracing/gemini/test_install_gemini.py`` for the extensions-array side.
"""

from __future__ import annotations

import json

import pytest

import tracing.omp.constants as _omp
import tracing.omp.install as _install

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

_HEADER_MARKER = "// Arize omp tracing hook (shim)."


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
    """Set cwd to tmp_path and patch core.setup + tracing.omp.constants paths for isolation."""
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

    # Redirect omp settings + extension paths into the temp tree.
    settings_dir = tmp_path / ".omp" / "agent"
    extensions_dir = tmp_path / ".omp" / "extensions"
    monkeypatch.setattr(_omp, "SETTINGS_DIR", settings_dir)
    monkeypatch.setattr(_omp, "SETTINGS_FILE", settings_dir / "settings.json")
    monkeypatch.setattr(_omp, "EXTENSIONS_DIR", extensions_dir)
    monkeypatch.setattr(_omp, "PLUGIN_FILE", extensions_dir / "arize-tracing.ts")

    return tmp_path


@pytest.fixture
def settings_file(cwd_tmp):
    """Return the path to the redirected omp settings.json."""
    return _omp.SETTINGS_FILE


@pytest.fixture
def plugin_file(cwd_tmp):
    """Return the path to the redirected omp plugin (shim) file."""
    return _omp.PLUGIN_FILE


@pytest.fixture
def plugin_source_text(cwd_tmp):
    """Return the text contents of the shipped plugin source asset."""
    return _omp.PLUGIN_SOURCE.read_text(encoding="utf-8")


def _read_settings(settings_file):
    return json.loads(settings_file.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Install tests — fresh install (config.json harness entry)
# ---------------------------------------------------------------------------


class TestInstallFreshWritesFlatHarnessEntry:
    """Fresh install writes a flat harness entry to config.json."""

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
        entry = config["harnesses"]["omp"]
        assert entry["target"] == expected_target
        assert entry["project_name"] == "omp"
        assert entry["endpoint"] == backend[1]["endpoint"]
        assert entry["api_key"] == backend[1]["api_key"]

        if expected_target == "arize":
            assert entry["space_id"] == backend[1]["space_id"]

        # No collector for omp
        assert "collector" not in entry


# ---------------------------------------------------------------------------
# Install tests — shim file drop
# ---------------------------------------------------------------------------


class TestInstallCopiesShim:
    """install() copies the shipped .ts into ~/.omp/extensions/."""

    def test_plugin_file_created(self, plugin_file, monkeypatch):
        _mock_prompts(monkeypatch)
        install()
        assert plugin_file.is_file()

    def test_extensions_dir_created(self, plugin_file, monkeypatch):
        """install should create the extensions parent dir if missing."""
        _mock_prompts(monkeypatch)
        assert not plugin_file.parent.exists()
        install()
        assert plugin_file.parent.is_dir()

    def test_plugin_file_content_matches_source(self, plugin_file, plugin_source_text, monkeypatch):
        """The installed shim content must equal the shipped source asset (not inline text)."""
        _mock_prompts(monkeypatch)
        install()
        assert plugin_file.read_text(encoding="utf-8") == plugin_source_text

    def test_plugin_file_has_arize_header_marker(self, plugin_file, monkeypatch):
        """The shim's first line must carry the Arize header marker (basis for uninstall guard)."""
        _mock_prompts(monkeypatch)
        install()
        text = plugin_file.read_text(encoding="utf-8")
        assert text.startswith(_HEADER_MARKER)


# ---------------------------------------------------------------------------
# Install tests — extensions-array registration in settings.json
# ---------------------------------------------------------------------------


class TestInstallRegistersExtension:
    """install() appends the shim's absolute path to settings.json "extensions"."""

    def test_settings_file_created(self, settings_file, monkeypatch):
        _mock_prompts(monkeypatch)
        install()
        assert settings_file.is_file()

    def test_settings_dir_created(self, settings_file, monkeypatch):
        """install should create ~/.omp/agent/ if missing."""
        _mock_prompts(monkeypatch)
        assert not settings_file.parent.exists()
        install()
        assert settings_file.parent.is_dir()

    def test_extensions_array_contains_plugin_path(self, settings_file, plugin_file, monkeypatch):
        _mock_prompts(monkeypatch)
        install()
        data = _read_settings(settings_file)
        assert isinstance(data.get("extensions"), list)
        assert str(plugin_file) in data["extensions"]

    def test_settings_json_pretty_printed_with_trailing_newline(self, settings_file, monkeypatch):
        _mock_prompts(monkeypatch)
        install()
        text = settings_file.read_text(encoding="utf-8")
        # Pretty-printed (indented) and ends with a newline.
        assert "\n  " in text
        assert text.endswith("\n")

    def test_extensions_entry_is_absolute_path(self, settings_file, plugin_file, monkeypatch):
        _mock_prompts(monkeypatch)
        install()
        data = _read_settings(settings_file)
        entry = [e for e in data["extensions"] if e == str(plugin_file)]
        assert entry, "shim path missing from extensions array"
        assert entry[0].endswith("arize-tracing.ts")


# ---------------------------------------------------------------------------
# Install tests — preserve unrelated settings.json content
# ---------------------------------------------------------------------------


class TestInstallPreservesSettings:
    """install must read-merge-write — never clobber unrelated keys/entries."""

    def test_preserves_unrelated_top_level_key(self, settings_file, plugin_file, monkeypatch):
        settings_file.parent.mkdir(parents=True, exist_ok=True)
        existing = {"theme": "dark", "model": {"provider": "anthropic"}}
        settings_file.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")

        _mock_prompts(monkeypatch)
        install()

        data = _read_settings(settings_file)
        assert data["theme"] == "dark"
        assert data["model"] == {"provider": "anthropic"}
        assert str(plugin_file) in data["extensions"]

    def test_preserves_existing_user_extensions(self, settings_file, plugin_file, monkeypatch):
        """A user's own extension paths in the array survive install."""
        settings_file.parent.mkdir(parents=True, exist_ok=True)
        user_ext = "/Users/me/.omp/extensions/my-own.ts"
        existing = {"extensions": [user_ext]}
        settings_file.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")

        _mock_prompts(monkeypatch)
        install()

        data = _read_settings(settings_file)
        assert user_ext in data["extensions"]
        assert str(plugin_file) in data["extensions"]

    def test_coerces_non_list_extensions_to_list(self, settings_file, plugin_file, monkeypatch):
        """A malformed (non-list) extensions value is overwritten with a fresh list."""
        settings_file.parent.mkdir(parents=True, exist_ok=True)
        existing = {"extensions": "not-a-list", "theme": "light"}
        settings_file.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")

        _mock_prompts(monkeypatch)
        install()

        data = _read_settings(settings_file)
        assert isinstance(data["extensions"], list)
        assert str(plugin_file) in data["extensions"]
        # Unrelated key still preserved.
        assert data["theme"] == "light"


# ---------------------------------------------------------------------------
# Install tests — missing / empty / malformed settings.json
# ---------------------------------------------------------------------------


class TestInstallHandlesMissingSettings:
    """Install creates settings.json if missing and tolerates an empty file."""

    def test_handles_empty_settings_file(self, settings_file, plugin_file, monkeypatch):
        """An empty settings.json should be treated as {}."""
        settings_file.parent.mkdir(parents=True, exist_ok=True)
        settings_file.write_text("", encoding="utf-8")

        _mock_prompts(monkeypatch)
        install()

        data = _read_settings(settings_file)
        assert str(plugin_file) in data["extensions"]

    def test_invalid_json_aborts_without_clobbering(self, settings_file, plugin_file, monkeypatch):
        """Malformed JSON aborts (SystemExit) instead of silently wiping the file."""
        settings_file.parent.mkdir(parents=True, exist_ok=True)
        original = "{this is not valid json!!!"
        settings_file.write_text(original, encoding="utf-8")

        _mock_prompts(monkeypatch)
        with pytest.raises(SystemExit):
            install()

        # The user's (malformed) file is left untouched, not overwritten with {}.
        assert settings_file.read_text(encoding="utf-8") == original


# ---------------------------------------------------------------------------
# Idempotency tests
# ---------------------------------------------------------------------------


class TestIdempotent:
    """Re-install is idempotent — one shim file, one array entry."""

    def test_no_duplicate_extension_entry(self, settings_file, plugin_file, monkeypatch):
        _mock_prompts(monkeypatch)
        install()
        install()
        data = _read_settings(settings_file)
        occurrences = [e for e in data["extensions"] if e == str(plugin_file)]
        assert len(occurrences) == 1

    def test_no_duplicate_plugin_files(self, plugin_file, monkeypatch):
        _mock_prompts(monkeypatch)
        install()
        install()
        assert plugin_file.is_file()
        siblings = list(plugin_file.parent.glob("arize-tracing*.ts"))
        assert siblings == [plugin_file]

    def test_reinstall_content_matches_source(self, plugin_file, plugin_source_text, monkeypatch):
        _mock_prompts(monkeypatch)
        install()
        install()
        assert plugin_file.read_text(encoding="utf-8") == plugin_source_text


# ---------------------------------------------------------------------------
# Second harness (copy-from) + existing entry re-install
# ---------------------------------------------------------------------------


class TestInstallSecondHarnessOffersCopyFrom:
    """When another harness exists, its credentials are offered to prompt_backend."""

    def test_copy_from_populates_credentials(self, cwd_tmp, monkeypatch):
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
        entry = config["harnesses"]["omp"]
        assert entry["target"] == "arize"
        assert entry["endpoint"] == ARIZE_BACKEND[1]["endpoint"]
        assert entry["api_key"] == ARIZE_BACKEND[1]["api_key"]
        assert entry["space_id"] == ARIZE_BACKEND[1]["space_id"]
        assert entry["project_name"] == "omp"


class TestInstallExistingOmpEntryOnlyUpdatesProjectName:
    """Re-install with an existing omp config entry only updates project_name."""

    def test_existing_entry_preserves_target(self, cwd_tmp, monkeypatch):
        config_dir = cwd_tmp / ".arize" / "harness"
        config_dir.mkdir(parents=True, exist_ok=True)
        config_path = config_dir / "config.json"

        seed_config = {
            "harnesses": {
                "omp": {
                    "project_name": "omp",
                    "target": "arize",
                    "endpoint": "otlp.arize.com:443",
                    "api_key": "ak-existing",
                    "space_id": "space-existing",
                },
            },
        }
        config_path.write_text(json.dumps(seed_config, indent=2))

        monkeypatch.setattr(_install, "prompt_project_name", lambda default: "my-omp")
        monkeypatch.setattr(
            _install,
            "prompt_content_logging",
            lambda: {"prompts": True, "tool_details": True, "tool_content": True},
        )
        monkeypatch.setattr(_install, "write_logging_config", lambda block, config_path=None: None)
        monkeypatch.setattr("sys.stdout", _fake_stdout())

        install()

        config = json.loads(config_path.read_text())
        entry = config["harnesses"]["omp"]
        assert entry["project_name"] == "my-omp"
        assert entry["target"] == "arize"
        assert entry["endpoint"] == "otlp.arize.com:443"
        assert entry["api_key"] == "ak-existing"
        assert entry["space_id"] == "space-existing"


class TestInstallExistingLoggingBlockSkipsPrompt:
    """When config.json already has a logging block, skip the logging prompt."""

    def test_existing_logging_not_reprompted(self, cwd_tmp, monkeypatch):
        config_dir = cwd_tmp / ".arize" / "harness"
        config_dir.mkdir(parents=True, exist_ok=True)
        config_path = config_dir / "config.json"

        seed_config = {"logging": {"prompts": False, "tool_details": True, "tool_content": False}}
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


class TestInstallPromptsForLogging:
    """Install prompts for logging settings when the logging block is missing."""

    def test_install_prompts_for_logging_when_block_missing(self, cwd_tmp, monkeypatch):
        from unittest.mock import MagicMock

        logging_result = {"prompts": True, "tool_details": True, "tool_content": True}
        mock_prompt_logging = MagicMock(return_value=logging_result)
        mock_write_logging = MagicMock()

        monkeypatch.setattr(_install, "prompt_backend", lambda existing_harnesses=None: PHOENIX_BACKEND)
        monkeypatch.setattr(_install, "prompt_project_name", lambda default: default)
        monkeypatch.setattr(_install, "prompt_user_id", lambda: "")
        monkeypatch.setattr(_install, "prompt_content_logging", mock_prompt_logging)
        monkeypatch.setattr(_install, "write_logging_config", mock_write_logging)
        monkeypatch.setattr("sys.stdout", _fake_stdout())

        install()

        mock_prompt_logging.assert_called_once()
        mock_write_logging.assert_called_once_with(logging_result)


# ---------------------------------------------------------------------------
# Plugin source resolution (regression for site-packages FileNotFoundError)
# ---------------------------------------------------------------------------


class TestPluginSourceResolution:
    """The installer must locate its bundled .ts relative to install.py itself."""

    def test_source_resolved_independently_of_constants(self, cwd_tmp, plugin_file, monkeypatch):
        _mock_prompts(monkeypatch)
        # Simulate constants imported from a tree lacking the data asset
        # (the venv site-packages situation).
        monkeypatch.setattr(_omp, "PLUGIN_SOURCE", cwd_tmp / "no-such-tree" / "arize-tracing.ts")

        install()

        assert plugin_file.is_file()
        assert plugin_file.read_text(encoding="utf-8").startswith(_HEADER_MARKER)

    def test_plugin_source_points_at_existing_asset(self):
        """_plugin_source() resolves to a file that actually exists on disk."""
        src = _install._plugin_source()
        assert src.is_file()
        assert src.name == "arize-tracing.ts"


# ---------------------------------------------------------------------------
# Uninstall tests
# ---------------------------------------------------------------------------


class TestUninstallRemovesEverything:
    """Uninstall removes the array entry, the shim, and the config entry."""

    def test_config_entry_removed(self, cwd_tmp, monkeypatch):
        _mock_prompts(monkeypatch)
        install()
        uninstall()
        config_path = cwd_tmp / ".arize" / "harness" / "config.json"
        if config_path.is_file():
            config = json.loads(config_path.read_text()) or {}
            harnesses = config.get("harnesses", {})
            assert "omp" not in harnesses

    def test_plugin_file_removed(self, plugin_file, monkeypatch):
        _mock_prompts(monkeypatch)
        install()
        assert plugin_file.is_file()
        uninstall()
        assert not plugin_file.is_file()

    def test_extension_entry_removed(self, settings_file, plugin_file, monkeypatch):
        _mock_prompts(monkeypatch)
        install()
        assert str(plugin_file) in _read_settings(settings_file)["extensions"]
        uninstall()
        if settings_file.is_file():
            data = _read_settings(settings_file)
            assert str(plugin_file) not in data.get("extensions", [])

    def test_uninstall_preserves_unrelated_settings(self, settings_file, plugin_file, monkeypatch):
        """Uninstall must not clobber unrelated keys or other extensions."""
        _mock_prompts(monkeypatch)
        install()

        data = _read_settings(settings_file)
        data["theme"] = "dark"
        data["extensions"].append("/Users/me/.omp/extensions/my-own.ts")
        settings_file.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

        uninstall()

        remaining = _read_settings(settings_file)
        assert remaining["theme"] == "dark"
        assert "/Users/me/.omp/extensions/my-own.ts" in remaining["extensions"]
        assert str(plugin_file) not in remaining["extensions"]


class TestUninstallIdempotent:
    """Uninstall is idempotent and tolerant of missing state."""

    def test_uninstall_is_idempotent(self, cwd_tmp, monkeypatch):
        _mock_prompts(monkeypatch)
        install()
        uninstall()
        uninstall()  # second call — no exception
        config_path = cwd_tmp / ".arize" / "harness" / "config.json"
        if config_path.is_file():
            config = json.loads(config_path.read_text()) or {}
            assert "omp" not in config.get("harnesses", {})

    def test_uninstall_when_no_plugin_file(self, cwd_tmp, monkeypatch):
        monkeypatch.setattr("sys.stdout", _fake_stdout())
        assert not _omp.PLUGIN_FILE.is_file()
        uninstall()  # should not raise

    def test_uninstall_when_no_settings_file(self, cwd_tmp, monkeypatch):
        monkeypatch.setattr("sys.stdout", _fake_stdout())
        assert not _omp.SETTINGS_FILE.is_file()
        uninstall()  # should not raise

    def test_uninstall_when_no_extensions_dir(self, cwd_tmp, monkeypatch):
        monkeypatch.setattr("sys.stdout", _fake_stdout())
        assert not _omp.EXTENSIONS_DIR.exists()
        uninstall()  # should not raise


class TestUninstallGuardsForeignPluginFile:
    """Uninstall must NOT delete a plugin file lacking our header marker."""

    def test_non_arize_file_not_deleted(self, plugin_file, monkeypatch):
        plugin_file.parent.mkdir(parents=True, exist_ok=True)
        foreign_text = "// SomeoneElse's extension\nexport default function () {};\n"
        plugin_file.write_text(foreign_text, encoding="utf-8")

        monkeypatch.setattr("sys.stdout", _fake_stdout())
        uninstall()

        assert plugin_file.is_file()
        assert plugin_file.read_text(encoding="utf-8") == foreign_text

    def test_preserves_other_user_extension_files(self, plugin_file, monkeypatch):
        """Uninstall must not touch unrelated *.ts files in the extensions dir."""
        _mock_prompts(monkeypatch)
        install()

        other = plugin_file.parent / "user-extension.ts"
        other_text = "// user's own extension\nexport const X = 1;\n"
        other.write_text(other_text, encoding="utf-8")

        uninstall()

        assert not plugin_file.is_file()
        assert other.is_file()
        assert other.read_text(encoding="utf-8") == other_text


# ---------------------------------------------------------------------------
# Dry-run tests
# ---------------------------------------------------------------------------


class TestInstallDryRunWritesNothing:
    """Dry-run install performs no filesystem writes."""

    def test_dry_run_no_plugin_file(self, plugin_file, monkeypatch):
        monkeypatch.setenv("ARIZE_DRY_RUN", "true")
        _mock_prompts(monkeypatch)
        install()
        assert not plugin_file.is_file()

    def test_dry_run_no_extensions_dir(self, cwd_tmp, monkeypatch):
        monkeypatch.setenv("ARIZE_DRY_RUN", "true")
        _mock_prompts(monkeypatch)
        install()
        assert not _omp.EXTENSIONS_DIR.exists()

    def test_dry_run_no_settings_file(self, settings_file, monkeypatch):
        monkeypatch.setenv("ARIZE_DRY_RUN", "true")
        _mock_prompts(monkeypatch)
        install()
        assert not settings_file.is_file()

    def test_dry_run_does_not_modify_existing_settings(self, settings_file, monkeypatch):
        settings_file.parent.mkdir(parents=True, exist_ok=True)
        original = {"theme": "dark", "extensions": ["/Users/me/.omp/extensions/my-own.ts"]}
        settings_file.write_text(json.dumps(original, indent=2) + "\n", encoding="utf-8")

        monkeypatch.setenv("ARIZE_DRY_RUN", "true")
        _mock_prompts(monkeypatch)
        install()

        assert _read_settings(settings_file) == original

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

    def test_dry_run_uninstall_preserves_settings(self, settings_file, plugin_file, monkeypatch):
        _mock_prompts(monkeypatch)
        install()
        original = settings_file.read_text(encoding="utf-8")

        monkeypatch.setenv("ARIZE_DRY_RUN", "true")
        uninstall()

        assert settings_file.read_text(encoding="utf-8") == original
        assert str(plugin_file) in _read_settings(settings_file)["extensions"]

    def test_dry_run_uninstall_no_state_no_error(self, cwd_tmp, monkeypatch):
        monkeypatch.setenv("ARIZE_DRY_RUN", "true")
        monkeypatch.setattr("sys.stdout", _fake_stdout())
        uninstall()  # should not raise


# ---------------------------------------------------------------------------
# CLI main() dispatch tests
# ---------------------------------------------------------------------------


class TestMainDispatch:
    """main() dispatches install/uninstall based on argv."""

    def test_bad_args_exits_1(self, monkeypatch):
        monkeypatch.setattr("sys.argv", ["tracing.omp.install", "bogus"])
        with pytest.raises(SystemExit) as exc_info:
            _install.main()
        assert exc_info.value.code == 1

    def test_no_args_exits_1(self, monkeypatch):
        monkeypatch.setattr("sys.argv", ["tracing.omp.install"])
        with pytest.raises(SystemExit) as exc_info:
            _install.main()
        assert exc_info.value.code == 1

    def test_install_arg_calls_install(self, cwd_tmp, monkeypatch):
        _mock_prompts(monkeypatch)
        monkeypatch.setattr("sys.argv", ["tracing.omp.install", "install"])
        called = []
        monkeypatch.setattr(_install, "install", lambda with_skills=False: called.append("install"))
        _install.main()
        assert called == ["install"]

    def test_uninstall_arg_calls_uninstall(self, cwd_tmp, monkeypatch):
        monkeypatch.setattr("sys.argv", ["tracing.omp.install", "uninstall"])
        called = []
        monkeypatch.setattr(_install, "uninstall", lambda: called.append("uninstall"))
        _install.main()
        assert called == ["uninstall"]


class TestMainDispatchSkillsFlag:
    """main() parses --with-skills and forwards it to install()."""

    def test_main_install_with_skills_flag(self, cwd_tmp, monkeypatch):
        monkeypatch.setattr("sys.argv", ["tracing.omp.install", "install", "--with-skills"])
        captured = {}
        monkeypatch.setattr(_install, "install", lambda with_skills=False: captured.update(with_skills=with_skills))
        _install.main()
        assert captured == {"with_skills": True}

    def test_main_install_without_skills_flag(self, cwd_tmp, monkeypatch):
        monkeypatch.setattr("sys.argv", ["tracing.omp.install", "install"])
        captured = {}
        monkeypatch.setattr(_install, "install", lambda with_skills=False: captured.update(with_skills=with_skills))
        _install.main()
        assert captured == {"with_skills": False}


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
        assert calls == ["omp"]

    def test_install_default_does_not_symlink(self, cwd_tmp, monkeypatch):
        _mock_prompts(monkeypatch)
        calls = []
        monkeypatch.setattr(_install, "symlink_skills", lambda harness: calls.append(harness))
        install()
        assert calls == []


# ---------------------------------------------------------------------------
# Constants verification
# ---------------------------------------------------------------------------


class TestConstants:
    """Verify omp constants are well-formed."""

    def test_harness_name_is_omp(self):
        assert _omp.HARNESS_NAME == "omp"

    def test_plugin_file_under_extensions_dir(self):
        assert _omp.PLUGIN_FILE.parent == _omp.EXTENSIONS_DIR

    def test_plugin_file_name(self):
        assert _omp.PLUGIN_FILE.name == "arize-tracing.ts"

    def test_settings_file_under_settings_dir(self):
        assert _omp.SETTINGS_FILE.parent == _omp.SETTINGS_DIR

    def test_settings_file_name(self):
        assert _omp.SETTINGS_FILE.name == "settings.json"

    def test_plugin_source_exists_in_repo(self):
        assert _omp.PLUGIN_SOURCE.is_file()

    def test_plugin_source_has_header_marker(self):
        text = _omp.PLUGIN_SOURCE.read_text(encoding="utf-8")
        assert text.startswith(_HEADER_MARKER)


# ---------------------------------------------------------------------------
# Setup wizard delegation
# ---------------------------------------------------------------------------


class TestSetupOmpModule:
    """core/setup/omp.py delegates to tracing.omp.install."""

    def test_setup_module_importable(self):
        import core.setup.omp as setup_omp

        assert callable(setup_omp.install)
        assert callable(setup_omp.uninstall)
        assert callable(setup_omp.main)

    def test_setup_install_delegates(self, cwd_tmp, monkeypatch):
        import core.setup.omp as setup_omp

        called = []
        monkeypatch.setattr(_install, "install", lambda: called.append("install"))
        setup_omp.install()
        assert called == ["install"]

    def test_setup_uninstall_delegates(self, cwd_tmp, monkeypatch):
        import core.setup.omp as setup_omp

        called = []
        monkeypatch.setattr(_install, "uninstall", lambda: called.append("uninstall"))
        setup_omp.uninstall()
        assert called == ["uninstall"]

    def test_setup_main_delegates_to_install(self, cwd_tmp, monkeypatch):
        import core.setup.omp as setup_omp

        called = []
        monkeypatch.setattr(_install, "install", lambda: called.append("install"))
        setup_omp.main()
        assert called == ["install"]
