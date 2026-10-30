"""Tests for core/setup/share.py: copying each harness's files to ~/.arize/harness/share/<harness>/."""

from __future__ import annotations

import ast
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from core.constants import HARNESSES
from core.setup import share
from tests._pyproject import PYPROJECT, REPO_ROOT, tomllib

TRACING = REPO_ROOT / "tracing"
posix_only = pytest.mark.skipif(os.name == "nt", reason="run-hook is a POSIX shell script")


@pytest.fixture
def install_dir(tmp_path, monkeypatch):
    import core.setup as setup_mod

    base = tmp_path / ".arize" / "harness"
    base.mkdir(parents=True)
    monkeypatch.setattr(setup_mod, "INSTALL_DIR", base)
    return base


def _files(root: Path) -> set[str]:
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}


def _shipped(sub: str) -> set[str]:
    """The non-Python files the repo ships for a harness, per SHARE_FILES."""
    root = TRACING / sub
    return {p.relative_to(root).as_posix() for pat in share.SHARE_FILES[sub] for p in share.package_glob(root, pat)}


class TestManifest:
    def test_matches_pyproject_package_data(self):
        setuptools = tomllib.loads(PYPROJECT.read_text())["tool"]["setuptools"]
        assert setuptools["package-data"] == {f"tracing.{sub}": pats for sub, pats in share.SHARE_FILES.items()}
        assert setuptools["packages"]["find"]["exclude"] == ["tracing.claude_code.core*", "tracing.cursor.core*"]

    def test_covers_every_harness_that_ships_files(self):
        """Every harness with skills, a plugin folder or a .ts plugin is listed; devin ships none."""
        for key in HARNESSES:
            sub = key.replace("-", "_")
            root = TRACING / sub
            ships = (
                bool(share.package_glob(root, "skills/**/*"))
                or (root / "scripts" / "run-hook").exists()
                or (root / "plugin").is_dir()
            )
            assert (sub in share.SHARE_FILES) == ships, sub
        assert "devin" not in share.SHARE_FILES

    def test_every_pattern_matches_in_the_repo(self):
        for sub, patterns in share.SHARE_FILES.items():
            for pat in patterns:
                assert share.package_glob(TRACING / sub, pat), f"tracing/{sub}/{pat}"

    def test_plugin_folders_are_the_ones_with_run_hook(self):
        assert share.PLUGIN_HARNESSES == {p.parent.parent.name for p in TRACING.glob("*/scripts/run-hook")}


class TestInstallShare:
    @pytest.mark.parametrize("sub", sorted(share.SHARE_FILES))
    def test_copies_the_shipped_files(self, install_dir, sub):
        share.install_share(sub)
        got = _files(install_dir / "share" / sub)
        assert _shipped(sub) <= got
        for rel in _shipped(sub):
            assert (install_dir / "share" / sub / rel).read_bytes() == (TRACING / sub / rel).read_bytes()
        if sub not in share.PLUGIN_HARNESSES:
            assert got == _shipped(sub)

    @pytest.mark.parametrize("sub", sorted(share.PLUGIN_HARNESSES))
    def test_plugin_folder_is_complete(self, install_dir, sub):
        share.install_share(sub)
        dest = install_dir / "share" / sub
        assert (dest / "hooks" / "handlers.py").is_file()
        assert (dest / "core" / "__init__.py").is_file()
        assert (dest / "core" / "setup" / "__init__.py").is_file()
        assert not (dest / "core").is_symlink()
        assert not (dest / share.RUNTIME_FILE).exists()
        if os.name != "nt":
            assert os.access(dest / "scripts" / "run-hook", os.X_OK)

    def test_hyphenated_name(self, install_dir):
        share.install_share("claude-code")
        assert (install_dir / "share" / "claude_code" / "scripts" / "run-hook").is_file()

    def test_removed_file_disappears(self, install_dir):
        share.install_share("codex")
        stale = install_dir / "share" / "codex" / "skills" / "old-skill" / "SKILL.md"
        stale.parent.mkdir(parents=True)
        stale.write_text("from an older release")
        share.install_share("codex")
        assert not stale.exists()
        assert _files(install_dir / "share" / "codex") == _shipped("codex")

    def test_other_harnesses_untouched(self, install_dir):
        share.install_share("codex")
        marker = install_dir / "share" / "codex" / "marker"
        marker.write_text("keep")
        share.install_share("gemini")
        assert marker.read_text() == "keep"

    def test_no_temp_folders_left(self, install_dir):
        share.install_share("omp")
        share.install_share("omp")
        assert sorted(p.name for p in (install_dir / "share").iterdir()) == ["omp"]

    def test_devin_gets_no_folder(self, install_dir):
        share.install_share("devin")
        assert not (install_dir / "share").exists()

    def test_dry_run_writes_nothing(self, install_dir, monkeypatch, capsys):
        monkeypatch.setenv("ARIZE_DRY_RUN", "true")
        share.install_share("claude-code")
        assert not (install_dir / "share").exists()
        assert "would copy" in capsys.readouterr().out

    def test_missing_file_exits_with_path(self, install_dir, monkeypatch, capsys):
        monkeypatch.setitem(share.SHARE_FILES, "codex", ["skills/**/*", "no-such-file.json"])
        with pytest.raises(SystemExit) as exc:
            share.install_share("codex")
        assert exc.value.code == 1
        assert str(TRACING / "codex" / "no-such-file.json") in capsys.readouterr().err
        assert not (install_dir / "share").exists()

    @pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="needs POSIX permissions, not root")
    def test_unwritable_share_exits_with_path(self, install_dir, capsys):
        (install_dir / "share").mkdir()
        (install_dir / "share").chmod(0o500)
        try:
            with pytest.raises(SystemExit) as exc:
                share.install_share("codex")
        finally:
            (install_dir / "share").chmod(0o700)
        assert exc.value.code == 1
        assert str(install_dir / "share") in capsys.readouterr().err

    def test_binary_install_has_no_python_and_writes_runtime(self, install_dir, monkeypatch):
        monkeypatch.setattr(share, "is_binary", lambda: True)
        monkeypatch.setattr(sys, "executable", "/opt/arize/arize-harness.bin")
        for sub in share.PLUGIN_HARNESSES:
            share.install_share(sub)
            dest = install_dir / "share" / sub
            assert _files(dest) == _shipped(sub) | {share.RUNTIME_FILE}
            assert (dest / share.RUNTIME_FILE).read_text() == "/opt/arize/arize-harness.bin\n"


class TestRemoveShare:
    def test_removes_folder(self, install_dir):
        share.install_share("kiro")
        share.install_share("codex")
        share.remove_share("kiro")
        assert not (install_dir / "share" / "kiro").exists()
        assert (install_dir / "share" / "codex").is_dir()

    def test_missing_folder_is_noop(self, install_dir):
        share.remove_share("kiro")

    def test_dry_run_keeps_folder(self, install_dir, monkeypatch):
        share.install_share("kiro")
        monkeypatch.setenv("ARIZE_DRY_RUN", "true")
        share.remove_share("kiro")
        assert (install_dir / "share" / "kiro").is_dir()


class TestInstallerWiring:
    @staticmethod
    def _calls(path: Path, func: str) -> set[str]:
        tree = ast.parse(path.read_text())
        (node,) = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == func]
        return {c.func.id for c in ast.walk(node) if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)}

    @pytest.mark.parametrize("path", sorted(TRACING.glob("*/install.py")), ids=lambda p: p.parent.name)
    def test_install_copies_and_uninstall_removes(self, path):
        assert "install_share" in self._calls(path, "install")
        assert {"unlink_skills", "remove_share"} <= self._calls(path, "uninstall")

    @pytest.mark.parametrize("harness", ["opencode", "omp"])
    def test_ts_plugin_read_from_share(self, install_dir, harness):
        import importlib

        mod = importlib.import_module(f"tracing.{harness}.install")
        assert mod._plugin_source() == install_dir / "share" / harness / "plugin" / "arize-tracing.ts"


@posix_only
class TestRunHookBinary:
    """run-hook on a binary install: runs the binary from share/<harness>/runtime, with no Python."""

    @pytest.fixture
    def no_python_path(self, tmp_path):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        for tool in ("cat", "dirname"):
            os.symlink(shutil.which(tool), bin_dir / tool)
        return str(bin_dir)

    def _plugin(self, install_dir, monkeypatch, runtime: Path, sub: str) -> Path:
        monkeypatch.setattr(share, "is_binary", lambda: True)
        monkeypatch.setattr(sys, "executable", str(runtime))
        share.install_share(sub)
        return install_dir / "share" / sub

    def _stub(self, tmp_path) -> tuple[Path, Path]:
        out = tmp_path / "argv"
        stub = tmp_path / "arize-harness.bin"
        stub.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > "{out}"\n')
        stub.chmod(0o755)
        return stub, out

    def test_claude_runs_binary(self, tmp_path, install_dir, monkeypatch, no_python_path):
        stub, out = self._stub(tmp_path)
        plugin = self._plugin(install_dir, monkeypatch, stub, "claude_code")
        env = {"PATH": no_python_path, "CLAUDE_PLUGIN_ROOT": str(plugin), "HOME": str(tmp_path)}
        p = subprocess.run(
            ["/bin/sh", str(plugin / "scripts" / "run-hook"), "arize-hook-session-start"],
            env=env,
            capture_output=True,
            text=True,
        )
        assert p.returncode == 0, p.stderr
        assert out.read_text().split() == ["hook", "claude", "session-start"]

    def test_cursor_runs_binary(self, tmp_path, install_dir, monkeypatch, no_python_path):
        stub, out = self._stub(tmp_path)
        plugin = self._plugin(install_dir, monkeypatch, stub, "cursor")
        p = subprocess.run(
            ["/bin/sh", str(plugin / "scripts" / "run-hook")],
            env={"PATH": no_python_path, "HOME": str(tmp_path)},
            capture_output=True,
            text=True,
        )
        assert p.returncode == 0, p.stderr
        assert out.read_text().split() == ["hook", "cursor"]

    @pytest.mark.parametrize("sub", ["claude_code", "cursor"])
    def test_missing_binary_exits_zero_with_one_line(self, tmp_path, install_dir, monkeypatch, no_python_path, sub):
        plugin = self._plugin(install_dir, monkeypatch, tmp_path / "gone.bin", sub)
        p = subprocess.run(
            ["/bin/sh", str(plugin / "scripts" / "run-hook"), "arize-hook-stop"],
            env={"PATH": no_python_path, "CLAUDE_PLUGIN_ROOT": str(plugin), "HOME": str(tmp_path)},
            capture_output=True,
            text=True,
        )
        assert p.returncode == 0
        assert len(p.stderr.splitlines()) == 1 and "arize-harness not found" in p.stderr
