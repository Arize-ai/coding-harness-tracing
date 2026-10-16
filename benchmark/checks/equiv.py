#!/usr/bin/env python3
"""Binary spike correctness checks (Arize-ai/arize#90274): does a frozen build behave like venv312?

  equiv.py sequence <label> <cmd> <out.json>   run the Claude, Gemini and Codex sequences through
                                               `<cmd> hook ...` in a throwaway HOME, dry-run off,
                                               sends to a local sink; write normalized payloads + state
  equiv.py diff <a.json> <b.json>              compare two sequence outputs (exit 1 on any difference)
  equiv.py sessionkey <cmd>                    two Claude hooks with no session_id from one parent:
                                               print the state files they created
  equiv.py dryrun <cmd>                        one Claude turn, dry-run on: exit 1 unless Stop logs its span
  equiv.py https <cmd> [endpoint]            one span to the real endpoint with a fake key; prints
                                               the logged send result

Ids (traceId/spanId/parentSpanId) are replaced by their order of first appearance, times by "T",
the throwaway HOME by "$HOME". Everything else must match byte for byte.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "timing"))
from harness import turn_lines  # noqa: E402  (same Claude v2 transcript the workload uses)

SID = "equiv-session"
KB = 1024


class Sink:
    """Local OTLP/HTTP sink that keeps every request (path, a few headers, JSON body, arrival time)."""

    def __init__(self, delay=0.0):
        sink = self
        self.received = []
        self.delay = delay

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                sink.received.append({
                    "at": time.time(), "path": self.path,
                    "headers": {k: self.headers.get(k) for k in ("Content-Type", "authorization", "space_id")},
                    "body": json.loads(body),
                })
                if sink.delay:
                    time.sleep(sink.delay)
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def wait(self, n, timeout=15):
        end = time.time() + timeout
        while len(self.received) < n and time.time() < end:
            time.sleep(0.05)
        return len(self.received) >= n

    def close(self):
        self.server.shutdown()


class Home:
    def __init__(self, cmd, endpoint):
        self.cmd = cmd
        self.home = Path(tempfile.mkdtemp(prefix="equiv-home-")).resolve()
        cfg = self.home / ".arize" / "harness" / "config.json"
        cfg.parent.mkdir(parents=True)
        backend = {"target": "arize", "endpoint": endpoint, "api_key": "equiv-fake-key",
                   "space_id": "equiv-fake-space", "project_name": "equiv"}
        cfg.write_text(json.dumps({"harnesses": {h: backend for h in ("claude-code", "gemini", "codex")}}))
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(("ARIZE_", "PHOENIX_", "CLAUDE_", "PYTHON", "GEMINI_", "CODEX_", "OTEL_"))}
        self.env.update(HOME=str(self.home), ARIZE_TRACE_ENABLED="true", ARIZE_DRY_RUN="false",
                        CODEX_HOME=str(self.home / ".codex"))

    def run(self, harness, event, payload=None, argv=(), env=None):
        """Run one hook to completion; return (exit time, stderr)."""
        p = subprocess.run([*self.cmd, "hook", harness, event, *argv], cwd=self.home,
                           input=json.dumps(payload).encode() if payload is not None else b"",
                           env=dict(self.env, **(env or {})), capture_output=True, timeout=60)
        if p.returncode != 0:
            raise SystemExit(f"{harness} {event} exited {p.returncode}: {p.stderr.decode()[-800:]}")
        return time.time(), p.stdout.decode(), p.stderr.decode()

    def states(self):
        out = {}
        root = self.home / ".arize" / "harness" / "state"
        for f in sorted(root.rglob("*")) if root.exists() else []:
            if f.is_file() and not f.name.startswith(".lock"):
                rel = str(f.relative_to(root))
                try:
                    out[rel] = json.loads(f.read_text())
                except ValueError:
                    out[rel] = f.read_text()
        return out

    def logs(self):
        d = self.home / ".arize" / "harness" / "logs"
        return {f.name: f.read_text(errors="replace") for f in sorted(d.glob("*"))} if d.exists() else {}

    def close(self):
        shutil.rmtree(self.home, ignore_errors=True)


# --------------------------------------------------------------------------- sequences


def claude(h):
    tr = h.home / "transcript.jsonl"
    tr.write_text("".join(turn_lines("seed")))
    base = {"session_id": SID, "transcript_path": str(tr), "cwd": "/equiv/project"}
    ev = lambda name, **kw: {**base, "hook_event_name": name, **kw}
    h.run("claude", "session-start", ev("SessionStart", source="startup"))
    h.run("claude", "user-prompt-submit", ev("UserPromptSubmit", prompt="list the files"))
    ids = [f"toolu_equiv_{i}" for i in range(3)]
    for tid in ids:
        tool = dict(tool_use_id=tid, tool_name="Read", tool_input={"file_path": f"/equiv/project/{tid}.py"})
        h.run("claude", "pre-tool-use", ev("PreToolUse", **tool))
        h.run("claude", "post-tool-use", ev("PostToolUse", **tool, tool_response="y" * KB))
    with open(tr, "a") as f:
        f.write("".join(turn_lines("equiv", ids, t0=100)))
    h.run("claude", "stop", ev("Stop", stop_hook_active=False))


def gemini(h, sink):
    """Gemini hooks are synchronous; spans go out from a double-forked grandchild."""
    base = {"session_id": "equiv-gemini", "cwd": "/equiv/project"}
    exits = []
    seq = [
        ("session-start", {"hook_event_name": "SessionStart", "source": "startup"}),
        ("before-agent", {"hook_event_name": "BeforeAgent", "prompt": "read the readme"}),
        ("before-model", {"hook_event_name": "BeforeModel", "model_call_id": "m1",
                          "llm_request": {"model": "gemini-equiv", "messages": [{"role": "user", "content": "read the readme"}]}}),
        ("after-model", {"hook_event_name": "AfterModel", "model_call_id": "m1",
                         "llm_request": {"model": "gemini-equiv"},
                         "llm_response": {"text": "calling read_file",
                                          "usage_metadata": {"prompt_token_count": 11, "candidates_token_count": 3}}}),
        ("before-tool", {"hook_event_name": "BeforeTool", "tool_name": "read_file", "tool_call_id": "t1",
                         "tool_input": {"file_path": "/equiv/project/README.md"}}),
        ("after-tool", {"hook_event_name": "AfterTool", "tool_name": "read_file", "tool_call_id": "t1",
                        "tool_input": {"file_path": "/equiv/project/README.md"},
                        "tool_response": {"llmContent": "# readme"}}),
        ("after-agent", {"hook_event_name": "AfterAgent", "prompt_response": "it is a readme"}),
        ("session-end", {"hook_event_name": "SessionEnd", "reason": "exit"}),
    ]
    for event, payload in seq:
        before = len(sink.received)
        t0 = time.time()
        t_exit, out, _ = h.run("gemini", event, {**base, **payload})
        exits.append({"event": event, "hook_s": round(t_exit - t0, 3), "stdout": out.strip(),
                      "sent_before_exit": len(sink.received) - before})
    return exits


def codex(h):
    """Codex: the notify payload is sys.argv[1]; the turn comes from the rollout file."""
    sid, turn = "equiv-codex-thread", "equiv-turn-1"
    folder = h.home / ".codex" / "sessions" / "2026" / "05" / "20"
    folder.mkdir(parents=True)
    ev = lambda p, ts="2026-05-20T00:00:00.000Z": {"timestamp": ts, "type": "event_msg", "payload": p}
    resp = lambda p, ts: {"timestamp": ts, "type": "response_item", "payload": p}
    records = [
        ev({"type": "task_started", "turn_id": turn, "started_at": 1779235200}),
        ev({"type": "user_message", "message": "run ls"}),
        resp({"type": "function_call", "name": "exec_command", "arguments": '{"cmd":"ls"}', "call_id": "c1"},
             "2026-05-20T00:00:01.000Z"),
        resp({"type": "function_call_output", "call_id": "c1", "output": "a\nb"}, "2026-05-20T00:00:02.000Z"),
        ev({"type": "agent_message", "message": "two files"}, "2026-05-20T00:00:03.000Z"),
        ev({"type": "task_complete", "turn_id": turn, "last_agent_message": "two files",
            "completed_at": 1779235204, "duration_ms": 4000}, "2026-05-20T00:00:04.000Z"),
    ]
    (folder / f"rollout-2026-05-20T00-00-00-{sid}.jsonl").write_text("\n".join(map(json.dumps, records)) + "\n")
    notify = {"type": "agent-turn-complete", "thread-id": sid, "turn-id": turn,
              "input-messages": ["run ls"], "last-assistant-message": "two files"}
    h.run("codex", "notify", argv=[json.dumps(notify)])


# --------------------------------------------------------------------------- normalization

ID_KEYS = {"traceId", "spanId", "parentSpanId"}
TIME_KEYS = re.compile(r"(TimeUnixNano|_time|_start|_ms$|start_time|timestamp|^started_at|ended_at|latency|duration)", re.I)
HEX = re.compile(r"^[0-9a-f]{16}$|^[0-9a-f]{32}$")
NOW_MS = re.compile(r"^1[78]\d{11}$")  # epoch ms in 2023..2027
NOW_NS = re.compile(r"^1[78]\d{17}$")


class Norm:
    def __init__(self, home):
        self.home = str(home)
        self.ids = {}

    def id(self, v):
        return self.ids.setdefault(v, f"<id{len(self.ids)}>")

    def str(self, s):
        s = s.replace(self.home, "$HOME")
        if HEX.match(s):
            return self.id(s)
        if NOW_MS.match(s) or NOW_NS.match(s):
            return "T"
        try:  # state files keep JSON-encoded blobs as strings
            if s[:1] in "{[":
                return json.dumps(self(json.loads(s)), sort_keys=True)
        except ValueError:
            pass
        return re.sub(r"\b[0-9a-f]{32}\b|\b[0-9a-f]{16}\b", lambda m: self.id(m.group()), s)

    def __call__(self, o, key=""):
        if isinstance(o, dict):
            # OTLP attribute: {"key": k, "value": {...}} -> normalize the value by its key
            if set(o) == {"key", "value"} and isinstance(o["key"], str):
                return {"key": o["key"], "value": self(o["value"], o["key"])}
            return {self.str(k): self(v, k if not key or k not in ("stringValue", "intValue") else key)
                    for k, v in o.items()}
        if isinstance(o, list):
            return [self(x, key) for x in o]
        if isinstance(o, str):
            if key in ID_KEYS:
                return self.id(o)
            if TIME_KEYS.search(key) and re.match(r"^\d+(\.\d+)?$", o):
                return "T"
            return self.str(o)
        if isinstance(o, (int, float)) and not isinstance(o, bool):
            if TIME_KEYS.search(key) or NOW_MS.match(str(o)) or NOW_NS.match(str(o)):
                return "T"
        return o


def span_names(body):
    return [s["name"] for rs in body.get("resourceSpans", []) for ss in rs.get("scopeSpans", []) for s in ss["spans"]]


def collect(h, sink, extra=None):
    reqs = sorted(sink.received, key=lambda r: (span_names(r["body"]), json.dumps(r["body"], sort_keys=True)))
    n = Norm(h.home)
    # States first: they hold the trace/span ids that payloads reference, so numbering is stable.
    states = n(h.states())
    payloads = [n({"path": r["path"], "headers": r["headers"], "body": r["body"]}) for r in reqs]
    out = {"requests": len(reqs), "payloads": payloads, "states": states}
    errors = [l for log in h.logs().values() for l in log.splitlines() if "ERROR" in l or "failed" in l]
    out["log_errors"] = [n.str(re.sub(r"^\S+ \S+ ", "", l)) for l in errors]
    if extra:
        out.update(extra)
    return out


def sequence(label, cmd, out_path):
    result = {"label": label, "cmd": cmd}
    for name in ("claude", "gemini", "codex"):
        delay = 1.0 if name == "gemini" else 0.0  # the hook must not wait for this
        sink = Sink(delay=delay)
        h = Home(cmd, sink.url)
        try:
            extra = None
            if name == "claude":
                claude(h)
            elif name == "codex":
                codex(h)
            else:
                exits = gemini(h, sink)
                n_at_exit = len(sink.received)
                time.sleep(3.0)  # let every grandchild finish (the sink holds each response 1 s)
                extra = {"gemini_hooks": exits,
                         "gemini_slowest_hook_s": max(e["hook_s"] for e in exits),
                         "gemini_requests_at_last_exit": n_at_exit,
                         "gemini_requests_after_wait": len(sink.received)}
            result[name] = collect(h, sink, extra)
        finally:
            h.close()
            sink.close()
        print(f"{label} {name}: {result[name]['requests']} request(s), {len(result[name]['states'])} state file(s), "
              f"{len(result[name]['log_errors'])} logged error(s)", flush=True)
    Path(out_path).write_text(json.dumps(result, indent=1, sort_keys=True))


def diff(a_path, b_path):
    a, b = json.loads(Path(a_path).read_text()), json.loads(Path(b_path).read_text())
    bad = 0
    for name in ("claude", "gemini", "codex"):
        for part in ("requests", "payloads", "states", "log_errors"):
            if a[name][part] != b[name][part]:
                bad += 1
                print(f"DIFF {name}.{part}")
                fa, fb = (Path(tempfile.mkstemp(suffix=f".{x}.json")[1]) for x in ("a", "b"))
                fa.write_text(json.dumps(a[name][part], indent=1, sort_keys=True))
                fb.write_text(json.dumps(b[name][part], indent=1, sort_keys=True))
                subprocess.run(["diff", "-u", str(fa), str(fb)])
            else:
                print(f"same {name}.{part}")
    return 1 if bad else 0


# --------------------------------------------------------------------------- session key


def sessionkey(cmd):
    """Fire two Claude hooks with no session_id from one parent shell; the key is the grandparent pid."""
    h = Home(cmd, "http://127.0.0.1:9")
    h.env["ARIZE_DRY_RUN"] = "true"
    try:
        script = ('set -e; p=\'{"hook_event_name":"SessionStart","source":"startup"}\'; '
                  'echo "$p" | "$@" hook claude session-start; echo "$p" | "$@" hook claude session-start; '
                  'echo "parent=$$"')
        p = subprocess.run(["/bin/sh", "-c", script, "sh", *cmd], env=h.env, cwd=h.home, capture_output=True,
                           text=True, timeout=60)
        parent = p.stdout.strip().splitlines()[-1]
        files = sorted(k for k in h.states())
        print(json.dumps({"driver_pid": os.getpid(), "sh": parent, "state_files": files,
                          "rc": p.returncode, "stderr": p.stderr[-300:]}))
    finally:
        h.close()


# --------------------------------------------------------------------------- dry-run span


def dryrun(cmd):
    """One Claude turn with dry-run + verbose on; exit 1 unless Stop logs its dry-run span."""
    h = Home(cmd, "http://127.0.0.1:9")
    h.env.update(ARIZE_DRY_RUN="true", ARIZE_VERBOSE="true")
    try:
        tr = h.home / "t.jsonl"
        tr.write_text("".join(turn_lines("seed")))
        base = {"session_id": "equiv-dryrun", "transcript_path": str(tr), "cwd": "/equiv"}
        h.run("claude", "session-start", {**base, "hook_event_name": "SessionStart"})
        h.run("claude", "user-prompt-submit", {**base, "hook_event_name": "UserPromptSubmit", "prompt": "x"})
        with open(tr, "a") as f:
            f.write("".join(turn_lines("d", t0=50)))
        h.run("claude", "stop", {**base, "hook_event_name": "Stop"})
        spans = [l for log in h.logs().values() for l in log.splitlines() if "[dry-run] would send span" in l]
        print(f"dry-run spans logged: {len(spans)}" + (f" (first: {spans[0].split('] ', 1)[-1]})" if spans else ""))
        return 0 if spans else 1
    finally:
        h.close()


# --------------------------------------------------------------------------- real HTTPS


def https(cmd, endpoint="otlp.arize.com:443", env_extra=None):
    h = Home(cmd, endpoint)
    try:
        tr = h.home / "t.jsonl"
        tr.write_text("".join(turn_lines("seed")))
        base = {"session_id": "equiv-https", "transcript_path": str(tr), "cwd": "/equiv"}
        env = dict(env_extra or {})
        h.run("claude", "session-start", {**base, "hook_event_name": "SessionStart"}, env=env)
        h.run("claude", "user-prompt-submit", {**base, "hook_event_name": "UserPromptSubmit", "prompt": "x"}, env=env)
        with open(tr, "a") as f:
            f.write("".join(turn_lines("h", t0=50)))
        h.run("claude", "stop", {**base, "hook_event_name": "Stop"}, env=env)
        lines = [l for log in h.logs().values() for l in log.splitlines() if "send" in l.lower()]
        print("\n".join(lines) or "(no send lines logged: the send succeeded)")
    finally:
        h.close()


if __name__ == "__main__":
    a = sys.argv[1:]
    if a[:1] == ["sequence"] and len(a) == 4:
        sequence(a[1], a[2].split(), a[3])
    elif a[:1] == ["diff"] and len(a) == 3:
        sys.exit(diff(a[1], a[2]))
    elif a[:1] == ["sessionkey"] and len(a) == 2:
        sessionkey(a[1].split())
    elif a[:1] == ["dryrun"] and len(a) == 2:
        sys.exit(dryrun(a[1].split()))
    elif a[:1] == ["https"] and len(a) in (2, 3):
        https(a[1].split(), *a[2:])
    else:
        sys.exit(__doc__)
