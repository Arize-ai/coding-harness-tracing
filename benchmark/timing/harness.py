"""A throwaway HOME and a way to run one Claude hook in it (timing/hooks.py), and the Claude v2
transcript lines a turn needs (turn_lines(), also used by checks/equiv.py).

Hooks run as `<cmd> hook claude <event>` with a scrubbed environment.
"""

import json
import math
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

SID = "bench-session"
EVENTS = {
    "SessionStart": "session-start",
    "UserPromptSubmit": "user-prompt-submit",
    "PreToolUse": "pre-tool-use",
    "PostToolUse": "post-tool-use",
    "Stop": "stop",
}
KB = 1024
HOOK_TIMEOUT_S = 30  # as in build/inputs.py check: a hung hook fails its build, not the whole run


# --------------------------------------------------------------------------- stats


def pct(values, p):
    """Nearest-rank percentile; values need not be sorted."""
    s = sorted(values)
    return s[min(len(s) - 1, max(0, math.ceil(p / 100 * len(s)) - 1))] if s else float("nan")


def fmt(ms):
    return f"{ms:,.0f}" if ms >= 100 else f"{ms:.1f}"


# --------------------------------------------------------------------------- transcripts


def _line(**entry):
    entry.setdefault("isSidechain", False)
    entry.setdefault("cwd", "/bench/project")
    entry.setdefault("sessionId", SID)
    entry.setdefault("version", "2.1.209")
    return json.dumps(entry, separators=(",", ":")) + "\n"


def _ts(seconds):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(1767268800 + seconds)) + ".000Z"


def turn_lines(tag, tool_ids=(), result_bytes=200, text_bytes=200, t0=0):
    """One Claude v2 turn: prompt, one assistant message per tool + its result, final text."""
    lines = [
        _line(
            type="user",
            uuid=f"{tag}-user",
            parentUuid=None,
            promptId=f"{tag}-prompt",
            timestamp=_ts(t0),
            message={"role": "user", "content": f"prompt {tag}"},
        )
    ]
    parent = f"{tag}-user"
    for i, tool_id in enumerate(tool_ids):
        a, r = f"{tag}-a{i}", f"{tag}-r{i}"
        lines.append(
            _line(
                type="assistant",
                uuid=a,
                parentUuid=parent,
                timestamp=_ts(t0 + 2 * i + 1),
                message={
                    "id": f"{tag}-m{i}",
                    "type": "message",
                    "role": "assistant",
                    "model": "bench-model",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": tool_id,
                            "name": "Read",
                            "input": {"file_path": f"/bench/project/{tool_id}.py"},
                        }
                    ],
                    "usage": {"input_tokens": 100, "output_tokens": 20},
                    "stop_reason": "tool_use",
                },
            )
        )
        lines.append(
            _line(
                type="user",
                uuid=r,
                parentUuid=a,
                promptId=f"{tag}-prompt",
                timestamp=_ts(t0 + 2 * i + 2),
                sourceToolAssistantUUID=a,
                message={
                    "role": "user",
                    "content": [{"tool_use_id": tool_id, "type": "tool_result", "content": "r" * result_bytes}],
                },
            )
        )
        parent = r
    lines.append(
        _line(
            type="assistant",
            uuid=f"{tag}-final",
            parentUuid=parent,
            timestamp=_ts(t0 + 2 * len(tool_ids) + 1),
            message={
                "id": f"{tag}-mfinal",
                "type": "message",
                "role": "assistant",
                "model": "bench-model",
                "content": [{"type": "text", "text": "t" * text_bytes}],
                "usage": {"input_tokens": 120, "output_tokens": 5},
                "stop_reason": "end_turn",
            },
        )
    )
    return lines


# --------------------------------------------------------------------------- the hooks under test


class Harness:
    """A throwaway HOME and one build's Claude hooks, run as `<cmd> hook claude <event>`."""

    def __init__(self, cmd, dry_run=False, timeout=HOOK_TIMEOUT_S):
        self.cmd = list(cmd)
        self.timeout = timeout
        self.home = Path(tempfile.mkdtemp(prefix="bench-home-"))
        self.state_file = self.home / ".arize" / "harness" / "state" / "claude-code" / f"state_{SID}.json"
        self.log_file = self.home / ".arize" / "harness" / "logs" / "claude-code.log"
        self.transcript = self.home / "transcript.jsonl"
        self.transcript.write_text("".join(turn_lines("seed")))
        self.env = {
            k: v for k, v in os.environ.items() if not k.startswith(("ARIZE_", "PHOENIX_", "CLAUDE_", "PYTHON"))
        }
        self.env.update(HOME=str(self.home), ARIZE_TRACE_ENABLED="true", ARIZE_DRY_RUN="true" if dry_run else "false")
        if dry_run:
            self.env["ARIZE_VERBOSE"] = "true"  # the dry-run span line is only logged when verbose

    def close(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def payload(self, event, **extra):
        p = {
            "session_id": SID,
            "hook_event_name": event,
            "transcript_path": str(self.transcript),
            "cwd": "/bench/project",
        }
        p.update(extra)
        return p

    def run(self, event, payload):
        """Run one hook to completion; return its wall time in ms. SystemExit if it fails or hangs."""
        data = json.dumps(payload).encode()
        t = time.perf_counter()
        try:
            proc = subprocess.run(
                self.cmd + ["hook", "claude", EVENTS[event]],
                input=data,
                env=self.env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired:  # run() has killed the hook by now
            raise SystemExit(f"{event} timed out after {self.timeout:g} s") from None
        ms = (time.perf_counter() - t) * 1000
        if proc.returncode != 0:
            raise SystemExit(f"{event} exited {proc.returncode}: {proc.stderr.decode()[-500:]}")
        return ms

    def log_count(self, needle):
        try:
            return self.log_file.read_text(errors="replace").count(needle)
        except OSError:
            return 0
