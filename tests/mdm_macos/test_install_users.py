"""scripts/mdm/macos/install.sh: credentials files and per-user setup (part 2 of Arize-ai/arize#95899)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Dict

import pytest
from conftest import INSTALL_SH, SPAN_LINE, TEST_KEY, _command_paths, _snapshot, _tree

from tests.core.test_system_install import _fire_turn

HARNESS_NAMES = ["claude-code", "cursor", "codex"]
# The skipped accounts are mapped too, so only list_users keeps them out.
EMAILS = {
    "alice": "alice@example.com",
    "bob": "bob@example.com",
    "root": "root@example.com",
    "_svcadmin": "svc@example.com",
    "ghost": "ghost@example.com",
    "svcdaemon": "daemon@example.com",
}


def _email_map(machine, emails: Dict[str, str]) -> str:
    path = machine.work / "emails.txt"
    lines = ["# username email", ""] + [f"{u} {e}" for u, e in emails.items()]
    path.write_text("\n".join(lines) + "\n")
    return str(path)


def _install(machine, install_env, emails: Dict[str, str] = EMAILS, **overrides) -> subprocess.CompletedProcess:
    (machine.root / "tmp").mkdir(exist_ok=True)  # TMPDIR for _fire_turn
    env = install_env(ARIZE_MDM_EMAIL_MAP_FILE=_email_map(machine, emails), **overrides)
    result = machine.run(INSTALL_SH, **env)
    assert TEST_KEY not in result.stdout + result.stderr
    return result


def _config(home: Path) -> dict:
    return json.loads((home / ".arize" / "harness" / "config.json").read_text())


def _homes(machine) -> list:
    return [u.home for u in machine.users]


def _state(machine) -> tuple:
    roots = [*_homes(machine), machine.credentials_dir]
    return [_tree(r) for r in roots], _snapshot(*roots)


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["missing", "unreadable"])
def test_bad_email_map_exits_2(machine, preflight_env, kind):
    path = machine.work / "emails.txt"
    if kind == "unreadable":
        path.write_text("alice alice@example.com\n")
        path.chmod(0)
    result = machine.run(INSTALL_SH, **preflight_env(ARIZE_MDM_EMAIL_MAP_FILE=str(path)))
    assert result.returncode == 2, result.stdout + result.stderr
    assert "email map file missing or unreadable" in result.stderr
    assert not machine.credentials_dir.exists()


# ---------------------------------------------------------------------------
# Per-user setup
# ---------------------------------------------------------------------------


@pytest.mark.slow
class TestUserSetup:
    def test_sets_up_each_user_from_their_own_credentials(self, machine, install_env):
        result = _install(machine, install_env)
        assert result.returncode == 0, result.stdout + result.stderr

        assert sorted(p.name for p in machine.credentials_dir.iterdir()) == ["alice.env", "bob.env"]
        assert {c["user"] for c in machine.calls() if c["cmd"] == "sudo"} == {"alice", "bob"}
        for user in machine.users:
            creds = machine.credentials_dir / f"{user.name}.env"
            assert creds.stat().st_mode & 0o777 == 0o400
            assert machine.owners()[str(creds.resolve())]["value"] == {"user": "root", "group": "wheel"}
            assert machine.acls()[str(creds.resolve())]["value"] == [f"{user.name} allow read"]
            assert f"ARIZE_USER_ID='{EMAILS[user.name]}'" in creds.read_text()

            assert _command_paths(user.home) == {str(machine.harness_bin)}
            config = _config(user.home)
            assert config["user_id"] == EMAILS[user.name]
            assert {n: h["project_name"] for n, h in config["harnesses"].items()} == {
                n: f"harness/{EMAILS[user.name]}" for n in HARNESS_NAMES
            }
            assert config["logging"] == {"prompts": False, "tool_details": False, "tool_content": False}

            log = _fire_turn(machine.root, machine.harness_bin, user.home, user.home, "claude-code")
            assert SPAN_LINE in log.read_text(), log.read_text()

    def test_rerun_changes_no_files(self, machine, install_env):
        assert _install(machine, install_env).returncode == 0
        before = _state(machine)
        again = _install(machine, install_env)
        assert again.returncode == 0, again.stdout + again.stderr
        assert _state(machine) == before

    def test_new_key_changes_only_credentials_and_saved_keys(self, machine, install_env):
        assert _install(machine, install_env).returncode == 0
        before = _snapshot(*_homes(machine), machine.credentials_dir)
        configs = {u.name: _config(u.home) for u in machine.users}
        result = _install(machine, install_env, ARIZE_API_KEY="mdm-rotated-key")
        assert result.returncode == 0, result.stdout + result.stderr

        after = _snapshot(*_homes(machine), machine.credentials_dir)
        changed = {Path(p) for p in after if after[p] != before.get(p)}
        expected = {machine.credentials_dir / f"{u.name}.env" for u in machine.users}
        expected |= {u.home / ".arize" / "harness" / "config.json" for u in machine.users}
        assert changed == expected
        assert set(after) == set(before)
        for user in machine.users:
            config = _config(user.home)
            for name, entry in config["harnesses"].items():
                assert entry.pop("api_key") == "mdm-rotated-key"
                assert configs[user.name]["harnesses"][name].pop("api_key") == TEST_KEY
            assert config == configs[user.name]

    def test_user_without_email_is_skipped_and_log_settings_pass_through(self, machine, install_env):
        alice = machine.user("alice")
        result = _install(machine, install_env, emails={"alice": EMAILS["alice"]}, ARIZE_LOG_PROMPTS="true")
        assert result.returncode == 0, result.stdout + result.stderr
        assert result.stdout.count("skipping bob: no email") == 1
        assert [p.name for p in machine.credentials_dir.iterdir()] == ["alice.env"]
        assert not (machine.user("bob").home / ".arize").exists()
        assert "ARIZE_LOG_PROMPTS='true'" in (machine.credentials_dir / "alice.env").read_text()
        assert _config(alice.home)["logging"]["prompts"] is True

    def test_setup_failure_continues_and_keeps_the_system_install(self, machine, install_env):
        bob = machine.user("bob")
        (bob.home / ".cursor").write_text("not a folder")  # cursor's setup can't write its hooks
        result = _install(machine, install_env)
        assert result.returncode == 1, result.stdout + result.stderr
        assert "setup cursor failed for bob" in result.stderr
        assert result.stderr.count("failed for") == 1

        version = subprocess.run([str(machine.harness_bin), "--version"], capture_output=True, text=True, timeout=60)
        assert version.returncode == 0, version.stderr
        assert (machine.arize_dir / "bin" / "uv").is_file()
        assert {"claude-code", "codex"} <= set(_config(bob.home)["harnesses"])
        alice = machine.user("alice")
        assert set(_config(alice.home)["harnesses"]) == set(HARNESS_NAMES)
        assert _command_paths(alice.home) == {str(machine.harness_bin)}
