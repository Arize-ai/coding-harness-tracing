"""scripts/smoke-wheel.sh and scripts/smoke_wheel_check.py.

``TestAgainstWheels`` is slow: it builds this checkout's wheel and runs the script on it and on broken copies.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from tests._pyproject import REPO_ROOT
from tests.core.test_wheel_install import wheel_dir  # noqa: F401  (module-scoped fixture)

SCRIPT = REPO_ROOT / "scripts" / "smoke-wheel.sh"
CHECK = REPO_ROOT / "scripts" / "smoke_wheel_check.py"

needs_uv = pytest.mark.skipif(shutil.which("uv") is None, reason="needs uv")
posix_only = pytest.mark.skipif(os.name == "nt", reason="bash script")


def _smoke(*args, timeout: int = 600, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(SCRIPT), *map(str, args)],
        capture_output=True,
        text=True,
        timeout=timeout,
        stdin=subprocess.DEVNULL,
        env=env,
    )


def _check(home: Path, cwd: Path, *args) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k not in ("CODEX_HOME", "DEVIN_PROJECT_DIR")}
    env["HOME"] = str(home)
    return subprocess.run(
        [sys.executable, "-I", "-B", str(CHECK), *map(str, args)],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


# ---------------------------------------------------------------------------
# Arguments and prerequisites
# ---------------------------------------------------------------------------


@posix_only
class TestBadInput:
    def test_no_arguments(self):
        result = _smoke()
        assert result.returncode == 2
        assert "usage: scripts/smoke-wheel.sh" in result.stderr

    def test_too_many_arguments(self, tmp_path):
        wheel = tmp_path / "x-1-py3-none-any.whl"
        wheel.write_bytes(b"")
        assert _smoke(wheel, tmp_path, tmp_path).returncode == 2

    def test_missing_wheel(self, tmp_path):
        result = _smoke(tmp_path / "nope.whl")
        assert result.returncode == 2
        assert "missing or unreadable" in result.stderr

    def test_not_a_wheel(self, tmp_path):
        (tmp_path / "x.tar.gz").write_bytes(b"")
        assert _smoke(tmp_path / "x.tar.gz").returncode == 2

    @pytest.mark.skipif(os.name != "nt" and os.geteuid() == 0, reason="root reads anything")
    def test_unreadable_wheel(self, tmp_path):
        wheel = tmp_path / "x-1-py3-none-any.whl"
        wheel.write_bytes(b"")
        wheel.chmod(0)
        try:
            assert _smoke(wheel).returncode == 2
        finally:
            wheel.chmod(0o600)

    def test_missing_dependency_dir(self, tmp_path):
        wheel = tmp_path / "x-1-py3-none-any.whl"
        wheel.write_bytes(b"")
        result = _smoke(wheel, tmp_path / "nope")
        assert result.returncode == 2
        assert "dependency wheel dir" in result.stderr

    @needs_uv
    def test_dependency_dir_without_python_dotenv(self, tmp_path):
        wheel = tmp_path / "x-1-py3-none-any.whl"
        wheel.write_bytes(b"")
        deps = tmp_path / "deps"
        deps.mkdir()
        result = _smoke(wheel, deps)
        assert result.returncode == 1
        assert "no python-dotenv wheel" in result.stderr

    def test_no_uv_on_path(self, tmp_path):
        wheel = tmp_path / "x-1-py3-none-any.whl"
        wheel.write_bytes(b"")
        # Only the tools the script needs before it looks for uv.
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        for tool in ("dirname", "basename"):
            (bin_dir / tool).symlink_to(shutil.which(tool))
        env = {"PATH": str(bin_dir), "HOME": str(tmp_path)}
        result = subprocess.run(
            [shutil.which("bash"), str(SCRIPT), str(wheel)], env=env, capture_output=True, text=True, timeout=60
        )
        assert result.returncode == 1
        assert "uv not found" in result.stderr


# ---------------------------------------------------------------------------
# The checker
# ---------------------------------------------------------------------------

COMMAND = "/opt/tools/bin/arize-harness"


def _claude_settings(home: Path, *commands: str) -> None:
    settings = home / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    hooks = [{"hooks": [{"type": "command", "command": c}]} for c in commands]
    settings.write_text(json.dumps({"hooks": {"Stop": hooks}}))


def _claude_plugin_hooks(home: Path, *commands: str) -> Path:
    hooks_json = home / ".arize" / "harness" / "claude_plugin" / "hooks" / "hooks.json"
    hooks_json.parent.mkdir(parents=True, exist_ok=True)
    hooks = [{"hooks": [{"type": "command", "command": c}]} for c in commands]
    hooks_json.write_text(json.dumps({"hooks": {"Stop": hooks}}))
    return hooks_json


def _opencode_plugin(home: Path, extra: str = "") -> Path:
    plugin = home / ".config" / "opencode" / "plugin" / "arize-tracing.ts"
    plugin.parent.mkdir(parents=True, exist_ok=True)
    plugin.write_text(
        f'function binaryPath(): string {{\n  return "{COMMAND}";\n}}\n{extra}'
        'export const plugin = () => spawn(binaryPath(), ["hook", "opencode"]);\n'
    )
    return plugin


class TestChecker:
    def test_entries_pass_when_every_entry_runs_the_command(self, tmp_path):
        _claude_settings(tmp_path, f"'{COMMAND}' hook claude stop")
        assert _check(tmp_path, tmp_path, "entries", COMMAND, "claude-code").returncode == 0

    def test_entries_fail_without_an_entry(self, tmp_path):
        result = _check(tmp_path, tmp_path, "entries", COMMAND, "claude-code")
        assert result.returncode == 1
        assert "no hook entry of ours" in result.stderr

    def test_entries_fail_on_an_old_venv_hook(self, tmp_path):
        venv = tmp_path / ".arize" / "harness" / "venv" / "bin" / "arize-hook-stop"
        _claude_settings(tmp_path, f"'{COMMAND}' hook claude stop", str(venv))
        result = _check(tmp_path, tmp_path, "entries", COMMAND, "claude-code")
        assert result.returncode == 1
        assert "venv path" in result.stderr

    def test_entries_fail_on_a_venv_path_in_another_form(self, tmp_path):
        python = tmp_path / ".arize" / "harness" / "venv" / "bin" / "python"
        _claude_settings(tmp_path, f"'{COMMAND}' hook claude stop", f"'{python}' -m tracing.claude_code.hooks stop")
        result = _check(tmp_path, tmp_path, "entries", COMMAND, "claude-code")
        assert result.returncode == 1
        assert result.stderr.count("venv path") == 1

    def test_entries_fail_on_arize_harness_in_the_venv(self, tmp_path):
        venv = tmp_path / ".arize" / "harness" / "venv" / "bin" / "arize-harness"
        _claude_settings(tmp_path, f"'{venv}' hook claude stop")
        result = _check(tmp_path, tmp_path, "entries", str(venv), "claude-code")
        assert result.returncode == 1
        assert "venv path" in result.stderr

    def test_entries_fail_on_another_arize_harness(self, tmp_path):
        _claude_settings(tmp_path, "'/elsewhere/arize-harness' hook claude stop")
        result = _check(tmp_path, tmp_path, "entries", COMMAND, "claude-code")
        assert result.returncode == 1
        assert "not /opt/tools/bin/arize-harness" in result.stderr

    def test_entries_check_the_claude_plugin_hooks(self, tmp_path):
        _claude_settings(tmp_path, f"'{COMMAND}' hook claude stop")
        venv = tmp_path / ".arize" / "harness" / "venv" / "bin" / "arize-hook-stop"
        plugin = _claude_plugin_hooks(tmp_path, f"'{COMMAND}' hook claude stop", str(venv))
        result = _check(tmp_path, tmp_path, "entries", COMMAND, "claude-code")
        assert result.returncode == 1
        assert f"{plugin}: names a venv path" in result.stderr

    def test_entries_fail_on_another_arize_harness_in_the_claude_plugin(self, tmp_path):
        _claude_settings(tmp_path, f"'{COMMAND}' hook claude stop")
        plugin = _claude_plugin_hooks(tmp_path, "'/elsewhere/arize-harness' hook claude stop")
        result = _check(tmp_path, tmp_path, "entries", COMMAND, "claude-code")
        assert result.returncode == 1
        assert f"{plugin}: runs /elsewhere/arize-harness" in result.stderr

    def test_no_entries_fails_on_a_leftover_in_the_claude_plugin(self, tmp_path):
        plugin = _claude_plugin_hooks(tmp_path, f"'{COMMAND}' hook claude stop")
        result = _check(tmp_path, tmp_path, "no-entries", "claude-code")
        assert result.returncode == 1
        assert f"{plugin}: entry left" in result.stderr

    def test_entries_pass_on_a_correct_opencode_plugin(self, tmp_path):
        _opencode_plugin(tmp_path)
        assert _check(tmp_path, tmp_path, "entries", COMMAND, "opencode").returncode == 0

    def test_entries_fail_on_a_venv_path_elsewhere_in_an_opencode_plugin(self, tmp_path):
        venv = tmp_path / ".arize" / "harness" / "venv" / "bin" / "python"
        plugin = _opencode_plugin(tmp_path, f'const PYTHON = "{venv}";\n')
        result = _check(tmp_path, tmp_path, "entries", COMMAND, "opencode")
        assert result.returncode == 1
        assert f"{plugin}: names a venv path in its source" in result.stderr

    @pytest.mark.parametrize("form", ["~", "$HOME", "${HOME}"])
    def test_entries_fail_on_an_unexpanded_venv_path(self, tmp_path, form):
        _claude_settings(tmp_path, f"'{COMMAND}' hook claude stop", f"{form}/.arize/harness/venv/bin/arize-hook-stop")
        result = _check(tmp_path, tmp_path, "entries", COMMAND, "claude-code")
        assert result.returncode == 1
        assert "venv path" in result.stderr

    def test_no_entries_fails_on_a_leftover(self, tmp_path):
        _claude_settings(tmp_path, f"'{COMMAND}' hook claude stop")
        result = _check(tmp_path, tmp_path, "no-entries", "claude-code", "codex")
        assert result.returncode == 1
        assert "entry left" in result.stderr

    def test_no_entries_ignores_the_users_hooks(self, tmp_path):
        _claude_settings(tmp_path, "echo mine")
        assert _check(tmp_path, tmp_path, "no-entries", "claude-code").returncode == 0

    def test_same_fails_on_a_changed_file(self, tmp_path):
        tree = tmp_path / "tree"
        tree.mkdir()
        (tree / "a").write_text("1")
        assert _check(tmp_path, tmp_path, "snapshot", tmp_path / "s1", tree).returncode == 0
        (tree / "a").write_text("2")
        assert _check(tmp_path, tmp_path, "snapshot", tmp_path / "s2", tree).returncode == 0
        result = _check(tmp_path, tmp_path, "same", tmp_path / "s1", tmp_path / "s2")
        assert result.returncode == 1
        assert "changed:" in result.stderr

    def test_same_fails_on_an_added_file(self, tmp_path):
        tree = tmp_path / "tree"
        tree.mkdir()
        _check(tmp_path, tmp_path, "snapshot", tmp_path / "s1", tree)
        (tree / "new").write_text("")
        _check(tmp_path, tmp_path, "snapshot", tmp_path / "s2", tree)
        assert "added:" in _check(tmp_path, tmp_path, "same", tmp_path / "s1", tmp_path / "s2").stderr

    @pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="needs POSIX permissions, not root")
    def test_snapshot_reports_an_unreadable_file(self, tmp_path):
        tree = tmp_path / "tree"
        tree.mkdir()
        locked = tree / "locked"
        locked.write_text("x")
        locked.chmod(0)
        try:
            result = _check(tmp_path, tmp_path, "snapshot", tmp_path / "s", tree)
        finally:
            locked.chmod(0o600)
        assert result.returncode == 1
        assert f"{locked}: cannot read" in result.stderr
        assert "Traceback" not in result.stderr
        assert not (tmp_path / "s").exists()

    @pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="needs POSIX permissions, not root")
    def test_snapshot_reports_an_unreadable_folder(self, tmp_path):
        tree = tmp_path / "tree"
        locked = tree / "locked"
        locked.mkdir(parents=True)
        (locked / "inside").write_text("x")
        locked.chmod(0)
        try:
            result = _check(tmp_path, tmp_path, "snapshot", tmp_path / "s", tree)
        finally:
            locked.chmod(0o700)
        assert result.returncode == 1
        assert f"{locked}: cannot open" in result.stderr
        assert "Traceback" not in result.stderr
        assert not (tmp_path / "s").exists()

    def test_hook_list_needs_every_harness_under_its_hook_name(self, tmp_path):
        listing = tmp_path / "list.txt"
        listing.write_text("claude stop\ncursor\n")
        assert _check(tmp_path, tmp_path, "hook-list", listing, "claude-code", "cursor").returncode == 0
        result = _check(tmp_path, tmp_path, "hook-list", listing, "claude-code", "kiro")
        assert result.returncode == 1
        assert "kiro" in result.stderr

    def test_harnesses_are_the_constants_keys(self, tmp_path):
        from core.constants import HARNESSES

        assert _check(tmp_path, tmp_path, "harnesses").stdout.split() == list(HARNESSES)

    def test_json(self, tmp_path):
        (tmp_path / "ok.json").write_text("{}")
        (tmp_path / "bad.json").write_text("{")
        assert _check(tmp_path, tmp_path, "json", tmp_path / "ok.json").returncode == 0
        assert _check(tmp_path, tmp_path, "json", tmp_path / "bad.json").returncode == 1


# ---------------------------------------------------------------------------
# The script against real wheels
# ---------------------------------------------------------------------------


def _patched(wheel: Path, out_dir: Path, drop: str | None = None, replace: dict | None = None) -> Path:
    """A copy of ``wheel`` without ``drop`` and with ``replace`` ({member: (old, new)}) applied, RECORD rewritten."""
    out_dir.mkdir()
    out = out_dir / wheel.name
    replace = replace or {}
    with zipfile.ZipFile(wheel) as src, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        record_name = next(n for n in src.namelist() if n.endswith(".dist-info/RECORD"))
        records = []
        for name in src.namelist():
            if name in (drop, record_name):
                continue
            data = src.read(name)
            if name in replace:
                old, new = replace[name]
                assert old.encode() in data, f"{old!r} not in {name}"
                data = data.replace(old.encode(), new.encode())
            dst.writestr(name, data)
            digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
            records.append(f"{name},sha256={digest},{len(data)}")
        records.append(f"{record_name},,")
        dst.writestr(record_name, "\n".join(records) + "\n")
    return out


@pytest.mark.slow
@posix_only
@needs_uv
class TestAgainstWheels:
    @pytest.fixture(scope="class")
    def built(self, wheel_dir, tmp_path_factory):  # noqa: F811
        """(this checkout's wheel, a dir holding only python-dotenv's wheel)."""
        (wheel,) = wheel_dir.glob("coding_harness_tracing-*.whl")
        deps = tmp_path_factory.mktemp("deps")
        for dotenv in wheel_dir.glob("python_dotenv-*.whl"):
            shutil.copy2(dotenv, deps)
        return wheel, deps

    def test_passes_on_this_checkout(self, built):
        wheel, deps = built
        result = _smoke(wheel, deps)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "smoke-wheel: OK" in result.stdout

    def test_fails_when_a_packaged_file_is_missing(self, built, tmp_path):
        wheel, deps = built
        broken = _patched(wheel, tmp_path / "w", drop="tracing/opencode/plugin/arize-tracing.ts")
        result = _smoke(broken, deps)
        assert result.returncode == 1
        assert "opencode: setup exited" in result.stderr

    def test_fails_when_a_hook_entry_points_at_a_venv(self, built, tmp_path):
        wheel, deps = built
        broken = _patched(
            wheel,
            tmp_path / "w",
            replace={
                "core/setup/runner.py": (
                    "    if is_binary():\n        return _absolute(sys.argv[0] or sys.executable)\n",
                    "    from core.setup import VENV_DIR\n\n    return VENV_DIR / 'bin' / COMMAND_NAME\n",
                )
            },
        )
        result = _smoke(broken, deps)
        assert result.returncode == 1
        assert "claude-code: hook entries do not all run" in result.stderr

    def test_fails_when_a_setup_rerun_changes_a_file(self, built, tmp_path):
        wheel, deps = built
        broken = _patched(
            wheel,
            tmp_path / "w",
            replace={
                "core/setup/commands.py": (
                    '    info("Setup complete!")\n',
                    '    info("Setup complete!")\n'
                    '    with open(Path.home() / "runs", "a") as f:\n        f.write("x")\n',
                )
            },
        )
        result = _smoke(broken, deps)
        assert result.returncode == 1
        assert "claude-code: setup re-run changed files" in result.stderr

    def test_fails_when_uninstall_leaves_an_entry(self, built, tmp_path):
        wheel, deps = built
        broken = _patched(
            wheel,
            tmp_path / "w",
            replace={
                "core/setup/commands.py": (
                    "        install_table.load(key).uninstall()\n        _refresh_files()\n",
                    "        _refresh_files()\n",
                )
            },
        )
        result = _smoke(broken, deps)
        assert result.returncode == 1
        assert "claude-code: uninstall left an entry of ours" in result.stderr

    def test_stays_offline_with_a_dependency_dir(self, built):
        wheel, deps = built
        # The script clears UV_* and PIP_*, not the proxy variables uv honours: point them at a closed port.
        dead = "http://127.0.0.1:9"
        env = {
            k: v
            for k, v in os.environ.items()
            if k.lower() not in ("no_proxy", "http_proxy", "https_proxy", "all_proxy")
        }
        env.update(HTTP_PROXY=dead, HTTPS_PROXY=dead, ALL_PROXY=dead)
        result = _smoke(wheel, deps, env=env)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "smoke-wheel: OK" in result.stdout

    @pytest.mark.skipif(os.geteuid() == 0, reason="root reads anything")
    def test_names_the_harness_and_check_when_a_file_is_unreadable(self, built, tmp_path):
        wheel, deps = built
        broken = _patched(
            wheel,
            tmp_path / "w",
            replace={
                "core/setup/commands.py": (
                    '    info("Setup complete!")\n',
                    '    info("Setup complete!")\n    (Path.home() / "locked").touch(mode=0)\n',
                )
            },
        )
        result = _smoke(broken, deps)
        assert result.returncode == 1
        assert "claude-code: snapshot before setup re-run failed" in result.stderr
        # A failed check moves on to the next harness rather than ending the run.
        assert "devin: snapshot before setup re-run failed" in result.stderr
        assert "locked: cannot read" in result.stderr
        assert "Traceback" not in result.stdout + result.stderr
