#!/usr/bin/env python3
"""results/<label>/*.json + meta.txt + first-run.txt + build_seconds -> Markdown table for the decision doc.

A row only gets PASS/FAIL if bench.sh recorded gate=ok (every timed Stop sent a span);
a failed or missing gate is INVALID, since a build that does nothing is also fast.
Build time is read from results/<label>/build_seconds if present (see run-all.sh).
"""
import json, statistics, sys
from pathlib import Path

BUDGET_MS = 25.0  # p95 may be at most this much slower than the venv baseline, on both events
root = Path(sys.argv[1])


def times(path):
    r = json.loads(path.read_text())["results"][0]
    ms = [1000 * (m["time_wall_clock"]["value"] if "measurements" in r else m)
          for m in r.get("measurements", r.get("times", []))]
    ms.sort()
    return statistics.mean(ms), ms[int(0.95 * (len(ms) - 1))]


def read(path):
    return path.read_text().strip() if path.exists() else None


rows = {}
for d in sorted(p for p in root.iterdir() if (p / "stop.json").exists()):
    meta_text = read(d / "meta.txt") or ""
    meta = dict(l.split("=", 1) for l in meta_text.splitlines() if "=" in l)
    first = read(d / "first-run.txt")
    first_ms = max(map(float, first.split())) if first else None
    first_ro = read(d / "first-run-ro.txt")
    first_ro_ms = max(map(float, first_ro.split())) if first_ro else None
    rows[d.name] = dict(start=times(d / "session-start.json"), stop=times(d / "stop.json"), first=first_ms, first_ro=first_ro_ms,
                        build_s=read(d / "build_seconds"), **meta)

base = rows.get("venv312")
if base and base.get("gate") != "ok":
    base = None  # an invalid baseline can't judge anyone
print("| build | SessionStart mean / p95 (ms) | Stop mean / p95 (ms) | first run, worst (ms) | first run read-only, worst (ms) | size | files "
      "| .so/.dylib | build time (s) | budget |")
print("|---|---|---|---|---|---|---|---|---|---|")
for name, r in rows.items():
    if r.get("gate") != "ok":
        ok = f"INVALID (spans {r.get('spans', 'unknown')})"
    elif name == "venv312":
        ok = "baseline"
    elif not base:
        ok = "no valid baseline"
    else:
        ok = "PASS" if all(r[e][1] - base[e][1] <= BUDGET_MS for e in ("start", "stop")) else "FAIL"
    first = f"{r['first']:.0f}" if r["first"] is not None else "-"
    first_ro = f"{r['first_ro']:.0f}" if r["first_ro"] is not None else "-"
    print(f"| {name} | {r['start'][0]:.1f} / {r['start'][1]:.1f} | {r['stop'][0]:.1f} / {r['stop'][1]:.1f} "
          f"| {first} | {first_ro} | {r.get('size', '-')} | {r.get('files', '-')} | {r.get('native_libs', '-')} "
          f"| {r['build_s'] or '-'} | {ok} |")
print()
print(f"Budget: warm p95 at most {BUDGET_MS:.0f} ms slower than venv312 on both events. "
      "First-run times are reported, not judged against it.")
