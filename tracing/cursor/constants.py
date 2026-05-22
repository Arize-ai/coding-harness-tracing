"""Constants for the Cursor tracing harness."""

from pathlib import Path

HARNESS_NAME = "cursor"
DISPLAY_NAME = "Cursor"
HARNESS_HOME = ".cursor"  # ~/.cursor — presence check for soft install detection
HARNESS_BIN = "cursor"  # binary name for shutil.which() fallback

HOOKS_FILE = Path.home() / ".cursor" / "hooks.json"
HOOK_BIN_NAME = "arize-hook-cursor"

# 15 events, all routed to a single CLI entry point (the handler dispatches
# based on hook_event_name / hookEventName in the JSON payload).
# Includes IDE events plus CLI-specific events (sessionStart, sessionEnd, postToolUse).
HOOK_EVENTS = (
    "beforeSubmitPrompt",
    "afterAgentResponse",
    "afterAgentThought",
    "beforeShellExecution",
    "afterShellExecution",
    "beforeMCPExecution",
    "afterMCPExecution",
    "beforeReadFile",
    "afterFileEdit",
    "stop",
    "beforeTabFileRead",
    "afterTabFileEdit",
    "sessionStart",
    "sessionEnd",
    "postToolUse",
)
