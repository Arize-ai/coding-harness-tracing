"""scripts/mdm/macos/uninstall.sh: per-user uninstall, credentials files and the system install (part 3 of Arize-ai/arize#95899)."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
from conftest import INSTALL_SH, UNINSTALL_SH, _command_paths, _tree

from tests.core.test_cli_commands import _env

HARNESSES = ("claude-code", "cursor", "codex")
PATH_CONSTANTS = (
    "ARIZE_DIR",
    "TOOL_DIR",
    "PYTHON_DIR",
    "BIN_DIR",
    "HARNESS_BIN",
    "WHEEL_DIR",
    "WHEEL_MARK",
    "CREDENTIALS_DIR",
)


def _setup_user(machine, user) -> None:
    """Configure each harness for ``user`` with the system arize-harness, as part 2 will."""
    env = _env(user.home, ARIZE_API_KEY="k", ARIZE_SPACE_ID="s", ARIZE_USER_ID=f"{user.name}@example.com")
    env.pop("PYTHONPATH", None)  # run the system install's package, not this checkout
    tmp = str(machine.work / "tmp")  # off the system TMPDIR, or setup calls the install temporary
    env.update(TMPDIR=tmp, TEMP=tmp, TMP=tmp)
    for name in HARNESSES:
        result = subprocess.run(
            [str(machine.harness_bin), "setup", name, "-y"],
            env=env,
            cwd=user.home,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr


@pytest.fixture
def installed(machine, install_env):
    """The system install, both users set up, and their credentials files."""
    result = machine.run(INSTALL_SH, **install_env())
    assert result.returncode == 0, result.stdout + result.stderr
    machine.credentials_dir.mkdir(parents=True)
    for user in machine.users:
        _setup_user(machine, user)
        assert _command_paths(user.home) == {str(machine.harness_bin)}
        (machine.credentials_dir / f"{user.name}.env").write_text("ARIZE_API_KEY=k\n")
    return machine


def _assert_user_clean(user) -> None:
    assert _command_paths(user.home) == set()
    assert not (user.home / ".arize" / "harness").exists()


@pytest.mark.slow
def test_removes_everything_and_a_second_run_exits_0(installed):
    machine = installed
    (machine.bin_dir / "other-tool").write_text("#!/bin/sh\n")
    (machine.bin_dir / "other-link").symlink_to(machine.work)
    (machine.root / "opt" / "other").mkdir()
    (machine.credentials_dir / ".alice.env.new").write_text("ARIZE_API_KEY=k\n")  # interrupted install.sh write

    result = machine.run(UNINSTALL_SH)
    assert result.returncode == 0, result.stdout + result.stderr
    for user in machine.users:
        _assert_user_clean(user)
    assert {c["user"] for c in machine.calls()} == {"alice", "bob"}
    assert not machine.arize_dir.exists()
    assert not machine.credentials_dir.exists()
    assert sorted(p.name for p in machine.bin_dir.iterdir()) == ["other-link", "other-tool"]
    assert sorted(p.name for p in (machine.root / "opt").iterdir()) == ["other"]

    again = machine.run(UNINSTALL_SH)
    assert again.returncode == 0, again.stdout + again.stderr


@pytest.mark.slow
@pytest.mark.parametrize("force", [False, True], ids=["keeps-install", "force"])
def test_one_user_failing_exits_1_and_the_other_is_cleaned(installed, tmp_path, force):
    machine = installed
    wrapper = tmp_path / "failing-sudo"
    wrapper.mkdir()
    sudo = wrapper / "sudo"
    sudo.write_text(f'#!/bin/bash\ncase " $* " in *" -u alice "*) exit 1 ;; esac\nexec "{machine.stubs}/sudo" "$@"\n')
    sudo.chmod(0o755)

    path = f"{wrapper}:{machine.env()['PATH']}"
    result = machine.run(UNINSTALL_SH, PATH=path, ARIZE_MDM_FORCE="1" if force else None)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "uninstall failed for alice" in result.stderr
    _assert_user_clean(machine.user("bob"))
    assert _command_paths(machine.user("alice").home) == {str(machine.harness_bin)}
    assert not machine.credentials_dir.exists()
    if force:
        assert "remove the hooks of alice by hand" in result.stderr
        assert not machine.arize_dir.exists()
        assert not machine.harness_bin.is_symlink()
    else:
        assert "set FORCE=1" in result.stderr
        assert machine.harness_bin.exists()
        again = machine.run(UNINSTALL_SH, PATH=path)
        assert again.returncode == 1  # still failing, not a silent exit 0


@pytest.mark.parametrize(
    "overrides,message",
    [({"FAKE_UID": "501"}, "must run as root"), ({"FAKE_UNAME_S": "Linux"}, "macOS only")],
    ids=["not-root", "not-macos"],
)
def test_preflight_exits_2_with_nothing_changed(machine, overrides, message):
    tool = machine.arize_dir / "tools" / "coding-harness-tracing" / "bin" / "arize-harness"
    tool.parent.mkdir(parents=True)
    tool.write_text("#!/bin/sh\n")
    tool.chmod(0o755)
    machine.bin_dir.mkdir(parents=True)
    machine.harness_bin.symlink_to(tool)
    machine.credentials_dir.mkdir(parents=True)
    (machine.credentials_dir / "alice.env").write_text("ARIZE_API_KEY=k\n")
    before = _tree(machine.root)

    result = machine.run(UNINSTALL_SH, **overrides)
    assert result.returncode == 2
    assert message in result.stderr
    assert _tree(machine.root) == before
    assert machine.calls() == []


def _constants(script: Path) -> dict:
    text = script.read_text()
    found = {name: re.search(rf'^{name}="([^"]*)"', text, re.M) for name in PATH_CONSTANTS}
    assert all(found.values()), f"missing in {script.name}: {[n for n, m in found.items() if not m]}"
    return {name: m.group(1) for name, m in found.items()}


def test_path_constants_match_install():
    assert _constants(UNINSTALL_SH) == _constants(INSTALL_SH)
