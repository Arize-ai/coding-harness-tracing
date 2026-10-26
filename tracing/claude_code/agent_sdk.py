"""Convenience helpers for Claude Agent SDK consumers.

Usage::

    from claude_agent_sdk import query
    from tracing.claude_code.agent_sdk import claude_options

    async for msg in query(prompt="hello", options=claude_options()):
        ...

The returned ``ClaudeAgentOptions`` is pre-configured with:

  - ``plugins=[{"type": "local", "path": "<install-dir>/share/claude_code"}]``
    so the SDK loads the local plugin and fires our hooks.
  - ``setting_sources=["user"]`` so user-level settings (including the
    Arize env vars written by ``install.py``) are honored.

Any keyword argument passed to ``claude_options(**overrides)`` is merged on
top of these defaults, with overrides winning. Pass ``plugins=[...]`` to
add to (not replace) the Arize plugin entry; pass ``setting_sources=[...]``
to override sources entirely.

This module imports lazily — if ``claude_agent_sdk`` isn't installed,
``claude_options`` raises ImportError with a hint on how to install it.
"""

from __future__ import annotations

from typing import Any

import core.setup


def claude_options(**overrides: Any) -> Any:
    """Return a ClaudeAgentOptions pre-configured for Arize tracing.

    Raises ImportError if `claude_agent_sdk` is not installed.
    """
    try:
        from claude_agent_sdk import ClaudeAgentOptions
    except ImportError as exc:
        raise ImportError(
            "claude_agent_sdk is required for Agent SDK tracing. " "Install with: pip install claude-agent-sdk"
        ) from exc

    plugin_path = str(core.setup.harness_dir("claude-code"))
    default_plugins = [{"type": "local", "path": plugin_path}]
    default_sources = ["user"]

    # Merge: user-passed plugins are added to ours (so they get tracing AND their own).
    user_plugins = overrides.pop("plugins", [])
    plugins = default_plugins + list(user_plugins)

    # setting_sources: if user provides, override entirely (they may explicitly want
    # to exclude `user` for some reason). If absent, use our default.
    setting_sources = overrides.pop("setting_sources", default_sources)

    return ClaudeAgentOptions(plugins=plugins, setting_sources=setting_sources, **overrides)
