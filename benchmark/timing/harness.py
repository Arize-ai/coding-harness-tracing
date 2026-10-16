"""A throwaway HOME and the ways to run one Claude hook in it, shared by timing/hooks.py,
workload/workload.py and checks/equiv.py.

Hooks run as `<cmd> hook claude <event>` (default: a venv's arize-harness) with a scrubbed
environment, and turn_lines() writes the Claude v2 transcript lines a turn needs.
"""
import json
import os
import shutil
import statistics
import subprocess
import tempfile
import threading
import time
from pathlib import Path

BENCH = Path(__file__).resolve().parent.parent
REPO = BENCH.parent
INSTRUMENT = BENCH / "workload" / "instrument"
SID = "workload-session"
BUFFER_KEY = "claude.tool_observation_buffer"
EVENTS = {
    "SessionStart": "session-start",
    "UserPromptSubmit": "user-prompt-submit",
    "PreToolUse": "pre-tool-use",
    "PostToolUse": "post-tool-use",
    "Stop": "stop",
}
KB = 1024


# --------------------------------------------------------------------------- stats


def pct(values, p):
    """Nearest-rank percentile; values need not be sorted."""
    s = sorted(values)
    return s[min(len(s) - 1, max(0, int(round(p / 100 * len(s) + 0.5)) - 1))] if s else float("nan")


def summary(values):
    return {
        "n": len(values),
        "mean": statistics.mean(values) if values else float("nan"),
        "p50": pct(values, 50),
        "p95": pct(values, 95),
        "p99": pct(values, 99),
        "max": max(values) if values else float("nan"),
    }


def fmt(ms):
    return f"{ms:,.0f}" if ms >= 100 else f"{ms:.1f}"


# --------------------------------------------------------------------------- transcripts


def _line(**entry):
    entry.setdefault("isSidechain", False)
    entry.setdefault("cwd", "/workload/project")
    entry.setdefault("sessionId", SID)
    entry.setdefault("version", "2.1.209")
    return json.dumps(entry, separators=(",", ":")) + "\n"


def _ts(seconds):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(1767268800 + seconds)) + ".000Z"


def turn_lines(tag, tool_ids=(), result_bytes=200, text_bytes=200, t0=0):
    """One Claude v2 turn: prompt, one assistant message per tool + its result, final text."""
    lines = [_line(type="user", uuid=f"{tag}-user", parentUuid=None, promptId=f"{tag}-prompt",
                   timestamp=_ts(t0), message={"role": "user", "content": f"prompt {tag}"})]
    parent = f"{tag}-user"
    for i, tool_id in enumerate(tool_ids):
        a, r = f"{tag}-a{i}", f"{tag}-r{i}"
        lines.append(_line(
            type="assistant", uuid=a, parentUuid=parent, timestamp=_ts(t0 + 2 * i + 1),
            message={"id": f"{tag}-m{i}", "type": "message", "role": "assistant", "model": "workload-model",
                     "content": [{"type": "tool_use", "id": tool_id, "name": "Read",
                                  "input": {"file_path": f"/workload/project/{tool_id}.py"}}],
                     "usage": {"input_tokens": 100, "output_tokens": 20}, "stop_reason": "tool_use"}))
        lines.append(_line(
            type="user", uuid=r, parentUuid=a, promptId=f"{tag}-prompt", timestamp=_ts(t0 + 2 * i + 2),
            sourceToolAssistantUUID=a,
            message={"role": "user", "content": [{"tool_use_id": tool_id, "type": "tool_result",
                                                  "content": "r" * result_bytes}]}))
        parent = r
    lines.append(_line(
        type="assistant", uuid=f"{tag}-final", parentUuid=parent, timestamp=_ts(t0 + 2 * len(tool_ids) + 1),
        message={"id": f"{tag}-mfinal", "type": "message", "role": "assistant", "model": "workload-model",
                 "content": [{"type": "text", "text": "t" * text_bytes}],
                 "usage": {"input_tokens": 120, "output_tokens": 5}, "stop_reason": "end_turn"}))
    return lines


# --------------------------------------------------------------------------- the hooks under test


class Harness:
    """A throwaway HOME, a backend config, and the ways to invoke one Claude hook."""

    def __init__(self, venv, endpoint=None, lock_probe=False, dry_run=False, cmd=None):
        # cmd: any build's executable (+ args) run as `<cmd> hook claude <event>`; else venv's arize-harness.
        self.venv = Path(venv).resolve() if venv else None
        self.cmd = list(cmd) if cmd else [str(self.venv / "bin" / "arize-harness")]
        self.home = Path(tempfile.mkdtemp(prefix="workload-home-"))
        self.state_dir = self.home / ".arize" / "harness" / "state" / "claude-code"
        self.state_file = self.state_dir / f"state_{SID}.json"
        self.log_file = self.home / ".arize" / "harness" / "logs" / "claude-code.log"
        self.transcript = self.home / "transcript.jsonl"
        self.transcript.write_text("".join(turn_lines("seed")))
        self.lock_log = self.home / "lock-probe.jsonl"
        self.plugin_data = self.home / "plugin-data"
        self.env = {
            k: v for k, v in os.environ.items()
            if not k.startswith(("ARIZE_", "PHOENIX_", "CLAUDE_", "PYTHON"))
        }
        self.env.update(HOME=str(self.home), ARIZE_TRACE_ENABLED="true",
                        ARIZE_DRY_RUN="true" if dry_run else "false")
        if dry_run:
            self.env["ARIZE_VERBOSE"] = "true"  # as bench.sh: the dry-run span line is only logged when verbose
        if lock_probe:
            self.env.update(PYTHONPATH=str(INSTRUMENT), WORKLOAD_LOCK_LOG=str(self.lock_log))
        if endpoint:
            self.set_endpoint(endpoint)

    def set_endpoint(self, endpoint):
        cfg = self.home / ".arize" / "harness" / "config.json"
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text(json.dumps({"harnesses": {"claude-code": {
            "target": "arize", "endpoint": endpoint, "api_key": "workload-fake-key",
            "space_id": "workload-fake-space", "project_name": "workload"}}}))

    def close(self):
        shutil.rmtree(self.home, ignore_errors=True)

    # -- invocation paths ----------------------------------------------------

    def command(self, event, path="harness"):
        name = EVENTS[event]
        if path == "harness":
            return self.cmd + ["hook", "claude", name], self.env
        if path == "console":
            return [str(self.venv / "bin" / f"arize-hook-{name}")], self.env
        env = dict(self.env, CLAUDE_PLUGIN_ROOT=str(REPO / "tracing" / "claude_code"),
                   CLAUDE_PLUGIN_DATA=str(self.plugin_data),
                   # run-hook resolves `python3` from PATH; pin it to venv312's interpreter.
                   PATH=f"{self.base_python_dir()}:{self.env.get('PATH', '')}")
        if path == "run-hook":
            return [str(REPO / "tracing" / "claude_code" / "scripts" / "run-hook"), f"arize-hook-{name}"], env
        if path == "launcher":
            return [str(self.plugin_data / "venv" / "bin" / f"arize-hook-{name}")], env
        raise ValueError(path)

    def base_python_dir(self):
        cfg = dict(l.split(" = ", 1) for l in (self.venv / "pyvenv.cfg").read_text().splitlines() if " = " in l)
        return cfg["home"]

    def bootstrap_plugin_venv(self):
        """First run-hook call creates CLAUDE_PLUGIN_DATA/venv and its launchers (untimed)."""
        self.run("SessionStart", {"session_id": "bootstrap"}, path="run-hook")
        if not (self.plugin_data / "venv" / "bin" / "arize-hook-stop").exists():
            raise SystemExit("run-hook bootstrap did not create the plugin venv")

    # -- running -----------------------------------------------------------

    def payload(self, event, **extra):
        p = {"session_id": SID, "hook_event_name": event, "transcript_path": str(self.transcript),
             "cwd": "/workload/project"}
        p.update(extra)
        return p

    def run(self, event, payload, path="harness", tag=""):
        """Run one hook to completion; return its wall time in ms."""
        cmd, env = self.command(event, path)
        if tag:
            env = dict(env, WORKLOAD_TAG=tag)
        data = json.dumps(payload).encode()
        t = time.perf_counter()
        proc = subprocess.run(cmd, input=data, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        ms = (time.perf_counter() - t) * 1000
        if proc.returncode != 0:
            raise SystemExit(f"{event} via {path} exited {proc.returncode}: {proc.stderr.decode()[-500:]}")
        return ms

    def parallel(self, jobs):
        """Start every (event, payload, tag) job at the same moment; return their wall times."""
        barrier = threading.Barrier(len(jobs))
        times = [None] * len(jobs)

        def worker(i, job):
            barrier.wait()
            times[i] = self.run(*job[:2], tag=job[2])

        threads = [threading.Thread(target=worker, args=(i, job)) for i, job in enumerate(jobs)]
        t = time.perf_counter()
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        if any(v is None for v in times):
            raise SystemExit("a parallel hook failed")
        return times, (time.perf_counter() - t) * 1000

    # -- state -----------------------------------------------------------

    def reset_session(self, transcript_text=None):
        """Forget the session and open a fresh turn (untimed)."""
        self.state_file.unlink(missing_ok=True)
        if transcript_text is not None:
            self.transcript.write_text(transcript_text)
        self.run("SessionStart", self.payload("SessionStart", source="startup"))
        self.run("UserPromptSubmit", self.payload("UserPromptSubmit", prompt="workload"))

    def state(self):
        try:
            return json.loads(self.state_file.read_text())
        except (OSError, ValueError):
            return {}

    def buffer(self):
        raw = self.state().get(BUFFER_KEY) or "{}"
        return json.loads(raw)

    def inject_buffer(self, observations):
        data = self.state()
        data[BUFFER_KEY] = json.dumps(observations, separators=(",", ":"))
        self.state_file.write_text(json.dumps(data))

    def lock_records(self):
        if not self.lock_log.exists():
            return []
        return [json.loads(l) for l in self.lock_log.read_text().splitlines() if l.strip()]

    def last_log_line(self, needle):
        try:
            lines = [l for l in self.log_file.read_text(errors="replace").splitlines() if needle in l]
        except OSError:
            return ""
        return lines[-1][-160:] if lines else ""

    def log_count(self, needle):
        try:
            return self.log_file.read_text(errors="replace").count(needle)
        except OSError:
            return 0
