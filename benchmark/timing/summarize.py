#!/usr/bin/env python3
"""results/<label>/*.json + meta.txt + first-run.txt + build_seconds -> Markdown table for the decision doc.

A row only gets PASS/FAIL if bench.sh recorded gate=ok (every timed Stop sent a span);
a failed or missing gate is INVALID, since a build that does nothing is also fast.
Build time is read from results/<label>/build_seconds if present (see run.sh).

Per-hook columns come from results/<label>/hooks.json (workload/workload.py hooks): p50 / p95
of UserPromptSubmit, PreToolUse, PostToolUse and Stop, back to back and after an idle pause,
plus a 20-tool turn estimate from the idle p50s. A failed hooks gate also makes the row INVALID;
the budget rule itself still uses only the startup columns.
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


HOOKS = (("UserPromptSubmit", "UPS"), ("PreToolUse", "Pre"), ("PostToolUse", "Post"), ("Stop", "Stop"))
TURN_TOOLS = 20


def hook_cells(hooks):
    """Per-hook p50 / p95 cells (back to back, then idle) and the 20-tool turn estimate."""
    if not hooks:
        return ["-"] * (2 * len(HOOKS) + 1)
    cells = []
    for mode in ("back-to-back", "idle"):
        for event, _ in HOOKS:
            s = hooks["modes"].get(mode, {}).get(event)
            cells.append(f"{s['p50']:.0f} / {s['p95']:.0f}" if s else "-")
    idle = hooks["modes"].get("idle", {})
    try:
        turn = (idle["UserPromptSubmit"]["p50"] + TURN_TOOLS * (idle["PreToolUse"]["p50"] + idle["PostToolUse"]["p50"])
                + idle["Stop"]["p50"])
        cells.append(f"{turn / 1000:.2f} s")
    except KeyError:
        cells.append("-")
    return cells


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
    hooks_text = read(d / "hooks.json")
    rows[d.name]["hooks"] = json.loads(hooks_text) if hooks_text else None

base = rows.get("venv312")
if base and base.get("gate") != "ok":
    base = None  # an invalid baseline can't judge anyone
idle_s = next((r["hooks"]["idle_s"] for r in rows.values() if r["hooks"]), 2)
hook_headers = [f"{short} {mode} p50 / p95 (ms)" for mode in ("b2b", f"idle {idle_s:g}s") for _, short in HOOKS]
print("| build | SessionStart mean / p95 (ms) | Stop mean / p95 (ms) | first run, worst (ms) | first run read-only, worst (ms) | size | files "
      "| .so/.dylib | build time (s) | budget | " + " | ".join(hook_headers) + f" | {TURN_TOOLS}-tool turn, idle (est.) |")
print("|---|---|---|---|---|---|---|---|---|---|" + "---|" * (len(hook_headers) + 1))
for name, r in rows.items():
    if r.get("gate") != "ok":
        ok = f"INVALID (spans {r.get('spans', 'unknown')})"
    elif r["hooks"] and r["hooks"].get("gate") != "ok":
        ok = f"INVALID (hook spans {r['hooks'].get('spans', 'unknown')})"
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
          f"| {r['build_s'] or '-'} | {ok} | " + " | ".join(hook_cells(r["hooks"])) + " |")
print()
print(f"Budget: warm p95 at most {BUDGET_MS:.0f} ms slower than venv312 on both events. "
      "First-run times are reported, not judged against it.")
print(f"Per-hook columns: {TURN_TOOLS}-tool turn = UPS + {TURN_TOOLS}×Pre + {TURN_TOOLS}×Post + Stop, idle p50s; "
      "reported, not judged against the budget.")
