#!/usr/bin/env bash
# Fixed-overhead floor for the baseline: what part of hook startup is not ours to win.
#   overhead.sh <python-3.12-interpreter>
set -euo pipefail
PY=$1
HERE=$(cd "$(dirname "$0")" && pwd); OUT=$HERE/results/overhead; mkdir -p "$OUT"
FAKE_HOME=$(mktemp -d)
trap 'rm -rf "$FAKE_HOME"' EXIT
export HOME=$FAKE_HOME

hyperfine -N --warmup 10 --runs 200 --export-json "$OUT/overhead.json" \
  -n 'python -c pass'        "$PY -c pass" \
  -n 'python -S -c pass'     "$PY -S -c pass" \
  -n 'python -I -c pass'     "$PY -I -c pass" \
  -n 'ps ppid lookup'        "ps -o ppid= -p $$" \
  -n 'import core.cli'       "$PY -c 'import core.cli'" \
  -n 'import claude handlers' "$PY -c 'import tracing.claude_code.hooks.handlers'"

# Import-time breakdown, read once by eye (sort by cumulative column).
"$PY" -X importtime -c 'import tracing.claude_code.hooks.handlers' 2> "$OUT/importtime.txt"
sort -t'|' -k2 -n -r "$OUT/importtime.txt" | head -15
