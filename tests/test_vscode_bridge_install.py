"""Tests for core.vscode_bridge.install."""

from __future__ import annotations

import types
from unittest import mock

import pytest

from core.vscode_bridge.install import install, set_user_id, uninstall
from core.vscode_bridge.models import HARNESS_KEYS

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _arize_backend():
    return {
        "target": "arize",
        "endpoint": "https://otlp.arize.com",
        "api_key": "test-key",
        "space_id": "space-123",
    }


def _phoenix_backend():
    return {
        "target": "phoenix",
        "endpoint": "http://localhost:6006",
        "api_key": "",
        "space_id": None,
    }


def _base_request(harness="claude-code", **overrides):
    req = {
        "harness": harness,
        "backend": _arize_backend(),
        "project_name": "my-project",
        "user_id": "user-1",
        "with_skills": False,
        "logging": None,
    }
    req.update(overrides)
    return req


def _fake_module(install_msg="installed", uninstall_msg="uninstalled"):
    """Return a module-like object with install/uninstall_noninteractive."""

    def _install(**kwargs):
        print(install_msg)

    def _uninstall():
        print(uninstall_msg)

    mod = types.ModuleType("fake_install")
    mod.install_noninteractive = _install
    mod.uninstall_noninteractive = _uninstall
    return mod


# ---------------------------------------------------------------------------
# Install — success path per harness
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("harness", list(HARNESS_KEYS))
def test_install_success_per_harness(harness):
    fake = _fake_module(install_msg=f"{harness} ok")
    with mock.patch("core.vscode_bridge.install._import_installer", return_value=fake):
        result = install(_base_request(harness=harness))

    assert result["success"] is True
    assert result["error"] is None
    assert result["harness"] == harness
    assert any(f"{harness} ok" in line for line in result["logs"])


# ---------------------------------------------------------------------------
# Install — unknown harness
# ---------------------------------------------------------------------------


def test_install_unknown_harness():
    result = install(_base_request(harness="vim"))
    assert result["success"] is False
    assert result["error"] == "unknown_harness"
    assert result["harness"] is None


# ---------------------------------------------------------------------------
# Install — missing space_id on arize
# ---------------------------------------------------------------------------


def test_install_arize_missing_space_id():
    backend = _arize_backend()
    backend["space_id"] = None
    result = install(_base_request(backend=backend))
    assert result["success"] is False
    assert result["error"] == "missing_credentials"


def test_install_invalid_target():
    backend = _arize_backend()
    backend["target"] = "unknown-target"
    result = install(_base_request(backend=backend))
    assert result["success"] is False
    assert result["error"] == "missing_credentials"


# ---------------------------------------------------------------------------
# Install — phoenix backend (no space_id required)
# ---------------------------------------------------------------------------


def test_install_phoenix_backend():
    fake = _fake_module()
    with mock.patch("core.vscode_bridge.install._import_installer", return_value=fake):
        result = install(_base_request(backend=_phoenix_backend()))
    assert result["success"] is True


# ---------------------------------------------------------------------------
# Install — exception from underlying installer
# ---------------------------------------------------------------------------


def test_install_exception_captured():
    def _boom(**kwargs):
        raise RuntimeError("disk full")

    fake = _fake_module()
    fake.install_noninteractive = _boom

    with mock.patch("core.vscode_bridge.install._import_installer", return_value=fake):
        result = install(_base_request())

    assert result["success"] is False
    assert result["error"] == "install_failed"
    assert result["harness"] == "claude-code"
    assert any("disk full" in line for line in result["logs"])


# ---------------------------------------------------------------------------
# Install — logs captured from stdout/stderr
# ---------------------------------------------------------------------------


def test_install_logs_captured():
    import sys

    def _chatty(**kwargs):
        print("line-1")
        print("line-2", file=sys.stderr)

    fake = _fake_module()
    fake.install_noninteractive = _chatty

    with mock.patch("core.vscode_bridge.install._import_installer", return_value=fake):
        result = install(_base_request())

    assert result["success"] is True
    assert "line-1" in result["logs"]
    assert "line-2" in result["logs"]


# ---------------------------------------------------------------------------
# Install — credentials passed correctly (target stripped)
# ---------------------------------------------------------------------------


def test_install_passes_correct_kwargs():
    captured = {}

    def _spy(**kwargs):
        captured.update(kwargs)

    fake = _fake_module()
    fake.install_noninteractive = _spy

    backend = _arize_backend()
    logging_block = {"prompts": False, "tool_details": True, "tool_content": False}
    req = _base_request(
        backend=backend,
        with_skills=True,
        logging=logging_block,
        user_id="uid-42",
    )

    with mock.patch("core.vscode_bridge.install._import_installer", return_value=fake):
        result = install(req)

    assert result["success"] is True
    assert captured["target"] == "arize"
    assert "target" not in captured["credentials"]
    assert captured["credentials"]["api_key"] == "test-key"
    assert captured["credentials"]["space_id"] == "space-123"
    assert captured["project_name"] == "my-project"
    assert captured["user_id"] == "uid-42"
    assert captured["with_skills"] is True
    assert captured["logging_block"] == logging_block


# ---------------------------------------------------------------------------
# Install — kiro_options pass-through
# ---------------------------------------------------------------------------


def test_install_kiro_with_kiro_options_passes_them_through():
    captured = {}

    def _spy(**kwargs):
        captured.update(kwargs)

    fake = _fake_module()
    fake.install_noninteractive = _spy

    req = _base_request(
        harness="kiro",
        kiro_options={"agent_name": "custom", "set_default": True},
    )

    with mock.patch("core.vscode_bridge.install._import_installer", return_value=fake):
        result = install(req)

    assert result["success"] is True
    assert captured["agent_name"] == "custom"
    assert captured["set_default"] is True


def test_install_kiro_without_kiro_options_omits_them():
    captured = {}

    def _spy(**kwargs):
        captured.update(kwargs)

    fake = _fake_module()
    fake.install_noninteractive = _spy

    req = _base_request(harness="kiro", kiro_options=None)

    with mock.patch("core.vscode_bridge.install._import_installer", return_value=fake):
        result = install(req)

    assert result["success"] is True
    assert "agent_name" not in captured
    assert "set_default" not in captured


def test_install_non_kiro_does_not_receive_kiro_kwargs():
    captured = {}

    def _spy(**kwargs):
        captured.update(kwargs)

    fake = _fake_module()
    fake.install_noninteractive = _spy

    req = _base_request(harness="codex", kiro_options=None)

    with mock.patch("core.vscode_bridge.install._import_installer", return_value=fake):
        result = install(req)

    assert result["success"] is True
    assert "agent_name" not in captured
    assert "set_default" not in captured


# ---------------------------------------------------------------------------
# Install — copilot repo_path pass-through
# ---------------------------------------------------------------------------


def test_install_copilot_with_repo_path_forwards_to_installer():
    from core.vscode_bridge.models import build_install_request

    captured = {}

    def _spy(**kwargs):
        captured.update(kwargs)

    fake = _fake_module()
    fake.install_noninteractive = _spy

    req = build_install_request(
        harness="copilot",
        backend=_arize_backend(),
        project_name="copilot",
        repo_path="/repo/a",
    )

    with mock.patch("core.vscode_bridge.install._import_installer", return_value=fake):
        result = install(req)

    assert result["success"] is True
    assert captured["repo_path"] == "/repo/a"
    assert captured["target"] == "arize"
    assert captured["project_name"] == "copilot"


def test_install_copilot_without_repo_path_omits_kwarg():
    from core.vscode_bridge.models import build_install_request

    captured = {}

    def _spy(**kwargs):
        captured.update(kwargs)

    fake = _fake_module()
    fake.install_noninteractive = _spy

    req = build_install_request(
        harness="copilot",
        backend=_arize_backend(),
        project_name="copilot",
    )

    with mock.patch("core.vscode_bridge.install._import_installer", return_value=fake):
        result = install(req)

    assert result["success"] is True
    assert "repo_path" not in captured


def test_install_non_copilot_ignores_repo_path():
    captured = {}

    def _spy(**kwargs):
        captured.update(kwargs)

    fake = _fake_module()
    fake.install_noninteractive = _spy

    req = _base_request(harness="claude-code")
    req["repo_path"] = "/x"

    with mock.patch("core.vscode_bridge.install._import_installer", return_value=fake):
        result = install(req)

    assert result["success"] is True
    assert "repo_path" not in captured


# ---------------------------------------------------------------------------
# Uninstall — success per harness
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("harness", list(HARNESS_KEYS))
def test_uninstall_success_per_harness(harness):
    fake = _fake_module(uninstall_msg=f"{harness} removed")
    with mock.patch("core.vscode_bridge.install._import_installer", return_value=fake):
        result = uninstall(harness)

    assert result["success"] is True
    assert result["error"] is None
    assert result["harness"] == harness
    assert any(f"{harness} removed" in line for line in result["logs"])


# ---------------------------------------------------------------------------
# Uninstall — unknown harness
# ---------------------------------------------------------------------------


def test_uninstall_unknown_harness():
    result = uninstall("vim")
    assert result["success"] is False
    assert result["error"] == "unknown_harness"
    assert result["harness"] is None


# ---------------------------------------------------------------------------
# Uninstall — exception from underlying uninstaller
# ---------------------------------------------------------------------------


def test_uninstall_exception_captured():
    def _boom():
        raise OSError("permission denied")

    fake = _fake_module()
    fake.uninstall_noninteractive = _boom

    with mock.patch("core.vscode_bridge.install._import_installer", return_value=fake):
        result = uninstall("claude-code")

    assert result["success"] is False
    assert result["error"] == "install_failed"
    assert any("permission denied" in line for line in result["logs"])


# ---------------------------------------------------------------------------
# set_user_id
# ---------------------------------------------------------------------------


def test_set_user_id_writes_top_level_key(tmp_path, monkeypatch):
    import core.config as config_mod

    config_path = tmp_path / "config.yaml"
    config_path.write_text("harnesses: {}\n")
    monkeypatch.setattr(config_mod, "CONFIG_FILE", str(config_path))

    result = set_user_id("alice")

    assert result["success"] is True
    assert result["harness"] is None
    config = config_mod.load_config()
    assert config["user_id"] == "alice"


def test_set_user_id_empty_string_clears_key(tmp_path, monkeypatch):
    import core.config as config_mod

    config_path = tmp_path / "config.yaml"
    config_path.write_text("harnesses: {}\nuser_id: alice\n")
    monkeypatch.setattr(config_mod, "CONFIG_FILE", str(config_path))

    result = set_user_id("")

    assert result["success"] is True
    config = config_mod.load_config()
    assert "user_id" not in config


def test_set_user_id_failure_captured(monkeypatch):
    def _boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("core.config.save_config", _boom)

    result = set_user_id("bob")
    assert result["success"] is False
    assert result["error"] == "set_user_id_failed"
    assert any("disk full" in line for line in result["logs"])
