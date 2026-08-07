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


# Every variable the non-interactive resolver reads. Two fixtures used to keep
# their own subsets, and the shorter one let PHOENIX_ENDPOINT, ARIZE_BACKEND and
# the ARIZE_LOG_* vars leak in from the developer's shell — the same
# non-hermetic failure mode that already bites this suite elsewhere.
_RESOLVED_ENV_KEYS = (
    "ARIZE_API_KEY",
    "ARIZE_SPACE_ID",
    "ARIZE_BACKEND",
    "ARIZE_OTLP_ENDPOINT",
    "ARIZE_PROJECT_NAME",
    "ARIZE_USER_ID",
    "ARIZE_LOG_PROMPTS",
    "ARIZE_LOG_TOOL_DETAILS",
    "ARIZE_LOG_TOOL_CONTENT",
    "ARIZE_ENV_FILE",
    "ARIZE_KIRO_AGENT",
    "ARIZE_KIRO_SET_DEFAULT",
    "PHOENIX_ENDPOINT",
    "PHOENIX_API_KEY",
)


def _isolate_resolver_env(monkeypatch):
    """Turn on non-interactive mode and clear every value the resolver reads."""
    from core.setup import _reset_dotenv_cache

    monkeypatch.setenv("ARIZE_NONINTERACTIVE", "1")
    for key in _RESOLVED_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    _reset_dotenv_cache()


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


class TestNonInteractive:
    """Tests for non-interactive resolution of the four setup prompts.

    Every test patches builtins.input to explode: reaching a prompt in this
    mode is the bug being guarded against, since there is nobody to answer it.
    """

    @pytest.fixture(autouse=True)
    def _non_interactive_env(self, monkeypatch):
        from core.setup import _reset_dotenv_cache

        _isolate_resolver_env(monkeypatch)
        yield
        _reset_dotenv_cache()

    @staticmethod
    def _no_prompts():
        """Patch input/getpass to fail loudly if the code tries to prompt."""
        return patch("builtins.input", side_effect=AssertionError("prompted in non-interactive mode"))

    def test_flag_detected(self):
        from core.setup import non_interactive

        assert non_interactive() is True

    def test_flag_off_by_default(self, monkeypatch):
        from core.setup import non_interactive

        monkeypatch.delenv("ARIZE_NONINTERACTIVE", raising=False)
        assert non_interactive() is False

    def test_arize_from_env(self, monkeypatch):
        """Space ID implies Arize AX; no backend flag needed."""
        from core.setup import prompt_backend

        monkeypatch.setenv("ARIZE_API_KEY", "key-123")
        monkeypatch.setenv("ARIZE_SPACE_ID", "space-456")

        with self._no_prompts():
            target, creds = prompt_backend()

        assert target == "arize"
        assert creds == {
            "endpoint": "otlp.arize.com:443",
            "api_key": "key-123",
            "space_id": "space-456",
        }

    def test_api_key_never_printed(self, monkeypatch, capsys):
        from core.setup import prompt_backend

        monkeypatch.setenv("ARIZE_API_KEY", "super-secret-key")
        monkeypatch.setenv("ARIZE_SPACE_ID", "space-456")

        with self._no_prompts():
            prompt_backend()

        captured = capsys.readouterr()
        assert "super-secret-key" not in captured.out
        assert "super-secret-key" not in captured.err

    def test_phoenix_inferred_from_endpoint(self, monkeypatch):
        from core.setup import prompt_backend

        monkeypatch.setenv("PHOENIX_ENDPOINT", "http://phoenix:6006")
        monkeypatch.setenv("PHOENIX_API_KEY", "phx-key")

        with self._no_prompts():
            target, creds = prompt_backend()

        assert target == "phoenix"
        assert creds == {"endpoint": "http://phoenix:6006", "api_key": "phx-key"}

    def test_explicit_backend_overrides_inference(self, monkeypatch):
        """ARIZE_BACKEND=phoenix wins even when a space ID is present."""
        from core.setup import prompt_backend

        monkeypatch.setenv("ARIZE_BACKEND", "phoenix")
        monkeypatch.setenv("ARIZE_SPACE_ID", "space-456")

        with self._no_prompts():
            target, creds = prompt_backend()

        assert target == "phoenix"
        assert creds["endpoint"] == "http://localhost:6006"

    def test_backend_alias_ax(self, monkeypatch):
        from core.setup import prompt_backend

        monkeypatch.setenv("ARIZE_BACKEND", "ax")
        monkeypatch.setenv("ARIZE_API_KEY", "key-123")
        monkeypatch.setenv("ARIZE_SPACE_ID", "space-456")

        with self._no_prompts():
            target, _ = prompt_backend()

        assert target == "arize"

    def test_custom_otlp_endpoint(self, monkeypatch):
        from core.setup import prompt_backend

        monkeypatch.setenv("ARIZE_API_KEY", "key-123")
        monkeypatch.setenv("ARIZE_SPACE_ID", "space-456")
        monkeypatch.setenv("ARIZE_OTLP_ENDPOINT", "custom.arize.com:443")

        with self._no_prompts():
            _, creds = prompt_backend()

        assert creds["endpoint"] == "custom.arize.com:443"

    def test_missing_api_key_exits(self, monkeypatch, capsys):
        from core.setup import prompt_backend

        monkeypatch.setenv("ARIZE_SPACE_ID", "space-456")

        with self._no_prompts():
            with pytest.raises(SystemExit):
                prompt_backend()

        assert "ARIZE_API_KEY" in capsys.readouterr().err

    def test_missing_space_id_exits(self, monkeypatch, capsys):
        """An API key alone is ambiguous — both backends use one."""
        from core.setup import prompt_backend

        monkeypatch.setenv("ARIZE_API_KEY", "key-123")

        with self._no_prompts():
            with pytest.raises(SystemExit):
                prompt_backend()

        err = capsys.readouterr().err
        assert "cannot tell which backend" in err
        assert "ARIZE_SPACE_ID" in err

    def test_no_credentials_at_all_exits(self, capsys):
        from core.setup import prompt_backend

        with self._no_prompts():
            with pytest.raises(SystemExit):
                prompt_backend()

        err = capsys.readouterr().err
        assert "No credentials found" in err
        assert "ARIZE_SPACE_ID" in err and "PHOENIX_ENDPOINT" in err

    def test_exit_code_is_nonzero(self, capsys):
        """The installer must fail loudly enough for a caller to detect."""
        from core.setup import prompt_backend

        with self._no_prompts():
            with pytest.raises(SystemExit) as excinfo:
                prompt_backend()

        assert excinfo.value.code == 1

    def test_arize_key_with_only_phoenix_endpoint_exits(self, monkeypatch, capsys):
        """Inferring Phoenix here would throw away the Arize key.

        Real setup: a dev runs Phoenix locally for their app, so its .env has
        PHOENIX_ENDPOINT, while they want Arize AX for the coding agent.
        """
        from core.setup import prompt_backend

        monkeypatch.setenv("ARIZE_API_KEY", "my-ax-key")
        monkeypatch.setenv("PHOENIX_ENDPOINT", "http://localhost:6006")

        with self._no_prompts():
            with pytest.raises(SystemExit):
                prompt_backend()

        assert "ARIZE_SPACE_ID" in capsys.readouterr().err

    def test_both_backends_configured_exits(self, monkeypatch, capsys):
        from core.setup import prompt_backend

        monkeypatch.setenv("ARIZE_API_KEY", "k")
        monkeypatch.setenv("ARIZE_SPACE_ID", "s")
        monkeypatch.setenv("PHOENIX_ENDPOINT", "http://localhost:6006")

        with self._no_prompts():
            with pytest.raises(SystemExit):
                prompt_backend()

        assert "ambiguous" in capsys.readouterr().err

    def test_explicit_backend_resolves_the_ambiguity(self, monkeypatch):
        """ARIZE_BACKEND is the documented way out of both errors above."""
        from core.setup import prompt_backend

        monkeypatch.setenv("ARIZE_BACKEND", "phoenix")
        monkeypatch.setenv("ARIZE_API_KEY", "k")
        monkeypatch.setenv("ARIZE_SPACE_ID", "s")
        monkeypatch.setenv("PHOENIX_ENDPOINT", "http://localhost:6006")

        with self._no_prompts():
            target, creds = prompt_backend()

        assert target == "phoenix"
        assert creds["endpoint"] == "http://localhost:6006"

    def test_phoenix_alone_still_infers_phoenix(self, monkeypatch):
        """No Arize credentials at all — inference is unambiguous."""
        from core.setup import prompt_backend

        monkeypatch.setenv("PHOENIX_ENDPOINT", "http://localhost:6006")

        with self._no_prompts():
            target, _ = prompt_backend()

        assert target == "phoenix"

    def test_unknown_backend_exits(self, monkeypatch, capsys):
        from core.setup import prompt_backend

        monkeypatch.setenv("ARIZE_BACKEND", "datadog")

        with self._no_prompts():
            with pytest.raises(SystemExit):
                prompt_backend()

        assert "datadog" in capsys.readouterr().err

    def test_project_name_defaults(self):
        from core.setup import prompt_project_name

        with self._no_prompts():
            assert prompt_project_name("claude-code") == "claude-code"

    def test_project_name_ignores_ambient_env(self, monkeypatch):
        """ARIZE_PROJECT_NAME in the environment belongs to another harness.

        An installed harness exports it into every session; inheriting it would
        name this harness's project after a different one and collide spans.
        """
        from core.setup import prompt_project_name

        monkeypatch.setenv("ARIZE_PROJECT_NAME", "claude-code")

        with self._no_prompts():
            assert prompt_project_name("codex") == "codex"

    def test_project_name_source_label_does_not_credit_env(self, monkeypatch, capsys):
        """The label must not name a source that was never consulted."""
        from core.setup import prompt_project_name

        monkeypatch.setenv("ARIZE_PROJECT_NAME", "claude-code")

        with self._no_prompts():
            prompt_project_name("codex")

        out = capsys.readouterr().out
        assert "(from default)" in out
        assert "ARIZE_PROJECT_NAME" not in out

    def test_user_id_blank_by_default(self):
        from core.setup import prompt_user_id

        with self._no_prompts():
            assert prompt_user_id() == ""

    def test_user_id_from_env(self, monkeypatch):
        from core.setup import prompt_user_id

        monkeypatch.setenv("ARIZE_USER_ID", "alice")

        with self._no_prompts():
            assert prompt_user_id() == "alice"

    def test_logging_defaults_to_capturing_nothing(self):
        """Unattended, every category must be opted into explicitly.

        The interactive wizard's three questions still default to yes — a person
        declining to change an answer they were shown is consent. The same
        default with nobody watching is not: `update` runs non-interactively
        whenever there is no terminal, so a cron job against a config with no
        logging block would have switched prompt and file-content capture on.
        """
        from core.setup import prompt_content_logging

        with self._no_prompts():
            block = prompt_content_logging()

        assert block == {"prompts": False, "tool_details": False, "tool_content": False}

    def test_logging_opt_in_per_category(self, monkeypatch):
        from core.setup import prompt_content_logging

        monkeypatch.setenv("ARIZE_LOG_PROMPTS", "true")
        monkeypatch.setenv("ARIZE_LOG_TOOL_CONTENT", "1")

        with self._no_prompts():
            block = prompt_content_logging()

        assert block == {"prompts": True, "tool_details": False, "tool_content": True}

    def test_logging_opt_out(self, monkeypatch):
        from core.setup import prompt_content_logging

        monkeypatch.setenv("ARIZE_LOG_PROMPTS", "false")
        monkeypatch.setenv("ARIZE_LOG_TOOL_DETAILS", "true")
        monkeypatch.setenv("ARIZE_LOG_TOOL_CONTENT", "0")

        with self._no_prompts():
            block = prompt_content_logging()

        assert block == {"prompts": False, "tool_details": True, "tool_content": False}

    def test_env_flag_default_off_needs_explicit_optin(self, monkeypatch):
        """A default-off setting must not be turned on by any old value."""
        from core.setup import env_flag

        assert env_flag("ARIZE_LOG_PROMPTS", default=False) is False
        monkeypatch.setenv("ARIZE_LOG_PROMPTS", "1")
        assert env_flag("ARIZE_LOG_PROMPTS", default=False) is True
        monkeypatch.setenv("ARIZE_LOG_PROMPTS", "no")
        assert env_flag("ARIZE_LOG_PROMPTS", default=False) is False

    def test_kiro_agent_name_defaults(self):
        """Kiro's own prompt resolves too — it is the one harness with extras."""
        from tracing.kiro.install import _prompt_agent_name

        with self._no_prompts():
            assert _prompt_agent_name() == "arize-traced"

    def test_kiro_agent_name_from_env(self, monkeypatch):
        from tracing.kiro.install import _prompt_agent_name

        monkeypatch.setenv("ARIZE_KIRO_AGENT", "my-agent")

        with self._no_prompts():
            assert _prompt_agent_name() == "my-agent"

    def test_kiro_set_default_is_opt_in(self, monkeypatch):
        """Never silently repoint the user's default Kiro agent."""
        from tracing.kiro import install as kiro_install

        called = []
        monkeypatch.setattr(kiro_install.shutil, "which", lambda _: called.append("which") or None)

        with self._no_prompts():
            kiro_install._maybe_set_default("my-agent")

        assert called == []

    def test_kiro_set_default_opt_in_honored(self, monkeypatch, capsys):
        from tracing.kiro import install as kiro_install

        monkeypatch.setenv("ARIZE_KIRO_SET_DEFAULT", "1")
        monkeypatch.setenv("ARIZE_DRY_RUN", "1")

        with self._no_prompts():
            kiro_install._maybe_set_default("my-agent")

        assert "set-default my-agent" in capsys.readouterr().out

    def test_harness_check_skipped(self, monkeypatch):
        """The y/N 'not installed' prompt must not fire under the flag, even on a TTY."""
        from core.setup import ensure_harness_installed

        monkeypatch.setattr(sys.stdout, "isatty", lambda: True)

        with self._no_prompts():
            assert ensure_harness_installed("Nope", home_subdir=".no-such-harness-dir") is True


def _named_env(tmp_path, monkeypatch, content, name="creds.env"):
    """Write a dotenv and point ARIZE_ENV_FILE at it.

    There is no automatic ./.env search: a named file outranks the process
    environment, so an implicit one would let a cloned repo's dotenv choose where
    credentials get sent. Tests therefore name the file, like real callers do.
    """
    from core.setup import _reset_dotenv_cache

    path = tmp_path / name
    path.write_text(content)
    monkeypatch.setenv("ARIZE_ENV_FILE", str(path))
    _reset_dotenv_cache()
    return path


class TestDotenvResolution:
    """Non-interactive installs can read credentials from a dotenv file.

    This is the path that keeps an API key out of argv and shell history:
    `ax api-keys create --env-file .env` writes it, the installer reads it.
    """

    @pytest.fixture(autouse=True)
    def _clean_env(self, monkeypatch):
        from core.setup import _reset_dotenv_cache

        _isolate_resolver_env(monkeypatch)
        yield
        _reset_dotenv_cache()

    def test_named_file_is_read(self, tmp_path, monkeypatch):
        from core.setup import prompt_backend

        _named_env(tmp_path, monkeypatch, "ARIZE_API_KEY=file-key\nARIZE_SPACE_ID=file-space\n")

        with patch("builtins.input", side_effect=AssertionError("prompted")):
            target, creds = prompt_backend()

        assert target == "arize"
        assert creds["api_key"] == "file-key"
        assert creds["space_id"] == "file-space"

    def test_cwd_dotenv_is_ignored(self, tmp_path, monkeypatch):
        """A dotenv in the working directory must never be read.

        Security boundary: a file's values outrank the process environment, and
        cwd is whatever repo the user is sitting in. Reading it let a cloned
        repo's .env choose ARIZE_OTLP_ENDPOINT while the user's real key came
        from the environment, so every later session shipped spans and a bearer
        token to an endpoint the repo picked.
        """
        from core.setup import _reset_dotenv_cache, prompt_backend

        (tmp_path / ".env").write_text("ARIZE_OTLP_ENDPOINT=otlp.attacker.example:443\n")
        (tmp_path / ".env.local").write_text("ARIZE_OTLP_ENDPOINT=otlp.attacker.example:443\n")
        monkeypatch.setenv("ARIZE_API_KEY", "real-key")
        monkeypatch.setenv("ARIZE_SPACE_ID", "real-space")
        _reset_dotenv_cache()

        with patch("builtins.input", side_effect=AssertionError("prompted")):
            _, creds = prompt_backend()

        assert "attacker" not in creds["endpoint"], "cwd dotenv must not choose the endpoint"
        assert creds["endpoint"] == "otlp.arize.com:443"

    def test_file_beats_real_env(self, tmp_path, monkeypatch):
        """The file wins: it is chosen, whereas ARIZE_* vars are often inherited."""
        from core.setup import prompt_backend

        _named_env(tmp_path, monkeypatch, "ARIZE_API_KEY=file-key\nARIZE_SPACE_ID=file-space\n")
        monkeypatch.setenv("ARIZE_API_KEY", "env-key")

        with patch("builtins.input", side_effect=AssertionError("prompted")):
            _, creds = prompt_backend()

        assert creds["api_key"] == "file-key"
        assert creds["space_id"] == "file-space"

    def test_ambient_harness_env_does_not_leak_in(self, tmp_path, monkeypatch):
        """Regression: an installed harness exports ARIZE_* into every session.

        Those inherited values used to beat the .env the caller had just
        written, pairing a fresh key with a stale space ID and overriding the
        project name.
        """
        from core.setup import prompt_backend, prompt_project_name

        _named_env(
            tmp_path,
            monkeypatch,
            "ARIZE_API_KEY=fresh-key\nARIZE_SPACE_ID=intended-space\nARIZE_PROJECT_NAME=intended-project\n",
        )
        monkeypatch.setenv("ARIZE_API_KEY", "stale-key")
        monkeypatch.setenv("ARIZE_SPACE_ID", "stale-space")
        monkeypatch.setenv("ARIZE_PROJECT_NAME", "claude-code")

        with patch("builtins.input", side_effect=AssertionError("prompted")):
            _, creds = prompt_backend()
            project = prompt_project_name("fallback")

        assert creds["api_key"] == "fresh-key"
        assert creds["space_id"] == "intended-space"
        assert project == "intended-project"

    def test_env_still_used_when_file_lacks_the_key(self, tmp_path, monkeypatch):
        """The file overrides only what it actually defines."""
        from core.setup import prompt_backend

        _named_env(tmp_path, monkeypatch, "ARIZE_SPACE_ID=file-space\n")
        monkeypatch.setenv("ARIZE_API_KEY", "env-key")

        with patch("builtins.input", side_effect=AssertionError("prompted")):
            _, creds = prompt_backend()

        assert creds["api_key"] == "env-key"
        assert creds["space_id"] == "file-space"

    def test_parses_export_quotes_and_comments(self, tmp_path, monkeypatch):
        from core.setup import prompt_backend, prompt_project_name

        _named_env(
            tmp_path,
            monkeypatch,
            "# Arize credentials\n"
            "\n"
            'export ARIZE_API_KEY="quoted-key"\n'
            "ARIZE_SPACE_ID='single-quoted'\n"
            "ARIZE_PROJECT_NAME = spaced-out \n"
            "MALFORMED_LINE\n",
        )

        with patch("builtins.input", side_effect=AssertionError("prompted")):
            _, creds = prompt_backend()
            project = prompt_project_name("fallback")

        assert creds["api_key"] == "quoted-key"
        assert creds["space_id"] == "single-quoted"
        assert project == "spaced-out"

    def test_unrelated_keys_ignored(self, tmp_path, monkeypatch):
        """An app's .env holds all sorts of things; only our keys are read."""
        from core.setup import _dotenv_values

        _named_env(tmp_path, monkeypatch, "OPENAI_API_KEY=sk-nope\nDATABASE_URL=postgres://x\nARIZE_SPACE_ID=space\n")

        assert _dotenv_values() == {"ARIZE_SPACE_ID": "space"}

    def test_dotenv_ignored_when_interactive(self, tmp_path, monkeypatch):
        """Without the flag the wizard still asks, even with a .env sitting there."""
        from core.setup import prompt_backend

        monkeypatch.delenv("ARIZE_NONINTERACTIVE", raising=False)
        _named_env(tmp_path, monkeypatch, "ARIZE_API_KEY=file-key\nARIZE_SPACE_ID=file-space\n")

        with patch("builtins.input", side_effect=["2", "typed-space", ""]):
            with patch("core.setup.getpass", return_value="typed-key"):
                with patch.object(sys.stdout, "isatty", return_value=False):
                    _, creds = prompt_backend()

        assert creds["api_key"] == "typed-key"
        assert creds["space_id"] == "typed-space"

    def test_missing_file_is_not_an_error(self, capsys):
        """No .env in the cwd is normal — only an *explicit* path must exist."""
        from core.setup import _dotenv_values

        assert _dotenv_values() == {}
        assert "Reading configuration" not in capsys.readouterr().out

    def test_unreadable_explicit_path_is_fatal(self, tmp_path, monkeypatch, capsys):
        """A typo in ARIZE_ENV_FILE must not fall through to the environment.

        Naming a file states where the credentials come from; ignoring it
        silently installed with whatever happened to be in the environment.
        """
        from core.setup import _dotenv_values

        monkeypatch.setenv("ARIZE_ENV_FILE", str(tmp_path / "typo.env"))

        with pytest.raises(SystemExit) as excinfo:
            _dotenv_values()

        assert excinfo.value.code == 1
        assert "ARIZE_ENV_FILE" in capsys.readouterr().err

    def test_explicit_path_that_is_a_directory_is_fatal(self, tmp_path, monkeypatch):
        from core.setup import _dotenv_values

        monkeypatch.setenv("ARIZE_ENV_FILE", str(tmp_path))

        with pytest.raises(SystemExit):
            _dotenv_values()

    def test_project_name_read_from_file(self, tmp_path, monkeypatch):
        """The file may set the project name; only the environment is ignored."""
        from core.setup import prompt_project_name

        _named_env(tmp_path, monkeypatch, "ARIZE_PROJECT_NAME=from-file\n")

        with patch("builtins.input", side_effect=AssertionError("prompted")):
            assert prompt_project_name("codex") == "from-file"

    def test_inline_comment_stripped(self, tmp_path, monkeypatch):
        """`KEY=value # note` must not yield a value with the comment attached."""
        from core.setup import prompt_backend

        _named_env(
            tmp_path,
            monkeypatch,
            "ARIZE_API_KEY=k # the key\nARIZE_SPACE_ID=space-abc # my main space\n",
        )

        with patch("builtins.input", side_effect=AssertionError("prompted")):
            _, creds = prompt_backend()

        assert creds["space_id"] == "space-abc"
        assert creds["api_key"] == "k"

    def test_tab_before_comment_stripped(self, tmp_path, monkeypatch):
        from core.setup import _dotenv_values

        _named_env(tmp_path, monkeypatch, "ARIZE_SPACE_ID=space-abc\t# tabbed note\n")

        assert _dotenv_values()["ARIZE_SPACE_ID"] == "space-abc"

    def test_hash_in_quoted_value_preserved(self, tmp_path, monkeypatch):
        """Quoting is the documented way to keep a literal '#'."""
        from core.setup import _dotenv_values

        _named_env(tmp_path, monkeypatch, 'ARIZE_PROJECT_NAME="has # hash"\n')

        assert _dotenv_values()["ARIZE_PROJECT_NAME"] == "has # hash"

    def test_hash_without_leading_space_is_kept(self, tmp_path, monkeypatch):
        """A '#' with no preceding whitespace is part of the value, per dotenv."""
        from core.setup import _dotenv_values

        _named_env(tmp_path, monkeypatch, "ARIZE_SPACE_ID=space#abc\n")

        assert _dotenv_values()["ARIZE_SPACE_ID"] == "space#abc"

    def test_unspaced_hash_is_part_of_the_value(self, tmp_path, monkeypatch):
        """`#` only starts a comment when whitespace precedes it.

        So `KEY=#nothing` is the literal value `#nothing`, consistent with
        `KEY=abc#123` yielding `abc#123`. This is python-dotenv's rule and a
        change from the hand-rolled parser, which returned "" here — worth
        knowing, because `ARIZE_SPACE_ID=# TODO` now installs that string
        instead of reporting a missing value.
        """
        from core.setup import _dotenv_values

        _named_env(tmp_path, monkeypatch, "ARIZE_SPACE_ID=#nothing here\nARIZE_API_KEY=k\n")

        assert _dotenv_values()["ARIZE_SPACE_ID"] == "#nothing here"

    @pytest.mark.parametrize(
        "body,expected",
        [
            ("ARIZE_API_KEY=abc123", "abc123"),
            ("export ARIZE_API_KEY=abc123", "abc123"),
            ('ARIZE_API_KEY="abc123"', "abc123"),
            ("ARIZE_API_KEY='abc123'", "abc123"),
            ("ARIZE_API_KEY=abc123 # my key", "abc123"),
            ("ARIZE_API_KEY=abc\t# my key", "abc"),
            ('ARIZE_API_KEY="abc#123"', "abc#123"),
            ("ARIZE_API_KEY=abc#123", "abc#123"),
            ("ARIZE_API_KEY=a=b=c", "a=b=c"),
            ("   ARIZE_API_KEY=abc123   ", "abc123"),
            ("ARIZE_API_KEY = abc123", "abc123"),
            ("ARIZE_API_KEY=abc123\r", "abc123"),
            ("ARIZE_API_KEY=first\nARIZE_API_KEY=second", "second"),
            ("ARIZE_API_KEY=$HOME", "$HOME"),
            ('ARIZE_API_KEY=ab"cd', 'ab"cd'),
            ("ARIZE_API_KEY=", ""),
        ],
    )
    def test_matches_reference_dotenv_semantics(self, tmp_path, body, expected):
        """Each case was checked against python-dotenv's dotenv_values.

        The parser is hand-rolled because the package ships with zero runtime
        dependencies, and adding one would also have to be bundled for the
        offline install. So the behaviour is pinned here instead. The one
        deliberate divergence is that python-dotenv expands ``\\n`` inside
        double quotes; none of the keys read here want a newline.
        """
        from core.setup import _parse_dotenv

        path = tmp_path / "ref.env"
        path.write_text(body + "\n")

        assert _parse_dotenv(path).get("ARIZE_API_KEY") == expected

    @pytest.mark.parametrize(
        "body",
        ['ARIZE_API_KEY="abc123', "ARIZE_API_KEY='abc123", "ARIZE_API_KEY=\"abc123'"],
    )
    def test_unbalanced_quote_is_fatal(self, tmp_path, body, capsys):
        """It used to yield the value with the stray quote still attached.

        `ARIZE_API_KEY="abc` became `"abc` — a credential that reports as found
        and then fails authentication with nothing pointing at the typo.
        python-dotenv rejects these lines; we stop, because the file was named
        explicitly and a corrupted credential is worse than a missing one.
        """
        from core.setup import _parse_dotenv

        path = tmp_path / "bad.env"
        path.write_text(body + "\n")

        with pytest.raises(SystemExit) as exc:
            _parse_dotenv(path)

        assert exc.value.code == 1
        assert "unbalanced" in capsys.readouterr().err

    def test_backslash_n_expands_in_double_quotes(self, tmp_path):
        """python-dotenv expands escapes inside double quotes; single quotes don't.

        None of the nine keys read here want a newline, so this is not useful —
        it is simply what a standard parser does, inherited rather than chosen.
        """
        from core.setup import _parse_dotenv

        dq = tmp_path / "dq.env"
        dq.write_text('ARIZE_API_KEY="a\\nb"\n')
        assert _parse_dotenv(dq)["ARIZE_API_KEY"] == "a\nb"

        sq = tmp_path / "sq.env"
        sq.write_text("ARIZE_API_KEY='a\\nb'\n")
        assert _parse_dotenv(sq)["ARIZE_API_KEY"] == "a\\nb"

    def test_reports_source_per_value(self, tmp_path, monkeypatch, capsys):
        """Mixed sources must be visible: which value came from where."""
        from core.setup import prompt_backend

        _named_env(tmp_path, monkeypatch, "ARIZE_API_KEY=fresh-key\n")
        monkeypatch.setenv("ARIZE_SPACE_ID", "space-from-env")

        with patch("builtins.input", side_effect=AssertionError("prompted")):
            prompt_backend()

        out = capsys.readouterr().out
        assert "$ARIZE_SPACE_ID" in out
        assert str(tmp_path / "creds.env") in out
        assert "fresh-key" not in out


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
        # project_name lives in config.json only; not baked into settings.json (#74).
        assert "ARIZE_PROJECT_NAME" not in result["env"]
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
        # project_name lives in config.json only; not baked into the env file (#74).
        assert "ARIZE_PROJECT_NAME" not in content

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
        # project_name lives in config.json only; not baked into the env file (#74).
        assert "ARIZE_PROJECT_NAME" not in content

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

    def test_malformed_toml_is_left_untouched(self, tmp_path):
        toml_path = tmp_path / "config.toml"
        original = "[otel\nendpoint = 'broken'\n"
        toml_path.write_text(original)
        from core.setup.codex import _update_toml_otel_section

        with pytest.raises(ValueError, match="Malformed TOML"):
            _update_toml_otel_section(toml_path, 4318)

        assert toml_path.read_text() == original

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

    def test_run_uses_custom_codex_home(self, tmp_path, monkeypatch):
        config_path = str(tmp_path / "config.json")
        custom_codex_dir = tmp_path / "alternate-codex"
        custom_codex_dir.mkdir()

        import core.config

        monkeypatch.setattr(core.config, "CONFIG_FILE", config_path)
        monkeypatch.setenv("CODEX_HOME", str(custom_codex_dir))
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

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

        assert (custom_codex_dir / "arize-env.sh").exists()
        assert (custom_codex_dir / "config.toml").exists()
        assert not (tmp_path / ".codex").exists()


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
