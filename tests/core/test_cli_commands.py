"""Tests for ``arize-harness setup | uninstall | status | update | --version`` (core/setup/commands.py).

End-to-end cases run ``python -m core.cli`` in a subprocess with ``HOME`` set to
a temp dir, because ``core.setup`` resolves its paths from ``Path.home()`` at
import. Branches that would run pip or git are tested in-process with those
calls replaced, so nothing here reaches the network or the real ``HOME``.
"""

from __future__ import annotations

import inspect
import json
import os
import subprocess
import sys
import types
from pathlib import Path
from typing import Dict, List, Optional

import pytest

from core import cli, install_table, runtime
from core.constants import HARNESSES
from core.setup import commands
from tests._pyproject import REPO_ROOT

INSTALL_SH = REPO_ROOT / "install.sh"
POSIX_ONLY = pytest.mark.skipif(os.name == "nt", reason="needs bash and a POSIX getpass fallback")

ARIZE_CREDS = "ARIZE_API_KEY=test-key\nARIZE_SPACE_ID=test-space\n"
# Answers for a fresh interactive install: backend menu, API key, space ID,
# OTLP endpoint, user ID, project name, then three content-logging questions.
INTERACTIVE_ARIZE = "2\ntest-key\ntest-space\n\n\n\n\n\n\n"
# The same answers with --backend given, which skips the menu.
INTERACTIVE_ARIZE_NO_MENU = INTERACTIVE_ARIZE[2:]
BACKEND_MENU = "Which backend do you want to use?"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _home(tmp_path: Path, name: str = "home") -> Path:
    """A throwaway HOME where Claude Code and Codex look installed, so no "install anyway?" prompt appears."""
    home = tmp_path / name
    for sub in (".claude", ".codex"):
        (home / sub).mkdir(parents=True)
    return home


def _env(home: Path, **extra: str) -> Dict[str, str]:
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("ARIZE_", "PHOENIX_", "OTEL_", "PIP_"))
        and k not in ("CODEX_HOME", "DEVIN_PROJECT_DIR", "PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV")
    }
    env.update(
        {
            "HOME": str(home),
            "USERPROFILE": str(home),
            "PYTHONPATH": str(REPO_ROOT),
            "NO_COLOR": "1",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": str(home / ".gitconfig"),
        }
    )
    env.update(extra)
    return env


def _run(home: Path, cmd: List[str], stdin: str = "", **extra_env: str) -> subprocess.CompletedProcess:
    # A new session has no controlling terminal, so getpass reads the answers
    # from stdin instead of waiting on the developer's /dev/tty.
    return subprocess.run(
        cmd,
        input=stdin,
        capture_output=True,
        text=True,
        cwd=home,
        env=_env(home, **extra_env),
        timeout=120,
        check=False,
        start_new_session=os.name != "nt",
    )


def _cli(home: Path, *args: str, stdin: str = "", **extra_env: str) -> subprocess.CompletedProcess:
    return _run(home, [sys.executable, "-m", "core.cli", *args], stdin, **extra_env)


def _install_sh_install(home: Path, harness: str, stdin: str = "", **extra_env: str) -> None:
    """What ``install.sh <harness>`` runs once the package is in its venv: migrate, then the harness's install.py."""
    for cmd in (
        [sys.executable, "-m", "core.config", "migrate"],
        [sys.executable, str(REPO_ROOT / "tracing" / harness / "install.py"), "install"],
    ):
        result = _run(home, cmd, stdin if "install.py" in cmd[1] else "", **extra_env)
        assert result.returncode == 0, result.stdout + result.stderr


def _fake_venv(home: Path) -> None:
    """A ``~/.arize/harness/venv/bin/python`` for install.sh that runs this test's interpreter."""
    bin_dir = home / ".arize" / "harness" / "venv" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    python = bin_dir / "python"
    python.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    python.chmod(0o755)


def _snapshot(home: Path) -> Dict[str, str]:
    """Every file under ``home``, with the HOME path itself normalized away so two homes compare equal."""
    files = {}
    for path in sorted(home.rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts:
            files[path.relative_to(home).as_posix()] = path.read_text(errors="replace").replace(str(home), "<HOME>")
    return files


def _write_creds(home: Path, text: str = ARIZE_CREDS) -> str:
    creds = home / "creds.env"
    creds.write_text(text)
    return str(creds)


def _config(home: Path) -> dict:
    return json.loads((home / ".arize" / "harness" / "config.json").read_text())


# ---------------------------------------------------------------------------
# Installer table
# ---------------------------------------------------------------------------


class TestInstallerTable:
    def test_every_install_py_is_in_the_table(self) -> None:
        on_disk = {f"tracing.{path.parent.name}.install" for path in (REPO_ROOT / "tracing").glob("*/install.py")}
        in_table = {installer.module for installer in install_table.INSTALLERS.values()}
        assert on_disk == in_table

    def test_keys_are_the_harnesses_keys(self) -> None:
        assert list(install_table.INSTALLERS) == list(HARNESSES)

    @pytest.mark.parametrize("key", list(install_table.INSTALLERS))
    def test_each_entry_loads_and_names_its_config_key(self, key) -> None:
        module = install_table.load(key)
        assert callable(module.install) and callable(module.uninstall)
        package = install_table.INSTALLERS[key].module.rsplit(".", 1)[0]
        constants = __import__(f"{package}.constants", fromlist=["HARNESS_NAME"])
        assert constants.HARNESS_NAME == key

    @pytest.mark.parametrize("key", list(install_table.INSTALLERS))
    def test_detection_values_match_the_harness_constants(self, key) -> None:
        package = install_table.INSTALLERS[key].module.rsplit(".", 1)[0]
        constants = __import__(f"{package}.constants", fromlist=["*"])
        expected = (getattr(constants, "HARNESS_HOME", None), getattr(constants, "HARNESS_BIN", None))
        if expected == (None, None):
            assert install_table.detection_values(key) is None
        else:
            assert install_table.detection_values(key) == expected

    def test_harnesses_without_detection_values(self) -> None:
        undetected = {key for key in install_table.INSTALLERS if install_table.detection_values(key) is None}
        assert undetected == {"antigravity", "copilot", "gemini"}

    def test_claude_alias(self) -> None:
        assert install_table.resolve("claude") == "claude-code"
        assert install_table.resolve("claude-code") == "claude-code"
        assert install_table.resolve("nope") is None


# ---------------------------------------------------------------------------
# setup: same result as install.sh <harness>
# ---------------------------------------------------------------------------


@POSIX_ONLY
class TestSetupMatchesInstallSh:
    @pytest.mark.parametrize("name, harness_dir", [("claude", "claude_code"), ("codex", "codex")])
    def test_non_interactive(self, name, harness_dir, tmp_path) -> None:
        legacy, new = _home(tmp_path, "legacy"), _home(tmp_path, "new")
        for home in (legacy, new):
            _write_creds(home)
        env = {"ARIZE_NONINTERACTIVE": "1"}
        _install_sh_install(legacy, harness_dir, ARIZE_ENV_FILE=str(legacy / "creds.env"), **env)
        result = _cli(new, "setup", name, "--non-interactive", ARIZE_ENV_FILE=str(new / "creds.env"))
        assert result.returncode == 0, result.stdout + result.stderr

        assert _snapshot(new) == _snapshot(legacy)
        assert _config(new)["harnesses"][install_table.resolve(name)]["target"] == "arize"

    def test_non_interactive_from_environment(self, tmp_path) -> None:
        legacy, new = _home(tmp_path, "legacy"), _home(tmp_path, "new")
        creds = {"ARIZE_API_KEY": "env-key", "ARIZE_SPACE_ID": "env-space"}
        _install_sh_install(legacy, "claude_code", ARIZE_NONINTERACTIVE="1", **creds)
        result = _cli(new, "setup", "claude", "-y", **creds)
        assert result.returncode == 0, result.stdout + result.stderr
        assert _snapshot(new) == _snapshot(legacy)

    @pytest.mark.parametrize("name, harness_dir", [("claude", "claude_code"), ("codex", "codex")])
    def test_interactive(self, name, harness_dir, tmp_path) -> None:
        legacy, new = _home(tmp_path, "legacy"), _home(tmp_path, "new")
        _install_sh_install(legacy, harness_dir, INTERACTIVE_ARIZE)
        result = _cli(new, "setup", name, stdin=INTERACTIVE_ARIZE)
        assert result.returncode == 0, result.stdout + result.stderr

        assert _snapshot(new) == _snapshot(legacy)
        entry = _config(new)["harnesses"][install_table.resolve(name)]
        assert (entry["target"], entry["api_key"], entry["space_id"]) == ("arize", "test-key", "test-space")


# ---------------------------------------------------------------------------
# setup --backend
# ---------------------------------------------------------------------------


@POSIX_ONLY
class TestBackendFlag:
    def test_interactive_skips_only_the_backend_menu(self, tmp_path) -> None:
        home = _home(tmp_path)
        result = _cli(home, "setup", "claude", "--backend", "arize", stdin=INTERACTIVE_ARIZE_NO_MENU)
        assert result.returncode == 0, result.stdout + result.stderr
        assert BACKEND_MENU not in result.stdout
        # The credential prompts still ran and took the answers.
        entry = _config(home)["harnesses"]["claude-code"]
        assert (entry["target"], entry["api_key"], entry["space_id"]) == ("arize", "test-key", "test-space")

    def test_interactive_still_offers_copy_from(self, tmp_path) -> None:
        home = _home(tmp_path)
        _write_creds(home)
        first = _cli(home, "setup", "codex", "-y", ARIZE_ENV_FILE=str(home / "creds.env"))
        assert first.returncode == 0, first.stdout + first.stderr

        # Copy from codex (1), then user ID and project name.
        result = _cli(home, "setup", "claude", "--backend", "arize", stdin="1\n\n\n")
        assert result.returncode == 0, result.stdout + result.stderr
        assert BACKEND_MENU not in result.stdout
        assert "Found existing harnesses using Arize AX" in result.stdout
        assert _config(home)["harnesses"]["claude-code"]["space_id"] == "test-space"

    def test_non_interactive_settles_an_ambiguous_environment(self, tmp_path) -> None:
        home = _home(tmp_path)
        creds = _write_creds(home, ARIZE_CREDS + "PHOENIX_ENDPOINT=http://phoenix.test:6006\n")
        ambiguous = _cli(home, "setup", "claude", "-y", ARIZE_ENV_FILE=creds)
        assert ambiguous.returncode != 0
        assert "backend is ambiguous" in ambiguous.stderr

        result = _cli(home, "setup", "claude", "-y", "--backend", "phoenix", ARIZE_ENV_FILE=creds)
        assert result.returncode == 0, result.stdout + result.stderr
        entry = _config(home)["harnesses"]["claude-code"]
        assert (entry["target"], entry["endpoint"]) == ("phoenix", "http://phoenix.test:6006")

    @pytest.mark.parametrize("mode", [[], ["--non-interactive"]])
    def test_invalid_value_is_an_error_and_changes_nothing(self, mode, tmp_path) -> None:
        home = _home(tmp_path)
        before = _snapshot(home)
        result = _cli(home, "setup", "claude", "--backend", "datadog", *mode, stdin=INTERACTIVE_ARIZE)
        assert result.returncode == 2
        assert "invalid choice: 'datadog'" in result.stderr
        assert _snapshot(home) == before

    @pytest.mark.parametrize("mode, stdin", [([], "\n"), (["-y"], "")])
    def test_already_configured_keeps_the_saved_backend(self, mode, stdin, tmp_path) -> None:
        home = _home(tmp_path)
        creds = _write_creds(home, "PHOENIX_ENDPOINT=http://phoenix.test:6006\n")
        first = _cli(home, "setup", "claude", "-y", ARIZE_ENV_FILE=creds)
        assert first.returncode == 0, first.stdout + first.stderr

        result = _cli(home, "setup", "claude", "--backend", "arize", *mode, stdin=stdin)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "claude-code is already configured for phoenix; keeping it and ignoring --backend arize" in result.stdout
        assert BACKEND_MENU not in result.stdout
        assert _config(home)["harnesses"]["claude-code"]["target"] == "phoenix"


class TestPromptBackend:
    """``prompt_backend`` skips the menu whenever ARIZE_BACKEND is set, interactive or not."""

    @pytest.fixture(autouse=True)
    def _no_dotenv(self):
        from core import setup

        setup._reset_dotenv_cache()
        yield
        setup._reset_dotenv_cache()

    def test_interactive_with_backend_asks_for_credentials_but_not_the_backend(self, monkeypatch, capsys) -> None:
        from core import setup

        monkeypatch.setenv("ARIZE_BACKEND", "phoenix")
        answers = iter(["http://phoenix.test:6006"])
        monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
        monkeypatch.setattr(setup, "getpass", lambda prompt="": "phx-key")
        assert setup.prompt_backend() == ("phoenix", {"endpoint": "http://phoenix.test:6006", "api_key": "phx-key"})
        assert BACKEND_MENU not in capsys.readouterr().out

    def test_interactive_without_backend_shows_the_menu(self, monkeypatch, capsys) -> None:
        from core import setup

        answers = iter(["1", ""])
        monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
        monkeypatch.setattr(setup, "getpass", lambda prompt="": "")
        assert setup.prompt_backend()[0] == "phoenix"
        assert BACKEND_MENU in capsys.readouterr().out

    def test_interactive_with_invalid_backend_exits(self, monkeypatch) -> None:
        from core import setup

        monkeypatch.setenv("ARIZE_BACKEND", "datadog")
        monkeypatch.setattr("builtins.input", lambda prompt="": pytest.fail("prompted"))
        with pytest.raises(SystemExit) as exc:
            setup.prompt_backend()
        assert exc.value.code == 1


# ---------------------------------------------------------------------------
# setup: --with-skills, the harness menu, and argument errors (in-process)
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_installers(monkeypatch):
    """Replace every installer with a recorder; returns {key: [calls]}.

    claude-code takes with_skills, gemini takes nothing, kiro also takes
    agent_name, like the real ones.
    """
    calls: Dict[str, list] = {}
    installers = {}

    def module_for(key: str, signature: str) -> types.ModuleType:
        module = types.ModuleType(f"fake_install_{key.replace('-', '_')}")
        namespace: dict = {"calls": calls.setdefault(key, []), "key": key}
        exec(
            f"def install({signature}):\n    calls.append(('install', dict(locals())))\n"
            "def uninstall():\n    calls.append(('uninstall', {}))\n",
            namespace,
        )
        module.install = namespace["install"]  # type: ignore[attr-defined]
        module.uninstall = namespace["uninstall"]  # type: ignore[attr-defined]
        return module

    for key in install_table.INSTALLERS:
        signature = "with_skills=False"
        if key in ("gemini", "copilot", "antigravity"):
            signature = ""
        elif key == "kiro":
            signature = "with_skills=False, agent_name=None"
        module = module_for(key, signature)
        monkeypatch.setitem(sys.modules, module.__name__, module)
        installers[key] = install_table.Installer(module.__name__, None)
    monkeypatch.setattr(install_table, "INSTALLERS", installers)
    monkeypatch.setattr(commands, "migrate_yaml_config", lambda: False)
    monkeypatch.setattr(commands, "load_config", lambda: {})
    return calls


class TestWithSkills:
    def test_reaches_an_installer_that_takes_it(self, fake_installers) -> None:
        assert cli.main(["setup", "claude", "--with-skills", "-y"]) == 0
        assert fake_installers["claude-code"] == [("install", {"with_skills": True})]

    def test_warns_and_completes_for_an_installer_that_does_not(self, fake_installers, capsys) -> None:
        assert cli.main(["setup", "gemini", "--with-skills", "-y"]) == 0
        assert fake_installers["gemini"] == [("install", {})]
        out = capsys.readouterr().out
        assert "gemini has no skills to install; ignoring --with-skills" in out
        assert "Setup complete!" in out

    def test_kiro_never_gets_an_agent_name(self, fake_installers) -> None:
        assert cli.main(["setup", "kiro", "--with-skills", "-y"]) == 0
        assert fake_installers["kiro"] == [("install", {"with_skills": True, "agent_name": None})]

    def test_without_the_flag_installers_get_their_defaults(self, fake_installers) -> None:
        assert cli.main(["setup", "claude", "-y"]) == 0
        assert fake_installers["claude-code"] == [("install", {"with_skills": False})]

    def test_real_installers_accepting_with_skills(self) -> None:
        takes = {
            key
            for key in install_table.INSTALLERS
            if "with_skills" in inspect.signature(install_table.load(key).install).parameters
        }
        assert takes == set(install_table.INSTALLERS) - {"antigravity", "copilot", "gemini"}


class TestSetupMenu:
    @pytest.fixture
    def detected(self, monkeypatch):
        monkeypatch.setattr(
            commands, "is_harness_installed", lambda home_subdir=None, bin_name=None: home_subdir == ".claude"
        )

    def _answer(self, monkeypatch, text: Optional[str]) -> None:
        def fake_input(prompt=""):
            print(prompt, end="")
            if text is None:
                raise EOFError
            return text

        monkeypatch.setattr("builtins.input", fake_input)

    def test_lists_every_harness_with_its_detection_mark(self, fake_installers, detected, monkeypatch, capsys):
        monkeypatch.setattr(
            install_table,
            "detection_values",
            lambda key: None if key in ("antigravity", "copilot", "gemini") else (f".{key}", key),
        )
        monkeypatch.setattr(
            commands, "is_harness_installed", lambda home_subdir=None, bin_name=None: home_subdir == ".claude-code"
        )
        self._answer(monkeypatch, "codex")
        assert cli.main(["setup"]) == 0
        lines = {line.split()[0]: line for line in capsys.readouterr().out.splitlines() if line.startswith("  ")}
        assert set(lines) == set(HARNESSES)
        assert lines["claude-code"].endswith(" detected") and "(claude)" in lines["claude-code"]
        assert lines["codex"].endswith(" not detected")
        for key in ("antigravity", "copilot", "gemini"):
            assert lines[key].endswith("not detected automatically")

    def test_sets_up_each_comma_separated_name_in_order(self, fake_installers, detected, monkeypatch) -> None:
        order: List[str] = []
        monkeypatch.setattr(commands, "_setup_one", lambda key, with_skills, backend: order.append(key))
        self._answer(monkeypatch, " codex, claude ,cursor")
        assert cli.main(["setup"]) == 0
        assert order == ["codex", "claude-code", "cursor"]

    @pytest.mark.parametrize("answer", ["codex,nope", "", None])
    def test_bad_answer_is_a_usage_error_and_sets_up_nothing(self, answer, fake_installers, detected, monkeypatch):
        self._answer(monkeypatch, answer)
        with pytest.raises(SystemExit) as exc:
            cli.main(["setup"])
        assert exc.value.code == 2
        assert all(not calls for calls in fake_installers.values())

    def test_non_interactive_without_a_name_is_an_error(self, fake_installers, monkeypatch, capsys) -> None:
        monkeypatch.setattr("builtins.input", lambda prompt="": pytest.fail("prompted"))
        with pytest.raises(SystemExit) as exc:
            cli.main(["setup", "--non-interactive"])
        assert exc.value.code == 2
        assert "needs a harness name" in capsys.readouterr().err

    @pytest.mark.parametrize("args", [["setup", "nope"], ["uninstall", "nope"], ["setup", "claude", "extra"]])
    def test_unknown_harness_exits_2(self, args, fake_installers, capsys) -> None:
        with pytest.raises(SystemExit) as exc:
            cli.main(args)
        assert exc.value.code == 2
        assert all(not calls for calls in fake_installers.values())


# ---------------------------------------------------------------------------
# uninstall
# ---------------------------------------------------------------------------


@pytest.fixture
def install_state(monkeypatch):
    """Control the install type seen by uninstall/update; defaults to a POSIX ~/.arize/harness/venv install."""
    state = types.SimpleNamespace(binary=False, venv=True, windows=False)
    monkeypatch.setattr(commands, "is_binary", lambda: state.binary)
    monkeypatch.setattr(commands, "running_from_install_venv", lambda: state.venv)
    monkeypatch.setattr(commands, "on_windows", lambda: state.windows)
    return state


class TestUninstall:
    @pytest.fixture
    def wipe(self, monkeypatch, fake_installers):
        # Never import the real core.setup.wipe here: it binds INSTALL_DIR at
        # import, so importing it in this process before a test that patches
        # core.setup.INSTALL_DIR would point that test's wipe at the real HOME.
        wiped: List[bool] = []
        fake = types.ModuleType("core.setup.wipe")
        fake.wipe_shared_runtime = lambda: wiped.append(True)  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "core.setup.wipe", fake)
        monkeypatch.setattr(commands, "list_installed_harnesses", lambda: ["claude-code", "codex", "bogus"])
        return wiped

    def test_no_harness_and_no_all_refuses(self, wipe, fake_installers, install_state, capsys) -> None:
        assert cli.main(["uninstall"]) != 0
        assert "Nothing was changed" in capsys.readouterr().err
        assert not wipe and all(not calls for calls in fake_installers.values())

    def test_harness_and_all_together_is_an_error(self, wipe, fake_installers, install_state) -> None:
        with pytest.raises(SystemExit) as exc:
            cli.main(["uninstall", "claude", "--all"])
        assert exc.value.code == 2
        assert not wipe

    @pytest.mark.parametrize(
        "binary, venv, windows", [(False, True, False), (True, False, True), (False, False, True), (False, True, True)]
    )
    def test_one_harness_works_on_every_install_type(
        self, binary, venv, windows, wipe, fake_installers, install_state
    ) -> None:
        install_state.binary, install_state.venv, install_state.windows = binary, venv, windows
        assert cli.main(["uninstall", "claude"]) == 0
        assert fake_installers["claude-code"] == [("uninstall", {})]
        assert not wipe

    def test_all_uninstalls_each_harness_then_wipes(self, wipe, fake_installers, install_state, capsys) -> None:
        assert cli.main(["uninstall", "--all"]) == 0
        assert fake_installers["claude-code"] == [("uninstall", {})]
        assert fake_installers["codex"] == [("uninstall", {})]
        assert wipe == [True]
        assert "Unknown harness: bogus (skipping)" in capsys.readouterr().out

    def test_all_continues_past_a_failing_harness(self, wipe, fake_installers, install_state, monkeypatch) -> None:
        def boom():
            raise SystemExit(1)

        monkeypatch.setattr(install_table.load("claude-code"), "uninstall", boom)
        assert cli.main(["uninstall", "--all"]) == 0
        assert fake_installers["codex"] == [("uninstall", {})]
        assert wipe == [True]

    @pytest.mark.parametrize("venv, windows", [(True, False), (False, False), (True, True)])
    def test_all_on_a_binary_install_refuses(self, venv, windows, wipe, fake_installers, install_state, capsys):
        install_state.binary, install_state.venv, install_state.windows = True, venv, windows
        assert cli.main(["uninstall", "--all"]) == 1
        err = capsys.readouterr().err
        assert "not supported on a binary install" in err and len(err.strip().splitlines()) == 1
        assert not wipe and all(not calls for calls in fake_installers.values())

    def test_all_in_the_windows_venv_refuses(self, wipe, fake_installers, install_state, capsys) -> None:
        install_state.windows = True
        assert cli.main(["uninstall", "--all"]) == 1
        err = capsys.readouterr().err
        assert "install.bat uninstall" in err and len(err.strip().splitlines()) == 1
        assert not wipe and all(not calls for calls in fake_installers.values())

    @pytest.mark.parametrize("windows", [False, True])
    def test_all_from_another_interpreter_runs(self, windows, wipe, fake_installers, install_state) -> None:
        install_state.venv, install_state.windows = False, windows
        assert cli.main(["uninstall", "--all"]) == 0
        assert wipe == [True]


@POSIX_ONLY
class TestUninstallMatchesInstallSh:
    def _setup_both(self, tmp_path):
        homes = []
        for name in ("legacy", "new"):
            home = _home(tmp_path, name)
            creds = _write_creds(home)
            for harness in ("claude", "codex"):
                result = _cli(home, "setup", harness, "-y", ARIZE_ENV_FILE=creds)
                assert result.returncode == 0, result.stdout + result.stderr
            _fake_venv(home)
            homes.append(home)
        assert _snapshot(homes[0]) == _snapshot(homes[1])
        return homes

    def test_all_matches_install_sh_uninstall(self, tmp_path) -> None:
        legacy, new = self._setup_both(tmp_path)

        old = _run(legacy, ["bash", str(INSTALL_SH), "uninstall"])
        assert old.returncode == 0, old.stdout + old.stderr
        # This interpreter is not ~/.arize/harness/venv: the "other interpreter" case.
        result = _cli(new, "uninstall", "--all")
        assert result.returncode == 0, result.stdout + result.stderr

        assert not (new / ".arize" / "harness").exists()
        assert _snapshot(new) == _snapshot(legacy)
        # The running package is untouched.
        assert Path(sys.executable).exists()
        assert (REPO_ROOT / "core" / "cli.py").is_file()

    def test_no_arguments_changes_nothing(self, tmp_path) -> None:
        _, home = self._setup_both(tmp_path)
        before = _snapshot(home)
        result = _cli(home, "uninstall")
        assert result.returncode != 0
        assert _snapshot(home) == before


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


@POSIX_ONLY
class TestStatusMatchesInstallSh:
    @pytest.mark.parametrize("state, code", [("empty", 1), ("healthy", 0), ("hooks-missing", 2)])
    @pytest.mark.parametrize("flags", [[], ["--json"]])
    def test_output_and_exit_code(self, state, code, flags, tmp_path) -> None:
        home = _home(tmp_path)
        _fake_venv(home)
        if state != "empty":
            result = _cli(home, "setup", "claude", "-y", ARIZE_ENV_FILE=_write_creds(home))
            assert result.returncode == 0, result.stdout + result.stderr
        if state == "hooks-missing":
            (home / ".claude" / "settings.json").unlink()

        old = _run(home, ["bash", str(INSTALL_SH), "status", *flags])
        new = _cli(home, "status", *flags)
        assert (new.returncode, new.stdout, new.stderr) == (old.returncode, old.stdout, old.stderr)
        assert new.returncode == code

    def test_works_without_the_venv(self, tmp_path) -> None:
        home = _home(tmp_path)
        result = _cli(home, "status", "--json")
        assert result.returncode == 1
        assert json.loads(result.stdout)["harnesses"] == []


# ---------------------------------------------------------------------------
# --version
# ---------------------------------------------------------------------------


class TestVersion:
    def test_prints_the_installed_version(self, capsys) -> None:
        from importlib.metadata import version

        assert cli.main(["--version"]) == 0
        assert capsys.readouterr().out == f"arize-harness {version('coding-harness-tracing')}\n"

    def test_falls_back_without_metadata(self, monkeypatch, capsys) -> None:
        import importlib.metadata

        def missing(name):
            raise importlib.metadata.PackageNotFoundError(name)

        monkeypatch.setattr(importlib.metadata, "version", missing)
        assert cli.main(["--version"]) == 0
        assert capsys.readouterr().out == "arize-harness 0.0.0\n"

    def test_does_not_import_core_setup(self, tmp_path) -> None:
        code = "import sys; from core import cli; cli.main(['--version']); print('core.setup' in sys.modules)"
        result = _run(_home(tmp_path), [sys.executable, "-c", code])
        assert result.returncode == 0, result.stderr
        assert result.stdout.splitlines()[-1] == "False"


# ---------------------------------------------------------------------------
# is_binary
# ---------------------------------------------------------------------------


class TestIsBinary:
    def test_false_under_an_interpreter(self) -> None:
        assert runtime.is_binary() is False

    def test_true_when_nuitka_defines_compiled(self, monkeypatch) -> None:
        monkeypatch.setattr(runtime, "__compiled__", types.SimpleNamespace(), raising=False)
        assert runtime.is_binary() is True

    def test_commands_use_the_runtime_check(self) -> None:
        assert commands.is_binary is runtime.is_binary


# ---------------------------------------------------------------------------
# update
# ---------------------------------------------------------------------------


class _Calls(list):
    """Commands update ran, in order; a command fails when one of its words is in ``fail``."""

    def __init__(self) -> None:
        super().__init__()
        self.fail: List[str] = []


class TestUpdate:
    @pytest.fixture
    def calls(self, monkeypatch, install_state, tmp_path) -> _Calls:
        recorded = _Calls()

        def fake_run(cmd):
            recorded.append(cmd)
            return not any(word in cmd for word in recorded.fail)

        monkeypatch.setattr(commands, "_run", fake_run)
        monkeypatch.setattr(commands, "INSTALL_DIR", tmp_path / "harness")
        monkeypatch.setattr(commands, "list_installed_harnesses", lambda: ["claude-code", "codex", "cursor"])
        (tmp_path / "harness").mkdir()
        return recorded

    def _only_err_line(self, capsys) -> str:
        err = capsys.readouterr().err.strip().splitlines()
        assert len(err) == 1, err
        return err[0]

    @pytest.mark.parametrize("venv, windows", [(True, False), (False, False), (False, True), (True, True)])
    def test_binary_refuses_first(self, venv, windows, calls, install_state, capsys) -> None:
        install_state.binary, install_state.venv, install_state.windows = True, venv, windows
        assert cli.main(["update"]) == 1
        assert "not supported on a binary install" in self._only_err_line(capsys)
        assert calls == []

    @pytest.mark.parametrize("windows", [False, True])
    def test_other_interpreter_says_how_to_update_it(self, windows, calls, install_state, capsys) -> None:
        install_state.venv, install_state.windows = False, windows
        assert cli.main(["update"]) == 1
        line = self._only_err_line(capsys)
        # The test suite runs from this checkout.
        assert f"source checkout at {REPO_ROOT.resolve()}" in line
        assert "`git pull` and `uv sync`" in line
        assert calls == []

    @pytest.mark.parametrize(
        "prefix, expected",
        [
            ("/home/u/.local/share/uv/tools/coding-harness-tracing", ["uv tool upgrade coding-harness-tracing"]),
            (
                "/opt/other-venv",
                [
                    "re-run the installer that created it",
                    "-m pip install -U --no-index --find-links <dir> coding-harness-tracing",
                ],
            ),
        ],
    )
    def test_other_interpreter_hints(self, prefix, expected, monkeypatch, tmp_path) -> None:
        import core

        monkeypatch.setattr(commands.sys, "prefix", prefix)
        monkeypatch.setattr(core, "__file__", str(tmp_path / "site-packages" / "core" / "__init__.py"))
        hint = commands._other_install_update_hint()
        assert all(part in hint for part in expected)
        # Nothing is on an index yet, so no hint may send pip to one.
        assert "--no-index" in hint or "pip install" not in hint

    def test_windows_venv_points_to_install_bat(self, calls, install_state, capsys) -> None:
        install_state.windows = True
        assert cli.main(["update"]) == 1
        assert "install.bat update" in self._only_err_line(capsys)
        assert calls == []

    def test_wheel_dir_installs_from_it_only(self, calls, tmp_path) -> None:
        wheels = tmp_path / "wheels"
        wheels.mkdir()
        (wheels / "coding_harness_tracing-9.9.9-py3-none-any.whl").write_text("")
        # A git checkout too: --wheel-dir wins, and nothing is pulled.
        (tmp_path / "harness" / ".git").mkdir()
        assert cli.main(["update", "--wheel-dir", str(wheels)]) == 0
        pip = calls[0]
        assert pip[:4] == [sys.executable, "-m", "pip", "install"]
        assert "--no-index" in pip and pip[pip.index("--find-links") + 1] == str(wheels.resolve())
        assert not any("--index-url" in arg or "--extra-index-url" in arg for cmd in calls for arg in cmd)
        assert not any(cmd[0] == "git" for cmd in calls)

    @pytest.mark.parametrize("make", ["missing", "no-wheel"])
    def test_wheel_dir_must_hold_a_wheel(self, make, calls, tmp_path, capsys) -> None:
        wheels = tmp_path / "wheels"
        if make == "no-wheel":
            wheels.mkdir()
        assert cli.main(["update", "--wheel-dir", str(wheels)]) == 1
        assert calls == []

    def test_git_checkout_pulls_then_reinstalls(self, calls, tmp_path) -> None:
        install_dir = tmp_path / "harness"
        (install_dir / ".git").mkdir()
        assert cli.main(["update"]) == 0
        assert calls[0] == ["git", "-C", str(install_dir), "pull", "--ff-only"]
        assert calls[1][:4] == [sys.executable, "-m", "pip", "install"] and calls[1][-1] == str(install_dir)

    def test_failed_pull_says_run_install_sh(self, calls, tmp_path, capsys) -> None:
        (tmp_path / "harness" / ".git").mkdir()
        calls.fail.append("pull")
        assert cli.main(["update"]) == 1
        assert "run `install.sh update`" in capsys.readouterr().err
        assert len(calls) == 1

    def test_tarball_install_says_run_install_sh_and_changes_nothing(self, calls, tmp_path, capsys) -> None:
        install_dir = tmp_path / "harness"
        (install_dir / "pyproject.toml").write_text("[project]\n")
        before = sorted(p.name for p in install_dir.iterdir())
        assert cli.main(["update"]) == 1
        assert "run `install.sh update`" in self._only_err_line(capsys)
        assert calls == []
        assert sorted(p.name for p in install_dir.iterdir()) == before

    def test_no_source_refuses(self, calls, capsys) -> None:
        assert cli.main(["update"]) == 1
        err = capsys.readouterr().err
        assert "no source tree to update" in err and "--wheel-dir" in err
        assert calls == []

    def test_failed_reinstall_stops(self, calls, tmp_path) -> None:
        (tmp_path / "harness" / ".git").mkdir()
        calls.fail.append("pip")
        assert cli.main(["update"]) == 1
        assert len(calls) == 2

    def test_migrates_then_sets_up_every_harness_past_failures(self, calls, tmp_path, monkeypatch, capsys) -> None:
        (tmp_path / "harness" / ".git").mkdir()
        monkeypatch.setattr(commands, "list_installed_harnesses", lambda: ["claude-code", "bogus", "codex", "cursor"])
        calls.fail.append("codex")
        assert cli.main(["update"]) == 0
        after_pip = calls[2:]
        assert after_pip[0] == [sys.executable, "-I", "-m", "core.config", "migrate"]
        assert [cmd[-1] for cmd in after_pip[1:]] == ["claude-code", "codex", "cursor"]
        assert all(cmd[:5] == [sys.executable, "-I", "-m", "core.cli", "setup"] for cmd in after_pip[1:])
        out = capsys.readouterr().out
        assert "codex re-registration failed (continuing)" in out
        assert "Unknown harness: bogus (skipping)" in out

    @pytest.mark.parametrize("tty", [False, True])
    def test_non_interactive_only_without_a_terminal(self, tty, calls, tmp_path, monkeypatch) -> None:
        (tmp_path / "harness" / ".git").mkdir()
        monkeypatch.setattr(commands.sys.stdin, "isatty", lambda: tty, raising=False)
        monkeypatch.delenv("ARIZE_NONINTERACTIVE", raising=False)
        assert cli.main(["update"]) == 0
        assert (os.environ.get("ARIZE_NONINTERACTIVE") == "1") is (not tty)


# ---------------------------------------------------------------------------
# hook is unchanged
# ---------------------------------------------------------------------------


class TestHookUnaffected:
    @pytest.fixture(autouse=True)
    def hook_log(self, monkeypatch, tmp_path) -> Path:
        log = tmp_path / "hook.log"
        monkeypatch.setenv("ARIZE_LOG_FILE", str(log))
        return log

    @pytest.mark.parametrize(
        "args",
        [
            ["hook", "setup"],
            ["hook", "--version"],
            ["hook", "--help"],
            ["hook", "status", "--json"],
            ["hook", "claude", "setup"],
            ["claude", "stop"],
            ["nope"],
            [],
        ],
    )
    def test_never_exits_2(self, args, capsys) -> None:
        assert cli.main(args) == 0

    def test_hook_path_never_imports_the_commands(self, tmp_path) -> None:
        code = (
            "import sys; from core import cli; cli.main(['hook', '--list']);"
            "print(sorted(m for m in ('argparse', 'core.setup', 'core.setup.commands', 'core.install_table', 'dotenv')"
            " if m in sys.modules))"
        )
        result = _run(_home(tmp_path), [sys.executable, "-c", code])
        assert result.returncode == 0, result.stderr
        assert result.stdout.splitlines()[-1] == "[]"
