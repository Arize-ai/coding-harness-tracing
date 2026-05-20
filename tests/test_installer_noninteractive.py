"""Tests for install_noninteractive / uninstall_noninteractive on every harness.

Each test monkeypatches input, getpass, and the prompt helpers to raise,
proving the noninteractive path never calls them.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT))


def _fail_if_called(*args, **kwargs):
    raise AssertionError("Interactive prompt was called in noninteractive path")


@pytest.fixture(autouse=True)
def _block_interactive(monkeypatch):
    """Ensure no interactive prompts are reachable."""
    monkeypatch.setattr("builtins.input", _fail_if_called)
    monkeypatch.setattr("getpass.getpass", _fail_if_called)
    for mod_path in (
        "core.setup.prompt_backend",
        "core.setup.prompt_project_name",
        "core.setup.prompt_user_id",
        "core.setup.prompt_content_logging",
    ):
        monkeypatch.setattr(mod_path, _fail_if_called)


@pytest.fixture(autouse=True)
def _fake_stdout(monkeypatch):
    """Suppress TTY detection so info() doesn't emit ANSI codes."""
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

PHOENIX_CREDS = {"endpoint": "http://localhost:6006", "api_key": "test-key"}
ARIZE_CREDS = {"endpoint": "otlp.arize.com:443", "api_key": "az-key", "space_id": "sp-123"}


def _load_config(harness_dir: Path) -> dict:
    config_path = harness_dir / "config.yaml"
    if not config_path.exists():
        return {}
    with open(config_path) as f:
        return yaml.safe_load(f) or {}


def _assert_harness_entry(
    config: dict, harness_name: str, project_name: str, target: str, endpoint: str, space_id: str | None = None
):
    entry = config.get("harnesses", {}).get(harness_name)
    assert entry is not None, f"harnesses.{harness_name} missing from config"
    assert entry["project_name"] == project_name
    assert entry["target"] == target
    assert entry["endpoint"] == endpoint
    if space_id is not None:
        assert entry["space_id"] == space_id


# ---------------------------------------------------------------------------
# Claude Code
# ---------------------------------------------------------------------------


class TestClaudeCode:
    @pytest.fixture(autouse=True)
    def _setup(self, tmp_harness_dir, tmp_path, monkeypatch):
        self.harness_dir = tmp_harness_dir
        self.tmp_path = tmp_path
        # Redirect SETTINGS_FILE to temp
        settings_file = tmp_path / ".claude" / "settings.json"
        import tracing.claude_code.constants as cc

        monkeypatch.setattr(cc, "SETTINGS_FILE", settings_file)

        # Also patch the install module's own binding of SETTINGS_FILE
        import tracing.claude_code.install as inst

        monkeypatch.setattr(inst, "SETTINGS_FILE", settings_file)

        # Patch setup module paths
        import core.setup as setup

        monkeypatch.setattr(setup, "INSTALL_DIR", tmp_harness_dir)
        monkeypatch.setattr(setup, "CONFIG_FILE", tmp_harness_dir / "config.yaml")
        monkeypatch.setattr(setup, "VENV_DIR", tmp_harness_dir / "venv")
        monkeypatch.setattr(setup, "BIN_DIR", tmp_harness_dir / "bin")
        monkeypatch.setattr(setup, "RUN_DIR", tmp_harness_dir / "run")
        monkeypatch.setattr(setup, "LOG_DIR", tmp_harness_dir / "logs")
        monkeypatch.setattr(setup, "STATE_DIR", tmp_harness_dir / "state")

        # Patch CONFIG_FILE in core.config so load_config/save_config use temp path
        import core.config as config_mod

        monkeypatch.setattr(config_mod, "CONFIG_FILE", str(tmp_harness_dir / "config.yaml"))

        # Create plugin dir so harness_dir() resolves
        (tmp_harness_dir / "tracing" / "claude_code").mkdir(parents=True, exist_ok=True)

    def test_phoenix_install_uninstall(self):
        from tracing.claude_code.install import install_noninteractive, uninstall_noninteractive

        install_noninteractive(
            target="phoenix",
            credentials=PHOENIX_CREDS,
            project_name="my-claude-project",
        )

        config = _load_config(self.harness_dir)
        _assert_harness_entry(config, "claude-code", "my-claude-project", "phoenix", "http://localhost:6006")
        assert config.get("logging") is not None

        uninstall_noninteractive()
        config = _load_config(self.harness_dir)
        assert "claude-code" not in config.get("harnesses", {})

    def test_arize_install(self):
        from tracing.claude_code.install import install_noninteractive

        install_noninteractive(
            target="arize",
            credentials=ARIZE_CREDS,
            project_name="arize-project",
            user_id="user-1",
        )

        config = _load_config(self.harness_dir)
        _assert_harness_entry(config, "claude-code", "arize-project", "arize", "otlp.arize.com:443", space_id="sp-123")


# ---------------------------------------------------------------------------
# Codex
# ---------------------------------------------------------------------------


class TestCodex:
    @pytest.fixture(autouse=True)
    def _setup(self, tmp_harness_dir, tmp_path, monkeypatch):
        self.harness_dir = tmp_harness_dir
        self.tmp_path = tmp_path

        # Redirect codex-specific paths to temp
        import tracing.codex.constants as cx

        codex_dir = tmp_path / ".codex"
        monkeypatch.setattr(cx, "CODEX_CONFIG_DIR", codex_dir)
        monkeypatch.setattr(cx, "CODEX_CONFIG_FILE", codex_dir / "config.toml")
        monkeypatch.setattr(cx, "CODEX_ENV_FILE", codex_dir / "arize-env.sh")

        # Also patch the module-level imports in the install module
        import tracing.codex.install as inst

        monkeypatch.setattr(inst, "CODEX_CONFIG_DIR", codex_dir)
        monkeypatch.setattr(inst, "CODEX_CONFIG_FILE", codex_dir / "config.toml")
        monkeypatch.setattr(inst, "CODEX_ENV_FILE", codex_dir / "arize-env.sh")

        import core.setup as setup

        monkeypatch.setattr(setup, "INSTALL_DIR", tmp_harness_dir)
        monkeypatch.setattr(setup, "CONFIG_FILE", tmp_harness_dir / "config.yaml")
        monkeypatch.setattr(setup, "VENV_DIR", tmp_harness_dir / "venv")
        monkeypatch.setattr(setup, "BIN_DIR", tmp_harness_dir / "bin")
        monkeypatch.setattr(setup, "RUN_DIR", tmp_harness_dir / "run")
        monkeypatch.setattr(setup, "LOG_DIR", tmp_harness_dir / "logs")
        monkeypatch.setattr(setup, "STATE_DIR", tmp_harness_dir / "state")

        # Patch CONFIG_FILE in core.config so load_config/save_config use temp path
        import core.config as config_mod

        monkeypatch.setattr(config_mod, "CONFIG_FILE", str(tmp_harness_dir / "config.yaml"))

        # Also patch module-level bindings in the codex install module
        monkeypatch.setattr(inst, "CONFIG_FILE", tmp_harness_dir / "config.yaml")
        monkeypatch.setattr(inst, "BIN_DIR", tmp_harness_dir / "bin")

        # Mock buffer service so we don't actually start processes
        monkeypatch.setattr(inst, "buffer_start", lambda: True)
        monkeypatch.setattr(inst, "buffer_stop", lambda: None)
        monkeypatch.setattr(inst, "buffer_status", lambda: ("stopped", None, None))

    def test_phoenix_install_uninstall(self):
        from tracing.codex.install import install_noninteractive, uninstall_noninteractive

        install_noninteractive(
            target="phoenix",
            credentials=PHOENIX_CREDS,
            project_name="my-codex-project",
        )

        config = _load_config(self.harness_dir)
        _assert_harness_entry(config, "codex", "my-codex-project", "phoenix", "http://localhost:6006")
        assert "collector" in config["harnesses"]["codex"]
        assert config.get("logging") is not None

        uninstall_noninteractive()
        config = _load_config(self.harness_dir)
        assert "codex" not in config.get("harnesses", {})

    def test_arize_install(self):
        from tracing.codex.install import install_noninteractive

        install_noninteractive(
            target="arize",
            credentials=ARIZE_CREDS,
            project_name="codex-arize",
        )

        config = _load_config(self.harness_dir)
        _assert_harness_entry(config, "codex", "codex-arize", "arize", "otlp.arize.com:443", space_id="sp-123")


# ---------------------------------------------------------------------------
# Cursor
# ---------------------------------------------------------------------------


class TestCursor:
    @pytest.fixture(autouse=True)
    def _setup(self, tmp_harness_dir, tmp_path, monkeypatch):
        self.harness_dir = tmp_harness_dir
        self.tmp_path = tmp_path

        # Redirect hooks file to temp
        hooks_file = tmp_path / ".cursor" / "hooks.json"
        import tracing.cursor.constants as cur

        monkeypatch.setattr(cur, "HOOKS_FILE", hooks_file)

        # Also patch the install module's imported reference
        import tracing.cursor.install as inst

        monkeypatch.setattr(inst, "HOOKS_FILE", hooks_file)
        monkeypatch.setattr(inst, "INSTALL_DIR", tmp_harness_dir)

        import core.setup as setup

        monkeypatch.setattr(setup, "INSTALL_DIR", tmp_harness_dir)
        monkeypatch.setattr(setup, "CONFIG_FILE", tmp_harness_dir / "config.yaml")
        monkeypatch.setattr(setup, "VENV_DIR", tmp_harness_dir / "venv")
        monkeypatch.setattr(setup, "BIN_DIR", tmp_harness_dir / "bin")
        monkeypatch.setattr(setup, "RUN_DIR", tmp_harness_dir / "run")
        monkeypatch.setattr(setup, "LOG_DIR", tmp_harness_dir / "logs")
        monkeypatch.setattr(setup, "STATE_DIR", tmp_harness_dir / "state")

        # Patch CONFIG_FILE in core.config so load_config/save_config use temp path
        import core.config as config_mod

        monkeypatch.setattr(config_mod, "CONFIG_FILE", str(tmp_harness_dir / "config.yaml"))

    def test_phoenix_install_uninstall(self):
        from tracing.cursor.install import install_noninteractive, uninstall_noninteractive

        install_noninteractive(
            target="phoenix",
            credentials=PHOENIX_CREDS,
            project_name="my-cursor-project",
        )

        config = _load_config(self.harness_dir)
        _assert_harness_entry(config, "cursor", "my-cursor-project", "phoenix", "http://localhost:6006")
        assert config.get("logging") is not None

        uninstall_noninteractive()
        config = _load_config(self.harness_dir)
        assert "cursor" not in config.get("harnesses", {})

    def test_arize_install(self):
        from tracing.cursor.install import install_noninteractive

        install_noninteractive(
            target="arize",
            credentials=ARIZE_CREDS,
            project_name="cursor-arize",
            user_id="user-2",
        )

        config = _load_config(self.harness_dir)
        _assert_harness_entry(config, "cursor", "cursor-arize", "arize", "otlp.arize.com:443", space_id="sp-123")


# ---------------------------------------------------------------------------
# Copilot
# ---------------------------------------------------------------------------


class TestCopilot:
    @pytest.fixture(autouse=True)
    def _setup(self, tmp_harness_dir, tmp_path, monkeypatch):
        self.harness_dir = tmp_harness_dir
        self.tmp_path = tmp_path

        # Copilot writes hooks to cwd/.github/hooks — use tmp_path as cwd
        monkeypatch.chdir(tmp_path)

        import core.setup as setup

        monkeypatch.setattr(setup, "INSTALL_DIR", tmp_harness_dir)
        monkeypatch.setattr(setup, "CONFIG_FILE", tmp_harness_dir / "config.yaml")
        monkeypatch.setattr(setup, "VENV_DIR", tmp_harness_dir / "venv")
        monkeypatch.setattr(setup, "BIN_DIR", tmp_harness_dir / "bin")
        monkeypatch.setattr(setup, "RUN_DIR", tmp_harness_dir / "run")
        monkeypatch.setattr(setup, "LOG_DIR", tmp_harness_dir / "logs")
        monkeypatch.setattr(setup, "STATE_DIR", tmp_harness_dir / "state")

        # Patch CONFIG_FILE in core.config so load_config/save_config use temp path
        import core.config as config_mod

        monkeypatch.setattr(config_mod, "CONFIG_FILE", str(tmp_harness_dir / "config.yaml"))

    def test_phoenix_install_uninstall(self):
        from tracing.copilot.install import install_noninteractive, uninstall_noninteractive

        install_noninteractive(
            target="phoenix",
            credentials=PHOENIX_CREDS,
            project_name="my-copilot-project",
        )

        config = _load_config(self.harness_dir)
        _assert_harness_entry(config, "copilot", "my-copilot-project", "phoenix", "http://localhost:6006")
        assert config.get("logging") is not None

        uninstall_noninteractive()
        config = _load_config(self.harness_dir)
        assert "copilot" not in config.get("harnesses", {})

    def test_arize_install(self):
        from tracing.copilot.install import install_noninteractive

        install_noninteractive(
            target="arize",
            credentials=ARIZE_CREDS,
            project_name="copilot-arize",
        )

        config = _load_config(self.harness_dir)
        _assert_harness_entry(config, "copilot", "copilot-arize", "arize", "otlp.arize.com:443", space_id="sp-123")


# ---------------------------------------------------------------------------
# Gemini
# ---------------------------------------------------------------------------


class TestGemini:
    @pytest.fixture(autouse=True)
    def _setup(self, tmp_harness_dir, tmp_path, monkeypatch):
        self.harness_dir = tmp_harness_dir
        self.tmp_path = tmp_path

        # Redirect gemini settings to temp
        import tracing.gemini.constants as gc

        settings_dir = tmp_path / ".gemini"
        monkeypatch.setattr(gc, "SETTINGS_DIR", settings_dir)
        monkeypatch.setattr(gc, "SETTINGS_FILE", settings_dir / "settings.json")

        import core.setup as setup

        monkeypatch.setattr(setup, "INSTALL_DIR", tmp_harness_dir)
        monkeypatch.setattr(setup, "CONFIG_FILE", tmp_harness_dir / "config.yaml")
        monkeypatch.setattr(setup, "VENV_DIR", tmp_harness_dir / "venv")
        monkeypatch.setattr(setup, "BIN_DIR", tmp_harness_dir / "bin")
        monkeypatch.setattr(setup, "RUN_DIR", tmp_harness_dir / "run")
        monkeypatch.setattr(setup, "LOG_DIR", tmp_harness_dir / "logs")
        monkeypatch.setattr(setup, "STATE_DIR", tmp_harness_dir / "state")

        # Patch CONFIG_FILE in core.config so load_config/save_config use temp path
        import core.config as config_mod

        monkeypatch.setattr(config_mod, "CONFIG_FILE", str(tmp_harness_dir / "config.yaml"))

    def test_phoenix_install_uninstall(self):
        from tracing.gemini.install import install_noninteractive, uninstall_noninteractive

        install_noninteractive(
            target="phoenix",
            credentials=PHOENIX_CREDS,
            project_name="my-gemini-project",
        )

        config = _load_config(self.harness_dir)
        _assert_harness_entry(config, "gemini", "my-gemini-project", "phoenix", "http://localhost:6006")
        assert config.get("logging") is not None

        uninstall_noninteractive()
        config = _load_config(self.harness_dir)
        assert "gemini" not in config.get("harnesses", {})

    def test_arize_install(self):
        from tracing.gemini.install import install_noninteractive

        install_noninteractive(
            target="arize",
            credentials=ARIZE_CREDS,
            project_name="gemini-arize",
        )

        config = _load_config(self.harness_dir)
        _assert_harness_entry(config, "gemini", "gemini-arize", "arize", "otlp.arize.com:443", space_id="sp-123")
