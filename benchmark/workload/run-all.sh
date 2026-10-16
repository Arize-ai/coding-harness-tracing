#!/usr/bin/env bash
# Per-turn hook cost on the venv312 install (follow-up to #90274). Measurement only.
#   run-all.sh [--quick]               all scenarios except replay
#   REPLAY=<transcript.jsonl> run-all.sh   also replay that recorded turn
# Uses $VENV, else benchmark/builds/venv312 (built here if missing). Output:
# benchmark/results/workload/<scenario>.{json,md}. The network scenario sends a few
# requests with a fake key to otlp.arize.com (expect 401); SKIP_REMOTE=1 skips it.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
OUT=$HERE/../results/workload
export VENV=${VENV:-$HERE/../builds/venv312}
if [ ! -x "$VENV/bin/arize-harness" ]; then
  # A venv path over ~127 bytes gets a /bin/sh shim instead of a shebang, which adds one exec
  # per hook; prefer a short VENV path when comparing against benchmark/ numbers.
  [ "$VENV" = "$HERE/../builds/venv312" ] || { echo "no arize-harness in VENV=$VENV" >&2; exit 1; }
  "$HERE/../build/venv.sh"
fi
mkdir -p "$OUT"
SCENARIOS=(concurrency buffer transcript invocation idle network)
for s in "${SCENARIOS[@]}"; do
  echo "== $s"
  python3 "$HERE/workload.py" "$s" "$@" | tee "$OUT/$s.md"
done
if [ -n "${REPLAY:-}" ]; then
  echo "== replay"
  python3 "$HERE/workload.py" replay --transcript "$REPLAY" "$@" | tee "$OUT/replay.md"
fi
