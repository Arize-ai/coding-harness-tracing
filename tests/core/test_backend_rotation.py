"""Non-interactive setup over a saved harness entry replaces its backend from the environment (#95898)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from core.setup import runner
from tests.core.test_cli_commands import _cli, _config, _home

ARIZE = "ARIZE_BACKEND=arize\nARIZE_API_KEY=key-1\nARIZE_SPACE_ID=space-1\nARIZE_OTLP_ENDPOINT=otlp.test:443\n"
PHOENIX_KEY_WARNING = (
    "[arize] warning: PHOENIX_API_KEY is not set; if your Phoenix server has auth turned on, "
    "it will reject every span (logged as HTTP 401 in ~/.arize/harness/logs)."
)


def _setup(home: Path, creds: str, *args: str, **env: str):
    path = home / f"creds-{len(list(home.glob('creds-*')))}.env"
    path.write_text(creds)
    return _cli(home, "setup", "claude", "-y", *args, ARIZE_ENV_FILE=str(path), **env)


def _files(home: Path) -> dict:
    return {
        str(p): (p.stat().st_mtime_ns, p.read_bytes())
        for p in sorted(home.rglob("*"))
        if p.is_file() and "logs" not in p.parts and not p.name.startswith("creds-")
    }


@pytest.fixture
def home(tmp_path) -> Path:
    home = _home(tmp_path)
    first = _setup(home, ARIZE + "ARIZE_USER_ID=saved@example.com\n")
    assert first.returncode == 0, first.stdout + first.stderr
    config = _config(home)
    config["harnesses"]["claude-code"]["collector"] = {"port": 1234}
    config["harnesses"]["claude-code"]["project_name"] = "saved-project"
    config["logging"] = {"prompts": True, "tool_details": False, "tool_content": False}
    (home / ".arize" / "harness" / "config.json").write_text(json.dumps(config, indent=2))
    return home


def test_keys_are_replaced_and_the_rest_is_kept(home):
    result = _setup(home, ARIZE.replace("key-1", "key-2").replace("space-1", "space-2"))
    assert result.returncode == 0, result.stdout + result.stderr
    config = _config(home)
    assert config["harnesses"]["claude-code"] == {
        "project_name": "saved-project",
        "target": "arize",
        "endpoint": "otlp.test:443",
        "api_key": "key-2",
        "space_id": "space-2",
        "collector": {"port": 1234},
    }
    assert config["user_id"] == "saved@example.com"
    assert config["logging"] == {"prompts": True, "tool_details": False, "tool_content": False}


@pytest.mark.parametrize("keep", [{}, {"ARIZE_KEEP_SAVED_BACKEND": "1"}], ids=["replace", "keep"])
def test_env_file_project_name_replaces_saved_project(home, keep):
    result = _setup(home, ARIZE.replace("key-1", "key-2") + "ARIZE_PROJECT_NAME=new-proj\n", **keep)
    assert result.returncode == 0, result.stdout + result.stderr
    entry = _config(home)["harnesses"]["claude-code"]
    assert entry["project_name"] == "new-proj"
    assert entry["api_key"] == ("key-1" if keep else "key-2")


def test_user_id_is_replaced_when_set(home):
    result = _setup(home, ARIZE + "ARIZE_USER_ID=new@example.com\n")
    assert result.returncode == 0, result.stdout + result.stderr
    assert _config(home)["user_id"] == "new@example.com"


def test_switching_backend_leaves_no_old_fields(home):
    result = _setup(home, "ARIZE_BACKEND=phoenix\nPHOENIX_ENDPOINT=http://phoenix.test:6006\nPHOENIX_API_KEY=pk\n")
    assert result.returncode == 0, result.stdout + result.stderr
    assert _config(home)["harnesses"]["claude-code"] == {
        "project_name": "saved-project",
        "target": "phoenix",
        "endpoint": "http://phoenix.test:6006",
        "api_key": "pk",
        "collector": {"port": 1234},
    }
    assert "warning" not in result.stderr


def test_backend_flag_names_the_backend(home):
    result = _setup(home, "PHOENIX_ENDPOINT=http://phoenix.test:6006\nPHOENIX_API_KEY=pk\n", "--backend", "phoenix")
    assert result.returncode == 0, result.stdout + result.stderr
    assert _config(home)["harnesses"]["claude-code"]["target"] == "phoenix"


@pytest.mark.parametrize(
    "creds, message",
    [
        ("ARIZE_API_KEY=key-2\nARIZE_SPACE_ID=space-2\n", "needs ARIZE_BACKEND (or --backend)"),
        ("ARIZE_BACKEND=phoenix\nPHOENIX_API_KEY=pk\n", "A Phoenix endpoint is required"),
        ("ARIZE_BACKEND=arize\nARIZE_API_KEY=key-2\n", "An Arize space ID is required"),
    ],
)
def test_missing_value_fails_and_changes_nothing(home, creds, message):
    before = _files(home)
    result = _setup(home, creds)
    assert result.returncode != 0
    assert message in result.stderr
    assert _files(home) == before


def _whole_tree(root: Path) -> dict:
    """``{path: (mode, mtime, bytes)}`` for everything under ``root``."""
    out = {}
    for p in sorted(root.rglob("*")):
        st = p.lstat()
        out[str(p)] = (st.st_mode, st.st_mtime_ns, p.read_bytes() if p.is_file() else b"")
    return out


def _fails_and_leaves_home_alone(tmp_path: Path, home: Path, harness: str) -> None:
    creds = tmp_path / "rotate.env"
    creds.write_text("ARIZE_API_KEY=key-2\nARIZE_SPACE_ID=space-2\n")
    before = _whole_tree(home)
    result = _cli(home, "setup", harness, "-y", ARIZE_ENV_FILE=str(creds))
    assert result.returncode == 1, result.stdout + result.stderr
    assert "needs ARIZE_BACKEND (or --backend)" in result.stderr
    assert _whole_tree(home) == before


def test_missing_backend_leaves_a_legacy_yaml_home_unchanged(tmp_path):
    home = _home(tmp_path)
    harness_dir = home / ".arize" / "harness"
    harness_dir.mkdir(parents=True)
    (harness_dir / "config.yaml").write_text(
        "harnesses:\n"
        "  claude-code:\n"
        "    project_name: saved-project\n"
        "    target: arize\n"
        "    endpoint: otlp.test:443\n"
        "    api_key: key-1\n"
        "    space_id: space-1\n"
    )
    _fails_and_leaves_home_alone(tmp_path, home, "claude")


def test_missing_backend_leaves_a_codex_v1_home_unchanged(tmp_path):
    home = _home(tmp_path)
    harness_dir = home / ".arize" / "harness"
    (harness_dir / "bin").mkdir(parents=True)
    (harness_dir / "bin" / "codex").write_text("#!/bin/sh\n# Arize Codex proxy shim\nexec arize-codex-proxy \"$@\"\n")
    (harness_dir / "config.json").write_text(
        json.dumps({"harnesses": {"codex": {"project_name": "p", "target": "phoenix", "endpoint": "http://x:6006"}}})
    )
    (home / ".zshrc").write_text(
        "# >>> arize codex tracing PATH >>>\nexport PATH=\"$HOME/.arize/harness/bin:$PATH\"\n"
        "# <<< arize codex tracing PATH <<<\n"
    )
    (home / ".codex" / "config.toml").write_text(
        '[otel.exporter.otlp-http]\nendpoint = "http://127.0.0.1:4318/v1/logs"\nprotocol = "json"\n'
    )
    _fails_and_leaves_home_alone(tmp_path, home, "codex")


def test_phoenix_without_api_key_warns(home):
    result = _setup(home, "ARIZE_BACKEND=phoenix\nPHOENIX_ENDPOINT=http://phoenix.test:6006\n")
    assert result.returncode == 0, result.stdout + result.stderr
    assert PHOENIX_KEY_WARNING in result.stderr
    assert _config(home)["harnesses"]["claude-code"]["api_key"] == ""


def test_default_arize_endpoint_warns(home):
    result = _setup(home, "ARIZE_BACKEND=arize\nARIZE_API_KEY=key-2\nARIZE_SPACE_ID=space-2\n")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "[arize] warning: ARIZE_OTLP_ENDPOINT is not set; using the default Arize endpoint" in result.stderr
    assert _config(home)["harnesses"]["claude-code"]["endpoint"] == "otlp.arize.com:443"


def test_a_second_identical_run_writes_nothing(home):
    assert _setup(home, ARIZE).returncode == 0
    before = _files(home)
    result = _setup(home, ARIZE)
    assert result.returncode == 0, result.stdout + result.stderr
    assert _files(home) == before


KEEP_MESSAGE = (
    "Keeping the saved claude-code backend (ARIZE_KEEP_SAVED_BACKEND is set); "
    "unset it to replace the backend from the environment."
)


def test_install_script_update_keeps_the_saved_backend(home):
    before = _config(home)
    result = _setup(home, "ARIZE_API_KEY=key-2\nARIZE_SPACE_ID=space-2\n", ARIZE_KEEP_SAVED_BACKEND="1")
    assert result.returncode == 0, result.stdout + result.stderr
    assert KEEP_MESSAGE in result.stdout
    after = _config(home)
    assert after["harnesses"]["claude-code"] == before["harnesses"]["claude-code"]
    assert after["user_id"] == before["user_id"] == "saved@example.com"


def test_keep_guard_is_silent_on_first_setup(tmp_path):
    home = _home(tmp_path)
    result = _setup(home, ARIZE, ARIZE_KEEP_SAVED_BACKEND="1")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ARIZE_KEEP_SAVED_BACKEND" not in result.stdout + result.stderr


def test_keep_guard_is_silent_when_interactive(home):
    result = _cli(home, "setup", "claude", stdin="\n", ARIZE_KEEP_SAVED_BACKEND="1")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ARIZE_KEEP_SAVED_BACKEND" not in result.stdout + result.stderr


def test_first_setup_still_infers_the_backend(tmp_path):
    home = _home(tmp_path)
    result = _setup(home, "PHOENIX_ENDPOINT=http://phoenix.test:6006\n")
    assert result.returncode == 0, result.stdout + result.stderr
    assert _config(home)["harnesses"]["claude-code"]["target"] == "phoenix"
    assert "warning" not in result.stderr


# ---------------------------------------------------------------------------
# is_system_uv_install
# ---------------------------------------------------------------------------


@pytest.mark.skipif(os.name == "nt", reason="no system-wide install on Windows")
class TestIsSystemUvInstall:
    @pytest.fixture
    def prefix(self, tmp_path, monkeypatch) -> Path:
        prefix = tmp_path / "opt" / "tools" / "coding-harness-tracing"
        prefix.mkdir(parents=True)
        (prefix / "uv-receipt.toml").write_text("")
        monkeypatch.setattr(runner.sys, "prefix", str(prefix))
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        monkeypatch.setattr(runner.os, "geteuid", lambda: 1000)
        return prefix

    def test_read_only_outside_home(self, prefix, monkeypatch):
        monkeypatch.setattr(runner.os, "access", lambda path, mode: False)
        assert runner.is_system_uv_install() is True

    def test_writable_outside_home_is_per_user(self, prefix):
        assert runner.is_system_uv_install() is False

    def test_inside_home_is_per_user(self, prefix, monkeypatch):
        monkeypatch.setenv("HOME", str(prefix.parent))
        monkeypatch.setattr(runner.os, "access", lambda path, mode: False)
        assert runner.is_system_uv_install() is False

    def test_root_needs_only_outside_home(self, prefix, monkeypatch):
        monkeypatch.setattr(runner.os, "geteuid", lambda: 0)
        assert runner.is_system_uv_install() is True

    def test_no_receipt_is_not_uv(self, prefix, monkeypatch):
        (prefix / "uv-receipt.toml").unlink()
        monkeypatch.setattr(runner.os, "access", lambda path, mode: False)
        assert runner.is_system_uv_install() is False

    def test_windows_never(self, prefix, monkeypatch):
        monkeypatch.setattr(runner, "_is_windows", lambda: True)
        monkeypatch.setattr(runner.os, "access", lambda path, mode: False)
        assert runner.is_system_uv_install() is False
