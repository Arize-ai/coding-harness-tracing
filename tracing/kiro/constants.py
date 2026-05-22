"""Constants for the Kiro CLI tracing harness."""

from __future__ import annotations

from pathlib import Path

HARNESS_NAME = "kiro"
DISPLAY_NAME = "Kiro"

# Soft install detection (presence check + binary lookup fallback).
HARNESS_HOME = ".kiro"
HARNESS_BIN = "kiro-cli"

# Where Kiro looks for global agent configs. Per-workspace dir
# (`<workspace>/.kiro/agents/`) is out of scope for v1.
KIRO_AGENTS_DIR = Path.home() / ".kiro" / "agents"

# Where Kiro writes the per-session sidecar files used to enrich LLM spans
# with model, token counts, and metering usage. Mined by the stop handler.
KIRO_SESSIONS_DIR = Path.home() / ".kiro" / "sessions" / "cli"

# Default agent name when the user doesn't specify one during install.
DEFAULT_AGENT_NAME = "arize-traced"

# Single hook binary; the handler dispatches by hook_event_name.
HOOK_BIN_NAME = "arize-hook-kiro"

# Five Kiro CLI hook events (camelCase — these are the exact strings Kiro
# accepts as keys in the agent config's `hooks` field).
HOOK_EVENTS = (
    "agentSpawn",
    "userPromptSubmit",
    "preToolUse",
    "postToolUse",
    "stop",
)

# Default body for a freshly-created `arize-traced` agent. The `hooks` field
# is filled in by install.py.  Shape verified 2026-05-08 against
# `kiro-cli agent create` output.
AGENT_SKELETON: dict = {
    "name": DEFAULT_AGENT_NAME,
    "description": "Kiro agent with Arize tracing hooks installed.",
    "prompt": None,
    "mcpServers": {},
    "tools": ["*"],
    "toolAliases": {},
    "allowedTools": [],
    "resources": [],
    "hooks": {},  # populated by install.py
    "toolsSettings": {},
    "includeMcpJson": True,
    "model": None,
}
