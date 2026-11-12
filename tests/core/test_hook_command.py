"""Tests for registering, recognising and removing ``arize-harness hook`` commands."""

from __future__ import annotations

import json
import os
import sys
import tomllib
from pathlib import Path
from typing import Callable, Dict, Iterator, List

import pytest

import core
import core.setup as setup_mod
from core import cli, install_table
from core.setup import (
    command_words,
    commands,
    filter_our_hooks,
    hook_command_for,
    is_arize_hook_command,
    is_old_venv_hook,
    is_our_hook_command,
    registered_command_path,
    runner,
)
from tests.core.test_cli_commands import _home, _write_creds

POSIX_ONLY = pytest.mark.skipif(os.name == "nt", reason="runs the installers against POSIX config paths")


@pytest.fixture
def venv(tmp_path, monkeypatch) -> Path:
    """Point ``core.setup.VENV_DIR`` at a throwaway old venv."""
    path = tmp_path / "home" / ".arize" / "harness" / "venv"
    monkeypatch.setattr(setup_mod, "VENV_DIR", path)
    return path


@pytest.fixture
def exe(tmp_path, monkeypatch) -> Path:
    """Fake the running ``arize-harness`` path."""
    path = tmp_path / "local bin" / "arize-harness"
    path.parent.mkdir(parents=True)
    path.write_text("")
    monkeypatch.setattr(runner, "arize_harness_path", lambda: path)
    return path


# ---------------------------------------------------------------------------
# Matchers
# ---------------------------------------------------------------------------


class TestOldVenvHook:
    def test_native_posix(self, venv):
        assert is_old_venv_hook(str(venv / "bin" / "arize-hook-stop"))

    def test_shell_quoted(self, venv):
        assert is_old_venv_hook(f"'{(venv / 'bin' / 'arize-hook-stop').as_posix()}'")

    def test_windows_native_and_forward_slashes(self, venv):
        native = str(venv).replace("/", "\\") + "\\Scripts\\arize-hook-gemini-before-tool.exe"
        assert is_old_venv_hook(native)
        assert is_old_venv_hook(f"'{(venv / 'Scripts' / 'arize-hook-cursor.exe').as_posix()}'")

    def test_one_word_argv(self, venv):
        assert is_old_venv_hook([str(venv / "bin" / "arize-hook-codex-notify")])

    @pytest.mark.parametrize(
        "make",
        [
            lambda v: str(v / "bin" / "something-else"),
            lambda v: str(v.parent.parent / "harness-old" / "venv" / "bin" / "arize-hook-stop"),
            lambda v: str(v / "bin" / "arize-hook-stop") + " --extra",
            lambda v: "/usr/local/bin/arize-hook-stop",
            lambda v: [str(v / "bin" / "arize-hook-codex-notify"), "x"],
            lambda v: 42,
        ],
    )
    def test_not_ours(self, venv, make):
        assert not is_old_venv_hook(make(venv))


class TestArizeHookCommand:
    def test_hook_command_round_trips(self, exe):
        cmd = hook_command_for("claude-code", "arize-hook-stop")
        assert is_arize_hook_command(cmd, "claude-code") and is_arize_hook_command(cmd, "claude")
        assert not is_arize_hook_command(cmd, "cursor")
        assert registered_command_path(cmd) == exe.as_posix()

    def test_any_arize_harness_path_counts(self):
        assert is_arize_hook_command("'/elsewhere/arize-harness' hook cursor", "cursor")
        assert is_arize_hook_command("'C:/u/.local/bin/arize-harness.exe' hook kiro", "kiro")

    def test_argv_list(self):
        argv = ["/x/arize-harness", "hook", "codex", "notify"]
        assert is_arize_hook_command(argv, "codex")
        assert registered_command_path(argv) == "/x/arize-harness"

    def test_powershell_and_cmd_forms(self):
        ps = "& 'C:/x/arize-harness.exe' hook gemini before-tool"
        assert is_arize_hook_command(ps, "gemini") and registered_command_path(ps) == "C:/x/arize-harness.exe"
        cmd = '"C:/x/arize-harness.exe" hook kiro'
        assert is_arize_hook_command(cmd, "kiro") and registered_command_path(cmd) == "C:/x/arize-harness.exe"

    @pytest.mark.parametrize(
        "command",
        ["echo hook claude stop", "'/x/arize-harness' status", "'/x/not-arize-harness' hook claude stop", "'unclosed"],
    )
    def test_not_ours(self, command):
        assert not is_arize_hook_command(command)
        assert registered_command_path(command) is None

    def test_command_words(self):
        assert command_words("'/a b/arize-harness' hook x") == ["/a b/arize-harness", "hook", "x"]
        assert command_words(["a", 1]) == []

    def test_command_words_powershell_form(self):
        """PowerShell's ``''`` is a literal ``'``."""
        ps = "& '/home/o''brien/.local/bin/arize-harness' hook copilot stop"
        assert command_words(ps) == ["&", "/home/o'brien/.local/bin/arize-harness", "hook", "copilot", "stop"]
        assert registered_command_path(ps) == "/home/o'brien/.local/bin/arize-harness"
        assert command_words("& 'C:/x/arize-harness.exe' hook gemini") == [
            "&",
            "C:/x/arize-harness.exe",
            "hook",
            "gemini",
        ]
        assert command_words("& 'unclosed hook x") == []


class TestFilterOurHooks:
    def test_keeps_others_and_one_current(self, venv, exe):
        current = hook_command_for("cursor", "arize-hook-cursor")
        entries = [
            {"command": str(venv / "bin" / "arize-hook-cursor")},
            {"command": "echo user"},
            {"command": "'/old/arize-harness' hook cursor"},
            {"command": current},
            {"command": current},
            {"command": "'/x/arize-harness' hook kiro"},  # another harness's: not ours here
            "not-a-dict",
        ]
        assert filter_our_hooks(entries, "cursor", keep=current) == [
            {"command": "echo user"},
            {"command": current},
            {"command": "'/x/arize-harness' hook kiro"},
            "not-a-dict",
        ]
        assert filter_our_hooks(entries, "cursor") == [
            {"command": "echo user"},
            {"command": "'/x/arize-harness' hook kiro"},
            "not-a-dict",
        ]


# ---------------------------------------------------------------------------
# Windows quoting per shell
# ---------------------------------------------------------------------------


class TestWindowsShells:
    @pytest.fixture
    def windows_exe(self, monkeypatch):
        path = Path("C:/Users/O'Neil/.local/bin/arize-harness.exe")
        monkeypatch.setattr(runner, "arize_harness_path", lambda: path)
        monkeypatch.setattr(setup_mod, "_windows", lambda: True)
        return path

    def test_posix_shell(self, windows_exe):
        cmd = hook_command_for("claude-code", "arize-hook-stop")
        assert cmd == "'C:/Users/O'\"'\"'Neil/.local/bin/arize-harness.exe' hook claude stop"

    def test_powershell_uses_the_call_operator(self, windows_exe):
        cmd = hook_command_for("gemini", "arize-hook-gemini-before-tool", shell="powershell")
        assert cmd == "& 'C:/Users/O''Neil/.local/bin/arize-harness.exe' hook gemini before-tool"
        assert is_our_hook_command(cmd, "gemini")

    def test_cmd_uses_double_quotes(self, windows_exe):
        cmd = hook_command_for("kiro", "arize-hook-kiro", shell="cmd")
        assert cmd == '"C:/Users/O\'Neil/.local/bin/arize-harness.exe" hook kiro'
        assert is_our_hook_command(cmd, "kiro")

    def test_powershell_leaves_a_safe_path_unquoted(self, monkeypatch):
        path = Path("C:/Users/u/.local/bin/arize-harness.exe")
        monkeypatch.setattr(runner, "arize_harness_path", lambda: path)
        monkeypatch.setattr(setup_mod, "_windows", lambda: True)
        cmd = hook_command_for("gemini", "arize-hook-gemini-after-tool", shell="powershell")
        assert cmd == "C:/Users/u/.local/bin/arize-harness.exe hook gemini after-tool"
        assert is_our_hook_command(cmd, "gemini") and registered_command_path(cmd) == path.as_posix()

    def test_posix_everywhere_off_windows(self, exe, monkeypatch):
        monkeypatch.setattr(setup_mod, "_windows", lambda: False)
        for shell in ("posix", "powershell", "cmd"):
            assert hook_command_for("kiro", "arize-hook-kiro", shell=shell) == runner.hook_command("kiro")

    def test_unknown_shell_or_entry_point(self, exe):
        with pytest.raises(ValueError):
            hook_command_for("kiro", "arize-hook-kiro", shell="fish")
        with pytest.raises(ValueError):
            hook_command_for("kiro", "arize-hook-nope")


class TestCommandFields:
    """Copilot's per-OS command fields."""

    @pytest.mark.parametrize("windows_host", [False, True])
    def test_spaced_path(self, monkeypatch, windows_host):
        path = Path("C:/Users/Test User/.local/bin/arize-harness.exe")
        monkeypatch.setattr(runner, "arize_harness_path", lambda: path)
        monkeypatch.setattr(setup_mod, "_windows", lambda: windows_host)
        fields = setup_mod.hook_command_fields("copilot", "arize-hook-copilot-stop")
        posix = "'C:/Users/Test User/.local/bin/arize-harness.exe' hook copilot stop"
        powershell = "& 'C:/Users/Test User/.local/bin/arize-harness.exe' hook copilot stop"
        assert fields == {"command": posix, "bash": posix, "windows": powershell, "powershell": powershell}

    def test_safe_path(self, monkeypatch):
        path = Path("C:/Users/u/.local/bin/arize-harness.exe")
        monkeypatch.setattr(runner, "arize_harness_path", lambda: path)
        fields = setup_mod.hook_command_fields("copilot", "arize-hook-copilot-pre-tool")
        assert fields["bash"] == fields["command"] == "'C:/Users/u/.local/bin/arize-harness.exe' hook copilot pre-tool"
        assert (
            fields["windows"] == fields["powershell"] == "C:/Users/u/.local/bin/arize-harness.exe hook copilot pre-tool"
        )

    def test_quote_in_path(self, monkeypatch):
        path = Path("/home/o'neil/.local/bin/arize-harness")
        monkeypatch.setattr(runner, "arize_harness_path", lambda: path)
        fields = setup_mod.hook_command_fields("copilot", "arize-hook-copilot-stop")
        assert fields["bash"] == "'/home/o'\"'\"'neil/.local/bin/arize-harness' hook copilot stop"
        assert fields["powershell"] == "& '/home/o''neil/.local/bin/arize-harness' hook copilot stop"

    def test_every_field_is_recognised_as_ours(self, monkeypatch):
        path = Path("C:/Users/Test User/.local/bin/arize-harness.exe")
        monkeypatch.setattr(runner, "arize_harness_path", lambda: path)
        for value in setup_mod.hook_command_fields("copilot", "arize-hook-copilot-stop").values():
            assert is_our_hook_command(value, "copilot")
            assert registered_command_path(value) == path.as_posix()


# ---------------------------------------------------------------------------
# Setup refusal
# ---------------------------------------------------------------------------


@pytest.fixture
def real_gate(monkeypatch, tmp_path):
    """Restore the real ``require_setup_writes`` and record installer calls."""
    monkeypatch.setattr(commands, "require_setup_writes", runner.require_setup_writes)
    monkeypatch.setattr(runner, "_temp_roots", lambda: [tmp_path / "tmp"])
    calls: List[str] = []

    class Fake:
        @staticmethod
        def install():
            calls.append("install")

    monkeypatch.setattr(install_table, "load", lambda key: Fake)
    monkeypatch.setattr(commands, "_refresh_files", lambda: calls.append("refresh"))
    monkeypatch.setattr(commands, "migrate_yaml_config", lambda: calls.append("migrate"))
    return calls


def _use_prefix(monkeypatch, prefix: Path, marker: str = "") -> None:
    package = prefix / "lib" / "site-packages" / "core"
    package.mkdir(parents=True, exist_ok=True)
    if marker:
        (prefix / marker).write_text("")
    monkeypatch.setattr(sys, "prefix", str(prefix))
    monkeypatch.setattr(core, "__file__", str(package / "__init__.py"))


class TestSetupGate:
    def test_uvx_is_refused_before_anything(self, real_gate, monkeypatch, tmp_path, capsys):
        _use_prefix(monkeypatch, tmp_path / "cache" / "uv" / "archive-v0" / "abc")
        assert cli.main(["setup", "claude", "-y"]) == 1
        assert real_gate == []
        err = capsys.readouterr().err
        assert "uv tool install coding-harness-tracing" in err and "Nothing was written" in err

    def test_old_venv_on_posix_is_refused(self, real_gate, monkeypatch, venv, capsys):
        monkeypatch.setattr(runner, "_is_windows", lambda: False)
        monkeypatch.setattr(runner, "_old_venv_dir", lambda: venv)
        _use_prefix(monkeypatch, venv)
        assert cli.main(["setup", "claude", "-y"]) == 1
        assert real_gate == []
        assert "old install.sh venv" in capsys.readouterr().err

    def test_dev_checkout_is_refused(self, real_gate, capsys):
        assert cli.main(["setup", "claude", "-y"]) == 1
        assert real_gate == []
        assert "source checkout" in capsys.readouterr().err

    def test_install_bat_venv_on_windows_sets_up(self, real_gate, monkeypatch, venv):
        monkeypatch.setattr(runner, "_is_windows", lambda: True)
        monkeypatch.setattr(runner, "_old_venv_dir", lambda: venv)
        _use_prefix(monkeypatch, venv)
        assert cli.main(["setup", "claude", "-y"]) == 0
        assert real_gate == ["migrate", "install", "refresh"]

    def test_uv_tool_sets_up(self, real_gate, monkeypatch, tmp_path):
        _use_prefix(monkeypatch, tmp_path / "tools" / "coding-harness-tracing", marker="uv-receipt.toml")
        assert cli.main(["setup", "claude", "-y"]) == 0
        assert "install" in real_gate

    def test_binary_registers_its_own_path(self, monkeypatch, tmp_path):
        binary = tmp_path / "opt" / "arize-harness"
        monkeypatch.setattr(runner, "is_binary", lambda: True)
        monkeypatch.setattr(sys, "argv", [str(binary), "setup"])
        cmd = hook_command_for("claude-code", "arize-hook-stop")
        assert registered_command_path(cmd) == binary.as_posix()

    def test_binary_in_temp_is_refused(self, real_gate, monkeypatch, tmp_path, capsys):
        monkeypatch.setattr(runner, "is_binary", lambda: True)
        monkeypatch.setattr(sys, "argv", [str(tmp_path / "tmp" / "arize-harness"), "setup"])
        assert cli.main(["setup", "claude", "-y"]) == 1
        assert real_gate == []
        assert "temporary directory" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Installers in a throwaway HOME
# ---------------------------------------------------------------------------

USER_CMD = "echo user-hook"


class Harness:
    """An installer's config files and the old-install state to seed them with."""

    def __init__(self, name: str, files: Callable[[Path], List[Path]], seed: Callable[[Path, Path], None]):
        self.name = name
        self.files = files
        self.seed = seed


def _old(home: Path, entry_point: str) -> str:
    return str(home / ".arize" / "harness" / "venv" / "bin" / entry_point)


def _write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")


def _seed_claude(home: Path, cwd: Path) -> None:
    _write(
        home / ".claude" / "settings.json",
        {
            "plugins": [
                {"type": "local", "path": str(home / ".arize" / "harness" / "tracing" / "claude_code")},
                "/user/plugin",
            ],
            "hooks": {
                "Stop": [
                    {
                        "hooks": [
                            {"type": "command", "command": f"'{_old(home, 'arize-hook-stop')}'"},
                            {"type": "command", "command": USER_CMD},
                        ]
                    }
                ],
                "SessionStart": [{"hooks": [{"type": "command", "command": _old(home, "arize-hook-session-start")}]}],
                "Retired": [{"hooks": [{"type": "command", "command": "'/old/arize-harness' hook claude retired"}]}],
            },
        },
    )


def _seed_codex(home: Path, cwd: Path) -> None:
    path = home / ".codex" / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'model = "gpt-5"\nnotify = ["{_old(home, "arize-hook-codex-notify")}"]\n')


def _seed_copilot(home: Path, cwd: Path) -> None:
    _write(
        cwd / ".github" / "hooks" / "hooks.json",
        {
            "hooks": {
                "Stop": [
                    {"type": "command", "command": _old(home, "arize-hook-copilot-stop")},
                    {"type": "command", "command": USER_CMD},
                ]
            }
        },
    )


def _seed_cursor(home: Path, cwd: Path) -> None:
    _write(
        home / ".cursor" / "hooks.json",
        {"version": 1, "hooks": {"stop": [{"command": _old(home, "arize-hook-cursor")}, {"command": USER_CMD}]}},
    )


def _seed_devin(home: Path, cwd: Path) -> None:
    _write(
        home / ".config" / "devin" / "config.json",
        {
            "theme": "dark",
            "hooks": {
                "Stop": [
                    {"hooks": [{"type": "command", "command": _old(home, "arize-hook-devin"), "timeout": 30}]},
                    {"hooks": [{"type": "command", "command": USER_CMD}]},
                ]
            },
        },
    )


def _seed_gemini(home: Path, cwd: Path) -> None:
    _write(
        home / ".gemini" / "settings.json",
        {
            "hooks": {
                "SessionStart": [
                    {
                        "matcher": "",
                        "hooks": [
                            {
                                "type": "command",
                                "name": "arize-tracing",
                                "command": _old(home, "arize-hook-gemini-session-start"),
                            }
                        ],
                    },
                    {"matcher": "", "hooks": [{"type": "command", "name": "user", "command": USER_CMD}]},
                ]
            }
        },
    )


def _seed_kiro(home: Path, cwd: Path) -> None:
    _write(
        home / ".kiro" / "agents" / "arize-traced.json",
        {
            "name": "arize-traced",
            "hooks": {"stop": [{"command": _old(home, "arize-hook-kiro")}, {"command": USER_CMD}]},
        },
    )


def _seed_antigravity(home: Path, cwd: Path) -> None:
    _write(
        home / ".gemini" / "config" / "hooks.json",
        {
            "arize-tracing": {"Stop": [{"type": "command", "command": _old(home, "arize-hook-antigravity-stop")}]},
            "user-hooks": {"Stop": [{"type": "command", "command": USER_CMD}]},
        },
    )


HARNESSES: Dict[str, Harness] = {
    h.name: h
    for h in (
        Harness("claude", lambda home, cwd: [home / ".claude" / "settings.json"], _seed_claude),
        Harness("codex", lambda home, cwd: [home / ".codex" / "config.toml"], _seed_codex),
        Harness("copilot", lambda home, cwd: [cwd / ".github" / "hooks" / "hooks.json"], _seed_copilot),
        Harness("cursor", lambda home, cwd: [home / ".cursor" / "hooks.json"], _seed_cursor),
        Harness("devin", lambda home, cwd: [home / ".config" / "devin" / "config.json"], _seed_devin),
        Harness("gemini", lambda home, cwd: [home / ".gemini" / "settings.json"], _seed_gemini),
        Harness("kiro", lambda home, cwd: [home / ".kiro" / "agents" / "arize-traced.json"], _seed_kiro),
        Harness("antigravity", lambda home, cwd: [home / ".gemini" / "config" / "hooks.json"], _seed_antigravity),
    )
}


def _values(data) -> Iterator[object]:
    if isinstance(data, str):
        yield data
    elif isinstance(data, list):
        if data and all(isinstance(x, str) for x in data):
            yield data
        for item in data:
            yield from _values(item)
    elif isinstance(data, dict):
        for item in data.values():
            yield from _values(item)


def _commands(path: Path) -> List[object]:
    if not path.exists():
        return []
    text = path.read_text()
    return list(_values(tomllib.loads(text) if path.suffix == ".toml" else json.loads(text)))


def _snapshot(*roots: Path) -> Dict[str, bytes]:
    out = {}
    for root in roots:
        for path in sorted(root.rglob("*")):
            if path.is_file() and not path.is_symlink() and "logs" not in path.parts:
                out[str(path)] = path.read_bytes()
    return out


@pytest.fixture
def link_home(tmp_path):
    """A throwaway HOME with a uv-tool-style link to this checkout's ``arize-harness``."""
    real = Path(sys.executable).parent / ("arize-harness.exe" if os.name == "nt" else "arize-harness")
    if not real.exists():
        pytest.skip(f"{real} is not installed (run `uv sync`)")
    home = _home(tmp_path)
    for sub in (".cursor", ".gemini", ".kiro", ".config/devin"):
        (home / sub).mkdir(parents=True, exist_ok=True)
    link = home / ".local" / "bin" / "arize-harness"
    link.parent.mkdir(parents=True)
    link.symlink_to(real)
    return home, link


def _harness_cli(home: Path, link: Path, *args: str) -> "object":
    """Run ``arize-harness`` through the link, so setup registers the link."""
    from tests.core.test_cli_commands import _run

    return _run(home, [str(link), *args], ARIZE_ENV_FILE=str(home / "creds.env"))


@POSIX_ONLY
@pytest.mark.parametrize("name", sorted(HARNESSES))
class TestInstallers:
    def test_migrates_old_entries_keeps_others_and_is_idempotent(self, name, link_home):
        home, link = link_home
        harness = HARNESSES[name]
        _write_creds(home)
        harness.seed(home, home)
        files = harness.files(home, home)

        first = _harness_cli(home, link, "setup", name, "--non-interactive")
        assert first.returncode == 0, first.stdout + first.stderr
        after_one = _snapshot(home)

        values = [v for f in files for v in _commands(f)]
        ours = [v for v in values if is_arize_hook_command(v, name)]
        assert ours, f"no arize-harness hook command written for {name}: {values}"
        assert {registered_command_path(v) for v in ours} == {link.as_posix()}
        assert not any(is_old_venv_hook(v) for v in values), values
        assert not any(isinstance(v, str) and ".arize/harness/venv" in v for v in values), values
        assert not any(isinstance(v, str) and "/old/arize-harness" in v for v in values), values
        if name == "codex":
            config = tomllib.loads(files[0].read_text())
            assert config["notify"] == [link.as_posix(), "hook", "codex", "notify"]
            assert config["model"] == "gpt-5"
        else:
            assert USER_CMD in values
        if name == "claude":
            plugins = json.loads(files[0].read_text())["plugins"]
            assert "/user/plugin" in plugins
            assert not any(isinstance(p, dict) and p["path"].endswith("tracing/claude_code") for p in plugins)
            assert len(ours) == len(set(map(str, ours)))

        second = _harness_cli(home, link, "setup", name, "--non-interactive")
        assert second.returncode == 0, second.stdout + second.stderr
        assert _snapshot(home) == after_one

        status = _harness_cli(home, link, "status", "--json")
        assert status.returncode == 0, status.stdout + status.stderr

        gone = _harness_cli(home, link, "uninstall", name)
        assert gone.returncode == 0, gone.stdout + gone.stderr
        left = [v for f in files for v in _commands(f)]
        assert not any(is_our_hook_command(v, name) for v in left), left
        if name != "codex":
            assert USER_CMD in left
        if name == "claude":
            assert json.loads(files[0].read_text())["plugins"] == ["/user/plugin"]

    def test_status_not_registered_once_the_command_is_gone(self, name, link_home):
        home, link = link_home
        _write_creds(home)
        result = _harness_cli(home, link, "setup", name, "--non-interactive")
        assert result.returncode == 0, result.stdout + result.stderr
        real = link.resolve()
        link.unlink()

        status = _run_status(home, real)
        assert status.returncode == 2, status.stdout + status.stderr
        report = json.loads(status.stdout)
        item = next(h for h in report["harnesses"])
        assert item["registered"] is False
        assert item["missing_command"] == link.as_posix()


def _run_status(home: Path, real: Path):
    from tests.core.test_cli_commands import _run

    return _run(home, [str(real), "status", "--json"])


def _status_item(home: Path, link: Path, name: str) -> dict:
    status = _harness_cli(home, link, "status", "--json")
    assert status.returncode == 0, status.stdout + status.stderr
    return next(h for h in json.loads(status.stdout)["harnesses"] if h["name"] == name)


@POSIX_ONLY
class TestApostropheInPath:
    def test_copilot_powershell_fields_count_as_registered(self, link_home):
        home, old_link = link_home
        link = home / "o'brien" / "bin" / "arize-harness"
        link.parent.mkdir(parents=True)
        link.symlink_to(old_link.resolve())
        _write_creds(home)

        first = _harness_cli(home, link, "setup", "copilot", "--non-interactive")
        assert first.returncode == 0, first.stdout + first.stderr
        hooks = home / ".github" / "hooks" / "hooks.json"
        assert "o''brien" in hooks.read_text()
        after_one = _snapshot(home)

        item = _status_item(home, link, "copilot")
        assert item["registered"] is True, item
        assert not item.get("missing_command"), item

        second = _harness_cli(home, link, "setup", "copilot", "--non-interactive")
        assert second.returncode == 0, second.stdout + second.stderr
        assert _snapshot(home) == after_one


@POSIX_ONLY
class TestJsoncComments:
    @pytest.mark.parametrize(
        "comment",
        ['// my 27" monitor config\n', '/* " */\n'],
        ids=["line-comment", "block-comment"],
    )
    def test_quote_in_a_comment_does_not_hide_the_registration(self, link_home, comment):
        home, link = link_home
        _write_creds(home)
        result = _harness_cli(home, link, "setup", "devin", "--non-interactive")
        assert result.returncode == 0, result.stdout + result.stderr
        config = home / ".config" / "devin" / "config.json"
        config.write_text(comment + config.read_text())

        item = _status_item(home, link, "devin")
        assert item["registered"] is True, item


# ---------------------------------------------------------------------------
# Codex notify set by the user
# ---------------------------------------------------------------------------


@POSIX_ONLY
class TestCodexForeignNotify:
    def test_left_alone_with_a_warning_and_status_says_not_registered(self, link_home):
        home, link = link_home
        _write_creds(home)
        config = home / ".codex" / "config.toml"
        config.write_text('notify = ["notify-send", "codex"]\n')
        result = _harness_cli(home, link, "setup", "codex", "--non-interactive")
        assert result.returncode == 0, result.stdout + result.stderr
        assert "Codex runs one notify program" in result.stderr
        assert tomllib.loads(config.read_text())["notify"] == ["notify-send", "codex"]
        status = _harness_cli(home, link, "status", "--json")
        assert status.returncode == 2

    def test_old_command_appended_to_the_users_list_is_removed(self, link_home):
        home, link = link_home
        _write_creds(home)
        config = home / ".codex" / "config.toml"
        config.write_text(f'notify = ["notify-send", "{_old(home, "arize-hook-codex-notify")}"]\n')
        result = _harness_cli(home, link, "setup", "codex", "--non-interactive")
        assert result.returncode == 0, result.stdout + result.stderr
        assert tomllib.loads(config.read_text())["notify"] == ["notify-send"]


# ---------------------------------------------------------------------------
# uninstall --all keeps the running package
# ---------------------------------------------------------------------------


class TestWipeKeepsRunningPackage:
    @pytest.fixture
    def install_dir(self, tmp_path, monkeypatch) -> Path:
        root = tmp_path / ".arize" / "harness"
        for sub in ("venv/lib", "logs", "state/x", "claude_plugin/hooks", "tracing/claude_code"):
            (root / sub).mkdir(parents=True)
        (root / "config.json").write_text("{}")
        (root / "manifest.json").write_text("{}")
        monkeypatch.setattr(setup_mod, "INSTALL_DIR", root)
        return root

    def test_running_from_the_venv_keeps_only_it(self, install_dir, monkeypatch):
        from core.setup import wipe

        monkeypatch.setattr(sys, "prefix", str(install_dir / "venv"))
        wipe.wipe_shared_runtime(keep_running_package=True)
        assert sorted(p.name for p in install_dir.iterdir()) == ["venv"]

    def test_running_from_elsewhere_removes_everything(self, install_dir, monkeypatch, tmp_path):
        from core.setup import wipe

        monkeypatch.setattr(sys, "prefix", str(tmp_path / "tools" / "env"))
        wipe.wipe_shared_runtime(keep_running_package=True)
        assert not install_dir.exists()

    def test_binary_under_the_install_dir_is_kept(self, install_dir, monkeypatch):
        from core.setup import wipe

        binary = install_dir / "bin" / "arize-harness"
        binary.parent.mkdir()
        binary.write_text("")
        monkeypatch.setattr(wipe, "is_binary", lambda: True)
        monkeypatch.setattr(sys, "executable", str(binary))
        monkeypatch.setattr(sys, "argv", [str(binary)])
        wipe.wipe_shared_runtime(keep_running_package=True)
        assert sorted(p.name for p in install_dir.iterdir()) == ["bin"]

    def test_binary_run_by_bare_name_through_path_is_kept(self, install_dir, monkeypatch, tmp_path):
        """argv[0] is a bare name when the binary was found on PATH."""
        from core.setup import wipe

        binary = install_dir / "bin" / "arize-harness"
        binary.parent.mkdir()
        binary.write_text("")
        elsewhere = tmp_path / "project"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)
        monkeypatch.setenv("PATH", str(binary.parent))
        monkeypatch.setattr(wipe, "is_binary", lambda: True)
        monkeypatch.setattr(sys, "executable", str(binary))
        monkeypatch.setattr(sys, "argv", ["arize-harness", "uninstall", "--all"])
        wipe.wipe_shared_runtime(keep_running_package=True)
        assert binary.is_file()
        assert sorted(p.name for p in install_dir.iterdir()) == ["bin"]

    def test_install_scripts_still_remove_the_venv(self, install_dir, monkeypatch):
        from core.setup import wipe

        monkeypatch.setattr(sys, "prefix", str(install_dir / "venv"))
        wipe.wipe_shared_runtime()
        assert not install_dir.exists()

    def test_dry_run_removes_nothing(self, install_dir, monkeypatch):
        from core.setup import wipe

        monkeypatch.setenv("ARIZE_DRY_RUN", "true")
        monkeypatch.setattr(sys, "prefix", str(install_dir / "venv"))
        before = sorted(p.name for p in install_dir.iterdir())
        wipe.wipe_shared_runtime(keep_running_package=True)
        assert sorted(p.name for p in install_dir.iterdir()) == before


def test_venv_bin_is_gone():
    assert not hasattr(setup_mod, "venv_bin")
