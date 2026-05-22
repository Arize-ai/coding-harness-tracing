"""Constants for the Copilot tracing harness installer."""

from __future__ import annotations

from pathlib import Path

HARNESS_NAME = "copilot"
HOOKS_DIR = Path(".github/hooks")  # project-local (relative)
HOOKS_FILE = HOOKS_DIR / "hooks.json"

# VS Code Copilot Chat reads any *.json under .github/hooks/ and expects:
#   {"hooks": {"<EventName>": [{"type": "command", "command": "<cmd>"}]}}
# See https://code.visualstudio.com/docs/copilot/customization/hooks.
# Only events with a corresponding handler entry point are mapped here.
HOOK_EVENTS: dict[str, str] = {
    "SessionStart": "arize-hook-copilot-session-start",
    "UserPromptSubmit": "arize-hook-copilot-user-prompt",
    "PreToolUse": "arize-hook-copilot-pre-tool",
    "PostToolUse": "arize-hook-copilot-post-tool",
    "Stop": "arize-hook-copilot-stop",
    "SubagentStop": "arize-hook-copilot-subagent-stop",
}
