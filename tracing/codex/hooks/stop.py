"""Codex Stop hook -- no-op turn-end signal under the deferred-parent architecture.

The Stop hook used to assemble and ship the end-of-turn span tree, but the
``agent-turn-complete`` notify callback consistently fires AFTER Stop with a
delay longer than any reasonable poll window. To avoid losing the assistant
message and token data that only notify carries, span shipping moved entirely
to the notify handler (``tracing.codex.hooks.handlers._finalize_turn_and_ship``).
``UserPromptSubmit`` pre-generates the trace/parent-span IDs so notify can build
the parent span referencing them; tool spans buffered by ``tool.py`` are picked
up by the same notify call.

This module is kept as a registered Codex hook only so existing ``/hooks`` trust
hashes in ``~/.codex/config.toml`` remain valid; the body is intentionally a
no-op apart from a single log line for diagnostics.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from core.common import error, log
from tracing.codex.hooks.adapter import check_requirements, load_env_file


def main() -> None:
    """Entry point for arize-hook-codex-stop. No-op; span assembly lives in notify."""
    try:
        load_env_file(Path.home() / ".codex" / "arize-env.sh")
        if not check_requirements():
            return
        raw = sys.stdin.read()
        if not raw.strip():
            return
        payload = json.loads(raw)
        thread_id = payload.get("session_id") or payload.get("thread_id") or ""
        log(f"stop hook: turn end signal (thread={thread_id})")
    except Exception as e:
        error(f"codex stop hook failed: {e}")


if __name__ == "__main__":
    main()
