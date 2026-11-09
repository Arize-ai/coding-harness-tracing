"""Tests for tracing.kiro.install — install/uninstall flow for Kiro CLI hooks."""

from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

import pytest

from tracing.kiro.constants import AGENT_SKELETON, DEFAULT_AGENT_NAME, HOOK_EVENTS

# The hook command string that install.py should write into agent configs.
# What hook_command_for() gives a uv tool install at /fake/bin.
HOOK_CMD = "'/fake/bin/arize-harness' hook kiro"

# Derive expected keys from the constant so the test stays in sync.
EXPECTED_SKELETON_KEYS = set(AGENT_SKELETON.keys())


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _mock_hook_command(monkeypatch):
    """Make hook_command_for() always return our fake command."""
    monkeypatch.setattr("tracing.kiro.install.hook_command_for", lambda *args, **kwargs: HOOK_CMD)


@pytest.fixture(autouse=True)
def _no_dry_run(monkeypatch):
    """Default: dry-run is off."""
    monkeypatch.delenv("ARIZE_DRY_RUN", raising=False)


@pytest.fixture()
def agents_dir(tmp_path, monkeypatch):
    """Redirect KIRO_AGENTS_DIR to a temp directory."""
    d = tmp_path / "agents"
    d.mkdir()
    monkeypatch.setattr("tracing.kiro.install.KIRO_AGENTS_DIR", d)
    return d


@pytest.fixture()
def mock_subprocess():
    """Patch subprocess.run to prevent real CLI calls."""
    with mock.patch("tracing.kiro.install.subprocess.run") as m:
        m.return_value = mock.MagicMock(returncode=0, stderr="", stdout="")
        yield m


@pytest.fixture()
def mock_shutil_which():
    """Patch shutil.which inside install module."""
    with mock.patch("tracing.kiro.install.shutil.which") as m:
        m.return_value = None  # default: kiro-cli not found
        yield m


# ---------------------------------------------------------------------------
# TestRegisterKiroHooks
# ---------------------------------------------------------------------------


class TestRegisterKiroHooks:
    """Tests for _register_kiro_hooks — writing hook entries into agent JSON."""

    def test_creates_new_agent_file(self, agents_dir, mock_subprocess, mock_shutil_which):
        from tracing.kiro.install import _register_kiro_hooks

        agent_path = agents_dir / "arize-traced.json"
        _register_kiro_hooks(agent_path, "arize-traced")

        assert agent_path.exists()
        data = json.loads(agent_path.read_text())

        # All 12 skeleton keys present
        assert set(data.keys()) == EXPECTED_SKELETON_KEYS
        assert data["name"] == "arize-traced"
        assert data["description"] == AGENT_SKELETON["description"]

        # hooks has one key per event
        assert set(data["hooks"].keys()) == set(HOOK_EVENTS)

    def test_each_event_has_hook_entry(self, agents_dir, mock_subprocess, mock_shutil_which):
        from tracing.kiro.install import _register_kiro_hooks

        agent_path = agents_dir / "arize-traced.json"
        _register_kiro_hooks(agent_path, "arize-traced")

        data = json.loads(agent_path.read_text())
        for event in HOOK_EVENTS:
            entries = data["hooks"][event]
            assert any(
                h.get("command") == HOOK_CMD for h in entries
            ), f"Event {event} missing hook entry with command={HOOK_CMD}"

    def test_edits_existing_agent_in_place(self, agents_dir, mock_subprocess, mock_shutil_which):
        from tracing.kiro.install import _register_kiro_hooks

        agent_path = agents_dir / "my-agent.json"
        existing = {
            "name": "my-agent",
            "description": "User's custom agent",
            "prompt": "Be helpful",
            "mcpServers": {},
            "tools": ["read", "shell"],
            "toolAliases": {},
            "allowedTools": [],
            "resources": [],
            "hooks": {
                "userPromptSubmit": [{"command": "/usr/bin/other"}],
            },
            "toolsSettings": {},
            "includeMcpJson": True,
            "model": None,
        }
        agent_path.write_text(json.dumps(existing))

        _register_kiro_hooks(agent_path, "my-agent")

        data = json.loads(agent_path.read_text())
        # User's tools preserved
        assert data["tools"] == ["read", "shell"]
        # User's existing hook preserved alongside ours
        ups = data["hooks"]["userPromptSubmit"]
        assert any(h.get("command") == "/usr/bin/other" for h in ups)
        assert any(h.get("command") == HOOK_CMD for h in ups)

    def test_idempotent(self, agents_dir, mock_subprocess, mock_shutil_which):
        from tracing.kiro.install import _register_kiro_hooks

        agent_path = agents_dir / "arize-traced.json"
        _register_kiro_hooks(agent_path, "arize-traced")
        _register_kiro_hooks(agent_path, "arize-traced")

        data = json.loads(agent_path.read_text())
        for event in HOOK_EVENTS:
            our_entries = [h for h in data["hooks"][event] if h.get("command") == HOOK_CMD]
            assert len(our_entries) == 1, f"Event {event} has {len(our_entries)} entries, expected 1"

    def test_dry_run_no_write(self, agents_dir, monkeypatch, mock_subprocess, mock_shutil_which):
        from tracing.kiro.install import _register_kiro_hooks

        monkeypatch.setenv("ARIZE_DRY_RUN", "true")
        agent_path = agents_dir / "arize-traced.json"
        _register_kiro_hooks(agent_path, "arize-traced")

        assert not agent_path.exists()


# ---------------------------------------------------------------------------
# TestUnregisterAllKiroHooks
# ---------------------------------------------------------------------------


class TestUnregisterAllKiroHooks:
    """Tests for _unregister_all_kiro_hooks — cleaning hooks from agent files."""

    def _write_agent(self, path: Path, data: dict) -> None:
        path.parent.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2) + "\n")

    def test_removes_only_our_entries(self, agents_dir):
        from tracing.kiro.install import _unregister_all_kiro_hooks

        agent_path = agents_dir / "mixed.json"
        self._write_agent(
            agent_path,
            {
                "name": "mixed",
                "description": "custom agent",
                "hooks": {
                    "agentSpawn": [
                        {"command": HOOK_CMD},
                        {"command": "/usr/bin/other-hook"},
                    ],
                    "stop": [{"command": HOOK_CMD}],
                },
            },
        )

        _unregister_all_kiro_hooks()

        data = json.loads(agent_path.read_text())
        # Our entry removed from agentSpawn, other kept
        assert len(data["hooks"]["agentSpawn"]) == 1
        assert data["hooks"]["agentSpawn"][0]["command"] == "/usr/bin/other-hook"
        # stop had only our entry → key dropped
        assert "stop" not in data["hooks"]

    def test_drops_empty_event_lists(self, agents_dir):
        from tracing.kiro.install import _unregister_all_kiro_hooks

        agent_path = agents_dir / "ours-only.json"
        hooks = {event: [{"command": HOOK_CMD}] for event in HOOK_EVENTS}
        self._write_agent(
            agent_path,
            {
                "name": "ours-only",
                "description": "custom agent",
                "hooks": hooks,
            },
        )

        _unregister_all_kiro_hooks()

        data = json.loads(agent_path.read_text())
        assert data["hooks"] == {}

    def test_deletes_agent_when_we_created_it(self, agents_dir):
        from tracing.kiro.install import _unregister_all_kiro_hooks

        agent_path = agents_dir / "arize-traced.json"
        hooks = {event: [{"command": HOOK_CMD}] for event in HOOK_EVENTS}
        self._write_agent(
            agent_path,
            {
                **AGENT_SKELETON,
                "hooks": hooks,
            },
        )

        _unregister_all_kiro_hooks()

        assert not agent_path.exists()

    def test_keeps_user_named_agent(self, agents_dir):
        from tracing.kiro.install import _unregister_all_kiro_hooks

        agent_path = agents_dir / "my-agent.json"
        hooks = {event: [{"command": HOOK_CMD}] for event in HOOK_EVENTS}
        self._write_agent(
            agent_path,
            {
                "name": "my-agent",
                "description": "my agent",
                "hooks": hooks,
            },
        )

        _unregister_all_kiro_hooks()

        # File kept, hooks cleared
        assert agent_path.exists()
        data = json.loads(agent_path.read_text())
        assert data["hooks"] == {}

    def test_skips_malformed_json(self, agents_dir):
        from tracing.kiro.install import _unregister_all_kiro_hooks

        bad_file = agents_dir / "bad.json"
        bad_file.write_text("{not valid json!!!")
        original = bad_file.read_text()

        _unregister_all_kiro_hooks()

        # File untouched, no crash
        assert bad_file.read_text() == original

    def test_no_op_when_dir_missing(self, tmp_path, monkeypatch):
        from tracing.kiro.install import _unregister_all_kiro_hooks

        nonexistent = tmp_path / "no-such-dir"
        monkeypatch.setattr("tracing.kiro.install.KIRO_AGENTS_DIR", nonexistent)

        # Should not raise
        _unregister_all_kiro_hooks()


# ---------------------------------------------------------------------------
# TestPromptAgentName
# ---------------------------------------------------------------------------


class TestPromptAgentName:
    """Tests for _prompt_agent_name — interactive agent name prompt."""

    def test_default_when_empty(self, monkeypatch):
        from tracing.kiro.install import _prompt_agent_name

        monkeypatch.setattr("builtins.input", lambda _: "")
        assert _prompt_agent_name() == DEFAULT_AGENT_NAME

    def test_uses_user_input(self, monkeypatch):
        from tracing.kiro.install import _prompt_agent_name

        monkeypatch.setattr("builtins.input", lambda _: "my-agent")
        assert _prompt_agent_name() == "my-agent"

    def test_strips_whitespace(self, monkeypatch):
        from tracing.kiro.install import _prompt_agent_name

        monkeypatch.setattr("builtins.input", lambda _: "  my-agent  ")
        assert _prompt_agent_name() == "my-agent"


# ---------------------------------------------------------------------------
# TestMaybeSetDefault
# ---------------------------------------------------------------------------


class TestMaybeSetDefault:
    """Tests for _maybe_set_default — optionally setting the Kiro default agent."""

    def test_yes_invokes_subprocess(self, monkeypatch, mock_subprocess, mock_shutil_which):
        from tracing.kiro.install import _maybe_set_default

        monkeypatch.setattr("builtins.input", lambda _: "y")
        mock_shutil_which.return_value = "/fake/kiro-cli"

        _maybe_set_default("arize-traced")

        mock_subprocess.assert_called_once()
        args = mock_subprocess.call_args[0][0]
        assert args == ["/fake/kiro-cli", "agent", "set-default", "arize-traced"]
        # Hooks must never raise — check=False is critical
        assert mock_subprocess.call_args[1].get("check") is False

    def test_no_skips_subprocess(self, monkeypatch, mock_subprocess, mock_shutil_which):
        from tracing.kiro.install import _maybe_set_default

        monkeypatch.setattr("builtins.input", lambda _: "")

        _maybe_set_default("arize-traced")

        mock_subprocess.assert_not_called()

    def test_no_kiro_bin_skips_with_message(self, monkeypatch, mock_subprocess, mock_shutil_which):
        from tracing.kiro.install import _maybe_set_default

        monkeypatch.setattr("builtins.input", lambda _: "y")
        mock_shutil_which.return_value = None
        # Also ensure macOS app path doesn't exist
        monkeypatch.setattr(
            "tracing.kiro.install._macos_app_kiro_path",
            lambda: None,
        )

        with mock.patch("tracing.kiro.install.info") as mock_info:
            _maybe_set_default("arize-traced")

        mock_subprocess.assert_not_called()
        # Should have printed a hint about running manually
        assert any("kiro-cli agent set-default" in str(call) for call in mock_info.call_args_list)

    def test_dry_run_skips_subprocess(self, monkeypatch, mock_subprocess, mock_shutil_which):
        from tracing.kiro.install import _maybe_set_default

        monkeypatch.setenv("ARIZE_DRY_RUN", "true")
        monkeypatch.setattr("builtins.input", lambda _: "y")
        mock_shutil_which.return_value = "/fake/kiro-cli"

        _maybe_set_default("arize-traced")

        mock_subprocess.assert_not_called()


# ---------------------------------------------------------------------------
# TestLoadAgent
# ---------------------------------------------------------------------------


class TestLoadAgent:
    """Tests for _load_agent — loading or creating agent config."""

    def test_loads_existing_valid_file(self, tmp_path):
        from tracing.kiro.install import _load_agent

        agent_path = tmp_path / "agent.json"
        original = {"name": "test", "tools": ["read"], "hooks": {"stop": []}}
        agent_path.write_text(json.dumps(original))

        data = _load_agent(agent_path, "fallback")
        assert data["name"] == "test"
        assert data["tools"] == ["read"]
        assert "hooks" in data

    def test_returns_skeleton_when_missing(self, tmp_path):
        from tracing.kiro.install import _load_agent

        agent_path = tmp_path / "nonexistent.json"
        data = _load_agent(agent_path, "my-agent")
        assert data["name"] == "my-agent"
        assert data["hooks"] == {}

    def test_returns_skeleton_on_malformed_json(self, tmp_path):
        from tracing.kiro.install import _load_agent

        agent_path = tmp_path / "bad.json"
        agent_path.write_text("not json!")

        data = _load_agent(agent_path, "fallback")
        assert data["name"] == "fallback"
        assert data["hooks"] == {}

    def test_adds_hooks_key_if_missing(self, tmp_path):
        from tracing.kiro.install import _load_agent

        agent_path = tmp_path / "no-hooks.json"
        agent_path.write_text(json.dumps({"name": "test"}))

        data = _load_agent(agent_path, "fallback")
        assert "hooks" in data
        assert data["hooks"] == {}


# ---------------------------------------------------------------------------
# TestSaveAgent
# ---------------------------------------------------------------------------


class TestSaveAgent:
    """Tests for _save_agent — writing agent JSON to disk."""

    def test_writes_json_with_trailing_newline(self, tmp_path):
        from tracing.kiro.install import _save_agent

        agent_path = tmp_path / "agent.json"
        _save_agent(agent_path, {"name": "test", "hooks": {}})

        content = agent_path.read_text()
        assert content.endswith("\n")
        data = json.loads(content)
        assert data["name"] == "test"

    def test_creates_parent_dirs(self, tmp_path):
        from tracing.kiro.install import _save_agent

        agent_path = tmp_path / "deep" / "nested" / "agent.json"
        _save_agent(agent_path, {"name": "test"})

        assert agent_path.exists()
