"""Tests for core/setup/runner.py: ``hook_command()`` and ``may_write_setup()``."""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

import core
from core.hook_table import iter_hooks
from core.setup import runner


@pytest.fixture
def link(tmp_path, monkeypatch):
    """``sys.argv[0]`` set to a uv-tool-style command link (a symlink to the tool venv's script)."""
    target = tmp_path / "tools" / "coding-harness-tracing" / "bin" / "arize-harness"
    target.parent.mkdir(parents=True)
    target.write_text("#!/bin/sh\n")
    bin_dir = tmp_path / "local bin"  # a space, so quoting matters
    bin_dir.mkdir()
    cmd = bin_dir / "arize-harness"
    try:
        cmd.symlink_to(target)
    except OSError:
        cmd.write_text("")
    monkeypatch.setattr(sys, "argv", [str(cmd), "setup"])
    return cmd


# ---------------------------------------------------------------------------
# <path>
# ---------------------------------------------------------------------------


class TestArizeHarnessPath:
    def test_command_link_as_invoked_not_resolved(self, link):
        assert runner.arize_harness_path() == link
        assert runner.arize_harness_path() != link.resolve() or not link.is_symlink()

    def test_relative_argv0_made_absolute_without_resolving(self, link, monkeypatch):
        monkeypatch.chdir(link.parent)
        monkeypatch.setattr(sys, "argv", ["./arize-harness"])
        assert runner.arize_harness_path() == link

    def test_falls_back_to_the_interpreters_scripts_dir(self, monkeypatch, tmp_path):
        monkeypatch.setattr(sys, "argv", [str(tmp_path / "install.py"), "install"])
        monkeypatch.setattr(runner, "_is_windows", lambda: False)
        assert runner.arize_harness_path() == Path(sys.executable).parent / "arize-harness"

    def test_windows_adds_exe(self, monkeypatch, tmp_path):
        monkeypatch.setattr(runner, "_is_windows", lambda: True)
        monkeypatch.setattr(sys, "argv", [str(tmp_path / "Scripts" / "arize-harness")])
        assert runner.arize_harness_path().name == "arize-harness.exe"
        monkeypatch.setattr(sys, "argv", [str(tmp_path / "Scripts" / "ARIZE-HARNESS.EXE")])
        assert runner.arize_harness_path().name == "ARIZE-HARNESS.EXE"

    def test_windows_install_bat_venv_registers_its_exe(self, monkeypatch, tmp_path):
        """install.bat runs install.py with the venv's python; the command is that venv's arize-harness.exe."""
        scripts = tmp_path / "venv" / "Scripts"
        monkeypatch.setattr(runner, "_is_windows", lambda: True)
        monkeypatch.setattr(sys, "executable", str(scripts / "python.exe"))
        monkeypatch.setattr(sys, "argv", [str(tmp_path / "tracing" / "claude_code" / "install.py"), "install"])
        assert runner.arize_harness_path() == scripts / "arize-harness.exe"

    def test_binary_uses_its_own_path(self, monkeypatch, tmp_path):
        binary = tmp_path / "bin" / "arize-harness"
        monkeypatch.setattr(runner, "is_binary", lambda: True)
        monkeypatch.setattr(sys, "argv", [str(binary), "setup"])
        assert runner.arize_harness_path() == binary


# ---------------------------------------------------------------------------
# hook_command / hook_argv / ts_argv
# ---------------------------------------------------------------------------


class TestHookCommand:
    def test_event_harness(self, link):
        assert runner.hook_command("claude", "session-start") == f"'{link.as_posix()}' hook claude session-start"

    def test_install_name_maps_to_hook_table_name(self, link):
        assert runner.hook_command("claude-code", "stop") == runner.hook_command("claude", "stop")
        assert runner.hook_command("claude_code", "stop") == runner.hook_command("claude", "stop")

    def test_single_entry_harness(self, link):
        assert runner.hook_command("cursor") == f"'{link.as_posix()}' hook cursor"

    @pytest.mark.parametrize("harness, event", list((h, e) for h, e, _ in iter_hooks()))
    def test_every_hook_round_trips_through_a_shell(self, link, harness, event):
        """The shell splits the command back into the argv ``arize-harness hook`` dispatches."""
        expected = [link.as_posix(), "hook", harness] + ([event] if event else [])
        assert shlex.split(runner.hook_command(harness, event)) == expected

    def test_quotes_a_single_quote_in_the_path(self, monkeypatch, tmp_path):
        weird = tmp_path / "it's here" / "arize-harness"
        monkeypatch.setattr(sys, "argv", [str(weird)])
        cmd = runner.hook_command("kiro")
        assert shlex.split(cmd) == [weird.as_posix(), "hook", "kiro"]
        if sys.platform != "win32":
            out = subprocess.run(["sh", "-c", f"printf '%s\\n' {cmd}"], capture_output=True, text=True, check=True)
            assert out.stdout.splitlines() == [weird.as_posix(), "hook", "kiro"]

    def test_json_config_value(self, link):
        """In a JSON config the command is the shell string, JSON-escaped by json.dumps."""
        cmd = runner.hook_command("claude", "stop")
        assert json.loads(json.dumps({"command": cmd}))["command"] == cmd

    def test_ts_literals_survive_windows_paths(self, monkeypatch):
        path = r"C:\Users\O'Brien\.local\bin\arize-harness.exe"
        monkeypatch.setattr(runner, "arize_harness_path", lambda: Path(path))
        file_lit, args_lit = runner.ts_argv("opencode")
        # JSON string syntax is TypeScript string syntax.
        assert json.loads(file_lit) == str(Path(path))
        assert json.loads(args_lit) == ["hook", "opencode"]

    @pytest.mark.parametrize(
        "harness, event",
        [("claude", None), ("claude", "nope"), ("cursor", "stop"), ("nope", None)],
    )
    def test_rejects_what_hook_would_not_dispatch(self, link, harness, event):
        with pytest.raises(ValueError):
            runner.hook_command(harness, event)


# ---------------------------------------------------------------------------
# may_write_setup
# ---------------------------------------------------------------------------


@pytest.fixture
def layout(tmp_path, monkeypatch):
    """Point sys.prefix / core.__file__ at a fake environment; return a function that sets one up."""
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    # Nothing here counts as "temp" unless a test says so.
    monkeypatch.setattr(runner, "_temp_roots", lambda: [tmp_path / "tmp"])
    monkeypatch.setattr(runner, "_old_venv_dir", lambda: home / ".arize" / "harness" / "venv")
    monkeypatch.setattr(runner, "is_binary", lambda: False)

    def use(prefix: Path, package_inside: bool = True, marker: str | None = None) -> None:
        pkg = (prefix / "lib" / "site-packages" / "core") if package_inside else (tmp_path / "checkout" / "core")
        pkg.mkdir(parents=True, exist_ok=True)
        if marker:
            (prefix / marker).write_text("")
        monkeypatch.setattr(sys, "prefix", str(prefix))
        monkeypatch.setattr(core, "__file__", str(pkg / "__init__.py"))

    use.home = home
    use.tmp = tmp_path
    return use


UV = "uv-receipt.toml"
PIPX = "pipx_metadata.json"


class TestMayWriteSetup:
    def test_uv_tool_install(self, layout):
        layout(layout.home / ".local" / "share" / "uv" / "tools" / "coding-harness-tracing", marker=UV)
        assert runner.may_write_setup()

    def test_uv_tool_in_a_custom_tool_dir(self, layout):
        layout(layout.tmp / "custom-tools" / "coding-harness-tracing", marker=UV)
        assert runner.may_write_setup()

    @pytest.mark.parametrize(
        "parts",
        [
            (".local", "share", "pipx", "venvs"),
            (".local", "pipx", "venvs"),
            ("Library", "Application Support", "pipx", "venvs"),  # pipx >= 1.3 on macOS
            ("AppData", "Local", "pipx", "pipx", "venvs"),  # pipx >= 1.3 on Windows
            ("pipx", "venvs"),
        ],
    )
    def test_pipx_install_wherever_pipx_keeps_venvs(self, layout, parts):
        layout(layout.home.joinpath(*parts, "coding-harness-tracing"), marker=PIPX)
        assert runner.may_write_setup()

    def test_pipx_install_with_a_suffix(self, layout):
        layout(layout.home / ".local" / "share" / "pipx" / "venvs" / "coding-harness-tracing@2", marker=PIPX)
        assert runner.may_write_setup()

    def test_env_without_a_receipt_is_refused(self, layout):
        layout(layout.home / ".local" / "share" / "uv" / "tools" / "coding-harness-tracing")
        assert "not running from a uv tool or pipx install" in runner.setup_write_refusal()

    def test_uvx_cache_env(self, layout):
        layout(layout.home / ".cache" / "uv" / "archive-v0" / "AbC123")
        reason = runner.setup_write_refusal()
        assert reason and "temporary environment" in reason

    def test_pipx_run_cache_even_with_metadata(self, layout):
        layout(layout.home / ".local" / "share" / "pipx" / ".cache" / "abc123", marker=PIPX)
        assert "temporary environment" in runner.setup_write_refusal()

    def test_uv_run_in_a_checkout(self, layout):
        """uv run installs the project editable: the package is outside the environment."""
        layout(layout.tmp / "checkout" / ".venv", package_inside=False)
        assert "source checkout" in runner.setup_write_refusal()

    def test_old_venv_on_posix(self, layout, monkeypatch):
        monkeypatch.setattr(runner, "_is_windows", lambda: False)
        layout(layout.home / ".arize" / "harness" / "venv")
        assert "old install.sh venv" in runner.setup_write_refusal()

    def test_install_bat_venv_on_windows(self, layout, monkeypatch):
        monkeypatch.setattr(runner, "_is_windows", lambda: True)
        layout(layout.home / ".arize" / "harness" / "venv")
        assert runner.may_write_setup()

    def test_temp_dir(self, layout):
        layout(layout.tmp / "tmp" / "uv" / "tools" / "coding-harness-tracing", marker=UV)
        assert not runner.may_write_setup()

    def test_unknown_layout_is_refused(self, layout):
        layout(layout.tmp / "some" / "venv")
        assert "not running from a uv tool or pipx install" in runner.setup_write_refusal()

    def test_binary(self, layout, monkeypatch):
        monkeypatch.setattr(runner, "is_binary", lambda: True)
        monkeypatch.setattr(sys, "argv", [str(layout.home / ".local" / "bin" / "arize-harness")])
        assert runner.may_write_setup()
        monkeypatch.setattr(sys, "argv", [str(layout.tmp / "tmp" / "arize-harness")])
        assert not runner.may_write_setup()

    def test_require_raises_with_the_install_hint(self, layout, capsys):
        layout(layout.home / ".cache" / "uv" / "archive-v0" / "x")
        with pytest.raises(runner.SetupRefused) as exc:
            runner.require_setup_writes()
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "uv tool install coding-harness-tracing" in err and "pipx install coding-harness-tracing" in err

    def test_this_test_run_is_refused(self):
        """pytest runs from the dev checkout, which may never write setup files."""
        assert not runner.may_write_setup()


class TestCacheLayouts:
    def test_home_named_cache_is_not_a_cache(self, layout, monkeypatch):
        home = layout.tmp / "cache"
        layout(home / ".local" / "share" / "uv" / "tools" / "coding-harness-tracing", marker=UV)
        assert runner.may_write_setup()

    def test_xdg_data_under_a_cache_dir(self, layout):
        layout(layout.tmp / "srv" / "cache" / "share" / "pipx" / "venvs" / "coding-harness-tracing", marker=PIPX)
        assert runner.may_write_setup()

    def test_pipx_run_in_dot_cache_pipx(self, layout):
        layout(layout.home / ".cache" / "pipx" / "abc123", marker=PIPX)
        assert "temporary environment" in runner.setup_write_refusal()

    def test_uv_builds_cache(self, layout):
        layout(layout.home / ".cache" / "uv" / "builds-v0" / "x", marker=UV)
        assert "temporary environment" in runner.setup_write_refusal()
