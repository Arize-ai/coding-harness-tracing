"""Wait briefly at Stop for Claude Code to finish appending the final assistant record.

The Stop hook can fire before the transcript JSONL has the turn's last assistant
message on disk. When that happens the final ``LLM call`` span is lost from the
turn. This helper polls the transcript, bounded by ``ARIZE_STOP_SETTLE_MS``
(default 1500, ``0`` disables), until the text reported in the hook's
``last_assistant_message`` shows up.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Callable, Optional

DEFAULT_SETTLE_MS = 1500
POLL_INTERVAL_MS = 100
NEEDLE_CHARS = 80
SETTLE_ENV = "ARIZE_STOP_SETTLE_MS"
_WHITESPACE = re.compile(r"\s+")


def settle_timeout_ms() -> int:
    """Timeout from the environment; invalid or negative values fall back to the default."""
    raw = os.environ.get(SETTLE_ENV, "").strip()
    if not raw:
        return DEFAULT_SETTLE_MS
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_SETTLE_MS
    return value if value >= 0 else DEFAULT_SETTLE_MS


def _normalize(text: str) -> str:
    return _WHITESPACE.sub(" ", text).strip()


def _assistant_text(record: dict) -> str:
    message = record.get("message") if isinstance(record.get("message"), dict) else {}
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return " ".join(
        str(block.get("text") or "") for block in content if isinstance(block, dict) and block.get("type") == "text"
    )


def transcript_has_text(transcript: Path, start_line: int, needle: str) -> bool:
    """True when an assistant record at or after *start_line* contains *needle*."""
    try:
        with transcript.open(encoding="utf-8") as handle:
            for index, raw_line in enumerate(handle):
                if index < start_line or not raw_line.strip():
                    continue
                try:
                    record = json.loads(raw_line)
                except (json.JSONDecodeError, TypeError, ValueError):
                    continue
                if record.get("type") == "assistant" and needle in _normalize(_assistant_text(record)):
                    return True
    except OSError:
        return False
    return False


def wait_for_final_assistant(
    transcript: Optional[Path],
    start_line: int,
    last_message: str,
    *,
    timeout_ms: Optional[int] = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> bool:
    """Poll until the transcript holds the final assistant text or the timeout elapses."""
    needle = _normalize(last_message or "")[:NEEDLE_CHARS]
    if transcript is None or not needle:
        return True
    budget_ms = settle_timeout_ms() if timeout_ms is None else timeout_ms
    deadline = clock() + budget_ms / 1000.0
    while True:
        if transcript_has_text(transcript, start_line, needle):
            return True
        if budget_ms <= 0 or clock() >= deadline:
            return False
        sleep(POLL_INTERVAL_MS / 1000.0)


__all__ = ["DEFAULT_SETTLE_MS", "SETTLE_ENV", "settle_timeout_ms", "transcript_has_text", "wait_for_final_assistant"]
