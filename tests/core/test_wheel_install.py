"""Slow: build the wheel, install it with ``uv tool install``, and set up every harness.

Runs offline: python-dotenv's wheel is rebuilt from this environment's copy.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.metadata
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from core.constants import HARNESSES
from core.setup import assets
from tests._pyproject import REPO_ROOT
from tests.core.test_assets import assert_equals_source_except_hook_command

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(shutil.which("uv") is None or shutil.which("git") is None, reason="needs uv and git"),
]

CREDS = "ARIZE_API_KEY=test-key\nARIZE_SPACE_ID=test-space\n"


def _run(cmd, cwd: Path, env: dict | None = None, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=600)
    if check:
        assert result.returncode == 0, f"{cmd} failed:\n{result.stdout}\n{result.stderr}"
    return result


def _dotenv_wheel(out: Path) -> None:
    """A wheel of the python-dotenv installed here (its files plus a fresh RECORD)."""
    dist = importlib.metadata.distribution("python-dotenv")
    name = f"python_dotenv-{dist.version}"
    records = []
    with zipfile.ZipFile(out / f"{name}-py3-none-any.whl", "w") as whl:
        for f in dist.files or []:
            rel = str(f).replace("\\", "/")
            if rel.startswith("..") or "__pycache__" in rel or rel.endswith(("/RECORD", "/INSTALLER", "/REQUESTED")):
                continue
            data = Path(dist.locate_file(f)).read_bytes()
            whl.writestr(rel, data)
            digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
            records.append(f"{rel},sha256={digest},{len(data)}")
        records.append(f"{name}.dist-info/RECORD,,")
        whl.writestr(f"{name}.dist-info/RECORD", "\n".join(records) + "\n")


@pytest.fixture(scope="module")
def wheel_dir(tmp_path_factory) -> Path:
    """This checkout's tracked files, built into a wheel, next to a python-dotenv wheel."""
    src = tmp_path_factory.mktemp("src") / "repo"
    files = _run(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], REPO_ROOT).stdout
    for name in filter(None, files.split("\0")):
        a, b = REPO_ROOT / name, src / name
        if not (a.is_file() or a.is_symlink()):
            continue
        b.parent.mkdir(parents=True, exist_ok=True)
        if a.is_symlink():
            os.symlink(os.readlink(a), b)
        else:
            shutil.copy2(a, b)
    out = tmp_path_factory.mktemp("wheels")
    env = dict(os.environ, SETUPTOOLS_SCM_PRETEND_VERSION="9.9.9")
    _run(["uv", "build", "--quiet", "--wheel", "--out-dir", str(out), str(src)], src, env)
    _dotenv_wheel(out)
    return out


def _wheel(wheel_dir: Path) -> Path:
    (whl,) = wheel_dir.glob("coding_harness_tracing-*.whl")
    return whl


class TestWheelContents:
    def test_ships_the_listed_files(self, wheel_dir):
        names = zipfile.ZipFile(_wheel(wheel_dir)).namelist()
        data = {n for n in names if not n.endswith(".py") and ".dist-info/" not in n}
        expected = {f"tracing/{sub}/{rel}" for sub, files in assets.PACKAGE_FILES.items() for rel in files}
        assert data == expected

    def test_exactly_one_core(self, wheel_dir):
        names = zipfile.ZipFile(_wheel(wheel_dir)).namelist()
        assert "core/__init__.py" in names
        assert [n for n in names if n.endswith("/core/__init__.py") or n.startswith("tracing/") and "/core/" in n] == []

    def test_no_run_hook(self, wheel_dir):
        assert not any("run-hook" in n for n in zipfile.ZipFile(_wheel(wheel_dir)).namelist())


@pytest.fixture(scope="module")
def tool(wheel_dir, tmp_path_factory):
    """``uv tool install`` of the wheel into a throwaway HOME; returns (home, command link, env)."""
    root = tmp_path_factory.mktemp("uvtool")
    home = root / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".codex").mkdir()
    (home / "creds.env").write_text(CREDS)
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("ARIZE_", "PHOENIX_", "OTEL_", "UV_", "PIP_", "PIPX_", "XDG_"))
        and k not in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "CODEX_HOME")
    }
    # may_write_setup() refuses installs under the temp dir, where this sandbox lives.
    (root / "tmp").mkdir()
    env.update(
        TMPDIR=str(root / "tmp"),
        TEMP=str(root / "tmp"),
        TMP=str(root / "tmp"),
        HOME=str(home),
        USERPROFILE=str(home),
        UV_TOOL_DIR=str(root / "tools"),
        UV_TOOL_BIN_DIR=str(root / "bin"),
        UV_CACHE_DIR=str(root / "cache"),
        UV_PYTHON_DOWNLOADS="never",
        NO_COLOR="1",
        ARIZE_ENV_FILE=str(home / "creds.env"),
    )
    _run(
        [
            "uv",
            "tool",
            "install",
            "--quiet",
            "--no-index",
            "--find-links",
            str(wheel_dir),
            "--python",
            sys.executable,
            "coding-harness-tracing",
        ],
        root,
        env,
    )
    link = root / "bin" / ("arize-harness.exe" if os.name == "nt" else "arize-harness")
    assert link.exists()
    project = home / "project"
    project.mkdir()
    return home, link, env, project


class TestUvToolInstall:
    @pytest.fixture(scope="class")
    def set_up(self, tool):
        home, link, env, project = tool
        results = {
            key: _run([str(link), "setup", key, "-y", "--with-skills"], project, env, check=False) for key in HARNESSES
        }
        return results

    @pytest.mark.parametrize("key", list(HARNESSES))
    def test_setup_succeeds(self, tool, set_up, key):
        assert set_up[key].returncode == 0, set_up[key].stdout + set_up[key].stderr

    def test_files_equal_package_sources_and_use_the_command_link(self, tool, set_up):
        home, link, _, _ = tool
        manifest = json.loads((home / ".arize" / "harness" / "manifest.json").read_text())
        assert {e["version"] for files in manifest["harnesses"].values() for e in files.values()} == {"9.9.9"}
        for harness, files in manifest["harnesses"].items():
            for path, entry in files.items():
                assert_equals_source_except_hook_command(harness, entry, Path(path))
        hooks = (home / ".arize" / "harness" / "claude_plugin" / "hooks" / "hooks.json").read_text()
        assert json.dumps(f"'{link.as_posix()}' hook claude stop") in hooks
        ts = (home / ".config" / "opencode" / "plugin" / "arize-tracing.ts").read_text()
        assert json.dumps(str(link)) in ts
        assert not (home / ".arize" / "harness" / "tracing").exists()
        assert not (home / ".arize" / "harness" / "venv").exists()

    def test_status_refreshes_other_versions_hook_does_not(self, tool, set_up):
        home, link, env, project = tool
        manifest_path = home / ".arize" / "harness" / "manifest.json"
        data = json.loads(manifest_path.read_text())
        for files in data["harnesses"].values():
            for entry in files.values():
                entry["version"] = "0.0.1"
        manifest_path.write_text(json.dumps(data))
        hooks = home / ".arize" / "harness" / "claude_plugin" / "hooks" / "hooks.json"
        good = hooks.read_bytes()
        hooks.write_text("stale")

        _run([str(link), "hook", "claude", "notification"], project, dict(env, ARIZE_TRACE_ENABLED="false"))
        assert hooks.read_text() == "stale"
        assert json.loads(manifest_path.read_text()) == data

        status = _run([str(link), "status"], project, env, check=False)
        assert hooks.read_bytes() == good, status.stdout + status.stderr
        assert {
            e["version"] for f in json.loads(manifest_path.read_text())["harnesses"].values() for e in f.values()
        } == {"9.9.9"}

    def test_uninstall_removes_manifest_files_only(self, tool, set_up):
        home, link, env, project = tool
        skill = project / ".agents" / "skills" / "manage-kiro-tracing"
        (skill / "mine.md").write_text("keep")
        _run([str(link), "uninstall", "kiro"], project, env)
        assert not (skill / "SKILL.md").exists()
        assert (skill / "mine.md").read_text() == "keep"
        assert (project / ".agents" / "skills" / "manage-codex-tracing" / "SKILL.md").is_file()
