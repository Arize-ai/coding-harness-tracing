"""Per-hook timing on any build: UPS, Pre, Post and Stop, back to back and after an idle pause.

Run through workload/workload.py's `hooks` scenario for now.
"""
import json
import sys
import time
from pathlib import Path

from harness import KB, Harness, fmt, summary, turn_lines


def save(name, result, out):
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{name}.json").write_text(json.dumps(result, indent=2))


DRY_RUN_SPAN = "[dry-run] would send span"
HOOKS = ("UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop")


def per_hook(args):
    """UPS, Pre, Post and Stop on any build, back to back and after an idle pause.

    Setup matches benchmark/bench.sh (throwaway HOME, dry-run + verbose, `<cmd> hook claude
    <event>`), so results/<label>/hooks.json sits beside its startup numbers. Before every
    timed call the session is reset and the untimed calls that make it do real work run first:
    SessionStart (+ UPS, which opens a turn), the matching Pre before a Post, and Pre/Post of
    the turn's tool before Stop, whose transcript then holds a 1-tool turn to correlate.
    Gate: every timed and warmup Stop logs one dry-run span.
    """
    if not args.cmd or not args.label:
        raise SystemExit("hooks needs --cmd '<build executable>' and --label <build label>")
    runs = 3 if args.quick else args.runs
    warmup = 1 if args.quick else 5
    idle = 0.2 if args.quick else args.idle
    h = Harness(None, dry_run=True, cmd=args.cmd.split())
    out = Path(args.results) / args.label
    result = {"cmd": args.cmd, "runs": runs, "warmup": warmup, "idle_s": idle, "modes": {}}
    stops = spans_seen = 0
    try:
        for mode, gap in (("back-to-back", 0.0), ("idle", idle)):
            result["modes"][mode] = {}
            for event in HOOKS:
                times = []
                for i in range(warmup + runs if mode == "back-to-back" else runs):
                    tid = f"toolu_{mode}_{event}_{i}"
                    tool = dict(tool_use_id=tid, tool_name="Read", tool_input={"file_path": "/w/x.py"})
                    h.state_file.unlink(missing_ok=True)
                    h.transcript.write_text("".join(turn_lines("seed")))
                    h.run("SessionStart", h.payload("SessionStart", source="startup"))
                    if event == "UserPromptSubmit":
                        payload = h.payload(event, prompt="workload")
                    else:
                        h.run("UserPromptSubmit", h.payload("UserPromptSubmit", prompt="workload"))
                        with open(h.transcript, "a") as f:
                            f.write("".join(turn_lines(f"t{i}", (tid,))))
                        if event in ("PostToolUse", "Stop"):
                            h.run("PreToolUse", h.payload("PreToolUse", **tool))
                        if event == "Stop":
                            h.run("PostToolUse", h.payload("PostToolUse", **tool, tool_response="y" * KB))
                            payload = h.payload(event, stop_hook_active=False)
                        elif event == "PostToolUse":
                            payload = h.payload(event, **tool, tool_response="y" * KB)
                        else:
                            payload = h.payload(event, **tool)
                    before = h.log_count(DRY_RUN_SPAN)
                    if gap:
                        time.sleep(gap)
                    ms = h.run(event, payload)
                    if event == "Stop":
                        stops += 1
                        spans_seen += min(1, h.log_count(DRY_RUN_SPAN) - before)
                    if mode == "idle" or i >= warmup:
                        times.append(ms)
                result["modes"][mode][event] = summary(times)
                s = result["modes"][mode][event]
                print(f"{args.label:<14} {mode:<12} {event:<17} p50 {fmt(s['p50']):>6}  p95 {fmt(s['p95']):>6} ms",
                      flush=True)
    finally:
        h.close()
    result["gate"] = "ok" if stops and spans_seen == stops else "fail"
    result["spans"] = f"{spans_seen}/{stops}"
    save("hooks", result, out)
    print(f"{args.label}: dry-run spans {result['spans']} gate={result['gate']} -> {out / 'hooks.json'}")
    if result["gate"] != "ok":
        sys.exit(1)
