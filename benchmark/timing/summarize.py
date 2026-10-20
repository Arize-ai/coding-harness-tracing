#!/usr/bin/env python3
"""results/bench-result.json (timing/hooks.py) -> Markdown table, one column per build.

  timing/summarize.py [results/bench-result.json]

A build gets PASS/FAIL only if its gate is ok: inputs.py check passed and every Stop logged a
dry-run span. Otherwise it is INVALID, since a build that does nothing is also fast. The budget
rule: back-to-back p95 at most BUDGET_MS slower than venv312 on SessionStart and on Stop. Every
other number is reported, not judged.
"""
import json
import sys
from pathlib import Path

BUDGET_MS = 25.0
BUDGET_EVENTS = ("SessionStart", "Stop")
EVENTS = (("SessionStart", "SessionStart"), ("UserPromptSubmit", "UPS"), ("PreToolUse", "Pre"),
          ("PostToolUse", "Post"), ("Stop", "Stop"))
TURN_TOOLS = 20
BENCH = Path(__file__).resolve().parent.parent


def verdict(name, r, base):
    if r["gate"] != "ok":
        why = r["error"] or ("inputs.py check failed" if r["inputs_check"] != "ok" else f"spans {r['spans']}")
        return f"INVALID ({why})"
    if name == "venv312":
        return "baseline"
    if not base:
        return "no valid baseline"
    try:
        slower = [r["hooks"]["b2b"][e]["p95"] - base["hooks"]["b2b"][e]["p95"] for e in BUDGET_EVENTS]
    except KeyError:
        return "no valid baseline"
    return "PASS" if all(d <= BUDGET_MS for d in slower) else "FAIL"


def ms(x):
    return "-" if x is None else f"{x:.0f}"


def first(r, key):
    f = r["first_run_ms"].get(key)
    return f"{ms(f['p50'])} / {ms(f['max'])}" if f else "-"


def hook(r, mode, event):
    s = r["hooks"].get(mode, {}).get(event)
    return f"{ms(s['p50'])} / {ms(s['p95'])}" if s else "-"


def turn(r):
    idle = r["hooks"].get("idle", {})
    try:
        t = (idle["UserPromptSubmit"]["p50"] + TURN_TOOLS * (idle["PreToolUse"]["p50"] + idle["PostToolUse"]["p50"])
             + idle["Stop"]["p50"])
    except KeyError:
        return "-"
    return f"{t / 1000:.2f} s"


def main():
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else BENCH / "results" / "bench-result.json"
    res = json.loads(path.read_text())
    builds = res["builds"]
    base = builds.get("venv312")
    if base and base["gate"] != "ok":
        base = None  # an invalid baseline can't judge anyone
    idle_s = res["config"]["idle_s"]
    rows = [
        ("budget", lambda n, r: verdict(n, r, base)),
        ("size / files / .so+.dylib", lambda n, r: f"{r['size_bytes'] / 2**20:.0f}M / {r['files']} / {r['native_libs']}"),
        ("build time (s)", lambda n, r: ms(r["build_s"])),
        ("first run, writable: p50 / worst (ms)", lambda n, r: first(r, "writable")),
        ("first run, read-only: p50 / worst (ms)", lambda n, r: first(r, "readonly")),
    ]
    for mode, title in (("b2b", "back to back"), ("idle", f"after {idle_s:g} s idle")):
        for event, short in EVENTS:
            rows.append((f"{short} {title}: p50 / p95 (ms)", lambda n, r, m=mode, e=event: hook(r, m, e)))
    rows.append((f"{TURN_TOOLS}-tool turn, idle p50s (est.)", lambda n, r: turn(r)))

    print(f"Python {res['python']}, Nuitka {res['nuitka']}, commit {res['commit'][:12] + ('-dirty' if res['commit'].endswith('-dirty') else '') or '?'}, "
          f"{res['config']['runs']} runs per event and mode, {res['config']['warmup']} b2b warmups, "
          f"macOS {res['config']['macos']} {res['config']['machine']}.\n")
    print("| | " + " | ".join(builds) + " |")
    print("|---|" + "---|" * len(builds))
    for title, cell in rows:
        print(f"| {title} | " + " | ".join(cell(n, r) for n, r in builds.items()) + " |")
    print()
    print(f"Budget: back-to-back p95 at most {BUDGET_MS:.0f} ms slower than venv312 on {' and '.join(BUDGET_EVENTS)}. "
          "First runs and idle numbers are reported, not judged.")
    print(f"{TURN_TOOLS}-tool turn = UPS + {TURN_TOOLS}×Pre + {TURN_TOOLS}×Post + Stop, idle p50s.")


if __name__ == "__main__":
    main()
