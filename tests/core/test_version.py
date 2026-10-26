"""Tests for the package version: ``core.version()`` and the setuptools-scm build config.

The fast tests cover the reader and the config. The slow ones build in a scratch
git repo made from this checkout, so tags never land in the real repo. They need
git, uv and network access for the build dependencies.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tarfile
import types
import zipfile
from importlib import metadata
from pathlib import Path

import pytest

import core
from tests._pyproject import PYPROJECT, REPO_ROOT, tomllib

SCM_CONFIG = {
    "version_file": "core/_version.py",
    "fallback_version": "0.0.0",
    "git_describe_command": "git describe --dirty --tags --long --match v[0-9]* --exclude v0.1.0-rc6",
}
DEV_VERSION = re.compile(r"0\.1\.dev\d+\+g[0-9a-f]+")
# Tags in the real repo that aren't releases of this package.
STRAY_TAGS = ("cmd/ax-trace/v0.1.0-rc5", "cmd/ax-trace/v0.1.0-rc6", "v0.1.0-rc6")


class TestVersionReader:
    def test_prefers_generated_file(self, monkeypatch):
        generated = types.ModuleType("core._version")
        generated.version = "9.8.7"
        monkeypatch.setitem(sys.modules, "core._version", generated)
        monkeypatch.setattr(metadata, "version", lambda name: "1.1.1")
        assert core.version() == "9.8.7"

    def test_falls_back_to_installed_metadata(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "core._version", None)  # import raises ImportError
        seen = []
        monkeypatch.setattr(metadata, "version", lambda name: seen.append(name) or "1.1.1")
        assert core.version() == "1.1.1"
        assert seen == ["coding-harness-tracing"]

    def test_falls_back_to_fallback_version(self, monkeypatch):
        def not_installed(name):
            raise metadata.PackageNotFoundError(name)

        monkeypatch.setitem(sys.modules, "core._version", None)
        monkeypatch.setattr(metadata, "version", not_installed)
        assert core.version() == core.FALLBACK_VERSION == "0.0.0"


def _version_on_plugin_path(plugin_root: Path, cwd: Path) -> subprocess.CompletedProcess:
    """Run ``core.version()`` the way a plugin hook does: PYTHONPATH only, nothing installed.

    -S drops site-packages, so this venv's editable install can't answer.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTHON")}
    env["PYTHONPATH"] = str(plugin_root)
    return subprocess.run(
        [sys.executable, "-S", "-c", "import core; print(core.version())"],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


class TestPluginPath:
    @pytest.fixture
    def plugin(self, tmp_path):
        root = tmp_path / "plugin"
        (root / "core").mkdir(parents=True)
        shutil.copy(REPO_ROOT / "core" / "__init__.py", root / "core" / "__init__.py")
        return root

    def test_no_generated_file_gives_fallback(self, plugin, tmp_path):
        result = _version_on_plugin_path(plugin, tmp_path)
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "0.0.0"

    def test_generated_file_wins(self, plugin, tmp_path):
        (plugin / "core" / "_version.py").write_text("__version__ = version = '1.2.3'\n")
        result = _version_on_plugin_path(plugin, tmp_path)
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "1.2.3"

    def test_real_claude_plugin_folder(self, tmp_path):
        """tracing/claude_code links core/ in; it reports the generated file's version or the fallback."""
        generated = REPO_ROOT / "core" / "_version.py"
        expected = "0.0.0"
        if generated.exists():
            namespace: dict = {}
            exec(generated.read_text(), namespace)
            expected = namespace["version"]
        result = _version_on_plugin_path(REPO_ROOT / "tracing" / "claude_code", tmp_path)
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == expected


class TestBuildConfig:
    @pytest.fixture(scope="class")
    def pyproject(self):
        return tomllib.loads(PYPROJECT.read_text())

    def test_version_is_dynamic(self, pyproject):
        assert "version" not in pyproject["project"]
        assert pyproject["project"]["dynamic"] == ["version"]

    def test_setuptools_scm_pinned_to_8x(self, pyproject):
        assert "setuptools-scm>=8,<9" in pyproject["build-system"]["requires"]

    def test_setuptools_scm_config(self, pyproject):
        assert pyproject["tool"]["setuptools_scm"] == SCM_CONFIG

    def test_uv_rebuilds_on_new_tags(self, pyproject):
        assert pyproject["tool"]["uv"]["cache-keys"] == [
            {"file": "pyproject.toml"},
            {"git": {"commit": True, "tags": True}},
        ]

    def test_lock_has_no_hard_coded_version(self):
        lock = tomllib.loads((REPO_ROOT / "uv.lock").read_text())
        (root,) = [p for p in lock["package"] if p["name"] == "coding-harness-tracing"]
        assert "version" not in root

    @pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
    def test_generated_file_is_git_ignored(self):
        result = subprocess.run(
            ["git", "check-ignore", "-q", "core/_version.py"], cwd=REPO_ROOT, capture_output=True, timeout=30
        )
        assert result.returncode == 0


# --- Slow: real builds in scratch repos ---------------------------------------

needs_tools = pytest.mark.skipif(
    shutil.which("git") is None or shutil.which("uv") is None or shutil.which("uvx") is None,
    reason="needs git and uv",
)
# No user or system git config: no signing, hooks or aliases from the host.
GIT_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
}


def _env(extra_env: dict | None) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in ("VIRTUAL_ENV", "SETUPTOOLS_SCM_PRETEND_VERSION")}
    return {**env, **GIT_ENV, **(extra_env or {})}


def _run(*cmd, cwd: Path, extra_env: dict | None = None) -> str:
    # The environment is built inline, so a failure's traceback never shows it.
    result = subprocess.run(cmd, cwd=cwd, env=_env(extra_env), capture_output=True, text=True, timeout=600)
    assert result.returncode == 0, f"{cmd} failed:\n{result.stdout}\n{result.stderr}"
    return result.stdout.strip()


def _git(repo: Path, *args: str) -> str:
    return _run("git", *args, cwd=repo)


def _scm_version(repo: Path) -> str:
    return _run("uvx", "--quiet", "--from", "setuptools-scm>=8,<9", "python", "-m", "setuptools_scm", cwd=repo)


def _build(src: Path, out: Path) -> tuple[str, str]:
    """``uv build`` src, return the (wheel, sdist) versions."""
    _run("uv", "build", "--quiet", "--out-dir", str(out), str(src), cwd=src)
    (wheel,) = out.glob("*.whl")
    (sdist,) = out.glob("*.tar.gz")
    wheel_version = wheel.name.split("-")[1]
    sdist_version = sdist.name[: -len(".tar.gz")].split("-", 1)[1]
    return wheel_version, sdist_version


def _installed_version(wheel_dir: Path, work: Path) -> str:
    """Install the wheel in a fresh venv and return what ``core.version()`` reports there."""
    venv = work / "venv"
    _run("uv", "venv", "--quiet", "--python", sys.executable, str(venv), cwd=work)
    python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    (wheel,) = wheel_dir.glob("*.whl")
    _run("uv", "pip", "install", "--quiet", "--python", str(python), str(wheel), cwd=work)
    return _run(str(python), "-I", "-c", "import core; print(core.version())", cwd=work)


@pytest.fixture(scope="module")
def base_repo(tmp_path_factory):
    """A git repo of this checkout's files, with the repo's stray tags reachable from HEAD.

    The ``cmd/ax-trace/*`` tags crash setuptools-scm and ``v0.1.0-rc6`` would make
    untagged builds ``0.1.0rc7.devN``, so a filter that does nothing fails the tests.
    """
    repo = tmp_path_factory.mktemp("scm") / "repo"
    files = _git(REPO_ROOT, "ls-files", "--cached", "--others", "--exclude-standard", "-z").split("\0")
    for name in filter(None, files):
        src, dest = REPO_ROOT / name, repo / name
        if not (src.is_file() or src.is_symlink()):
            continue  # deleted in the working tree
        dest.parent.mkdir(parents=True, exist_ok=True)
        if src.is_symlink():
            os.symlink(os.readlink(src), dest)
        else:
            shutil.copy2(src, dest)
    _git(repo, "init", "--quiet", "--initial-branch=main")
    _git(repo, "add", "--all")
    _git(repo, "commit", "--quiet", "-m", "base")
    for tag in STRAY_TAGS:
        _git(repo, "tag", tag)
    (repo / "NOTES").write_text("second commit\n")
    _git(repo, "add", "NOTES")
    _git(repo, "commit", "--quiet", "-m", "second")
    return repo


@pytest.fixture
def repo(base_repo, tmp_path):
    """A fresh clone of base_repo (tags included), so tags one test adds don't leak."""
    clone = tmp_path / "repo"
    _run("git", "clone", "--quiet", str(base_repo), str(clone), cwd=tmp_path)
    return clone


@pytest.mark.slow
@needs_tools
class TestBuilds:
    def test_stray_tags_are_reachable_but_ignored(self, repo, tmp_path):
        # Unfiltered, git describe picks a stray tag.
        assert _git(repo, "describe", "--tags", "--abbrev=0") in STRAY_TAGS
        assert DEV_VERSION.fullmatch(_scm_version(repo))

    def test_untagged_commit_builds_dev_version(self, repo, tmp_path):
        wheel, sdist = _build(repo, tmp_path / "dist")
        assert DEV_VERSION.fullmatch(wheel), wheel
        assert DEV_VERSION.fullmatch(sdist), sdist

    def test_tagged_commit_builds_tag_version(self, repo, tmp_path):
        _git(repo, "tag", "-a", "v0.2.0", "-m", "v0.2.0")
        assert _build(repo, tmp_path / "dist") == ("0.2.0", "0.2.0")
        assert _installed_version(tmp_path / "dist", tmp_path) == "0.2.0"

    def test_wheel_holds_python_modules_only(self, repo, tmp_path):
        """The wheel built from the sdist holds the .py modules plus exactly the SHARE_FILES, and one core.

        setuptools-scm's git file finder must not add other tracked files.
        """
        from core.setup.share import SHARE_FILES, package_glob

        _build(repo, tmp_path / "dist")
        (wheel,) = (tmp_path / "dist").glob("*.whl")
        names = zipfile.ZipFile(wheel).namelist()
        assert "core/_version.py" in names
        shipped = {
            p.relative_to(repo).as_posix()
            for sub, patterns in SHARE_FILES.items()
            for pattern in patterns
            for p in package_glob(repo / "tracing" / sub, pattern)
        }
        assert {n for n in names if not n.endswith(".py") and ".dist-info/" not in n} == shipped
        assert [n for n in names if re.match(r"tracing/[^/]+/core/", n)] == []

    def test_commit_after_tag_builds_next_dev_version(self, repo, tmp_path):
        _git(repo, "tag", "-a", "v0.2.0", "-m", "v0.2.0")
        _git(repo, "commit", "--quiet", "--allow-empty", "-m", "after the tag")
        assert re.fullmatch(r"0\.2\.1\.dev1\+g[0-9a-f]+", _scm_version(repo))

    def test_shallow_clone_of_tag_builds_tag_version(self, repo, tmp_path):
        _git(repo, "tag", "-a", "v0.2.0", "-m", "v0.2.0")
        shallow = tmp_path / "shallow"
        # file:// so --depth applies to a local clone.
        _run("git", "clone", "--quiet", "--depth", "1", "--branch", "v0.2.0", repo.as_uri(), str(shallow), cwd=tmp_path)
        assert _git(shallow, "rev-parse", "--is-shallow-repository") == "true"
        assert _build(shallow, tmp_path / "dist") == ("0.2.0", "0.2.0")

    def test_source_archive_without_git_builds_fallback(self, repo, tmp_path):
        """Like GitHub's tarball of main: the files, no .git."""
        archive = tmp_path / "main.tar"
        _git(repo, "archive", "--format=tar", "--prefix=src/", "-o", str(archive), "HEAD")
        with tarfile.open(archive) as tar:
            tar.extractall(tmp_path / "archive")
        src = tmp_path / "archive" / "src"
        assert not (src / ".git").exists()
        assert _build(src, tmp_path / "dist") == ("0.0.0", "0.0.0")
        assert _installed_version(tmp_path / "dist", tmp_path) == "0.0.0"

    def test_uv_sync_picks_up_new_tag_without_cache_clearing(self, repo):
        env = {"UV_PYTHON": sys.executable}
        report = ("uv", "run", "--no-sync", "python", "-c", "import core; print(core.version())")
        _run("uv", "sync", "--quiet", cwd=repo, extra_env=env)
        assert DEV_VERSION.fullmatch(_run(*report, cwd=repo, extra_env=env))
        _git(repo, "tag", "-a", "v0.3.0", "-m", "v0.3.0")
        _run("uv", "sync", "--quiet", cwd=repo, extra_env=env)
        assert _run(*report, cwd=repo, extra_env=env) == "0.3.0"
