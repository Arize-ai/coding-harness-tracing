"""The hook table: every (harness, event) pair a coding harness can invoke.

This is the single source of truth for hook targets. ``arize-harness hook``
dispatches through it, and launcher generation reads it instead of keeping a
separate list.

Targets are ``"module:function"`` strings so that looking a hook up never
imports a harness; only the module of the hook actually being run is imported.
This module must stay standard-library only and must not import ``tracing.*``:
it is importable from the Claude plugin path, where neither resolves.
"""

from typing import Dict, Iterator, Optional, Tuple

_CLAUDE = "tracing.claude_code.hooks.handlers"
_COPILOT = "tracing.copilot.hooks.handlers"
_GEMINI = "tracing.gemini.hooks.handlers"
_ANTIGRAVITY = "tracing.antigravity.hooks.handlers"

# Harnesses with one handler per event. Event names are kebab-case, taken from
# the legacy ``arize-hook-*`` command names.
EVENT_HOOKS: Dict[str, Dict[str, str]] = {
    "claude": {
        "session-start": f"{_CLAUDE}:session_start",
        "pre-tool-use": f"{_CLAUDE}:pre_tool_use",
        "post-tool-use": f"{_CLAUDE}:post_tool_use",
        "user-prompt-submit": f"{_CLAUDE}:user_prompt_submit",
        "stop": f"{_CLAUDE}:stop",
        "subagent-stop": f"{_CLAUDE}:subagent_stop",
        "stop-failure": f"{_CLAUDE}:stop_failure",
        "notification": f"{_CLAUDE}:notification",
        "permission-request": f"{_CLAUDE}:permission_request",
        "session-end": f"{_CLAUDE}:session_end",
        "post-tool-use-failure": f"{_CLAUDE}:post_tool_use_failure",
        "subagent-start": f"{_CLAUDE}:subagent_start",
        "user-prompt-expansion": f"{_CLAUDE}:user_prompt_expansion",
        "pre-compact": f"{_CLAUDE}:pre_compact",
        "post-compact": f"{_CLAUDE}:post_compact",
        "permission-denied": f"{_CLAUDE}:permission_denied",
    },
    "codex": {
        "notify": "tracing.codex.hooks.handlers:notify",
    },
    "copilot": {
        "session-start": f"{_COPILOT}:session_start",
        "user-prompt": f"{_COPILOT}:user_prompt_submitted",
        "pre-tool": f"{_COPILOT}:pre_tool_use",
        "post-tool": f"{_COPILOT}:post_tool_use",
        "stop": f"{_COPILOT}:stop",
        "subagent-stop": f"{_COPILOT}:subagent_stop",
    },
    "gemini": {
        "session-start": f"{_GEMINI}:session_start",
        "session-end": f"{_GEMINI}:session_end",
        "before-agent": f"{_GEMINI}:before_agent",
        "after-agent": f"{_GEMINI}:after_agent",
        "before-model": f"{_GEMINI}:before_model",
        "after-model": f"{_GEMINI}:after_model",
        "before-tool": f"{_GEMINI}:before_tool",
        "after-tool": f"{_GEMINI}:after_tool",
    },
    "antigravity": {
        "pre-invocation": f"{_ANTIGRAVITY}:pre_invocation",
        "stop": f"{_ANTIGRAVITY}:stop",
    },
}

# Harnesses with a single handler that takes no event argument. Everything
# after the harness name is passed through to the handler.
SINGLE_HOOKS: Dict[str, str] = {
    "cursor": "tracing.cursor.hooks.handlers:main",
    "kiro": "tracing.kiro.hooks.handlers:main",
    "opencode": "tracing.opencode.hooks.handlers:main",
    "omp": "tracing.omp.hooks.handlers:main",
    "devin": "tracing.devin.hooks.handlers:main",
}

# Alternate harness names, matching install.sh.
ALIASES: Dict[str, str] = {
    "claude-code": "claude",
}


def canonical_harness(name: str) -> str:
    """Return the table name for ``name``, resolving aliases."""
    return ALIASES.get(name, name)


def iter_hooks() -> Iterator[Tuple[str, Optional[str], str]]:
    """Yield ``(harness, event, target)`` for every hook; ``event`` is None for single-entry harnesses."""
    for harness, events in EVENT_HOOKS.items():
        for event, target in events.items():
            yield harness, event, target
    for harness, target in SINGLE_HOOKS.items():
        yield harness, None, target


def legacy_entry_point(harness: str, event: Optional[str]) -> str:
    """Return the ``arize-hook-*`` command name that runs the same hook.

    Claude Code's commands predate the other harnesses and carry no harness
    prefix (``arize-hook-stop``); every other harness is prefixed
    (``arize-hook-gemini-before-tool``, ``arize-hook-cursor``).
    """
    if harness == "claude":
        return f"arize-hook-{event}"
    if event is None:
        return f"arize-hook-{harness}"
    return f"arize-hook-{harness}-{event}"
