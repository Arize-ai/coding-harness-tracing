"""Tests for tracing.kiro.install.install_noninteractive / uninstall_noninteractive."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest import mock

import pytest
import yaml

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT))

from tracing.kiro.constants import HOOK_EVENTS  # noqa: E402

FAKE_VENV_BIN = Path("/fake/venv/bin/arize-hook-kiro")
HOOK_CMD = str(FAKE_VENV_BIN)
FAKE_KIRO_CLI = "/fake/bin/kiro-cli"

PHOENIX_CREDS = {"endpoint": "http://x", "api_key": ""}


def _fail_if_called(*args, **kwargs):
    raise AssertionError("Interactive prompt was called in noninteractive path")


@pytest.fixture(autouse=True)
def _block_interactive(monkeypatch):
    """Noninteractive path must never reach a prompt."""
    monkeypatch.setattr("builtins.input", _fail_if_called)
    monkeypatch.setattr("getpass.getpass", _fail_if_called)


@pytest.fixture(autouse=True)
def _fake_stdout(monkeypatch):
    """Suppress TTY detection so info() doesn't emit ANSI codes."""
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)


@pytest.fixture(autouse=True)
def _mock_venv_bin(monkeypatch):
    monkeypatch.setattr("tracing.kiro.install.venv_bin", lambda name: FAKE_VENV_BIN)


@pytest.fixture(autouse=True)
def _no_dry_run(monkeypatch):
    monkeypatch.delenv("ARIZE_DRY_RUN", raising=False)


@pytest.fixture()
def kiro_env(tmp_path, monkeypatch):
    """Isolated environment: ~/.kiro/agents and ~/.arize/harness/ in tmp_path,
    with a fake kiro-cli resolvable on PATH and patched subprocess.run."""
    home = tmp_path / "home"
    home.mkdir()
    kiro_agents_dir = home / ".kiro" / "agents"
    kiro_agents_dir.mkdir(parents=True)
    harness_dir = home / ".arize" / "harness"
    harness_dir.mkdir(parents=True)

    # Redirect KIRO_AGENTS_DIR inside install module
    import tracing.kiro.install as inst

    monkeypatch.setattr(inst, "KIRO_AGENTS_DIR", kiro_agents_dir)

    # Patch core.setup paths
    import core.setup as setup

    monkeypatch.setattr(setup, "INSTALL_DIR", harness_dir)
    monkeypatch.setattr(setup, "CONFIG_FILE", harness_dir / "config.yaml")
    monkeypatch.setattr(setup, "VENV_DIR", harness_dir / "venv")
    monkeypatch.setattr(setup, "BIN_DIR", harness_dir / "bin")
    monkeypatch.setattr(setup, "RUN_DIR", harness_dir / "run")
    monkeypatch.setattr(setup, "LOG_DIR", harness_dir / "logs")
    monkeypatch.setattr(setup, "STATE_DIR", harness_dir / "state")

    # Also patch install module bindings
    monkeypatch.setattr(inst, "INSTALL_DIR", harness_dir)

    # Patch CONFIG_FILE in core.config so load_config/save_config use temp path
    import core.config as config_mod

    monkeypatch.setattr(config_mod, "CONFIG_FILE", str(harness_dir / "config.yaml"))

    # Default: kiro-cli is on PATH
    monkeypatch.setattr(inst.shutil, "which", lambda name: FAKE_KIRO_CLI if name == "kiro-cli" else None)
    monkeypatch.setattr(inst, "_macos_app_kiro_path", lambda: None)

    # Default: subprocess.run is a no-op success
    subproc = mock.MagicMock(return_value=mock.MagicMock(returncode=0, stderr="", stdout=""))
    monkeypatch.setattr(inst.subprocess, "run", subproc)

    return {
        "home": home,
        "agents_dir": kiro_agents_dir,
        "harness_dir": harness_dir,
        "config_file": harness_dir / "config.yaml",
        "subprocess_run": subproc,
    }


def _load_config(harness_dir: Path) -> dict:
    config_path = harness_dir / "config.yaml"
    if not config_path.exists():
        return {}
    with open(config_path) as f:
        return yaml.safe_load(f) or {}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_install_noninteractive_writes_agent_file_with_hooks(kiro_env):
    from tracing.kiro.install import install_noninteractive

    install_noninteractive(
        target="phoenix",
        credentials=PHOENIX_CREDS,
        project_name="proj",
        agent_name="my-agent",
    )

    agent_path = kiro_env["agents_dir"] / "my-agent.json"
    assert agent_path.exists()
    data = json.loads(agent_path.read_text())
    assert isinstance(data["hooks"], dict)
    assert set(data["hooks"].keys()) == set(HOOK_EVENTS)
    for event in HOOK_EVENTS:
        entries = data["hooks"][event]
        assert len(entries) == 1
        assert entries[0]["command"] == HOOK_CMD


def test_install_noninteractive_defaults_agent_name(kiro_env):
    from tracing.kiro.install import install_noninteractive

    install_noninteractive(
        target="phoenix",
        credentials=PHOENIX_CREDS,
        project_name="proj",
    )

    agent_path = kiro_env["agents_dir"] / "arize-traced.json"
    assert agent_path.exists()
    data = json.loads(agent_path.read_text())
    assert set(data["hooks"].keys()) == set(HOOK_EVENTS)


def test_install_noninteractive_persists_agent_name_in_config(kiro_env):
    from tracing.kiro.install import install_noninteractive

    install_noninteractive(
        target="phoenix",
        credentials=PHOENIX_CREDS,
        project_name="proj",
        agent_name="custom",
    )

    config = _load_config(kiro_env["harness_dir"])
    assert config["harnesses"]["kiro"]["agent_name"] == "custom"


def test_install_noninteractive_calls_set_default_when_flag_true(kiro_env):
    from tracing.kiro.install import install_noninteractive

    install_noninteractive(
        target="phoenix",
        credentials=PHOENIX_CREDS,
        project_name="proj",
        agent_name="chosen",
        set_default=True,
    )

    matching = [
        call
        for call in kiro_env["subprocess_run"].call_args_list
        if list(call[0][0]) == [FAKE_KIRO_CLI, "agent", "set-default", "chosen"]
    ]
    assert len(matching) == 1


def test_install_noninteractive_skips_set_default_when_flag_false(kiro_env):
    from tracing.kiro.install import install_noninteractive

    install_noninteractive(
        target="phoenix",
        credentials=PHOENIX_CREDS,
        project_name="proj",
        agent_name="chosen",
        set_default=False,
    )

    set_default_calls = [
        call
        for call in kiro_env["subprocess_run"].call_args_list
        if len(call[0]) > 0 and "set-default" in list(call[0][0])
    ]
    assert set_default_calls == []


def test_install_noninteractive_unregisters_existing_hooks_before_install(kiro_env):
    from tracing.kiro.install import install_noninteractive

    old_path = kiro_env["agents_dir"] / "old-agent.json"
    old_path.write_text(
        json.dumps(
            {
                "name": "old-agent",
                "description": "user-defined",
                "hooks": {
                    "stop": [{"command": HOOK_CMD}],
                },
            }
        )
    )

    install_noninteractive(
        target="phoenix",
        credentials=PHOENIX_CREDS,
        project_name="proj",
        agent_name="new-agent",
    )

    new_path = kiro_env["agents_dir"] / "new-agent.json"
    assert new_path.exists()
    new_data = json.loads(new_path.read_text())
    assert set(new_data["hooks"].keys()) == set(HOOK_EVENTS)
    for event in HOOK_EVENTS:
        entries = new_data["hooks"][event]
        assert any(h.get("command") == HOOK_CMD for h in entries)

    # Old agent either gone, or no longer has our hook command.
    if old_path.exists():
        old_data = json.loads(old_path.read_text())
        old_hooks = old_data.get("hooks", {})
        for event_list in old_hooks.values():
            for h in event_list:
                assert h.get("command") != HOOK_CMD


def test_install_noninteractive_fails_when_kiro_cli_missing(kiro_env, monkeypatch):
    import tracing.kiro.install as inst

    monkeypatch.setattr(inst.shutil, "which", lambda name: None)
    monkeypatch.setattr(inst, "_macos_app_kiro_path", lambda: None)

    from tracing.kiro.install import install_noninteractive

    with pytest.raises(RuntimeError, match="kiro-cli"):
        install_noninteractive(
            target="phoenix",
            credentials=PHOENIX_CREDS,
            project_name="proj",
            agent_name="any",
        )

    agents = list(kiro_env["agents_dir"].iterdir())
    assert agents == []


def test_install_noninteractive_rejects_unsafe_agent_name(kiro_env):
    from tracing.kiro.install import install_noninteractive

    with pytest.raises(ValueError):
        install_noninteractive(
            target="phoenix",
            credentials=PHOENIX_CREDS,
            project_name="proj",
            agent_name="../escape",
        )

    # No file written outside the agents directory or anywhere under home.
    home = kiro_env["home"]
    escapes = list(home.rglob("escape*"))
    assert escapes == []


def test_uninstall_noninteractive_removes_harness_entry(kiro_env):
    from tracing.kiro.install import install_noninteractive, uninstall_noninteractive

    install_noninteractive(
        target="phoenix",
        credentials=PHOENIX_CREDS,
        project_name="proj",
        agent_name="my-agent",
    )

    config = _load_config(kiro_env["harness_dir"])
    assert "kiro" in config.get("harnesses", {})

    uninstall_noninteractive()

    config = _load_config(kiro_env["harness_dir"])
    assert "kiro" not in config.get("harnesses", {})
