#!/usr/bin/env python3
"""Per-hook timing for several builds at once -> results/bench-result.json (Arize-ai/arize#90274).

  timing/hooks.py [--quick] [--runs N] [--idle S] [--results DIR] --build LABEL DIR EXE [--build ...]

Each build is DIR/EXE, run as `<exe> hook claude <event>` in its own throwaway HOME with dry-run
and verbose on. Every event is timed the same way: SessionStart, UserPromptSubmit, PreToolUse,
PostToolUse and Stop, back to back (b2b) and after an idle pause (default 0.5 s). Builds
alternate: each round runs every build once, in an order that rotates per round, so drift on
the machine (heat, background work) falls on all builds alike instead of on whichever ran last.

Before every timed call the session is reset and the untimed calls that make it do real work run
first: SessionStart (unless SessionStart is the one timed), UPS (which opens a turn), the matching
Pre before a Post, and Pre/Post of the turn's tool before Stop, whose transcript then holds a
1-tool turn to correlate. Timings are wall clock from the driver's spawn to the hook's exit.

Gates, per build. A failed gate marks the build INVALID, since a build that does nothing is fast too:
  inputs  build/inputs.py check passes before timing (every hook loads and exits 0)
  spans   every Stop, warmups included, logs one dry-run span
A hook that exits non-zero while being timed also fails its build, which then stops being timed.

Also recorded per build: size_bytes, files and native_libs (.so/.dylib) of DIR, counting regular
files only; build_s from results/<label>/build_seconds; first_run_ms from first-run.sh's
results/<label>/first-run.txt (writable) and first-run-ro.txt (readonly).
"""
import argparse
import json
import platform
import re
import subprocess
import sys
import time
from pathlib import Path

from harness import KB, Harness, fmt, pct, turn_lines

HERE = Path(__file__).resolve().parent
BENCH = HERE.parent
SCHEMA = 1
DRY_RUN_SPAN = "[dry-run] would send span"
EVENTS = ("SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop")
MODES = ("b2b", "idle")


class Build:
    def __init__(self, label, root, exe):
        self.label = label
        self.root = Path(root).resolve()
        self.exe = str(self.root / exe)
        self.harness = None
        self.times = {mode: {event: [] for event in EVENTS} for mode in MODES}
        self.stops = self.spans = 0
        self.error = None


def footprint(root):
    """Size, file count and native library count of a build, like `find -type f`."""
    files = [p for p in root.rglob("*") if p.is_file() and not p.is_symlink()]
    return {
        "size_bytes": sum(p.stat().st_size for p in files),
        "files": len(files),
        "native_libs": sum(p.suffix in (".so", ".dylib") for p in files),
    }


def first_runs(results, label):
    out = {}
    for key, name in (("writable", "first-run.txt"), ("readonly", "first-run-ro.txt")):
        f = results / label / name
        runs = [float(x) for x in f.read_text().split()] if f.exists() else []
        out[key] = {"p50": pct(runs, 50), "max": max(runs), "runs": runs} if runs else None
    return out


def read_int(path):
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return None


def inputs_check(build):
    p = subprocess.run(
        [sys.executable, "-I", str(BENCH / "build" / "inputs.py"), "check", build.exe], capture_output=True, text=True
    )
    print(f"{build.label}: {(p.stdout or p.stderr).strip().splitlines()[-1:]}", flush=True)
    if p.returncode != 0:
        print(p.stderr.strip()[-2000:], file=sys.stderr)
    return p.returncode == 0


def prepare(h, event, tag):
    """Reset the session and run the untimed calls `event` needs; return the timed call's payload."""
    tid = f"toolu_{tag}"
    tool = dict(tool_use_id=tid, tool_name="Read", tool_input={"file_path": "/w/x.py"})
    h.state_file.unlink(missing_ok=True)
    h.transcript.write_text("".join(turn_lines("seed")))
    if event == "SessionStart":
        return h.payload(event, source="startup")
    h.run("SessionStart", h.payload("SessionStart", source="startup"))
    if event == "UserPromptSubmit":
        return h.payload(event, prompt="bench")
    h.run("UserPromptSubmit", h.payload("UserPromptSubmit", prompt="bench"))
    with open(h.transcript, "a") as f:
        f.write("".join(turn_lines(f"t-{tag}", (tid,))))
    if event == "PreToolUse":
        return h.payload(event, **tool)
    h.run("PreToolUse", h.payload("PreToolUse", **tool))
    if event == "PostToolUse":
        return h.payload(event, **tool, tool_response="y" * KB)
    h.run("PostToolUse", h.payload("PostToolUse", **tool, tool_response="y" * KB))
    return h.payload(event, stop_hook_active=False)


def one_call(b, mode, event, i, gap, timed):
    try:
        payload = prepare(b.harness, event, f"{mode}_{event}_{i}")
        before = b.harness.log_count(DRY_RUN_SPAN) if event == "Stop" else 0
        if gap:
            time.sleep(gap)
        ms = b.harness.run(event, payload)
    except SystemExit as e:  # Harness.run: the hook exited non-zero
        b.error = f"{mode} {event}: {e}"
        print(f"{b.label}: FAILED, no longer timed: {b.error}", file=sys.stderr, flush=True)
        return
    if event == "Stop":
        b.stops += 1
        b.spans += min(1, b.harness.log_count(DRY_RUN_SPAN) - before)
    if timed:
        b.times[mode][event].append(ms)


def time_builds(builds, runs, warmup, idle):
    for mode, gap in (("b2b", 0.0), ("idle", idle)):
        for event in EVENTS:
            n = warmup + runs if mode == "b2b" else runs  # warm up once, before the b2b runs
            for i in range(n):
                live = [b for b in builds if not b.error]
                k = i % len(live) if live else 0
                for b in live[k:] + live[:k]:
                    one_call(b, mode, event, i, gap, timed=mode == "idle" or i >= warmup)
            for b in builds:
                t = b.times[mode][event]
                if t:
                    print(
                        f"{b.label:<17} {mode:<5} {event:<17} p50 {fmt(pct(t, 50)):>6}  p95 {fmt(pct(t, 95)):>6} ms",
                        flush=True,
                    )


def git(*args):
    p = subprocess.run(["git", "-C", str(BENCH), *args], capture_output=True, text=True)
    return p.stdout.strip() if p.returncode == 0 else ""


def pins():
    python = re.search(r"^PY_VERSION=(\S+)", (BENCH / "build" / "common.sh").read_text(), re.M)
    nuitka = re.search(r"^nuitka==(\S+)", (BENCH / "requirements-build.txt").read_text(), re.M)
    return python and python.group(1), nuitka and nuitka.group(1)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument(
        "--build",
        nargs=3,
        action="append",
        required=True,
        metavar=("LABEL", "DIR", "EXE"),
        help="a build to time: its label, its directory, the executable inside it",
    )
    ap.add_argument("--runs", type=int, default=30, help="timed runs per event per mode, per build")
    ap.add_argument("--warmup", type=int, default=5, help="untimed b2b runs per event, per build")
    ap.add_argument("--idle", type=float, default=0.5, help="idle seconds before each idle-mode call")
    ap.add_argument("--quick", action="store_true", help="3 runs, 1 warmup: check the harness works")
    ap.add_argument("--results", default=str(BENCH / "results"), help="results root")
    a = ap.parse_args()
    runs, warmup = (3, 1) if a.quick else (a.runs, a.warmup)
    results = Path(a.results)
    builds = [Build(*spec) for spec in a.build]

    checked = {}
    for b in builds:
        checked[b.label] = inputs_check(b)
        if checked[b.label]:
            b.harness = Harness([b.exe], dry_run=True)
        else:
            b.error = "inputs.py check failed"
    try:
        time_builds(builds, runs, warmup, a.idle)
    finally:
        for b in builds:
            if b.harness:
                b.harness.close()

    commit = git("rev-parse", "HEAD")
    if commit and git("status", "--porcelain", "--untracked-files=no"):
        commit += "-dirty"
    python, nuitka = pins()
    out = {
        "schema": SCHEMA,
        "commit": commit,
        "python": python,
        "nuitka": nuitka,
        "config": {
            "runs": runs,
            "warmup": warmup,
            "idle_s": a.idle,
            "events": list(EVENTS),
            "order": "builds alternate per run, order rotated each run",
            "driver_python": platform.python_version(),
            "machine": platform.machine(),
            "macos": platform.mac_ver()[0],
        },
        "builds": {},
    }
    for b in builds:
        ok = checked[b.label] and not b.error and b.stops > 0 and b.spans == b.stops
        out["builds"][b.label] = {
            "cmd": b.exe,
            "gate": "ok" if ok else "fail",
            "inputs_check": "ok" if checked[b.label] else "fail",
            "spans": f"{b.spans}/{b.stops}",
            "error": b.error,
            **footprint(b.root),
            "build_s": read_int(results / b.label / "build_seconds"),
            "first_run_ms": first_runs(results, b.label),
            "hooks": {
                mode: {
                    event: {"p50": pct(t, 50), "p95": pct(t, 95), "n": len(t)}
                    for event, t in b.times[mode].items()
                    if t
                }
                for mode in MODES
            },
        }
        print(
            f"{b.label}: inputs check {out['builds'][b.label]['inputs_check']}, dry-run spans {b.spans}/{b.stops}, "
            f"gate={out['builds'][b.label]['gate']}",
            flush=True,
        )
    results.mkdir(parents=True, exist_ok=True)
    path = results / "bench-result.json"
    path.write_text(json.dumps(out, indent=2) + "\n")
    print(f"-> {path}")
    return 0 if all(r["gate"] == "ok" for r in out["builds"].values()) else 1


if __name__ == "__main__":
    sys.exit(main())
