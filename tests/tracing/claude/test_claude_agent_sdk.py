"""Tests for the Agent SDK convenience helper."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

import pytest


@pytest.fixture(autouse=True)
def _isolate_agent_sdk_module():
    """Remove cached agent_sdk module between tests so monkeypatch takes effect."""
    mod_key = "tracing.claude_code.agent_sdk"
    saved = sys.modules.pop(mod_key, None)
    yield
    # Restore (or remove) after test
    if saved is not None:
        sys.modules[mod_key] = saved
    else:
        sys.modules.pop(mod_key, None)


def test_raises_import_error_when_sdk_missing(monkeypatch):
    """If claude_agent_sdk isn't importable, claude_options should raise ImportError with a hint."""
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", None)
    from tracing.claude_code import agent_sdk

    with pytest.raises(ImportError, match="claude_agent_sdk is required"):
        agent_sdk.claude_options()


def test_includes_local_plugin_by_default(monkeypatch):
    """Returned options include our plugin path + setting_sources=['user']."""
    fake_options_cls = mock.MagicMock()
    fake_sdk = mock.MagicMock(ClaudeAgentOptions=fake_options_cls)
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake_sdk)

    from tracing.claude_code import agent_sdk

    agent_sdk.claude_options()

    args, kwargs = fake_options_cls.call_args
    assert kwargs["setting_sources"] == ["user"]
    assert len(kwargs["plugins"]) == 1
    assert kwargs["plugins"][0]["type"] == "local"
    assert kwargs["plugins"][0]["path"].endswith("share/claude_code")


def test_user_plugins_appended_not_replaced(monkeypatch):
    """User-passed plugins are added to ours, not replacing."""
    fake_options_cls = mock.MagicMock()
    fake_sdk = mock.MagicMock(ClaudeAgentOptions=fake_options_cls)
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake_sdk)

    from tracing.claude_code import agent_sdk

    agent_sdk.claude_options(plugins=[{"type": "local", "path": "/other"}])

    _, kwargs = fake_options_cls.call_args
    assert len(kwargs["plugins"]) == 2
    paths = [p["path"] for p in kwargs["plugins"]]
    assert any(p.endswith("share/claude_code") for p in paths)
    assert "/other" in paths


def test_setting_sources_overridable(monkeypatch):
    """User-passed setting_sources fully overrides our default."""
    fake_options_cls = mock.MagicMock()
    fake_sdk = mock.MagicMock(ClaudeAgentOptions=fake_options_cls)
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake_sdk)

    from tracing.claude_code import agent_sdk

    agent_sdk.claude_options(setting_sources=["project", "local"])

    _, kwargs = fake_options_cls.call_args
    assert kwargs["setting_sources"] == ["project", "local"]


def test_other_kwargs_passed_through(monkeypatch):
    """Unrelated kwargs (model, system_prompt, etc.) pass through to ClaudeAgentOptions."""
    fake_options_cls = mock.MagicMock()
    fake_sdk = mock.MagicMock(ClaudeAgentOptions=fake_options_cls)
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake_sdk)

    from tracing.claude_code import agent_sdk

    agent_sdk.claude_options(model="claude-opus-4-7", system_prompt="you are a test")

    _, kwargs = fake_options_cls.call_args
    assert kwargs["model"] == "claude-opus-4-7"
    assert kwargs["system_prompt"] == "you are a test"


def test_plugin_path_uses_install_dir(monkeypatch):
    """The plugin path should be based on INSTALL_DIR from core.setup."""
    fake_options_cls = mock.MagicMock()
    fake_sdk = mock.MagicMock(ClaudeAgentOptions=fake_options_cls)
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake_sdk)

    import core.setup as setup_mod

    custom_dir = Path("/custom/install/dir")
    monkeypatch.setattr(setup_mod, "INSTALL_DIR", custom_dir)

    from tracing.claude_code import agent_sdk

    agent_sdk.claude_options()

    _, kwargs = fake_options_cls.call_args
    assert kwargs["plugins"][0]["path"] == str(custom_dir / "share" / "claude_code")


def test_empty_user_plugins_list(monkeypatch):
    """Passing plugins=[] should not add extra entries beyond our default."""
    fake_options_cls = mock.MagicMock()
    fake_sdk = mock.MagicMock(ClaudeAgentOptions=fake_options_cls)
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake_sdk)

    from tracing.claude_code import agent_sdk

    agent_sdk.claude_options(plugins=[])

    _, kwargs = fake_options_cls.call_args
    assert len(kwargs["plugins"]) == 1
    assert kwargs["plugins"][0]["type"] == "local"
