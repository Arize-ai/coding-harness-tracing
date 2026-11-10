"""Tests for core/setup/assets.py: package data, the Claude plugin folder, generated .ts, skills and the manifest.

Covers #90021's acceptance criteria that don't need a built wheel (see
TestWheel in test_wheel_install.py for those): every harness sets up from the
installed package in a throwaway HOME, written files equal their packaged
source except the hook command, a version mismatch is refreshed by
setup/status/uninstall but never by hook, uninstall removes only what the
manifest lists (plus old symlinks), and ARIZE_DRY_RUN writes nothing.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

import pytest

import core
import core.setup
from core import install_table
from core.constants import HARNESSES
from core.hook_table import EVENT_HOOKS
from core.setup import assets, commands, runner
from tests._pyproject import PYPROJECT, REPO_ROOT, tomllib
from tests.core.test_cli_commands import _cli, _home, _write_creds

TRACING = REPO_ROOT / "tracing"
POSIX_ONLY = pytest.mark.skipif(os.name == "nt", reason="symlinks and POSIX shell quoting")


@pytest.fixture
def install_dir():
    """The temp ``~/.arize/harness`` conftest's isolate_setup_files points core.setup at."""
    return core.setup.INSTALL_DIR


@pytest.fixture
def writable(monkeypatch):
    """Pretend this is a uv tool install, so refresh may write."""
    monkeypatch.setattr(assets, "may_write_setup", lambda: True)


def _strip_commands(node):
    """The hooks.json structure with every command blanked, to compare everything but the commands."""
    if isinstance(node, dict):
        return {k: (None if k == "command" else _strip_commands(v)) for k, v in node.items()}
    if isinstance(node, list):
        return [_strip_commands(v) for v in node]
    return node


def _commands(node):
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "command":
                yield v
            else:
                yield from _commands(v)
    elif isinstance(node, list):
        for v in node:
            yield from _commands(v)


def assert_equals_source_except_hook_command(harness: str, entry: dict, written: Path) -> None:
    """A written file is its packaged source, except where the source names the old hook command."""
    sub = harness.replace("-", "_")
    rel = entry["source"][len(f"tracing/{sub}/") :]
    source = (TRACING / sub / rel).read_bytes()
    data = written.read_bytes()
    if entry["kind"] == "copy":
        assert data == source, written
    elif entry["kind"] == "claude_hooks":
        new, old = json.loads(data), json.loads(source)
        assert _strip_commands(new) == _strip_commands(old)
        for cmd, old_cmd in zip(_commands(new), _commands(old)):
            argv = shlex.split(cmd)
            assert argv[1:3] == ["hook", "claude"] and argv[3] in EVENT_HOOKS["claude"]
            assert old_cmd.endswith(f"arize-hook-{argv[3]}")
    elif entry["kind"] == "plugin_ts":
        assert_ts_changes_only_the_hook_command(harness, source.decode(), data.decode())
    else:
        raise AssertionError(entry["kind"])


def assert_ts_changes_only_the_hook_command(harness: str, source: str, written: str) -> None:
    """The written .ts differs from its source in exactly two places, found independently of plugin_ts.

    1. The body of ``function binaryPath()`` (today's venv path) is one ``return "<arize-harness>"`` line.
    2. ``spawn(binaryPath(), [],`` passes ``["hook", "<harness>"]`` instead of ``[]``.
    """
    src = source.replace("\r\n", "\n").splitlines()
    out = written.replace("\r\n", "\n").splitlines()
    start = next(i for i, line in enumerate(src) if line.startswith("function binaryPath(): string {"))
    end = next(i for i in range(start, len(src)) if src[i] == "}")
    (spawn,) = [i for i, line in enumerate(src) if "spawn(binaryPath(), []," in line]
    expected_spawn = src[spawn].replace("spawn(binaryPath(), [],", f'spawn(binaryPath(), ["hook", "{harness}"],')
    removed = end - start - 1
    # Line numbers in `out` after the body shrank to one line.
    assert out[: start + 1] == src[: start + 1]
    ret = out[start + 1]
    assert ret.startswith('  return "') and ret.rstrip(";").endswith('"'), ret
    exe = json.loads(ret.strip().removeprefix("return ").rstrip(";"))
    assert Path(exe).is_absolute() and Path(exe).name.lower().startswith("arize-harness"), exe
    shift = removed - 1
    assert out[start + 2 : spawn - shift] == src[end:spawn]
    assert out[spawn - shift] == expected_spawn
    assert out[spawn - shift + 1 :] == src[spawn + 1 :]


# ---------------------------------------------------------------------------
# Package data
# ---------------------------------------------------------------------------


class TestPackageData:
    def test_matches_pyproject(self):
        setuptools = tomllib.loads(PYPROJECT.read_text())["tool"]["setuptools"]
        assert setuptools["include-package-data"] is False
        assert setuptools["package-data"] == {f"tracing.{sub}": files for sub, files in assets.PACKAGE_FILES.items()}

    def test_core_symlinks_are_not_packages(self):
        find = tomllib.loads(PYPROJECT.read_text())["tool"]["setuptools"]["packages"]["find"]
        links = {f"tracing.{p.parent.name}.core*" for p in TRACING.glob("*/core") if p.is_symlink()}
        assert links and set(find["exclude"]) == links

    def test_every_listed_file_exists(self):
        for sub, files in assets.PACKAGE_FILES.items():
            for rel in files:
                assert (TRACING / sub / rel).is_file(), f"tracing/{sub}/{rel}"

    def test_lists_the_plugin_and_ts_files(self):
        assert {".claude-plugin/plugin.json", "hooks/hooks.json"} <= set(assets.PACKAGE_FILES["claude_code"])
        for sub in ("opencode", "omp"):
            assert "plugin/arize-tracing.ts" in assets.PACKAGE_FILES[sub]
        assert "cursor" in assets.PACKAGE_FILES and not any(
            not f.startswith("skills/") for f in assets.PACKAGE_FILES["cursor"]
        ), "Cursor gets no plugin folder"

    def test_skills_of_every_harness_whose_installer_takes_with_skills(self):
        """Every skill file of claude_code, codex, cursor, kiro, omp, opencode; none of the others."""
        import inspect

        expected = set()
        for key in HARNESSES:
            sub = key.replace("-", "_")
            takes = "with_skills" in inspect.signature(install_table.load(key).install).parameters
            ships = [p for p in (TRACING / sub / "skills").rglob("*") if p.is_file() and p.name != ".gitkeep"]
            if takes and ships:
                expected.add(sub)
                listed = {f for f in assets.PACKAGE_FILES[sub] if f.startswith("skills/")}
                assert listed == {p.relative_to(TRACING / sub).as_posix() for p in ships}, sub
            else:
                assert not any(f.startswith("skills/") for f in assets.PACKAGE_FILES.get(sub, [])), sub
        assert expected == set(assets.SKILL_HARNESSES) == {"claude_code", "codex", "cursor", "kiro", "omp", "opencode"}

    @pytest.mark.parametrize("sub, rel", [(s, r) for s, files in assets.PACKAGE_FILES.items() for r in files])
    def test_reads_through_importlib_resources(self, sub, rel):
        assert assets.read_package_file(sub, rel) == (TRACING / sub / rel).read_bytes()

    def test_missing_package_file_exits_naming_it(self, monkeypatch, capsys):
        monkeypatch.setitem(assets.PACKAGE_FILES, "codex", ["skills/gone/SKILL.md"])
        with pytest.raises(assets.AssetError) as exc:
            assets.read_package_file("codex", "skills/gone/SKILL.md")
        assert exc.value.code == 1
        assert "tracing/codex/skills/gone/SKILL.md" in capsys.readouterr().err

    def test_setup_never_reads_the_source_tree_install_dir(self, install_dir, monkeypatch, tmp_path):
        """Nothing under ~/.arize/harness/tracing is needed: it doesn't exist here, and setup still works."""
        monkeypatch.chdir(tmp_path)
        assert not (install_dir / "tracing").exists()
        assets.install_claude_plugin()
        assets.install_plugin_ts("opencode", tmp_path / "oc.ts")
        assets.install_skills("kiro", tmp_path)
        assert not (install_dir / "tracing").exists()


# ---------------------------------------------------------------------------
# Generated files
# ---------------------------------------------------------------------------


class TestClaudePlugin:
    def test_folder_holds_only_the_manifest_and_hooks(self, install_dir):
        root = assets.install_claude_plugin()
        assert root == install_dir / "claude_plugin"
        files = {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}
        assert files == {".claude-plugin/plugin.json", "hooks/hooks.json"}

    def test_plugin_json_is_the_packaged_one(self):
        root = assets.install_claude_plugin()
        assert (root / ".claude-plugin" / "plugin.json").read_bytes() == (
            TRACING / "claude_code" / ".claude-plugin" / "plugin.json"
        ).read_bytes()

    def test_hooks_use_hook_command(self):
        root = assets.install_claude_plugin()
        hooks = json.loads((root / "hooks" / "hooks.json").read_text())
        commands_ = list(_commands(hooks))
        assert commands_ and "run-hook" not in json.dumps(hooks)
        for cmd in commands_:
            event = shlex.split(cmd)[3]
            assert cmd == runner.hook_command("claude", event)

    def test_only_the_commands_change(self):
        root = assets.install_claude_plugin()
        entry = assets.recorded("claude-code")[str(root / "hooks" / "hooks.json")]
        assert_equals_source_except_hook_command("claude-code", entry, root / "hooks" / "hooks.json")
        # Byte-for-byte outside the command strings.
        src = (TRACING / "claude_code" / "hooks" / "hooks.json").read_text()
        out = (root / "hooks" / "hooks.json").read_text()
        assert out == assets.claude_hooks_json(src)
        assert len(out.splitlines()) == len(src.splitlines())


class TestPluginTs:
    @pytest.mark.parametrize("harness", ["opencode", "omp"])
    def test_spawns_arize_harness_hook(self, harness, tmp_path, monkeypatch):
        exe = tmp_path / "local bin" / "arize-harness"
        monkeypatch.setattr(runner, "arize_harness_path", lambda: exe)
        target = tmp_path / "out.ts"
        assets.install_plugin_ts(harness, target)
        text = target.read_text()
        assert f"return {json.dumps(str(exe))}" in text
        assert f'spawn(binaryPath(), ["hook", "{harness}"],' in text
        assert "arize-hook-" not in text.split("function binaryPath", 1)[1].split("function forward", 1)[0]
        assert_equals_source_except_hook_command(harness, assets.recorded(harness)[str(target)], target)

    @pytest.mark.parametrize("harness", ["opencode", "omp"])
    def test_overwrites_an_old_copy(self, harness, tmp_path):
        target = tmp_path / "out.ts"
        target.write_text((TRACING / harness / "plugin" / "arize-tracing.ts").read_text())
        assets.install_plugin_ts(harness, target)
        assert '"venv"' not in target.read_text().split("function forward", 1)[0]

    def test_source_without_the_venv_path_fails(self):
        with pytest.raises(assets.AssetError):
            assets.plugin_ts("opencode", "// nothing to replace\n")


# ---------------------------------------------------------------------------
# Writing, dry run, errors
# ---------------------------------------------------------------------------


class TestWrite:
    def test_records_version_and_source(self, install_dir):
        root = assets.install_claude_plugin()
        manifest = json.loads((install_dir / "manifest.json").read_text())
        entry = manifest["harnesses"]["claude-code"][str(root / ".claude-plugin" / "plugin.json")]
        assert entry == {
            "kind": "copy",
            "root": str(root),
            "source": "tracing/claude_code/.claude-plugin/plugin.json",
            "version": core.version(),
        }

    def test_dry_run_writes_nothing(self, install_dir, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("ARIZE_DRY_RUN", "true")
        assets.install_claude_plugin()
        assets.install_plugin_ts("omp", tmp_path / "x.ts")
        assets.install_skills("codex", tmp_path)
        assert not install_dir.exists()
        assert not (tmp_path / "x.ts").exists() and not (tmp_path / ".agents").exists()
        assert "would write" in capsys.readouterr().out

    def test_unwritable_target_exits_naming_it(self, tmp_path, capsys):
        blocker = tmp_path / "file"
        blocker.write_text("")
        with pytest.raises(assets.AssetError) as exc:
            assets.install_plugin_ts("opencode", blocker / "sub" / "arize-tracing.ts")
        assert exc.value.code == 1
        assert str(blocker / "sub" / "arize-tracing.ts") in capsys.readouterr().err

    def test_overwrites_without_prompting(self, tmp_path, monkeypatch):
        monkeypatch.setattr("builtins.input", lambda *_: pytest.fail("prompted"))
        target = tmp_path / "x.ts"
        target.write_text("old")
        assets.install_plugin_ts("omp", target)
        assert target.read_text() != "old"


# ---------------------------------------------------------------------------
# Skills
# ---------------------------------------------------------------------------


class TestSkills:
    def test_copies_not_symlinks(self, tmp_path):
        assets.install_skills("cursor", tmp_path)
        skill = tmp_path / ".agents" / "skills" / "manage-cursor-tracing"
        assert skill.is_dir() and not skill.is_symlink()
        assert (skill / "SKILL.md").read_bytes() == (
            TRACING / "cursor" / "skills" / "manage-cursor-tracing" / "SKILL.md"
        ).read_bytes()

    @POSIX_ONLY
    def test_replaces_our_old_symlink(self, tmp_path, install_dir):
        old_src = install_dir / "tracing" / "cursor" / "skills" / "manage-cursor-tracing"
        old_src.mkdir(parents=True)
        (old_src / "SKILL.md").write_text("old")
        dest = tmp_path / ".agents" / "skills"
        dest.mkdir(parents=True)
        (dest / "manage-cursor-tracing").symlink_to(old_src)

        assets.install_skills("cursor", tmp_path)

        skill = dest / "manage-cursor-tracing"
        assert not skill.is_symlink()
        assert (skill / "SKILL.md").read_text() != "old"
        assert (old_src / "SKILL.md").read_text() == "old", "never writes through the old link"

    def test_leaves_a_skill_folder_setup_did_not_write(self, install_dir, tmp_path, capsys):
        skill = tmp_path / ".agents" / "skills" / "manage-cursor-tracing"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("mine")

        assets.install_skills("cursor", tmp_path)
        assert (skill / "SKILL.md").read_text() == "mine"
        assert f"{skill} is a folder setup did not make" in capsys.readouterr().out

        assets.uninstall_files("cursor")
        assert (skill / "SKILL.md").read_text() == "mine", "uninstall never removes it"

    def test_rewrites_a_skill_folder_it_wrote(self, install_dir, tmp_path):
        assets.install_skills("cursor", tmp_path)
        skill_md = tmp_path / ".agents" / "skills" / "manage-cursor-tracing" / "SKILL.md"
        skill_md.write_text("edited")

        assets.install_skills("cursor", tmp_path)
        assert (
            skill_md.read_bytes() == (TRACING / "cursor" / "skills" / "manage-cursor-tracing" / "SKILL.md").read_bytes()
        )

    def test_harness_without_skills_is_a_no_op(self, tmp_path):
        for harness in ("devin", "antigravity", "copilot", "gemini"):
            assets.install_skills(harness, tmp_path)
        assert not (tmp_path / ".agents").exists()


# ---------------------------------------------------------------------------
# Uninstall
# ---------------------------------------------------------------------------


class TestUninstall:
    def test_removes_only_manifest_files_and_keeps_a_user_file(self, install_dir, tmp_path):
        root = assets.install_claude_plugin()
        assets.install_skills("claude-code", tmp_path)
        skill = tmp_path / ".agents" / "skills" / "manage-claude-code-tracing"
        (skill / "notes.md").write_text("mine")
        (root / "hooks" / "mine.json").write_text("{}")
        (tmp_path / ".agents" / "skills" / "other-skill").mkdir()

        core.setup.remove_harness_files("claude-code", tmp_path)

        assert not (root / ".claude-plugin").exists()
        assert not (root / "hooks" / "hooks.json").exists()
        assert (root / "hooks" / "mine.json").read_text() == "{}"
        assert (skill / "notes.md").read_text() == "mine" and not (skill / "SKILL.md").exists()
        assert (tmp_path / ".agents" / "skills" / "other-skill").is_dir()
        assert "claude-code" not in assets.load_manifest()["harnesses"]

    def test_prunes_folders_we_own_once_empty(self, install_dir, tmp_path):
        root = assets.install_claude_plugin()
        assets.install_skills("kiro", tmp_path)
        core.setup.remove_harness_files("claude-code", tmp_path)
        core.setup.remove_harness_files("kiro", tmp_path)
        assert not root.exists()
        assert not (tmp_path / ".agents" / "skills" / "manage-kiro-tracing").exists()
        assert (tmp_path / ".agents" / "skills").is_dir(), "never removes .agents/skills itself"

    def test_other_harnesses_untouched(self, tmp_path):
        assets.install_skills("codex", tmp_path)
        assets.install_skills("cursor", tmp_path)
        core.setup.remove_harness_files("codex", tmp_path)
        assert (tmp_path / ".agents" / "skills" / "manage-cursor-tracing" / "SKILL.md").is_file()
        assert set(assets.load_manifest()["harnesses"]) == {"cursor"}

    @POSIX_ONLY
    def test_removes_old_symlinks_into_the_source_tree(self, install_dir, tmp_path):
        dest = tmp_path / ".agents" / "skills"
        dest.mkdir(parents=True)
        ours = install_dir / "tracing" / "opencode" / "skills" / "manage-opencode-tracing"
        (dest / "manage-opencode-tracing").symlink_to(ours)  # dangling is fine
        (dest / "elsewhere").symlink_to(tmp_path)
        (dest / "codex-one").symlink_to(install_dir / "tracing" / "codex" / "skills" / "manage-codex-tracing")

        core.setup.remove_harness_files("opencode", tmp_path)

        assert not os.path.lexists(dest / "manage-opencode-tracing")
        assert (dest / "elsewhere").is_symlink()
        assert (dest / "codex-one").is_symlink(), "another harness's link is not ours to remove here"

    def test_dry_run_removes_nothing(self, tmp_path, monkeypatch):
        assets.install_skills("omp", tmp_path)
        monkeypatch.setenv("ARIZE_DRY_RUN", "true")
        core.setup.remove_harness_files("omp", tmp_path)
        assert (tmp_path / ".agents" / "skills" / "manage-omp-tracing" / "SKILL.md").is_file()
        assert "omp" in assets.load_manifest()["harnesses"]


# ---------------------------------------------------------------------------
# Refresh on version mismatch
# ---------------------------------------------------------------------------


def _age(install_dir: Path, version: str = "0.0.0-older") -> None:
    """Pretend another package version wrote every recorded file."""
    path = install_dir / "manifest.json"
    data = json.loads(path.read_text())
    for files in data["harnesses"].values():
        for entry in files.values():
            entry["version"] = version
    path.write_text(json.dumps(data))


class TestRefresh:
    def test_rewrites_stale_files(self, install_dir, tmp_path, writable):
        root = assets.install_claude_plugin()
        assets.install_skills("cursor", tmp_path)
        hooks = root / "hooks" / "hooks.json"
        skill = tmp_path / ".agents" / "skills" / "manage-cursor-tracing" / "SKILL.md"
        good_hooks, good_skill = hooks.read_bytes(), skill.read_bytes()
        hooks.write_text("stale")
        skill.write_text("stale")
        _age(install_dir)

        rewritten = assets.refresh_stale()

        assert set(rewritten) == set(assets.recorded("claude-code")) | set(assets.recorded("cursor"))
        assert hooks.read_bytes() == good_hooks and skill.read_bytes() == good_skill
        assert not assets.stale_entries()

    def test_current_files_left_alone(self, writable):
        root = assets.install_claude_plugin()
        (root / "hooks" / "hooks.json").write_text("user edit")
        assert assets.refresh_stale() == []
        assert (root / "hooks" / "hooks.json").read_text() == "user edit"

    def test_ephemeral_install_writes_nothing(self, install_dir):
        """From uvx / uv run / a checkout (this test run), nothing is rewritten."""
        assert not runner.may_write_setup()
        root = assets.install_claude_plugin()
        (root / "hooks" / "hooks.json").write_text("stale")
        _age(install_dir)
        before = (install_dir / "manifest.json").read_text()
        assert assets.refresh_stale() == []
        assert (root / "hooks" / "hooks.json").read_text() == "stale"
        assert (install_dir / "manifest.json").read_text() == before

    def test_dry_run_rewrites_nothing(self, install_dir, writable, monkeypatch):
        root = assets.install_claude_plugin()
        (root / "hooks" / "hooks.json").write_text("stale")
        _age(install_dir)
        monkeypatch.setenv("ARIZE_DRY_RUN", "true")
        assert assets.refresh_stale() == []
        assert (root / "hooks" / "hooks.json").read_text() == "stale"

    def test_skips_skills_of_a_project_that_is_gone(self, install_dir, tmp_path, writable):
        project = tmp_path / "proj"
        assets.install_skills("codex", project)
        _age(install_dir)
        import shutil

        shutil.rmtree(project)
        assert assets.refresh_stale() == []
        assert not project.exists()


class TestRefreshFromCommands:
    """setup, status and uninstall refresh; hook never does (subprocess, real dispatch)."""

    def _stale_plugin(self, install_dir):
        root = assets.install_claude_plugin()
        hooks = root / "hooks" / "hooks.json"
        good = hooks.read_bytes()
        hooks.write_text("stale")
        _age(install_dir)
        return hooks, good

    def test_status_refreshes(self, install_dir, writable, capsys):
        hooks, good = self._stale_plugin(install_dir)
        commands.run(["status"])
        assert hooks.read_bytes() == good
        assert "Refreshed" in capsys.readouterr().out

    def test_status_json_keeps_stdout_json(self, install_dir, writable, capsys):
        hooks, good = self._stale_plugin(install_dir)
        commands.run(["status", "--json"])
        out = capsys.readouterr()
        json.loads(out.out)
        assert "Refreshed" in out.err and hooks.read_bytes() == good

    def test_uninstall_refreshes_the_other_harnesses(self, install_dir, writable, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        hooks, good = self._stale_plugin(install_dir)
        assets.install_skills("kiro", tmp_path)
        monkeypatch.setattr(install_table, "load", lambda key: type("M", (), {"uninstall": staticmethod(lambda: None)}))
        commands.run(["uninstall", "kiro"])
        assert hooks.read_bytes() == good

    def test_status_from_ephemeral_install_writes_nothing(self, install_dir, capsys):
        hooks, _ = self._stale_plugin(install_dir)
        before = (install_dir / "manifest.json").read_text()
        commands.run(["status"])
        assert hooks.read_text() == "stale"
        assert (install_dir / "manifest.json").read_text() == before

    def test_hook_writes_nothing(self, tmp_path):
        """``arize-harness hook`` from a different version touches no recorded file."""
        home = _home(tmp_path)
        _write_creds(home)
        result = _cli(home, "setup", "claude", "-y", ARIZE_ENV_FILE=str(home / "creds.env"))
        assert result.returncode == 0, result.stdout + result.stderr
        install_dir = home / ".arize" / "harness"
        _age(install_dir)
        manifest = json.loads((install_dir / "manifest.json").read_text())
        files = [Path(p) for p in manifest["harnesses"]["claude-code"]]
        before = {p: p.read_bytes() for p in files + [install_dir / "manifest.json"]}

        for argv in (["hook", "claude", "notification"], ["hook", "cursor"], ["hook", "--list"]):
            run = _cli(home, *argv, stdin="{}", ARIZE_TRACE_ENABLED="false", ARIZE_DRY_RUN="false")
            assert run.returncode == 0, run.stdout + run.stderr

        assert {p: p.read_bytes() for p in before} == before


# ---------------------------------------------------------------------------
# End to end: every harness, throwaway HOME, from the installed package
# ---------------------------------------------------------------------------


def _all_files(root: Path) -> dict:
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file() and "__pycache__" not in p.parts
    }


class TestEveryHarness:
    @pytest.fixture(scope="class")
    def set_up(self, tmp_path_factory):
        home = _home(tmp_path_factory.mktemp("every"))
        creds = _write_creds(home)
        results = {}
        for key in HARNESSES:
            results[key] = _cli(home, "setup", key, "--non-interactive", "--with-skills", ARIZE_ENV_FILE=creds)
        return home, results

    @pytest.mark.parametrize("key", list(HARNESSES))
    def test_setup_succeeds(self, set_up, key):
        _, results = set_up
        assert results[key].returncode == 0, results[key].stdout + results[key].stderr

    def test_written_files_equal_package_sources_except_hook_command(self, set_up):
        home, _ = set_up
        manifest = json.loads((home / ".arize" / "harness" / "manifest.json").read_text())
        assert set(manifest["harnesses"]) == {"claude-code", "codex", "cursor", "kiro", "omp", "opencode"}
        kinds = set()
        for harness, files in manifest["harnesses"].items():
            for path, entry in files.items():
                assert entry["version"] == core.version()
                assert Path(path).is_file(), path
                assert_equals_source_except_hook_command(harness, entry, Path(path))
                kinds.add(entry["kind"])
        assert kinds == {"copy", "claude_hooks", "plugin_ts"}

    def test_claude_plugin_entry_points_at_the_plugin_folder(self, set_up):
        home, _ = set_up
        settings = json.loads((home / ".claude" / "settings.json").read_text())
        paths = [p.get("path") for p in settings["plugins"] if isinstance(p, dict)]
        assert str(home / ".arize" / "harness" / "claude_plugin") in paths
        assert all(not str(p).endswith("tracing/claude_code") for p in paths)

    def test_cursor_has_no_plugin_folder(self, set_up):
        home, _ = set_up
        assert sorted(p.name for p in (home / ".arize" / "harness").iterdir() if "plugin" in p.name) == [
            "claude_plugin"
        ]

    def test_nothing_under_the_source_tree_dir(self, set_up):
        home, _ = set_up
        assert not (home / ".arize" / "harness" / "tracing").exists()

    def test_uninstall_removes_manifest_files_only(self, set_up):
        home, _ = set_up
        manifest = json.loads((home / ".arize" / "harness" / "manifest.json").read_text())
        files = [Path(p) for p in manifest["harnesses"]["opencode"]]
        user_file = home / ".agents" / "skills" / "manage-opencode-tracing" / "mine.md"
        user_file.write_text("keep")
        result = _cli(home, "uninstall", "opencode")
        assert result.returncode == 0, result.stdout + result.stderr
        assert not any(p.exists() for p in files)
        assert user_file.read_text() == "keep"
        assert (home / ".agents" / "skills" / "manage-cursor-tracing" / "SKILL.md").is_file()

    def test_dry_run_writes_nothing(self, tmp_path):
        home = _home(tmp_path)
        creds = _write_creds(home)
        before = _all_files(home)
        for key in HARNESSES:
            result = _cli(home, "setup", key, "-y", "--with-skills", ARIZE_ENV_FILE=creds, ARIZE_DRY_RUN="true")
            assert result.returncode == 0, key + result.stdout + result.stderr
        assert _all_files(home) == before
        assert not (home / ".arize").exists() and not (home / ".agents").exists()

    def test_missing_package_file_fails_setup_naming_it(self, tmp_path):
        """A package whose tracing/opencode data is missing: setup exits non-zero and names the file."""
        home = _home(tmp_path)
        creds = _write_creds(home)
        shim = tmp_path / "shim"
        shim.mkdir()
        empty = tmp_path / "empty-package"
        empty.mkdir()
        (shim / "sitecustomize.py").write_text(
            "import importlib.resources as r, pathlib\n"
            "_files = r.files\n"
            f"r.files = lambda pkg: pathlib.Path({str(empty)!r}) if pkg == 'tracing.opencode' else _files(pkg)\n"
        )
        result = _cli(
            home,
            "setup",
            "opencode",
            "-y",
            ARIZE_ENV_FILE=creds,
            PYTHONPATH=os.pathsep.join([str(shim), str(REPO_ROOT)]),
        )
        assert result.returncode == 1, result.stdout + result.stderr
        assert "tracing/opencode/plugin/arize-tracing.ts" in result.stderr

    def test_unwritable_target_fails_setup_naming_it(self, tmp_path):
        home = _home(tmp_path)
        creds = _write_creds(home)
        (home / ".arize" / "harness").mkdir(parents=True)
        (home / ".arize" / "harness" / "claude_plugin").write_text("a file where the folder goes")
        result = _cli(home, "setup", "claude", "-y", ARIZE_ENV_FILE=creds)
        assert result.returncode == 1, result.stdout + result.stderr
        assert str(home / ".arize" / "harness" / "claude_plugin") in result.stderr


# ---------------------------------------------------------------------------
# Review fixes
# ---------------------------------------------------------------------------


class TestCrlfSource:
    @pytest.mark.parametrize("harness", ["opencode", "omp"])
    def test_crlf_source_is_substituted_and_keeps_crlf(self, harness, tmp_path, monkeypatch):
        """A Windows checkout without .gitattributes ships the .ts with CRLF."""
        crlf = (TRACING / harness / "plugin" / "arize-tracing.ts").read_bytes().replace(b"\n", b"\r\n")
        monkeypatch.setattr(assets, "read_package_file", lambda h, rel: crlf)
        target = tmp_path / "out.ts"
        assets.install_plugin_ts(harness, target)
        out = target.read_bytes()
        assert b'spawn(binaryPath(), ["hook", "' + harness.encode() + b'"],' in out
        assert out.count(b"\r\n") == out.count(b"\n"), "every line still ends in CRLF"
        lf = (TRACING / harness / "plugin" / "arize-tracing.ts").read_text()
        assert out.decode().replace("\r\n", "\n") == assets.plugin_ts(harness, lf)


class TestManifestConcurrency:
    def test_one_manifest_save_per_operation(self, monkeypatch, tmp_path):
        saves = []
        real = assets.save_manifest
        monkeypatch.setattr(assets, "save_manifest", lambda data: (saves.append(1), real(data)))
        assets.install_claude_plugin()
        assert len(saves) == 1
        assets.install_skills("claude-code", tmp_path)
        assert len(saves) == 2

    def test_no_temp_file_left_behind(self, install_dir):
        assets.install_claude_plugin()
        assert sorted(p.name for p in install_dir.iterdir() if p.is_file()) == ["manifest.json", "manifest.json.lock"]

    def test_parallel_writers_keep_every_entry(self, tmp_path):
        """Many processes recording files at once: none of their entries is lost."""
        install = tmp_path / "home" / ".arize" / "harness"
        code = (
            "import sys; from pathlib import Path; import core.setup as s; "
            f"s.INSTALL_DIR = Path({str(install)!r}); "
            "from core.setup import assets; "
            "[assets.install_skills(h, Path(sys.argv[1])) for h in ('codex', 'cursor', 'kiro', 'omp', 'opencode')]"
        )
        env = dict(os.environ, PYTHONPATH=str(REPO_ROOT))
        procs = [
            subprocess.Popen([sys.executable, "-c", code, str(tmp_path / f"proj{i}")], env=env, cwd=tmp_path)
            for i in range(8)
        ]
        assert [p.wait(timeout=120) for p in procs] == [0] * 8
        manifest = json.loads((install / "manifest.json").read_text())
        for harness in ("codex", "cursor", "kiro", "omp", "opencode"):
            assert len(manifest["harnesses"][harness]) == 8, harness
        assert not list(install.glob(".manifest.json.*.tmp"))


class TestUninstallFailure:
    def test_file_that_cannot_be_removed_stays_recorded(self, tmp_path, monkeypatch):
        assets.install_skills("codex", tmp_path)
        assets.install_skills("cursor", tmp_path)
        stuck = tmp_path / ".agents" / "skills" / "manage-codex-tracing" / "SKILL.md"
        real_unlink = Path.unlink

        def unlink(self, *a, **k):
            if self == stuck:
                raise PermissionError("in use")
            return real_unlink(self, *a, **k)

        monkeypatch.setattr(Path, "unlink", unlink)
        assert assets.uninstall_files("codex") is False
        assert list(assets.recorded("codex")) == [str(stuck)]

        monkeypatch.setattr(Path, "unlink", real_unlink)
        assert assets.uninstall_files("codex") is True
        assert not stuck.exists() and "codex" not in assets.load_manifest()["harnesses"]
        assert assets.recorded("cursor"), "other harnesses untouched"


class TestRefreshHardening:
    def test_deleted_file_is_not_recreated_and_is_forgotten(self, install_dir, tmp_path, writable):
        target = tmp_path / "plugin" / "arize-tracing.ts"
        assets.install_plugin_ts("opencode", target)
        target.unlink()
        _age(install_dir)
        assert assets.refresh_stale() == []
        assert not target.exists()
        assert "opencode" not in assets.load_manifest()["harnesses"]

    def test_unexpected_error_does_not_crash(self, install_dir, writable, monkeypatch, capsys):
        root = assets.install_claude_plugin()
        (root / "hooks" / "hooks.json").write_text("stale")
        _age(install_dir)

        def boom(harness, rel, kind):
            if kind == "claude_hooks":
                raise ValueError("unknown event 'new-event' for harness 'claude'")
            return (TRACING / "claude_code" / rel).read_bytes()

        monkeypatch.setattr(assets, "render", boom)
        rewritten = assets.refresh_stale()
        assert rewritten == [str(root / ".claude-plugin" / "plugin.json")]
        assert "unknown event" in capsys.readouterr().err
        assert (root / "hooks" / "hooks.json").read_text() == "stale"
        stale = {p for _h, p, _e in assets.stale_entries()}
        assert stale == {str(root / "hooks" / "hooks.json")}, "the failed file stays stale"

    @pytest.mark.parametrize("bad", ['{"harnesses": {"x": {"/a": {"root": 1, "kind": "copy"}}}}', "[]", "{not json"])
    def test_malformed_manifest_does_not_crash(self, install_dir, writable, bad):
        install_dir.mkdir(parents=True, exist_ok=True)
        (install_dir / "manifest.json").write_text(bad)
        assert assets.refresh_stale() == []

    def test_status_main_refreshes_for_install_bat(self, install_dir, writable, capsys):
        """install.bat / install.sh run ``python -m core.setup.status`` directly; that path refreshes too."""
        from core.setup import status as status_mod

        root = assets.install_claude_plugin()
        good = (root / "hooks" / "hooks.json").read_bytes()
        (root / "hooks" / "hooks.json").write_text("stale")
        _age(install_dir)
        status_mod.main(["--json"])
        out = capsys.readouterr()
        json.loads(out.out)
        assert (root / "hooks" / "hooks.json").read_bytes() == good
        assert "Refreshed" in out.err


# ---------------------------------------------------------------------------
# Second review fixes
# ---------------------------------------------------------------------------


class TestTsHelperIsStrict:
    @pytest.mark.parametrize("harness", ["opencode", "omp"])
    def test_rejects_any_other_change(self, harness):
        source = (TRACING / harness / "plugin" / "arize-tracing.ts").read_text()
        good = assets.plugin_ts(harness, source)
        assert_ts_changes_only_the_hook_command(harness, source, good)
        lines = good.splitlines()
        for mutated in (
            "\n".join(lines[:-3] + lines[-2:]),  # an unrelated line dropped
            good.replace("detached: true", "detached: false"),  # an unrelated line changed
        ):
            with pytest.raises(AssertionError):
                assert_ts_changes_only_the_hook_command(harness, source, mutated)


class TestUninstallExitCode:
    def test_remove_harness_files_exits_1_when_a_file_stays(self, tmp_path, monkeypatch, capsys):
        assets.install_skills("kiro", tmp_path)
        monkeypatch.setattr(assets, "uninstall_files", lambda harness: False)
        with pytest.raises(assets.AssetError) as exc:
            core.setup.remove_harness_files("kiro", tmp_path)
        assert exc.value.code == 1
        assert "uninstall kiro" in capsys.readouterr().err

    def test_cli_uninstall_exits_non_zero(self, tmp_path):
        home = _home(tmp_path)
        creds = _write_creds(home)
        assert _cli(home, "setup", "kiro", "-y", "--with-skills", ARIZE_ENV_FILE=creds).returncode == 0
        skill = home / ".agents" / "skills" / "manage-kiro-tracing"
        if os.name == "nt":
            pytest.skip("chmod does not block deletes on Windows")
        skill.chmod(0o500)  # SKILL.md can't be unlinked
        try:
            result = _cli(home, "uninstall", "kiro")
        finally:
            skill.chmod(0o700)
        assert result.returncode == 1, result.stdout + result.stderr
        assert "Kiro tracing uninstalled" not in result.stdout
        assert "could not remove" in result.stderr


class TestLocking:
    def test_filesystem_without_locks_proceeds_unlocked(self, monkeypatch, capsys):
        import errno
        import fcntl

        def no_locks(fd, op):
            raise OSError(errno.ENOLCK, "No locks available")

        monkeypatch.setattr(fcntl, "flock", no_locks)
        assets.install_claude_plugin()
        assert "claude-code" in assets.load_manifest()["harnesses"]
        assert "does not support file locks" in capsys.readouterr().out

    def test_other_lock_errors_still_fail(self, monkeypatch):
        import errno
        import fcntl

        def denied(fd, op):
            raise OSError(errno.EBADF, "Bad file descriptor")

        monkeypatch.setattr(fcntl, "flock", denied)
        with pytest.raises(assets.AssetError):
            assets.install_claude_plugin()

    def test_windows_retries_until_the_lock_frees(self, monkeypatch, tmp_path):
        import errno
        import types

        attempts = []

        def locking(fd, mode, n):
            if mode == fake.LK_NBLCK:
                attempts.append(1)
                if len(attempts) < 4:
                    raise OSError(errno.EACCES, "held by another process")

        fake = types.SimpleNamespace(LK_NBLCK=2, LK_UNLCK=0, locking=locking)
        monkeypatch.setitem(sys.modules, "msvcrt", fake)
        monkeypatch.setattr(assets.sys, "platform", "win32")
        monkeypatch.setattr(assets.time, "sleep", lambda s: None)
        with assets._manifest_lock():
            pass
        assert len(attempts) == 4

    def test_windows_gives_up_after_the_timeout(self, monkeypatch):
        import errno
        import types

        def locking(fd, mode, n):
            if mode == 2:
                raise OSError(errno.EACCES, "held")

        monkeypatch.setitem(sys.modules, "msvcrt", types.SimpleNamespace(LK_NBLCK=2, LK_UNLCK=0, locking=locking))
        monkeypatch.setattr(assets.sys, "platform", "win32")
        monkeypatch.setattr(assets, "LOCK_TIMEOUT_SECONDS", 0.0)
        with pytest.raises(assets.AssetError):
            with assets._manifest_lock():
                pass


class TestUserSkillSymlink:
    @POSIX_ONLY
    def test_leaves_a_symlink_setup_did_not_make(self, tmp_path, capsys):
        theirs = tmp_path / "my-checkout" / "manage-cursor-tracing"
        theirs.mkdir(parents=True)
        (theirs / "SKILL.md").write_text("mine")
        dest = tmp_path / "proj" / ".agents" / "skills"
        dest.mkdir(parents=True)
        (dest / "manage-cursor-tracing").symlink_to(theirs)

        assets.install_skills("cursor", tmp_path / "proj")

        assert (dest / "manage-cursor-tracing").is_symlink()
        assert (theirs / "SKILL.md").read_text() == "mine"
        assert "symlink setup did not make" in capsys.readouterr().out
        assert not assets.recorded("cursor")

    @POSIX_ONLY
    def test_old_link_kept_when_rendering_fails(self, install_dir, tmp_path, monkeypatch):
        old = install_dir / "tracing" / "cursor" / "skills" / "manage-cursor-tracing"
        old.mkdir(parents=True)
        dest = tmp_path / ".agents" / "skills"
        dest.mkdir(parents=True)
        (dest / "manage-cursor-tracing").symlink_to(old)

        def missing(harness, rel):
            raise assets.AssetError(f"package file tracing/{harness}/{rel} is missing")

        monkeypatch.setattr(assets, "read_package_file", missing)
        with pytest.raises(assets.AssetError):
            assets.install_skills("cursor", tmp_path)
        assert (dest / "manage-cursor-tracing").is_symlink()


class TestManifestContainment:
    def _plant(self, install_dir, harness, path, source, kind="copy"):
        install_dir.mkdir(parents=True, exist_ok=True)
        entry = {"source": source, "kind": kind, "version": "0.0.0-older"}
        (install_dir / "manifest.json").write_text(json.dumps({"harnesses": {harness: {str(path): entry}}}))

    def test_uninstall_never_deletes_a_planted_path(self, install_dir, tmp_path):
        victim = tmp_path / ".ssh" / "config"
        victim.parent.mkdir()
        victim.write_text("Host *")
        self._plant(install_dir, "codex", victim, "tracing/codex/skills/manage-codex-tracing/SKILL.md")
        assert assets.uninstall_files("codex") is True
        assert victim.read_text() == "Host *"
        assert "codex" not in assets.load_manifest()["harnesses"]

    def test_refresh_never_overwrites_a_planted_path(self, install_dir, tmp_path, writable):
        victim = tmp_path / ".bashrc"
        victim.write_text("export X=1")
        self._plant(install_dir, "codex", victim, "tracing/codex/skills/manage-codex-tracing/SKILL.md")
        assert assets.refresh_stale() == []
        assert victim.read_text() == "export X=1"

    @pytest.mark.parametrize(
        "harness, rel_path, source, kind",
        [
            ("opencode", "notes.ts", "tracing/opencode/plugin/arize-tracing.ts", "plugin_ts"),
            ("claude-code", "elsewhere/hooks/hooks.json", "tracing/claude_code/hooks/hooks.json", "claude_hooks"),
            ("codex", "x/.agents/skills/manage-codex-tracing/SKILL.md", "tracing/codex/skills/other/SKILL.md", "copy"),
            ("opencode", "arize-tracing.ts", "tracing/opencode/plugin/arize-tracing.ts", "copy"),
        ],
    )
    def test_entries_outside_setup_locations(self, tmp_path, harness, rel_path, source, kind):
        assert assets._setup_location(harness, str(tmp_path / rel_path), {"source": source, "kind": kind}) is None

    def test_real_entries_are_setup_locations(self, tmp_path):
        assets.install_claude_plugin()
        assets.install_skills("omp", tmp_path)
        assets.install_plugin_ts("omp", tmp_path / "ext" / "arize-tracing.ts")
        for harness in ("claude-code", "omp"):
            for path, entry in assets.recorded(harness).items():
                assert assets._setup_location(harness, path, entry) is not None, path


class TestRefreshKeepsTheSetupCommand:
    def test_leaves_files_another_install_set_up(self, install_dir, tmp_path, writable, monkeypatch, capsys):
        first = tmp_path / "pipx" / "arize-harness"
        second = tmp_path / "uv" / "arize-harness"
        first.parent.mkdir()
        first.write_text("")  # the install that set it up still exists
        monkeypatch.setattr(assets, "arize_harness_path", lambda: first)
        monkeypatch.setattr(runner, "arize_harness_path", lambda: first)
        root = assets.install_claude_plugin()
        assets.install_skills("claude-code", tmp_path)
        hooks = root / "hooks" / "hooks.json"
        hooks.write_text("stale")
        _age(install_dir)

        monkeypatch.setattr(assets, "arize_harness_path", lambda: second)
        monkeypatch.setattr(runner, "arize_harness_path", lambda: second)
        rewritten = assets.refresh_stale()

        assert hooks.read_text() == "stale", "left to the install that set it up"
        assert str(hooks) not in rewritten
        assert str(tmp_path / ".agents" / "skills" / "manage-claude-code-tracing" / "SKILL.md") in rewritten
        assert "leaving" in capsys.readouterr().out

    def test_takes_over_files_whose_install_is_gone(self, install_dir, tmp_path, writable, monkeypatch):
        """pipx uninstalled, uv tool installed: hooks naming the removed arize-harness are rewritten."""
        removed = tmp_path / "pipx" / "arize-harness"  # never created: that install is gone
        current = tmp_path / "uv" / "arize-harness"
        monkeypatch.setattr(assets, "arize_harness_path", lambda: removed)
        monkeypatch.setattr(runner, "arize_harness_path", lambda: removed)
        root = assets.install_claude_plugin()
        ts = tmp_path / "ext" / "arize-tracing.ts"
        assets.install_plugin_ts("opencode", ts)
        _age(install_dir)

        monkeypatch.setattr(assets, "arize_harness_path", lambda: current)
        monkeypatch.setattr(runner, "arize_harness_path", lambda: current)
        rewritten = assets.refresh_stale()

        hooks = root / "hooks" / "hooks.json"
        assert {str(hooks), str(ts)} <= set(rewritten)
        assert current.as_posix() in hooks.read_text() and removed.as_posix() not in hooks.read_text()
        assert json.dumps(str(current)) in ts.read_text()
        for path in (hooks, ts):
            entry = assets.recorded("claude-code" if path == hooks else "opencode")[str(path)]
            assert entry["command"] == str(current) and entry["version"] == core.version()

    def test_same_install_refreshes_and_records_its_command(self, install_dir, writable):
        root = assets.install_claude_plugin()
        (root / "hooks" / "hooks.json").write_text("stale")
        _age(install_dir)
        assert str(root / "hooks" / "hooks.json") in assets.refresh_stale()
        entry = assets.recorded("claude-code")[str(root / "hooks" / "hooks.json")]
        assert entry["command"] == str(runner.arize_harness_path())


class TestWriteErrorNotMasked:
    def test_write_error_wins_over_manifest_error(self, tmp_path, monkeypatch, capsys):
        good = tmp_path / "ok" / "arize-tracing.ts"
        blocker = tmp_path / "file"
        blocker.write_text("")
        bad = blocker / "arize-tracing.ts"

        def broken_txn():
            raise assets.AssetError("cannot write manifest")

        monkeypatch.setattr(assets, "manifest_transaction", broken_txn)
        with pytest.raises(assets.AssetError) as exc:
            assets.write_files(
                "opencode",
                [
                    ("plugin/arize-tracing.ts", good, "plugin_ts", None),
                    ("plugin/arize-tracing.ts", bad, "plugin_ts", None),
                ],
            )
        assert str(bad) in str(exc.value)


# ---------------------------------------------------------------------------
# PR review: folder ownership and operation-wide locking
# ---------------------------------------------------------------------------


def _replace_skill_folder_with_link(project: Path, home: Path) -> Path:
    """Swap the installed codex skill folder for a symlink to a user folder holding their own SKILL.md."""
    skill = project / ".agents" / "skills" / "manage-codex-tracing"
    theirs = home / "my-skills" / "manage-codex-tracing"
    theirs.mkdir(parents=True)
    (theirs / "SKILL.md").write_text("the user's own skill")
    (skill / "SKILL.md").unlink()
    skill.rmdir()
    skill.symlink_to(theirs)
    return theirs / "SKILL.md"


@POSIX_ONLY
class TestSymlinkedSkillFolder:
    def test_refresh_leaves_the_users_file(self, install_dir, tmp_path, writable, capsys):
        assets.install_skills("codex", tmp_path / "proj")
        users = _replace_skill_folder_with_link(tmp_path / "proj", tmp_path)
        _age(install_dir)

        assert assets.refresh_stale() == []

        assert users.read_text() == "the user's own skill"
        assert "is a symlink setup did not make" in capsys.readouterr().out
        assert "codex" not in assets.load_manifest()["harnesses"], "no longer tracked"

    def test_uninstall_leaves_the_users_file(self, install_dir, tmp_path, capsys):
        assets.install_skills("codex", tmp_path / "proj")
        users = _replace_skill_folder_with_link(tmp_path / "proj", tmp_path)

        assert assets.uninstall_files("codex") is True

        assert users.read_text() == "the user's own skill"
        assert (tmp_path / "proj" / ".agents" / "skills" / "manage-codex-tracing").is_symlink()
        assert "is a symlink setup did not make" in capsys.readouterr().out
        assert "codex" not in assets.load_manifest()["harnesses"]

    def test_symlinked_claude_plugin_subfolder(self, install_dir, tmp_path, writable):
        root = assets.install_claude_plugin()
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "hooks.json").write_text("theirs")
        (root / "hooks" / "hooks.json").unlink()
        (root / "hooks").rmdir()
        (root / "hooks").symlink_to(elsewhere)
        _age(install_dir)

        assets.refresh_stale()
        assert (elsewhere / "hooks.json").read_text() == "theirs"
        assets.uninstall_files("claude-code")
        assert (elsewhere / "hooks.json").read_text() == "theirs"

    def test_real_folders_still_refresh_and_uninstall(self, install_dir, tmp_path, writable):
        assets.install_skills("codex", tmp_path / "proj")
        skill = tmp_path / "proj" / ".agents" / "skills" / "manage-codex-tracing" / "SKILL.md"
        skill.write_text("stale")
        _age(install_dir)
        assert assets.refresh_stale() == [str(skill)]
        assert assets.uninstall_files("codex") is True
        assert not skill.exists()


@pytest.mark.timeout(60)  # a non-reentrant or missing lock must fail, not hang
class TestLockSpansTheOperation:
    def test_lock_is_reentrant_in_one_thread(self):
        with assets._manifest_lock():
            with assets.manifest_transaction() as manifest:
                manifest["harnesses"]["x"] = {}
        assert "x" in assets.load_manifest()["harnesses"]

    def test_setup_during_uninstall_waits_and_stays_recorded(self, install_dir, tmp_path, monkeypatch):
        """Uninstall paused after deleting a skill file; setup of the same skill must wait, then be recorded."""
        project = tmp_path / "proj"
        assets.install_skills("codex", project)
        target = project / ".agents" / "skills" / "manage-codex-tracing" / "SKILL.md"
        code = (
            "import sys; from pathlib import Path; import core.setup as s; "
            f"s.INSTALL_DIR = Path({str(install_dir)!r}); "
            "from core.setup import assets; assets.install_skills('codex', Path(sys.argv[1]))"
        )
        env = dict(os.environ, PYTHONPATH=str(REPO_ROOT))
        seen = {}
        real_unlink = Path.unlink

        def unlink_then_race(self, *args, **kwargs):
            real_unlink(self, *args, **kwargs)
            if self == target and "proc" not in seen:
                # Mid-uninstall: start a concurrent setup and give it time to run if it could.
                seen["proc"] = subprocess.Popen([sys.executable, "-c", code, str(project)], env=env, cwd=tmp_path)
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline and seen["proc"].poll() is None and not target.exists():
                    time.sleep(0.05)
                seen["blocked"] = seen["proc"].poll() is None and not target.exists()

        monkeypatch.setattr(Path, "unlink", unlink_then_race)
        assert assets.uninstall_files("codex") is True
        monkeypatch.setattr(Path, "unlink", real_unlink)
        assert seen["proc"].wait(timeout=60) == 0

        assert seen["blocked"], "setup ran while uninstall held the manifest"
        assert target.is_file(), "setup ran after uninstall finished"
        assert str(target) in assets.recorded("codex"), "and its file is recorded, so the next uninstall removes it"
        assert assets.uninstall_files("codex") is True
        assert not target.exists()

    def test_dry_run_creates_no_lock_file(self, install_dir, tmp_path, monkeypatch):
        monkeypatch.setenv("ARIZE_DRY_RUN", "true")
        assets.install_skills("codex", tmp_path)
        assets.uninstall_files("codex")
        assets.refresh_stale()
        assert not install_dir.exists()
