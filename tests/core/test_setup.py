#!/usr/bin/env python3
"""Tests for core/setup/ — shared utilities and per-harness setup wizards."""

import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _isolate_cwd(tmp_path, monkeypatch):
    """Run every test under tmp_path so cwd-relative writes (e.g. .github/hooks
    from copilot install) don't leak into the project directory."""
    monkeypatch.chdir(tmp_path)


def _patched_path_class(tmp_path):
    """Create a Path subclass that redirects home() and relative .claude/ to tmp_path."""
    _real_path = Path

    class _FakePath(_real_path):
        @classmethod
        def home(cls):
            return _real_path(tmp_path)

        def __new__(cls, *args, **kwargs):
            # Redirect ".claude/..." to tmp_path/.claude/...
            if args and str(args[0]).startswith(".claude"):
                return _real_path(tmp_path / args[0])
            return _real_path.__new__(cls, *args, **kwargs)

    return _FakePath


# ---------------------------------------------------------------------------
# Shared utility tests (core.setup.__init__)
# ---------------------------------------------------------------------------


class TestPrintColor:
    """Tests for print_color()."""

    def test_no_color_when_not_tty(self, capsys):
        """print_color with non-tty stdout should not emit ANSI codes."""
        from core.setup import print_color

        with patch.object(sys.stdout, "isatty", return_value=False):
            print_color("hello", "green")
        out = capsys.readouterr().out
        assert "\033[" not in out
        assert "hello" in out

    def test_no_color_with_empty_color(self, capsys):
        """print_color with no color arg should not emit ANSI codes."""
        from core.setup import print_color

        print_color("hello")
        out = capsys.readouterr().out
        assert "\033[" not in out
        assert "hello" in out

    def test_no_color_with_invalid_color(self, capsys):
        """print_color with unrecognized color should not emit ANSI codes."""
        from core.setup import print_color

        print_color("hello", "magenta")
        out = capsys.readouterr().out
        assert "\033[" not in out
        assert "hello" in out

    @pytest.mark.skipif(os.name == "nt", reason="ANSI color tests only on Unix")
    def test_color_when_tty(self, capsys):
        """print_color with tty stdout should emit ANSI codes."""
        from core.setup import print_color

        with patch.object(sys.stdout, "isatty", return_value=True):
            print_color("hello", "green")
        out = capsys.readouterr().out
        assert "\033[0;32m" in out
        assert "\033[0m" in out
        assert "hello" in out


class TestPromptBackend:
    """Tests for prompt_backend()."""

    def test_phoenix_default_endpoint(self):
        """Choosing Phoenix with default endpoint."""
        from core.setup import prompt_backend

        # input: "1" for Phoenix, "" for default endpoint; getpass for api_key
        with patch("builtins.input", side_effect=["1", ""]):
            with patch("core.setup.getpass", return_value=""):
                target, creds = prompt_backend()
        assert target == "phoenix"
        assert creds["endpoint"] == "http://localhost:6006"
        assert creds["api_key"] == ""

    def test_phoenix_custom_endpoint(self):
        """Choosing Phoenix with custom endpoint."""
        from core.setup import prompt_backend

        with patch("builtins.input", side_effect=["1", "http://my-phoenix:9090"]):
            with patch("core.setup.getpass", return_value=""):
                target, creds = prompt_backend()
        assert target == "phoenix"
        assert creds["endpoint"] == "http://my-phoenix:9090"

    def test_phoenix_empty_choice_defaults_to_phoenix(self):
        """Empty choice defaults to Phoenix."""
        from core.setup import prompt_backend

        with patch("builtins.input", side_effect=["", ""]):
            with patch("core.setup.getpass", return_value=""):
                target, creds = prompt_backend()
        assert target == "phoenix"

    def test_arize_with_credentials(self):
        """Choosing Arize AX with all credentials."""
        from core.setup import prompt_backend

        with patch("builtins.input", side_effect=["2", "my-space-id", ""]):
            with patch("core.setup.getpass", return_value="my-api-key"):
                with patch.object(sys.stdout, "isatty", return_value=False):
                    target, creds = prompt_backend()
        assert target == "arize"
        assert creds["api_key"] == "my-api-key"
        assert creds["space_id"] == "my-space-id"
        assert creds["endpoint"] == "otlp.arize.com:443"

    def test_arize_custom_endpoint(self):
        """Choosing Arize AX with custom OTLP endpoint."""
        from core.setup import prompt_backend

        with patch("builtins.input", side_effect=["2", "space", "custom.endpoint:443"]):
            with patch("core.setup.getpass", return_value="key"):
                with patch.object(sys.stdout, "isatty", return_value=False):
                    target, creds = prompt_backend()
        assert target == "arize"
        assert creds["endpoint"] == "custom.endpoint:443"

    def test_arize_missing_api_key_exits(self):
        """Arize AX with empty API key should exit."""
        from core.setup import prompt_backend

        with patch("builtins.input", side_effect=["2", "space-id"]):
            with patch("core.setup.getpass", return_value=""):
                with pytest.raises(SystemExit):
                    prompt_backend()

    def test_arize_missing_space_id_exits(self):
        """Arize AX with empty space ID should exit."""
        from core.setup import prompt_backend

        with patch("builtins.input", side_effect=["2", ""]):
            with patch("core.setup.getpass", return_value="api-key"):
                with pytest.raises(SystemExit):
                    prompt_backend()

    def test_invalid_choice_exits(self):
        """Invalid backend choice should exit."""
        from core.setup import prompt_backend

        with patch("builtins.input", side_effect=["3"]):
            with pytest.raises(SystemExit):
                prompt_backend()


class TestPromptUserId:
    """Tests for prompt_user_id()."""

    def test_returns_user_id(self):
        from core.setup import prompt_user_id

        with patch("builtins.input", return_value="alice"):
            with patch.object(sys.stdout, "isatty", return_value=False):
                result = prompt_user_id()
        assert result == "alice"

    def test_returns_empty_when_skipped(self):
        from core.setup import prompt_user_id

        with patch("builtins.input", return_value=""):
            with patch.object(sys.stdout, "isatty", return_value=False):
                result = prompt_user_id()
        assert result == ""


class TestWriteConfig:
    """Tests for write_config()."""

    def test_creates_new_config_phoenix(self, tmp_path, monkeypatch):
        """write_config creates fresh config.json for Phoenix."""
        config_path = str(tmp_path / "config.json")

        # Monkeypatch core.config to use our temp path
        import core.config

        monkeypatch.setattr(core.config, "CONFIG_FILE", config_path)

        from core.setup import write_config

        write_config(
            "phoenix",
            {"endpoint": "http://localhost:6006", "api_key": ""},
            "claude-code",
            "claude-code",
            config_path=config_path,
        )

        config = json.loads(Path(config_path).read_text())
        entry = config["harnesses"]["claude-code"]
        assert entry["target"] == "phoenix"
        assert entry["endpoint"] == "http://localhost:6006"
        assert entry["project_name"] == "claude-code"
        assert "backend" not in config

    def test_creates_new_config_arize(self, tmp_path, monkeypatch):
        """write_config creates fresh config.json for Arize AX."""
        config_path = str(tmp_path / "config.json")
        import core.config

        monkeypatch.setattr(core.config, "CONFIG_FILE", config_path)

        from core.setup import write_config

        write_config(
            "arize",
            {"endpoint": "otlp.arize.com:443", "api_key": "k", "space_id": "s"},
            "codex",
            "codex",
            config_path=config_path,
        )

        config = json.loads(Path(config_path).read_text())
        entry = config["harnesses"]["codex"]
        assert entry["target"] == "arize"
        assert entry["api_key"] == "k"
        assert entry["space_id"] == "s"
        assert entry["project_name"] == "codex"
        assert "backend" not in config

    def test_merge_harness_preserves_existing(self, tmp_path, monkeypatch):
        """write_config with existing config adds harness, preserves others."""
        config_path = str(tmp_path / "config.json")
        import core.config

        monkeypatch.setattr(core.config, "CONFIG_FILE", config_path)

        # Pre-existing config in new flat format
        existing = {
            "harnesses": {
                "claude-code": {
                    "project_name": "claude-code",
                    "target": "phoenix",
                    "endpoint": "http://custom:9999",
                    "api_key": "secret",
                },
            },
        }
        os.makedirs(os.path.dirname(config_path), exist_ok=True)
        with open(config_path, "w") as f:
            json.dump(existing, f, indent=2)

        from core.setup import write_config

        write_config(
            "phoenix",
            {"endpoint": "http://localhost:6006", "api_key": ""},
            "cursor",
            "cursor",
            config_path=config_path,
        )

        config = json.loads(Path(config_path).read_text())
        # New harness should be added
        assert config["harnesses"]["cursor"]["project_name"] == "cursor"
        assert config["harnesses"]["cursor"]["target"] == "phoenix"
        # Old harness should be preserved
        assert config["harnesses"]["claude-code"]["project_name"] == "claude-code"
        assert config["harnesses"]["claude-code"]["endpoint"] == "http://custom:9999"

    def test_write_config_with_user_id(self, tmp_path, monkeypatch):
        """write_config sets user_id when provided."""
        config_path = str(tmp_path / "config.json")
        import core.config

        monkeypatch.setattr(core.config, "CONFIG_FILE", config_path)

        from core.setup import write_config

        write_config(
            "phoenix",
            {"endpoint": "http://localhost:6006", "api_key": ""},
            "claude-code",
            "claude-code",
            user_id="alice",
            config_path=config_path,
        )

        config = json.loads(Path(config_path).read_text())
        assert config["user_id"] == "alice"


# ---------------------------------------------------------------------------
# Claude setup tests (core.setup.claude)
# ---------------------------------------------------------------------------


class TestClaudeSetup:
    """Tests for core.setup.claude."""

    def test_settings_json_phoenix(self, tmp_path):
        """Claude setup creates settings.json with Phoenix env block."""
        settings_path = tmp_path / ".claude" / "settings.local.json"

        from core.setup.claude import _ensure_settings_file, _load_settings, _save_settings

        _ensure_settings_file(settings_path)
        assert settings_path.exists()

        settings = _load_settings(settings_path)
        env_block = settings.setdefault("env", {})
        env_block["PHOENIX_ENDPOINT"] = "http://localhost:6006"
        env_block["ARIZE_TRACE_ENABLED"] = "true"
        _save_settings(settings_path, settings)

        result = json.loads(settings_path.read_text())
        assert result["env"]["PHOENIX_ENDPOINT"] == "http://localhost:6006"
        assert result["env"]["ARIZE_TRACE_ENABLED"] == "true"

    def test_settings_json_arize(self, tmp_path):
        """Claude setup creates settings.json with Arize AX env block."""
        settings_path = tmp_path / ".claude" / "settings.local.json"

        from core.setup.claude import _ensure_settings_file, _load_settings, _save_settings

        _ensure_settings_file(settings_path)
        settings = _load_settings(settings_path)
        env_block = settings.setdefault("env", {})
        env_block["ARIZE_API_KEY"] = "test-key"
        env_block["ARIZE_SPACE_ID"] = "test-space"
        env_block["ARIZE_OTLP_ENDPOINT"] = "otlp.arize.com:443"
        env_block["ARIZE_TRACE_ENABLED"] = "true"
        _save_settings(settings_path, settings)

        result = json.loads(settings_path.read_text())
        assert result["env"]["ARIZE_API_KEY"] == "test-key"
        assert result["env"]["ARIZE_SPACE_ID"] == "test-space"
        assert result["env"]["ARIZE_OTLP_ENDPOINT"] == "otlp.arize.com:443"
        assert result["env"]["ARIZE_TRACE_ENABLED"] == "true"

    def test_existing_settings_merged(self, tmp_path):
        """Existing settings.json keys are preserved when adding env block."""
        settings_path = tmp_path / ".claude" / "settings.local.json"
        settings_path.parent.mkdir(parents=True)
        settings_path.write_text(
            json.dumps(
                {
                    "theme": "dark",
                    "env": {"EXISTING_VAR": "keep_me"},
                }
            )
        )

        from core.setup.claude import _load_settings, _save_settings

        settings = _load_settings(settings_path)
        env_block = settings.setdefault("env", {})
        env_block["PHOENIX_ENDPOINT"] = "http://localhost:6006"
        _save_settings(settings_path, settings)

        result = json.loads(settings_path.read_text())
        assert result["theme"] == "dark"
        assert result["env"]["EXISTING_VAR"] == "keep_me"
        assert result["env"]["PHOENIX_ENDPOINT"] == "http://localhost:6006"

    def test_check_existing_config_no_overwrite(self, tmp_path):
        """Declining overwrite returns False."""
        settings_path = tmp_path / "settings.json"
        settings_path.write_text(json.dumps({"env": {"PHOENIX_ENDPOINT": "http://localhost:6006"}}))

        from core.setup.claude import _check_existing_configuration

        with patch("builtins.input", return_value="n"):
            result = _check_existing_configuration(settings_path)
        assert result is False

    def test_check_existing_config_overwrite(self, tmp_path):
        """Accepting overwrite returns True."""
        settings_path = tmp_path / "settings.json"
        settings_path.write_text(json.dumps({"env": {"PHOENIX_ENDPOINT": "http://localhost:6006"}}))

        from core.setup.claude import _check_existing_configuration

        with patch("builtins.input", return_value="y"):
            result = _check_existing_configuration(settings_path)
        assert result is True

    def test_check_existing_config_arize_no_overwrite(self, tmp_path):
        """Declining overwrite for Arize config returns False."""
        settings_path = tmp_path / "settings.json"
        settings_path.write_text(json.dumps({"env": {"ARIZE_API_KEY": "some-key"}}))

        from core.setup.claude import _check_existing_configuration

        with patch("builtins.input", return_value="N"):
            result = _check_existing_configuration(settings_path)
        assert result is False

    def test_check_no_existing_config(self, tmp_path):
        """No existing config returns True (proceed)."""
        settings_path = tmp_path / "settings.json"
        settings_path.write_text("{}")

        from core.setup.claude import _check_existing_configuration

        result = _check_existing_configuration(settings_path)
        assert result is True

    def test_load_settings_missing_file(self, tmp_path):
        """_load_settings returns {} for missing file."""
        from core.setup.claude import _load_settings

        result = _load_settings(tmp_path / "nonexistent.json")
        assert result == {}

    def test_load_settings_invalid_json(self, tmp_path):
        """_load_settings returns {} for invalid JSON."""
        path = tmp_path / "bad.json"
        path.write_text("not json{{{")
        from core.setup.claude import _load_settings

        result = _load_settings(path)
        assert result == {}

    def test_main_keyboard_interrupt(self):
        """main() catches KeyboardInterrupt gracefully."""
        from core.setup.claude import main

        with patch("core.setup.claude._run", side_effect=KeyboardInterrupt):
            with pytest.raises(SystemExit) as exc_info:
                main()
            assert exc_info.value.code == 1

    def test_main_eof_error(self):
        """main() catches EOFError gracefully."""
        from core.setup.claude import main

        with patch("core.setup.claude._run", side_effect=EOFError):
            with pytest.raises(SystemExit) as exc_info:
                main()
            assert exc_info.value.code == 1

    def _setup_install_env(self, tmp_path, monkeypatch):
        """Set up the environment so _run() → install() can resolve all paths."""
        import core.config
        import core.setup as setup_mod

        install_dir = tmp_path / ".arize" / "harness"
        config_path = install_dir / "config.json"

        monkeypatch.setattr(setup_mod, "INSTALL_DIR", install_dir)
        monkeypatch.setattr(setup_mod, "VENV_DIR", install_dir / "venv")
        monkeypatch.setattr(setup_mod, "CONFIG_FILE", config_path)
        monkeypatch.setattr(setup_mod, "BIN_DIR", install_dir / "bin")
        monkeypatch.setattr(setup_mod, "RUN_DIR", install_dir / "run")
        monkeypatch.setattr(setup_mod, "LOG_DIR", install_dir / "logs")
        monkeypatch.setattr(setup_mod, "STATE_DIR", install_dir / "state")
        monkeypatch.setattr(core.config, "CONFIG_FILE", str(config_path))

        # Create the harness plugin dir so harness_dir() resolves
        plugin_dir = install_dir / "tracing" / "claude_code"
        plugin_dir.mkdir(parents=True, exist_ok=True)

        # Patch SETTINGS_FILE in install module
        settings_file = tmp_path / ".claude" / "settings.json"
        import tracing.claude_code.constants as claude_constants
        import tracing.claude_code.install as claude_install

        monkeypatch.setattr(claude_install, "SETTINGS_FILE", settings_file)
        monkeypatch.setattr(claude_constants, "SETTINGS_FILE", settings_file)

        monkeypatch.setattr(
            "sys.stdout",
            type(
                "FakeOut",
                (),
                {
                    "isatty": lambda self: False,
                    "write": lambda self, s: None,
                    "flush": lambda self: None,
                },
            )(),
        )

        return config_path, settings_file

    def test_run_phoenix_flow(self, tmp_path, monkeypatch):
        """Full Claude _run() flow for Phoenix backend writes settings.json and config.json."""
        config_path, settings_file = self._setup_install_env(tmp_path, monkeypatch)

        # Inputs: backend=1 (Phoenix), endpoint=default, project_name=default, user_id="",
        # then three content-logging prompts (defaults: Y, N, N).
        inputs = iter(["1", "", "", "", "", "", ""])
        monkeypatch.setattr("builtins.input", lambda prompt="": next(inputs))
        monkeypatch.setattr("core.setup.getpass", lambda prompt="": "")

        from core.setup.claude import _run

        _run()

        config = json.loads(config_path.read_text())
        assert config["harnesses"]["claude-code"]["target"] == "phoenix"
        assert config["harnesses"]["claude-code"]["project_name"] == "claude-code"

        # settings.json should have hooks and env vars
        result = json.loads(settings_file.read_text())
        assert result["env"]["ARIZE_TRACE_ENABLED"] == "true"
        assert result["env"]["ARIZE_PROJECT_NAME"] == "claude-code"
        assert len(result.get("hooks", {})) == 16

    def test_run_arize_flow(self, tmp_path, monkeypatch):
        """Full Claude _run() flow for Arize AX backend."""
        config_path, settings_file = self._setup_install_env(tmp_path, monkeypatch)

        # Inputs: backend=2, space_id, otlp_endpoint=default, project_name=default,
        # user_id="alice", then three content-logging prompts (defaults).
        # api_key goes through getpass.
        inputs = iter(["2", "my-space", "", "", "alice", "", "", ""])
        monkeypatch.setattr("builtins.input", lambda prompt="": next(inputs))
        monkeypatch.setattr("core.setup.getpass", lambda prompt="": "my-key")

        from core.setup.claude import _run

        _run()

        config = json.loads(config_path.read_text())
        assert config["harnesses"]["claude-code"]["target"] == "arize"

        # settings.json should have hooks and env vars
        result = json.loads(settings_file.read_text())
        assert result["env"]["ARIZE_TRACE_ENABLED"] == "true"
        assert len(result.get("hooks", {})) == 16
        assert config["user_id"] == "alice"


# ---------------------------------------------------------------------------
# Codex setup tests (core.setup.codex)
# ---------------------------------------------------------------------------


class TestCodexWriteEnvFile:
    """Tests for _write_env_file()."""

    def test_phoenix_env_file(self, tmp_path):
        """Env file for Phoenix backend has correct exports."""
        env_path = tmp_path / ".codex" / "arize-env.sh"
        from core.setup.codex import _write_env_file

        _write_env_file(env_path, "phoenix", {"endpoint": "http://localhost:6006", "api_key": ""})

        content = env_path.read_text()
        assert "export ARIZE_TRACE_ENABLED=true" in content
        assert 'export PHOENIX_ENDPOINT="http://localhost:6006"' in content
        assert "PHOENIX_API_KEY" not in content  # empty api_key should be skipped
        assert 'export ARIZE_PROJECT_NAME="codex"' in content

    def test_phoenix_env_file_with_api_key(self, tmp_path):
        """Env file for Phoenix with API key includes it."""
        env_path = tmp_path / ".codex" / "arize-env.sh"
        from core.setup.codex import _write_env_file

        _write_env_file(env_path, "phoenix", {"endpoint": "http://localhost:6006", "api_key": "my-key"})

        content = env_path.read_text()
        assert 'export PHOENIX_API_KEY="my-key"' in content

    def test_arize_env_file(self, tmp_path):
        """Env file for Arize AX backend has correct exports."""
        env_path = tmp_path / ".codex" / "arize-env.sh"
        from core.setup.codex import _write_env_file

        _write_env_file(
            env_path,
            "arize",
            {
                "endpoint": "otlp.arize.com:443",
                "api_key": "test-key",
                "space_id": "test-space",
            },
        )

        content = env_path.read_text()
        assert "export ARIZE_TRACE_ENABLED=true" in content
        assert 'export ARIZE_API_KEY="test-key"' in content
        assert 'export ARIZE_SPACE_ID="test-space"' in content
        assert 'export ARIZE_OTLP_ENDPOINT="otlp.arize.com:443"' in content
        assert 'export ARIZE_PROJECT_NAME="codex"' in content

    def test_env_file_creates_parent_dir(self, tmp_path):
        """_write_env_file creates parent directories."""
        env_path = tmp_path / "deep" / "nested" / "arize-env.sh"
        from core.setup.codex import _write_env_file

        _write_env_file(env_path, "phoenix", {"endpoint": "http://localhost:6006", "api_key": ""})
        assert env_path.exists()

    def test_env_file_permissions(self, tmp_path):
        """Env file should be chmod 600 on Unix."""
        if os.name == "nt":
            pytest.skip("chmod test only on Unix")
        env_path = tmp_path / ".codex" / "arize-env.sh"
        from core.setup.codex import _write_env_file

        _write_env_file(env_path, "phoenix", {"endpoint": "http://localhost:6006", "api_key": ""})
        mode = oct(env_path.stat().st_mode & 0o777)
        assert mode == "0o600"


class TestCodexUpdateToml:
    """Tests for _update_toml_otel_section()."""

    def test_adds_otel_to_empty_file(self, tmp_path):
        """Adds [otel] section to a new/empty file."""
        toml_path = tmp_path / ".codex" / "config.toml"
        from core.setup.codex import _update_toml_otel_section

        _update_toml_otel_section(toml_path, 4318)

        content = toml_path.read_text()
        assert "[otel]" in content
        assert "[otel.exporter.otlp-http]" in content
        assert 'endpoint = "http://127.0.0.1:4318/v1/logs"' in content
        assert 'protocol = "json"' in content

    def test_replaces_existing_otel_section(self, tmp_path):
        """Replaces existing [otel] section with new one."""
        toml_path = tmp_path / "config.toml"
        toml_path.write_text(
            '[general]\nname = "test"\n\n' '[otel]\nold_key = "old_value"\n\n' '[other]\nfoo = "bar"\n'
        )
        from core.setup.codex import _update_toml_otel_section

        _update_toml_otel_section(toml_path, 9999)

        content = toml_path.read_text()
        assert "old_key" not in content
        assert 'endpoint = "http://127.0.0.1:9999/v1/logs"' in content
        assert "[general]" in content
        assert "[other]" in content
        assert 'foo = "bar"' in content

    def test_preserves_other_sections(self, tmp_path):
        """Other TOML sections are preserved when replacing [otel]."""
        toml_path = tmp_path / "config.toml"
        original = '[auth]\ntoken = "secret"\n\n[otel]\nnotify = ["old-cmd"]\n'
        toml_path.write_text(original)

        from core.setup.codex import _update_toml_otel_section

        _update_toml_otel_section(toml_path, 4318)

        content = toml_path.read_text()
        assert "[auth]" in content
        assert 'token = "secret"' in content
        assert "old-cmd" not in content
        assert "[otel]" in content

    def test_replaces_otel_subsection(self, tmp_path):
        """Replaces [otel.exporter.otlp-http] as part of otel section."""
        toml_path = tmp_path / "config.toml"
        toml_path.write_text(
            '[otel]\n[otel.exporter.otlp-http]\nendpoint = "http://old:1234"\nprotocol = "json"\n\n'
            '[other]\nkey = "val"\n'
        )
        from core.setup.codex import _update_toml_otel_section

        _update_toml_otel_section(toml_path, 5555)

        content = toml_path.read_text()
        assert "http://old:1234" not in content
        assert 'endpoint = "http://127.0.0.1:5555/v1/logs"' in content
        assert "[other]" in content

    def test_preserves_otelother_section(self, tmp_path):
        """A section named [otelother] should NOT be removed as part of [otel]."""
        toml_path = tmp_path / "config.toml"
        toml_path.write_text('[otel]\nold = "val"\n\n' '[otelother]\nkeep = "this"\n')
        from core.setup.codex import _update_toml_otel_section

        _update_toml_otel_section(toml_path, 4318)

        content = toml_path.read_text()
        assert "[otelother]" in content
        assert 'keep = "this"' in content
        assert 'old = "val"' not in content

    def test_custom_port(self, tmp_path):
        """Uses the provided collector port."""
        toml_path = tmp_path / "config.toml"
        from core.setup.codex import _update_toml_otel_section

        _update_toml_otel_section(toml_path, 12345)

        content = toml_path.read_text()
        assert "12345" in content

    def test_main_keyboard_interrupt(self):
        """main() catches KeyboardInterrupt."""
        from core.setup.codex import main

        with patch("core.setup.codex._run", side_effect=KeyboardInterrupt):
            with pytest.raises(SystemExit) as exc_info:
                main()
            assert exc_info.value.code == 1


class TestCodexRunFlow:
    """Integration tests for codex _run() flow."""

    def test_run_fresh_phoenix(self, tmp_path, monkeypatch):
        """Codex _run() with no existing config prompts and writes all files."""
        config_path = str(tmp_path / "config.json")
        codex_dir = tmp_path / ".codex"

        import core.config

        monkeypatch.setattr(core.config, "CONFIG_FILE", config_path)

        # Patch Path.home() to use tmp_path
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        # Inputs: project_name=default, backend=1 (Phoenix), endpoint=default, user_id=""
        inputs = iter(["", "1", "", ""])
        monkeypatch.setattr("builtins.input", lambda prompt="": next(inputs))
        monkeypatch.setattr("core.setup.getpass", lambda prompt="": "")
        monkeypatch.setattr(
            "sys.stdout",
            type(
                "FakeOut",
                (),
                {
                    "isatty": lambda self: False,
                    "write": lambda self, s: None,
                    "flush": lambda self: None,
                },
            )(),
        )

        from core.setup.codex import _run

        _run()

        # config.json written
        config = json.loads(Path(config_path).read_text())
        assert config["harnesses"]["codex"]["target"] == "phoenix"
        assert config["harnesses"]["codex"]["project_name"] == "codex"

        # arize-env.sh written
        env_file = codex_dir / "arize-env.sh"
        assert env_file.exists()
        env_content = env_file.read_text()
        assert "export ARIZE_TRACE_ENABLED=true" in env_content
        assert 'export PHOENIX_ENDPOINT="http://localhost:6006"' in env_content

        # config.toml written with [otel] section
        toml_file = codex_dir / "config.toml"
        assert toml_file.exists()
        toml_content = toml_file.read_text()
        assert "[otel]" in toml_content
        assert "4318" in toml_content

    def test_run_existing_config_skips_prompts(self, tmp_path, monkeypatch):
        """Codex _run() with existing config skips backend prompts."""
        config_path = str(tmp_path / "config.json")
        codex_dir = tmp_path / ".codex"
        existing = {
            "harnesses": {
                "codex": {
                    "project_name": "codex",
                    "target": "phoenix",
                    "endpoint": "http://localhost:6006",
                    "api_key": "",
                    "collector": {"host": "127.0.0.1", "port": 4318},
                }
            },
        }
        Path(config_path).parent.mkdir(parents=True, exist_ok=True)
        with open(config_path, "w") as f:
            json.dump(existing, f, indent=2)

        import core.config

        monkeypatch.setattr(core.config, "CONFIG_FILE", config_path)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        # Inputs: project_name=default, user_id="" (no backend prompts)
        inputs = iter(["", ""])
        monkeypatch.setattr("builtins.input", lambda prompt="": next(inputs))
        monkeypatch.setattr(
            "sys.stdout",
            type(
                "FakeOut",
                (),
                {
                    "isatty": lambda self: False,
                    "write": lambda self, s: None,
                    "flush": lambda self: None,
                },
            )(),
        )

        from core.setup.codex import _run

        _run()

        config = json.loads(Path(config_path).read_text())
        assert config["harnesses"]["codex"]["project_name"] == "codex"
        assert config["harnesses"]["codex"]["target"] == "phoenix"

        # env file and toml should still be written
        assert (codex_dir / "arize-env.sh").exists()
        assert (codex_dir / "config.toml").exists()


# ---------------------------------------------------------------------------
# Cursor setup tests (core.setup.cursor)
# ---------------------------------------------------------------------------


class TestCursorSetup:
    """Tests for core.setup.cursor."""

    def test_config_written_with_cursor_harness(self, tmp_path, monkeypatch):
        """write_config creates config with cursor harness entry."""
        config_path = str(tmp_path / "config.json")
        import core.config

        monkeypatch.setattr(core.config, "CONFIG_FILE", config_path)

        from core.setup import write_config

        write_config(
            "phoenix", {"endpoint": "http://localhost:6006", "api_key": ""}, "cursor", "cursor", config_path=config_path
        )

        config = json.loads(Path(config_path).read_text())
        assert config["harnesses"]["cursor"]["project_name"] == "cursor"
        assert config["harnesses"]["cursor"]["target"] == "phoenix"

    def test_existing_config_adds_cursor_harness(self, tmp_path, monkeypatch):
        """Existing config gets cursor harness added, other harnesses preserved."""
        config_path = str(tmp_path / "config.json")
        import core.config

        monkeypatch.setattr(core.config, "CONFIG_FILE", config_path)

        existing = {
            "harnesses": {
                "claude-code": {
                    "project_name": "claude-code",
                    "target": "arize",
                    "endpoint": "otlp.arize.com:443",
                    "api_key": "key",
                    "space_id": "space",
                },
            },
        }
        os.makedirs(os.path.dirname(config_path), exist_ok=True)
        with open(config_path, "w") as f:
            json.dump(existing, f, indent=2)

        config = core.config.load_config(config_path)
        core.config.set_value(config, "harnesses.cursor.project_name", "cursor")
        core.config.save_config(config, config_path)

        result = json.loads(Path(config_path).read_text())
        assert result["harnesses"]["cursor"]["project_name"] == "cursor"
        assert result["harnesses"]["claude-code"]["project_name"] == "claude-code"
        assert result["harnesses"]["claude-code"]["target"] == "arize"
        assert result["harnesses"]["claude-code"]["api_key"] == "key"

    def test_main_keyboard_interrupt(self):
        """main() catches KeyboardInterrupt."""
        from core.setup.cursor import main

        with patch("core.setup.cursor._run", side_effect=KeyboardInterrupt):
            with pytest.raises(SystemExit) as exc_info:
                main()
            assert exc_info.value.code == 1

    def _patch_cursor_install(self, tmp_path, monkeypatch):
        """Shared patching for cursor _run() tests — patches config and install module paths."""
        import core.config
        import core.setup as setup_mod

        config_path = str(tmp_path / "config.json")
        install_dir = tmp_path / ".arize" / "harness"
        hooks_file = tmp_path / ".cursor" / "hooks.json"

        monkeypatch.setattr(core.config, "CONFIG_FILE", config_path)
        monkeypatch.setattr(setup_mod, "CONFIG_FILE", Path(config_path))
        monkeypatch.setattr(setup_mod, "INSTALL_DIR", install_dir)
        monkeypatch.setattr(setup_mod, "VENV_DIR", install_dir / "venv")
        monkeypatch.setattr(setup_mod, "BIN_DIR", install_dir / "bin")
        monkeypatch.setattr(setup_mod, "RUN_DIR", install_dir / "run")
        monkeypatch.setattr(setup_mod, "LOG_DIR", install_dir / "logs")
        monkeypatch.setattr(setup_mod, "STATE_DIR", install_dir / "state")

        # Patch HOOKS_FILE + INSTALL_DIR in the cursor install module.
        import tracing.cursor.install as cursor_install

        monkeypatch.setattr(cursor_install, "HOOKS_FILE", hooks_file)
        monkeypatch.setattr(cursor_install, "INSTALL_DIR", install_dir)

        monkeypatch.setattr(
            "sys.stdout",
            type(
                "FakeOut",
                (),
                {
                    "isatty": lambda self: False,
                    "write": lambda self, s: None,
                    "flush": lambda self: None,
                },
            )(),
        )

        return config_path

    def test_run_fresh_phoenix(self, tmp_path, monkeypatch):
        """Cursor _run() with no existing config prompts and writes config.json."""
        config_path = self._patch_cursor_install(tmp_path, monkeypatch)

        # Inputs: backend=1, endpoint=default, project_name=default, user_id="",
        # then three content-logging prompts (defaults).
        inputs = iter(["1", "", "", "", "", "", ""])
        monkeypatch.setattr("builtins.input", lambda prompt="": next(inputs))
        monkeypatch.setattr("core.setup.getpass", lambda prompt="": "")

        from core.setup.cursor import _run

        _run()

        config = json.loads(Path(config_path).read_text())
        assert config["harnesses"]["cursor"]["target"] == "phoenix"
        assert config["harnesses"]["cursor"]["project_name"] == "cursor"

    def test_run_existing_config_skips_prompts(self, tmp_path, monkeypatch):
        """Cursor _run() with existing cursor entry skips backend prompts."""
        config_path = self._patch_cursor_install(tmp_path, monkeypatch)
        existing = {
            "harnesses": {
                "claude-code": {
                    "project_name": "claude-code",
                    "target": "arize",
                    "endpoint": "otlp.arize.com:443",
                    "api_key": "k",
                    "space_id": "s",
                },
                "cursor": {
                    "project_name": "cursor",
                    "target": "arize",
                    "endpoint": "otlp.arize.com:443",
                    "api_key": "k",
                    "space_id": "s",
                },
            },
        }
        Path(config_path).parent.mkdir(parents=True, exist_ok=True)
        with open(config_path, "w") as f:
            json.dump(existing, f, indent=2)

        # Inputs: project_name=default (no backend prompts since cursor entry exists),
        # then three content-logging prompts (defaults).
        inputs = iter(["", "", "", ""])
        monkeypatch.setattr("builtins.input", lambda prompt="": next(inputs))

        from core.setup.cursor import _run

        _run()

        config = json.loads(Path(config_path).read_text())
        assert config["harnesses"]["cursor"]["project_name"] == "cursor"
        assert config["harnesses"]["claude-code"]["project_name"] == "claude-code"
        assert "backend" not in config


# ---------------------------------------------------------------------------
# Info/err helper tests
# ---------------------------------------------------------------------------


class TestInfoErr:
    """Tests for info() and err() helpers."""

    def test_info_non_tty(self, capsys):
        """info() on non-tty has no ANSI codes."""
        from core.setup import info

        with patch.object(sys.stdout, "isatty", return_value=False):
            info("test message")
        out = capsys.readouterr().out
        assert "[arize] test message" in out
        assert "\033[" not in out

    def test_err_non_tty(self, capsys):
        """err() on non-tty has no ANSI codes."""
        from core.setup import err

        with patch.object(sys.stderr, "isatty", return_value=False):
            err("error message")
        captured = capsys.readouterr().err
        assert "[arize] error message" in captured
        assert "\033[" not in captured


# ---------------------------------------------------------------------------
# Copilot setup tests (core.setup.copilot)
# ---------------------------------------------------------------------------


class TestCopilotSetup:
    """Tests for core.setup.copilot."""

    def test_main_keyboard_interrupt(self):
        """main() catches KeyboardInterrupt gracefully."""
        from core.setup.copilot import main

        with patch("core.setup.copilot._run", side_effect=KeyboardInterrupt):
            with pytest.raises(SystemExit) as exc_info:
                main()
            assert exc_info.value.code == 1

    def test_main_eof_error(self):
        """main() catches EOFError gracefully."""
        from core.setup.copilot import main

        with patch("core.setup.copilot._run", side_effect=EOFError):
            with pytest.raises(SystemExit) as exc_info:
                main()
            assert exc_info.value.code == 1

    def test_run_delegates_to_installer(self):
        """_run() delegates to tracing.copilot/install.py install()."""
        import core.setup.copilot as copilot_mod

        mock_mod = MagicMock()
        with patch.object(copilot_mod, "_install_mod", mock_mod):
            copilot_mod._run()
            mock_mod.install.assert_called_once()

    def test_install_delegates_to_installer(self):
        """install() delegates to tracing.copilot/install.py install()."""
        import core.setup.copilot as copilot_mod

        mock_mod = MagicMock()
        with patch.object(copilot_mod, "_install_mod", mock_mod):
            copilot_mod.install()
            mock_mod.install.assert_called_once()

    def test_uninstall_delegates_to_installer(self):
        """uninstall() delegates to tracing.copilot/install.py uninstall()."""
        import core.setup.copilot as copilot_mod

        mock_mod = MagicMock()
        with patch.object(copilot_mod, "_install_mod", mock_mod):
            copilot_mod.uninstall()
            mock_mod.uninstall.assert_called_once()


# ---------------------------------------------------------------------------
# Gemini setup tests (core.setup.gemini)
# ---------------------------------------------------------------------------


class TestGeminiSetup:
    """Tests for core.setup.gemini."""

    def test_main_keyboard_interrupt(self):
        """main() catches KeyboardInterrupt gracefully."""
        from core.setup.gemini import main

        with patch("core.setup.gemini._run", side_effect=KeyboardInterrupt):
            with pytest.raises(SystemExit) as exc_info:
                main()
            assert exc_info.value.code == 1

    def test_main_eof_error(self):
        """main() catches EOFError gracefully."""
        from core.setup.gemini import main

        with patch("core.setup.gemini._run", side_effect=EOFError):
            with pytest.raises(SystemExit) as exc_info:
                main()
            assert exc_info.value.code == 1

    def test_main_prints_cancelled_on_interrupt(self, capsys):
        """main() prints 'Setup cancelled.' on KeyboardInterrupt."""
        from core.setup.gemini import main

        with patch("core.setup.gemini._run", side_effect=KeyboardInterrupt):
            with pytest.raises(SystemExit):
                main()
        assert "Setup cancelled." in capsys.readouterr().out

    def test_run_delegates_to_installer(self):
        """_run() delegates to tracing.gemini/install.py install()."""
        import core.setup.gemini as gemini_mod

        mock_mod = MagicMock()
        with patch.object(gemini_mod, "_install_mod", mock_mod):
            gemini_mod._run()
            mock_mod.install.assert_called_once()

    def test_install_delegates_to_installer(self):
        """install() delegates to tracing.gemini/install.py install()."""
        import core.setup.gemini as gemini_mod

        mock_mod = MagicMock()
        with patch.object(gemini_mod, "_install_mod", mock_mod):
            gemini_mod.install()
            mock_mod.install.assert_called_once()

    def test_uninstall_delegates_to_installer(self):
        """uninstall() delegates to tracing.gemini/install.py uninstall()."""
        import core.setup.gemini as gemini_mod

        mock_mod = MagicMock()
        with patch.object(gemini_mod, "_install_mod", mock_mod):
            gemini_mod.uninstall()
            mock_mod.uninstall.assert_called_once()


# ---------------------------------------------------------------------------
# Entry point registration tests
# ---------------------------------------------------------------------------


class TestEntryPoints:
    """Tests that entry points are properly defined in pyproject.toml."""

    def test_pyproject_has_setup_entry_points(self):
        """pyproject.toml defines all five setup wizard entry points."""
        pyproject_path = Path(__file__).parent.parent.parent / "pyproject.toml"
        content = pyproject_path.read_text()
        assert 'arize-setup-claude = "core.setup.claude:main"' in content
        assert 'arize-setup-codex = "core.setup.codex:main"' in content
        assert 'arize-setup-copilot = "core.setup.copilot:main"' in content
        assert 'arize-setup-cursor = "core.setup.cursor:main"' in content
        assert 'arize-setup-gemini = "core.setup.gemini:main"' in content

    def test_claude_main_is_callable(self):
        """core.setup.claude.main is importable and callable."""
        from core.setup.claude import main

        assert callable(main)

    def test_codex_main_is_callable(self):
        """core.setup.codex.main is importable and callable."""
        from core.setup.codex import main

        assert callable(main)

    def test_copilot_main_is_callable(self):
        """core.setup.copilot.main is importable and callable."""
        from core.setup.copilot import main

        assert callable(main)

    def test_cursor_main_is_callable(self):
        """core.setup.cursor.main is importable and callable."""
        from core.setup.cursor import main

        assert callable(main)

    def test_gemini_main_is_callable(self):
        """core.setup.gemini.main is importable and callable."""
        from core.setup.gemini import main

        assert callable(main)

    def test_gemini_install_uninstall_importable(self):
        """core.setup.gemini exports install and uninstall."""
        from core.setup.gemini import install, uninstall

        assert callable(install)
        assert callable(uninstall)
