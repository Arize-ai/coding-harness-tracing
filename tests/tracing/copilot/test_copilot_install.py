"""Tests for Copilot entry points in pyproject.toml.

Shell/batch install-flow tests that previously lived here have been removed;
equivalent coverage now lives in tests/test_install_copilot.py (Wave 2) which
tests the Python install()/uninstall() functions against a fake home.

The embedded Python JSON-generation functional tests were also removed — the
logic they tested (VS Code + CLI hooks JSON merging) now lives in
tracing.copilot/install.py and is exercised by test_install_copilot.py.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).parent.parent.parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"


# ---------------------------------------------------------------------------
# pyproject.toml entry point tests
# ---------------------------------------------------------------------------


class TestCopilotEntryPoints:
    """Verify all 7 Copilot entry points (6 hooks + 1 setup) in pyproject.toml."""

    @pytest.fixture(autouse=True)
    def _load_pyproject(self):
        self.text = PYPROJECT.read_text()

    def test_session_start_entry_point(self):
        assert 'arize-hook-copilot-session-start = "tracing.copilot.hooks.handlers:session_start"' in self.text

    def test_user_prompt_entry_point(self):
        assert 'arize-hook-copilot-user-prompt = "tracing.copilot.hooks.handlers:user_prompt_submitted"' in self.text

    def test_pre_tool_entry_point(self):
        assert 'arize-hook-copilot-pre-tool = "tracing.copilot.hooks.handlers:pre_tool_use"' in self.text

    def test_post_tool_entry_point(self):
        assert 'arize-hook-copilot-post-tool = "tracing.copilot.hooks.handlers:post_tool_use"' in self.text

    def test_stop_entry_point(self):
        assert 'arize-hook-copilot-stop = "tracing.copilot.hooks.handlers:stop"' in self.text

    def test_subagent_stop_entry_point(self):
        assert 'arize-hook-copilot-subagent-stop = "tracing.copilot.hooks.handlers:subagent_stop"' in self.text

    def test_setup_entry_point(self):
        assert 'arize-setup-copilot = "core.setup.copilot:main"' in self.text

    def test_exactly_6_hook_entry_points(self):
        """There should be exactly 6 copilot hook entry points."""
        count = self.text.count("arize-hook-copilot-")
        assert count == 6, f"Expected 6 copilot hook entries, got {count}"

    def test_entry_points_importable(self):
        """All referenced handler functions should be importable."""
        from tracing.copilot.hooks.handlers import (
            post_tool_use,
            pre_tool_use,
            session_start,
            stop,
            subagent_stop,
            user_prompt_submitted,
        )

        for fn in [
            session_start,
            user_prompt_submitted,
            pre_tool_use,
            post_tool_use,
            stop,
            subagent_stop,
        ]:
            assert callable(fn)


# ---------------------------------------------------------------------------
# install_noninteractive / uninstall_noninteractive: per-repo path tracking
# ---------------------------------------------------------------------------


PHOENIX_CREDS = {"endpoint": "http://localhost:6006", "api_key": ""}


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    """Point all config writes at a temp file and silence stdout."""
    config_path = tmp_path / ".arize" / "harness" / "config.yaml"

    import core.config as config_mod
    import core.constants as constants_mod
    import core.setup as setup_mod

    monkeypatch.setattr(setup_mod, "INSTALL_DIR", tmp_path / ".arize" / "harness")
    monkeypatch.setattr(setup_mod, "VENV_DIR", tmp_path / ".arize" / "harness" / "venv")
    monkeypatch.setattr(setup_mod, "CONFIG_FILE", config_path)
    monkeypatch.setattr(setup_mod, "BIN_DIR", tmp_path / ".arize" / "harness" / "bin")
    monkeypatch.setattr(setup_mod, "RUN_DIR", tmp_path / ".arize" / "harness" / "run")
    monkeypatch.setattr(setup_mod, "LOG_DIR", tmp_path / ".arize" / "harness" / "logs")
    monkeypatch.setattr(setup_mod, "STATE_DIR", tmp_path / ".arize" / "harness" / "state")

    monkeypatch.setattr(constants_mod, "BASE_DIR", tmp_path / ".arize" / "harness")
    monkeypatch.setattr(constants_mod, "CONFIG_FILE", config_path)

    monkeypatch.setattr(config_mod, "CONFIG_FILE", str(config_path))

    fake_stdout = type(
        "FakeOut",
        (),
        {
            "isatty": lambda self: False,
            "write": lambda self, s: None,
            "flush": lambda self: None,
        },
    )()
    monkeypatch.setattr("sys.stdout", fake_stdout)

    return config_path


def _read_config(config_path: Path) -> dict:
    return yaml.safe_load(config_path.read_text()) or {}


def test_install_noninteractive_default_repo_path_is_cwd(tmp_path, monkeypatch, isolated_config):
    from tracing.copilot.install import install_noninteractive

    monkeypatch.chdir(tmp_path)

    install_noninteractive(
        target="phoenix",
        credentials=PHOENIX_CREDS,
        project_name="copilot",
    )

    assert (tmp_path / ".github" / "hooks" / "hooks.json").is_file()
    config = _read_config(isolated_config)
    assert config["harnesses"]["copilot"]["repo_paths"] == [str(tmp_path.resolve())]


def test_install_noninteractive_explicit_repo_path(tmp_path, monkeypatch, isolated_config):
    from tracing.copilot.install import install_noninteractive

    # cwd is unrelated; the install target is repo_path.
    other_cwd = tmp_path / "elsewhere"
    other_cwd.mkdir()
    monkeypatch.chdir(other_cwd)

    repo = tmp_path / "target-repo"
    repo.mkdir()

    install_noninteractive(
        target="phoenix",
        credentials=PHOENIX_CREDS,
        project_name="copilot",
        repo_path=str(repo),
    )

    assert (repo / ".github" / "hooks" / "hooks.json").is_file()
    assert not (other_cwd / ".github" / "hooks" / "hooks.json").exists()
    config = _read_config(isolated_config)
    assert config["harnesses"]["copilot"]["repo_paths"] == [str(repo.resolve())]


def test_install_noninteractive_appends_path_without_duplicates(tmp_path, monkeypatch, isolated_config):
    from tracing.copilot.install import install_noninteractive

    monkeypatch.chdir(tmp_path)

    repo_a = tmp_path / "repo_a"
    repo_a.mkdir()
    repo_b = tmp_path / "repo_b"
    repo_b.mkdir()

    install_noninteractive(
        target="phoenix",
        credentials=PHOENIX_CREDS,
        project_name="copilot",
        repo_path=str(repo_a),
    )
    install_noninteractive(
        target="phoenix",
        credentials=PHOENIX_CREDS,
        project_name="copilot",
        repo_path=str(repo_a),
    )

    config = _read_config(isolated_config)
    assert config["harnesses"]["copilot"]["repo_paths"] == [str(repo_a.resolve())]

    install_noninteractive(
        target="phoenix",
        credentials=PHOENIX_CREDS,
        project_name="copilot",
        repo_path=str(repo_b),
    )

    config = _read_config(isolated_config)
    assert config["harnesses"]["copilot"]["repo_paths"] == [
        str(repo_a.resolve()),
        str(repo_b.resolve()),
    ]


def test_install_noninteractive_preserves_existing_repo_paths_through_reconfigure(
    tmp_path, monkeypatch, isolated_config
):
    from tracing.copilot.install import install_noninteractive

    monkeypatch.chdir(tmp_path)

    repo_a = tmp_path / "repo_a"
    repo_a.mkdir()

    install_noninteractive(
        target="phoenix",
        credentials=PHOENIX_CREDS,
        project_name="copilot",
        repo_path=str(repo_a),
    )
    # Re-install with a different project_name; this exercises the
    # merge_harness_entry branch.
    install_noninteractive(
        target="phoenix",
        credentials=PHOENIX_CREDS,
        project_name="my-renamed-copilot",
        repo_path=str(repo_a),
    )

    config = _read_config(isolated_config)
    entry = config["harnesses"]["copilot"]
    assert entry["project_name"] == "my-renamed-copilot"
    assert entry["repo_paths"] == [str(repo_a.resolve())]


def test_uninstall_noninteractive_removes_hooks_from_all_paths(tmp_path, monkeypatch, isolated_config):
    from tracing.copilot.install import install_noninteractive, uninstall_noninteractive

    monkeypatch.chdir(tmp_path)

    repo_a = tmp_path / "repo_a"
    repo_a.mkdir()
    repo_b = tmp_path / "repo_b"
    repo_b.mkdir()

    install_noninteractive(
        target="phoenix",
        credentials=PHOENIX_CREDS,
        project_name="copilot",
        repo_path=str(repo_a),
    )
    install_noninteractive(
        target="phoenix",
        credentials=PHOENIX_CREDS,
        project_name="copilot",
        repo_path=str(repo_b),
    )

    hooks_a = repo_a / ".github" / "hooks" / "hooks.json"
    hooks_b = repo_b / ".github" / "hooks" / "hooks.json"
    assert hooks_a.is_file()
    assert hooks_b.is_file()

    uninstall_noninteractive()

    assert not hooks_a.exists()
    assert not hooks_b.exists()

    config = _read_config(isolated_config)
    assert "copilot" not in config.get("harnesses", {})


def test_uninstall_noninteractive_falls_back_to_cwd_when_no_paths(tmp_path, monkeypatch, isolated_config):
    from tracing.copilot.install import uninstall_noninteractive

    isolated_config.parent.mkdir(parents=True, exist_ok=True)
    seed = {
        "harnesses": {
            "copilot": {
                "project_name": "copilot",
                "target": "phoenix",
                "endpoint": "http://localhost:6006",
                "api_key": "",
            }
        }
    }
    isolated_config.write_text(yaml.safe_dump(seed))

    # Seed cwd with a hooks.json that contains a copilot entry.
    monkeypatch.chdir(tmp_path)
    hooks_dir = tmp_path / ".github" / "hooks"
    hooks_dir.mkdir(parents=True)
    hooks_file = hooks_dir / "hooks.json"

    # Compute the actual cmd path the installer would target so the entry
    # matches what _uninstall_hooks looks for.
    from core.setup import venv_bin
    from tracing.copilot.constants import HOOK_EVENTS

    hooks_payload = {
        "hooks": {
            event: [{"type": "command", "command": str(venv_bin(entry_point))}]
            for event, entry_point in HOOK_EVENTS.items()
        }
    }
    hooks_file.write_text(json.dumps(hooks_payload, indent=2) + "\n")

    uninstall_noninteractive()

    assert not hooks_file.exists()

    config = _read_config(isolated_config)
    assert "copilot" not in config.get("harnesses", {})


def test_uninstall_noninteractive_tolerates_missing_repo(tmp_path, monkeypatch, isolated_config):
    from tracing.copilot.install import uninstall_noninteractive

    monkeypatch.chdir(tmp_path)

    isolated_config.parent.mkdir(parents=True, exist_ok=True)
    missing = tmp_path / "gone"
    seed = {
        "harnesses": {
            "copilot": {
                "project_name": "copilot",
                "target": "phoenix",
                "endpoint": "http://localhost:6006",
                "api_key": "",
                "repo_paths": [str(missing)],
            }
        }
    }
    isolated_config.write_text(yaml.safe_dump(seed))

    # Should not raise — missing-dir is tolerated.
    uninstall_noninteractive()

    config = _read_config(isolated_config)
    assert "copilot" not in config.get("harnesses", {})
