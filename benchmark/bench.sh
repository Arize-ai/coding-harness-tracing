#!/usr/bin/env bash
# Binary feasibility spike (Arize-ai/arize#90274): startup benchmarks.
#
#   bench.sh <label> <arize-harness command>      e.g.
#   bench.sh venv312        "$VENV/bin/arize-harness"
#   bench.sh pyi-onedir     dist/arize-harness/arize-harness
#   bench.sh nuitka         arize-harness.dist/arize-harness.bin
#   bench.sh pbs            pbs/bin/arize-harness
#
# Writes results/<label>/{session-start,stop}.json and meta.txt (size and file
# counts of BUILD_ROOT, default: the directory holding the executable). first-run.sh adds
# first-run.txt to the same directory. Never touches the real HOME.
set -euo pipefail

LABEL=$1; CMD=$2
REPO=${REPO:-$(cd "$(dirname "$0")/.." && pwd)}
HERE=$(cd "$(dirname "$0")" && pwd)
OUT=$HERE/results/$LABEL
RUNS=${RUNS:-200}
mkdir -p "$OUT"
rm -f "$OUT/meta.txt"  # no stale gate=ok from an earlier run if this one dies early

# Throwaway HOME for every run of this script: no real ~/.claude, ~/.arize.
FAKE_HOME=$(mktemp -d)
trap 'rm -rf "$FAKE_HOME"' EXIT
export HOME=$FAKE_HOME ARIZE_DRY_RUN=true ARIZE_VERBOSE=true ARIZE_TRACE_ENABLED=true

# Payloads use the fixture's real session id. Stop only sends a span when a
# UserPromptSubmit opened a turn first, so every timed Stop gets an untimed
# prepare step that opens a fresh turn; without it all but the first run are no-ops.
TRANSCRIPT=$FAKE_HOME/transcript.jsonl
cp "$REPO/tests/tracing/claude/fixtures/main_tool_cycle.jsonl" "$TRANSCRIPT"
SID=session-main-1
printf '{"session_id":"%s","hook_event_name":"SessionStart","source":"startup"}' "$SID" > "$FAKE_HOME/start.json"
printf '{"session_id":"%s","hook_event_name":"UserPromptSubmit","prompt":"bench","transcript_path":"%s"}' \
  "$SID" "$TRANSCRIPT" > "$FAKE_HOME/prompt.json"
printf '{"session_id":"%s","hook_event_name":"Stop","transcript_path":"%s","stop_hook_active":false}' \
  "$SID" "$TRANSCRIPT" > "$FAKE_HOME/stop.json"
OPEN_TURN="sh -c '$CMD hook claude user-prompt-submit < $FAKE_HOME/prompt.json'"

WARMUP=10
bench() {  # name, stdin file, prepare command ("" for none), command...
  local name=$1 input=$2 prepare=$3; shift 3
  hyperfine -N --warmup $WARMUP --runs "$RUNS" --input "$input" \
    ${prepare:+--prepare "$prepare"} --export-json "$OUT/$name.json" "$*" >/dev/null
  python3 - "$OUT/$name.json" "$name" <<'PY'
import json, sys, statistics
r = json.load(open(sys.argv[1]))["results"][0]
# hyperfine 2.x: results[].measurements[].time_wall_clock.value; 1.x: results[].times
t = sorted(1000 * (m["time_wall_clock"]["value"] if "measurements" in r else m)
           for m in r.get("measurements", r.get("times", [])))
p95 = t[int(0.95 * (len(t) - 1))]
print(f"{sys.argv[2]:<16} mean {statistics.mean(t):6.1f} ms  p95 {p95:6.1f} ms  n={len(t)}")
PY
}

echo "== $LABEL =="
bench session-start "$FAKE_HOME/start.json" ""           "$CMD hook claude session-start"
bench stop          "$FAKE_HOME/stop.json"  "$OPEN_TURN" "$CMD hook claude stop"

# Correctness gate: every timed Stop (plus warmups) must have sent a span,
# otherwise the numbers measure a no-op. Recorded as gate= in meta.txt so
# summarize.py marks the row INVALID; exit 1 only after meta.txt is written.
LOG=$FAKE_HOME/.arize/harness/logs/claude-code.log
SPANS=$(grep -c '\[dry-run\] would send span' "$LOG" 2>/dev/null || true)
WANT=$((RUNS + WARMUP))
if [ "${SPANS:-0}" -ge "$WANT" ]; then
  GATE=ok
  echo "dry-run spans: $SPANS/$WANT OK"
else
  GATE=fail
  echo "dry-run spans: ${SPANS:-0}/$WANT FAIL, numbers not trustworthy (log copied to $OUT)" >&2
  cp "$LOG" "$OUT/claude-code.log" 2>/dev/null || true
fi

# Size / file count / nested native libs of BUILD_ROOT (default: the directory of CMD's first word).
BIN=$(command -v "${CMD%% *}")
ROOT=${BUILD_ROOT:-$(dirname "$BIN")}
{
  echo "label=$LABEL"
  echo "cmd=$CMD"
  echo "gate=$GATE"
  echo "spans=${SPANS:-0}/$WANT"
  echo "size=$(du -sh "$ROOT/" | cut -f1)"
  echo "files=$(find "$ROOT/" -type f | wc -l | tr -d ' ')"
  echo "native_libs=$(find "$ROOT/" -type f \( -name '*.so' -o -name '*.dylib' \) | wc -l | tr -d ' ')"
} > "$OUT/meta.txt"
cat "$OUT/meta.txt"
[ "$GATE" = ok ]
