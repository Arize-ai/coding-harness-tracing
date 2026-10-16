#!/usr/bin/env python3
"""Per-turn cost of the Claude Code hooks on one fixed install (venv312). Measurement only.

The packaging comparison in benchmark/ times startup (SessionStart, a near-empty Stop).
This measures the costs that grow with real use and that no packaging format changes:

  concurrency  N parallel PreToolUse/PostToolUse pairs on one session (lock waits)
  buffer       Pre/Post/Stop with a pre-filled tool observation buffer
  transcript   UserPromptSubmit/Stop with large session transcripts
  invocation   the same event via run-hook, its venv launcher, the console script, arize-harness
  idle         back-to-back runs vs runs after 0.5/2/5 s of idle
  hooks        UPS/Pre/Post/Stop on any build (--cmd), back to back and idle -> results/<label>/hooks.json
  network      dry-run off: local sink, real endpoint + fake key, refused port, blackholed IP
  replay       a recorded turn's hook sequence with its real gaps, with and without hooks

    workload.py <scenario> [--venv DIR] [--quick]      results -> benchmark/results/workload/

Every scenario runs in a throwaway HOME. Except for invocation (which compares paths),
hooks run as `<venv>/bin/arize-harness hook claude <event>`. Dry-run is off: Stop sends
to a local HTTP sink (or the scenario's endpoint), so payload encoding and the send are
included. Timings are wall clock from the driver's spawn to the hook's exit.
"""
import argparse
import json
import os
import socket
import statistics
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE.parent / "results" / "workload"
sys.path.insert(0, str(HERE.parent / "timing"))
from harness import BUFFER_KEY, KB, Harness, fmt, pct, summary, turn_lines  # noqa: E402
from hooks import per_hook  # noqa: E402



# --------------------------------------------------------------------------- sink


class Sink:
    """Local OTLP/HTTP sink: answers 200 to every POST and counts what it got."""

    def __init__(self):
        sink = self
        self.requests = 0
        self.bytes = 0

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                sink.requests += 1
                sink.bytes += len(body)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


# --------------------------------------------------------------------------- transcripts


def filler(path, size):
    """Earlier turns (two ~4 KB tool results each) until the file holds at least `size` bytes."""
    written, i = 0, 0
    with open(path, "w") as f:
        while written < size:
            chunk = "".join(turn_lines(f"old{i}", (f"old{i}-t0", f"old{i}-t1"), result_bytes=4 * KB,
                                       text_bytes=500))
            f.write(chunk)
            written += len(chunk)
            i += 1
    return written


def observation(tool_id, response_bytes):
    """A buffered, completed tool call as tool_buffer.ToolObservation.to_dict() writes it."""
    return {"tool_use_id": tool_id, "tool_name": "Read", "tool_input": {"file_path": f"/workload/{tool_id}.py"},
            "tool_response": "x" * response_bytes, "error": None, "started_at_ms": 1767268800000,
            "ended_at_ms": 1767268800100, "status": "success",
            "hook_event_metadata": {"hook_event_name": "PostToolUse"}}


def save(name, result, out=None):
    out = out or OUT
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{name}.json").write_text(json.dumps(result, indent=2))


def stat_row(label, values):
    s = summary(values)
    return f"| {label} | {s['n']} | {fmt(s['p50'])} | {fmt(s['p95'])} | {fmt(s['p99'])} | {fmt(s['max'])} |"


# --------------------------------------------------------------------------- 1. concurrency


def concurrency(args):
    sink = Sink()
    h = Harness(args.venv, endpoint=sink.url, lock_probe=True)
    rows, result = [], {}
    try:
        for n in (1, 4, 8):
            rounds = 5 if args.quick else max(10, 80 // n)
            pre, post, pre_wall, post_wall, lost = [], [], [], [], 0
            first_tag = f"n{n}-"
            for r in range(rounds):
                h.reset_session()
                ids = [f"toolu_n{n}_r{r}_{i}" for i in range(n)]
                t, wall = h.parallel([("PreToolUse", h.payload("PreToolUse", tool_use_id=t, tool_name="Read",
                                                                tool_input={"file_path": f"/w/{t}"}),
                                       f"{first_tag}pre") for t in ids])
                pre += t
                pre_wall.append(wall)
                t, wall = h.parallel([("PostToolUse", h.payload("PostToolUse", tool_use_id=t, tool_name="Read",
                                                                 tool_input={"file_path": f"/w/{t}"},
                                                                 tool_response="y" * KB),
                                       f"{first_tag}post") for t in ids])
                post += t
                post_wall.append(wall)
                buf = h.buffer()
                lost += sum(1 for t in ids if buf.get(t, {}).get("status") != "success")
            probes = [p for p in h.lock_records() if p["tag"].startswith(first_tag)]
            sleeps = [p["sleeps"] for p in probes]
            result[n] = {
                "rounds": rounds, "pre": summary(pre), "post": summary(post),
                "pre_phase_wall": summary(pre_wall), "post_phase_wall": summary(post_wall),
                "lost_observations": lost, "observations": rounds * n,
                "processes_probed": len(probes),
                "processes_that_slept": sum(1 for s in sleeps if s),
                "lock_sleeps_total": sum(sleeps), "lock_sleeps_max_per_process": max(sleeps, default=0),
                "forced_breaks": sum(p["breaks"] for p in probes),
            }
            rows.append(stat_row(f"N={n} PreToolUse", pre))
            rows.append(stat_row(f"N={n} PostToolUse", post))
    finally:
        h.close()
        sink.close()
    save("concurrency", result)
    print("| hooks | n | p50 | p95 | p99 | max (ms) |\n|---|---|---|---|---|---|")
    print("\n".join(rows))
    print("\n| N | processes that slept on the lock | 100 ms sleeps (total / max in one process) "
          "| forced breaks | lost observations | Pre phase p50 | Post phase p50 (ms) |\n|---|---|---|---|---|---|---|")
    for n, r in result.items():
        print(f"| {n} | {r['processes_that_slept']}/{r['processes_probed']} | {r['lock_sleeps_total']} / "
              f"{r['lock_sleeps_max_per_process']} | {r['forced_breaks']} | {r['lost_observations']}/"
              f"{r['observations']} | {fmt(r['pre_phase_wall']['p50'])} | {fmt(r['post_phase_wall']['p50'])} |")


# --------------------------------------------------------------------------- 2. buffer growth


def buffer_growth(args):
    sink = Sink()
    h = Harness(args.venv, endpoint=sink.url, lock_probe=True)
    counts = (0, 10, 50) if args.quick else (0, 10, 50, 200)
    sizes = (KB, 50 * KB) if args.quick else (KB, 50 * KB, 500 * KB)
    result, rows = [], []
    try:
        for size in sizes:
            for count in counts:
                reps = 2 if args.quick else (10 if count * size <= 5_000_000 else 4)
                times = {"PreToolUse": [], "PostToolUse": [], "Stop": []}
                state_bytes, sent, leftover = 0, 0, 0
                for rep in range(reps):
                    prefilled = [f"toolu_b{rep}_{i}" for i in range(count)]
                    timed = f"toolu_b{rep}_timed"
                    h.reset_session(transcript_text="".join(turn_lines("seed")))
                    with open(h.transcript, "a") as f:
                        f.write("".join(turn_lines(f"b{rep}", prefilled + [timed])))
                    h.inject_buffer({t: observation(t, size) for t in prefilled})
                    times["PreToolUse"].append(h.run("PreToolUse", h.payload(
                        "PreToolUse", tool_use_id=timed, tool_name="Read", tool_input={"file_path": "/w/x"})))
                    times["PostToolUse"].append(h.run("PostToolUse", h.payload(
                        "PostToolUse", tool_use_id=timed, tool_name="Read", tool_input={"file_path": "/w/x"},
                        tool_response="y" * size)))
                    state_bytes = max(state_bytes, h.state_file.stat().st_size)
                    before = sink.requests
                    times["Stop"].append(h.run("Stop", h.payload("Stop", stop_hook_active=False)))
                    sent += sink.requests - before
                    leftover += len(h.buffer())
                row = {"tools": count, "response_bytes": size, "reps": reps, "state_bytes": state_bytes,
                       "spans_sent": sent, "buffer_left_after_stop": leftover,
                       **{e: summary(v) for e, v in times.items()}}
                result.append(row)
                ok = "ok" if sent == reps and leftover == 0 else f"GATE FAIL sent={sent}/{reps} left={leftover}"
                rows.append(f"| {count} | {size // KB} KB | {state_bytes / 1e6:.2f} | {fmt(row['PreToolUse']['p50'])} "
                            f"| {fmt(row['PostToolUse']['p50'])} | {fmt(row['Stop']['p50'])} "
                            f"| {fmt(row['Stop']['max'])} | {ok} |")
                print(rows[-1], flush=True)
    finally:
        h.close()
        sink.close()
    save("buffer", result)
    print("\n| buffered tools | tool_response | state file (MB) | Pre p50 | Post p50 | Stop p50 | Stop max (ms) "
          "| gate |\n|---|---|---|---|---|---|---|---|")
    print("\n".join(rows))


# --------------------------------------------------------------------------- 3. transcript size


def transcript_size(args):
    sink = Sink()
    h = Harness(args.venv, endpoint=sink.url)
    sizes = (10 * KB, KB * KB) if args.quick else (10 * KB, KB * KB, 10 * KB * KB, 50 * KB * KB)
    last_turn = "".join(turn_lines("last", ("toolu_last_0", "toolu_last_1")))
    result, rows = [], []
    try:
        for size in sizes:
            reps = 2 if args.quick else (20 if size <= KB * KB else 10 if size <= 10 * KB * KB else 5)
            base = h.home / "filler.jsonl"
            actual = filler(base, size)
            body = base.read_text()
            ups, stop, sent = [], [], 0
            for _ in range(reps):
                h.state_file.unlink(missing_ok=True)
                h.transcript.write_text(body)
                h.run("SessionStart", h.payload("SessionStart", source="startup"))
                ups.append(h.run("UserPromptSubmit", h.payload("UserPromptSubmit", prompt="workload")))
                with open(h.transcript, "a") as f:
                    f.write(last_turn)
                before = sink.requests
                stop.append(h.run("Stop", h.payload("Stop", stop_hook_active=False)))
                sent += sink.requests - before
            row = {"transcript_bytes": actual, "reps": reps, "spans_sent": sent,
                   "UserPromptSubmit": summary(ups), "Stop": summary(stop)}
            result.append(row)
            ok = "ok" if sent == reps else f"GATE FAIL sent={sent}/{reps}"
            rows.append(f"| {actual / 1e6:.2f} | {fmt(row['UserPromptSubmit']['p50'])} | "
                        f"{fmt(row['UserPromptSubmit']['max'])} | {fmt(row['Stop']['p50'])} | "
                        f"{fmt(row['Stop']['max'])} | {ok} |")
            print(rows[-1], flush=True)
    finally:
        h.close()
        sink.close()
    save("transcript", result)
    print("\n| transcript (MB) | UserPromptSubmit p50 | UPS max | Stop p50 | Stop max (ms) | gate |"
          "\n|---|---|---|---|---|---|")
    print("\n".join(rows))


# --------------------------------------------------------------------------- 4. invocation path


def invocation(args):
    sink = Sink()
    h = Harness(args.venv, endpoint=sink.url)
    paths = ("run-hook", "launcher", "console", "harness")
    runs = 10 if args.quick else 60
    result, rows = {}, []
    try:
        h.bootstrap_plugin_venv()
        for event in ("SessionStart", "PreToolUse", "Stop"):
            for path in paths:
                for _ in range(3):  # warm the page cache and __pycache__ for this path
                    h.run(event, h.payload(event), path=path)
                times = []
                for i in range(runs):
                    if event == "Stop":
                        h.reset_session()
                    p = h.payload(event, tool_use_id=f"toolu_inv_{i}", tool_name="Read",
                                  tool_input={"file_path": "/w/x"}, source="startup")
                    times.append(h.run(event, p, path=path))
                result[f"{event}/{path}"] = summary(times)
                rows.append(f"| {event} | {path} | {fmt(statistics.mean(times))} | {fmt(pct(times, 50))} | "
                            f"{fmt(pct(times, 95))} |")
                print(rows[-1], flush=True)
    finally:
        h.close()
        sink.close()
    save("invocation", result)
    print("\n| event | path | mean | p50 | p95 (ms) |\n|---|---|---|---|---|")
    print("\n".join(rows))


# --------------------------------------------------------------------------- 4b. idle gap


def idle_gap(args):
    """Back-to-back runs (what hyperfine and the other scenarios measure) vs runs after a pause.

    In a real session every hook follows a gap (model thinking, a tool running), so this
    shows how far tight-loop numbers understate what a user sees.
    """
    sink = Sink()
    h = Harness(args.venv, endpoint=sink.url)
    runs = 5 if args.quick else 20
    result, rows = {}, []
    try:
        h.reset_session()
        for gap in (0, 0.5, 2, 5):
            for event in ("PreToolUse", "Stop"):
                times = []
                for i in range(runs):
                    if event == "Stop":
                        h.reset_session()
                    time.sleep(gap)
                    times.append(h.run(event, h.payload(event, tool_use_id=f"toolu_idle_{gap}_{i}",
                                                        tool_name="Read", tool_input={"file_path": "/w/x"})))
                result[f"{event}/{gap}"] = summary(times)
                rows.append(stat_row(f"{event} after {gap} s idle", times))
                print(rows[-1], flush=True)
    finally:
        h.close()
        sink.close()
    save("idle", result)
    print("\n| hook | n | p50 | p95 | p99 | max (ms) |\n|---|---|---|---|---|---|")
    print("\n".join(rows))


# --------------------------------------------------------------------------- 5. network


def _closed_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def network(args):
    sink = Sink()
    targets = [
        ("sink", sink.url),
        ("fake key (real endpoint)", "otlp.arize.com:443"),
        ("refused port", f"http://127.0.0.1:{_closed_port()}"),
        ("blackholed IP", "http://10.255.255.1:4318"),
    ]
    if os.environ.get("SKIP_REMOTE"):
        targets = [t for t in targets if not t[1].startswith("otlp.arize.com")]
    turns = 2 if args.quick else 3
    result, rows = {}, []
    try:
        for label, endpoint in targets:
            h = Harness(args.venv, endpoint=endpoint)
            try:
                h.transcript.write_text("".join(turn_lines("seed")))
                h.run("SessionStart", h.payload("SessionStart", source="startup"))
                per_turn = []
                for t in range(turns):
                    fails0 = h.log_count("send failed")
                    ups = h.run("UserPromptSubmit", h.payload("UserPromptSubmit", prompt=f"turn {t}"))
                    ids = [f"toolu_net{t}_{i}" for i in range(3)]
                    tool_ms = 0.0
                    for tid in ids:
                        tool_ms += h.run("PreToolUse", h.payload("PreToolUse", tool_use_id=tid, tool_name="Read",
                                                                 tool_input={"file_path": "/w/x"}))
                        tool_ms += h.run("PostToolUse", h.payload("PostToolUse", tool_use_id=tid, tool_name="Read",
                                                                  tool_input={"file_path": "/w/x"},
                                                                  tool_response="y" * 10 * KB))
                    with open(h.transcript, "a") as f:
                        f.write("".join(turn_lines(f"net{t}", ids, t0=100 * (t + 1))))
                    stop = h.run("Stop", h.payload("Stop", stop_hook_active=False))
                    st = h.state()
                    per_turn.append({
                        "turn": t, "UserPromptSubmit_ms": ups, "pre_post_ms_total": tool_ms, "Stop_ms": stop,
                        "failed_sends": h.log_count("send failed") - fails0,
                        "buffer_entries": len(json.loads(st.get(BUFFER_KEY) or "{}")),
                        "state_bytes": h.state_file.stat().st_size,
                        "turn_left_open": bool(st.get("current_trace_id")),
                        "trace_count": st.get("trace_count"),
                        "last_failure": h.last_log_line("send failed"),
                    })
                    r = per_turn[-1]
                    rows.append(f"| {label} | {t + 1} | {fmt(ups)} | {fmt(stop)} | {r['failed_sends']} | "
                                f"{r['buffer_entries']} | {r['state_bytes'] / 1e3:.1f} | {r['trace_count']} |")
                    print(rows[-1], flush=True)
                result[label] = per_turn
                if per_turn[-1]["last_failure"]:
                    print(f"  {label}: {per_turn[-1]['last_failure']}", flush=True)
            finally:
                h.close()
    finally:
        sink.close()
    save("network", result)
    print("\n| endpoint | turn | UserPromptSubmit (ms) | Stop (ms) | failed sends this turn | buffered tools after Stop "
          "| state (KB) | trace_count |\n|---|---|---|---|---|---|---|---|")
    print("\n".join(rows))


# --------------------------------------------------------------------------- 6. replay


def replay_schedule(transcript):
    """(offset seconds, event, payload extras) for the last turn of a recorded transcript.

    Only timestamps, tool ids and tool names are read; prompts and tool content are not.
    """
    entries = []
    for raw in Path(transcript).read_text(errors="replace").splitlines():
        try:
            e = json.loads(raw)
        except ValueError:
            continue
        if isinstance(e, dict) and isinstance(e.get("message"), dict) and e.get("timestamp"):
            entries.append(e)
    starts = [i for i, e in enumerate(entries) if e["message"].get("role") == "user"
              and isinstance(e["message"].get("content"), str) and not e.get("isSidechain")]
    if not starts:
        raise SystemExit("no user prompt found in the recorded transcript")
    turn = entries[starts[-1]:]

    def secs(e):
        from datetime import datetime
        return datetime.fromisoformat(e["timestamp"].replace("Z", "+00:00")).timestamp()

    t0 = secs(turn[0])
    events = [(0.0, "UserPromptSubmit", {})]
    for e in turn:
        content = e["message"].get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                events.append((secs(e) - t0, "PreToolUse", {"tool_use_id": block.get("id"),
                                                            "tool_name": block.get("name")}))
            elif block.get("type") == "tool_result":
                events.append((secs(e) - t0, "PostToolUse", {"tool_use_id": block.get("tool_use_id"),
                                                             "tool_response": "y" * KB}))
    events.append((secs(turn[-1]) - t0, "Stop", {}))
    events.sort(key=lambda x: x[0])
    return events


def replay(args):
    if not args.transcript:
        raise SystemExit("replay needs --transcript <recorded Claude Code session .jsonl>")
    schedule = replay_schedule(args.transcript)
    if args.max_gap:
        # Cap long gaps (a human reading, a slow tool) so a replay stays short; the hook cost is unaffected.
        capped, prev, shift = [], 0.0, 0.0
        for at, ev, extra in schedule:
            shift += max(0.0, (at - prev) - args.max_gap)
            prev = at
            capped.append((at - shift, ev, extra))
        schedule = capped
    sink = Sink()
    h = Harness(args.venv, endpoint=sink.url)
    tools = sum(1 for _, e, _ in schedule if e == "PreToolUse")
    result = {"events": len(schedule), "tools": tools, "recorded_span_s": schedule[-1][0]}
    try:
        h.transcript.write_text("".join(turn_lines("seed")))
        h.run("SessionStart", h.payload("SessionStart", source="startup"))
        tool_ids = [x["tool_use_id"] for _, e, x in schedule if e == "PreToolUse"]
        for mode in ("without hooks", "with hooks"):
            # Hooks are synchronous: Claude waits for each one before it goes on, so a hook's time
            # pushes every later event back. Model that by sleeping only the remaining gap.
            hook_ms, t_start = [], time.perf_counter()
            for at, event, extra in schedule:
                delay = at - (time.perf_counter() - t_start) + (sum(hook_ms) / 1000 if mode == "with hooks" else 0)
                if delay > 0:
                    time.sleep(delay)
                if mode == "with hooks":
                    if event == "Stop":
                        with open(h.transcript, "a") as f:
                            f.write("".join(turn_lines("replay", tool_ids)))
                    hook_ms.append(h.run(event, h.payload(event, **extra)))
            result[mode] = {"wall_s": time.perf_counter() - t_start, "hook_ms_total": sum(hook_ms),
                            "hooks": summary(hook_ms) if hook_ms else None}
            print(f"{mode}: {result[mode]['wall_s']:.2f} s wall, hooks {sum(hook_ms):.0f} ms over "
                  f"{len(hook_ms)} calls", flush=True)
        result["spans_sent"] = sink.requests
    finally:
        h.close()
        sink.close()
    save("replay", result)
    extra = result["with hooks"]["wall_s"] - result["without hooks"]["wall_s"]
    print(f"\nturn of {tools} tools, {len(schedule)} hook events: +{extra * 1000:.0f} ms "
          f"({100 * extra / result['without hooks']['wall_s']:.1f}%) with hooks; spans sent: {result['spans_sent']}")


SCENARIOS = {"concurrency": concurrency, "buffer": buffer_growth, "transcript": transcript_size,
             "invocation": invocation, "idle": idle_gap, "hooks": per_hook, "network": network, "replay": replay}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("scenario", choices=SCENARIOS)
    ap.add_argument("--venv", default=os.environ.get("VENV", str(HERE.parent / "builds" / "venv312")))
    ap.add_argument("--quick", action="store_true", help="few reps, small sizes: check the harness works")
    ap.add_argument("--transcript", help="replay: a recorded Claude Code transcript")
    ap.add_argument("--max-gap", type=float, default=5.0, help="replay: cap each inter-hook gap (s); 0 = none")
    ap.add_argument("--cmd", help="hooks: a build's executable, run as `<cmd> hook claude <event>`")
    ap.add_argument("--label", help="hooks: build label; results go to <results>/<label>/hooks.json")
    ap.add_argument("--results", default=str(HERE.parent / "results"), help="hooks: results root")
    ap.add_argument("--runs", type=int, default=30, help="hooks: timed runs per hook per mode")
    ap.add_argument("--idle", type=float, default=2.0, help="hooks: idle seconds before each idle-mode call")
    args = ap.parse_args()
    if args.scenario != "hooks" and not (Path(args.venv) / "bin" / "arize-harness").exists():
        raise SystemExit(f"no venv312 install at {args.venv}: build one with setup.sh or pass --venv")
    SCENARIOS[args.scenario](args)


if __name__ == "__main__":
    main()
