#!/usr/bin/env bash
# First launch of a freshly copied build: what a user sees right after an MDM install,
# before macOS has scanned (and cached a verdict for) the new binaries. Warm hyperfine
# runs hide this. Each trial copies the build to a new path and times exactly one run.
#   first-run.sh <label> <build dir> <executable path relative to build dir> [trials]
#
# A pip console script (venv312, pbs: bin/arize-harness) has the ORIGINAL interpreter's
# absolute path in its shebang, so running the copy would run the old, already-scanned
# Python. For builds with bin/python3 we run the copied interpreter instead. A venv's
# bin/python3 is itself a symlink to uv's shared interpreter, so venv312's first-run
# number is never a true first run; only self-contained builds give a meaningful one.
set -euo pipefail
LABEL=$1; BUILD=$2; EXE=$3; TRIALS=${4:-5}
HERE=$(cd "$(dirname "$0")" && pwd); OUT=$HERE/results/$LABEL; mkdir -p "$OUT"
FAKE_HOME=$(mktemp -d); WORK=$(mktemp -d)
trap 'rm -rf "$FAKE_HOME" "$WORK"' EXIT
export HOME=$FAKE_HOME ARIZE_DRY_RUN=true
for i in $(seq "$TRIALS"); do
  COPY=$WORK/build-$i; cp -R "$BUILD" "$COPY"
  if [ -x "$COPY/bin/python3" ]; then
    RUN=("$COPY/bin/python3" -m core.cli)
  else
    RUN=("$COPY/$EXE")
  fi
  # cwd=$WORK so `-m core.cli` can't pick up the repo's own core/ from the current directory.
  (cd "$WORK" && python3 - "${RUN[@]}" <<'PY'
import subprocess, sys, time
t = time.perf_counter()
subprocess.run(sys.argv[1:] + ["hook", "claude", "session-start"], input=b"{}", check=False)
print(f"{(time.perf_counter() - t) * 1000:.1f}")
PY
  )
  rm -rf "$COPY"
done | tee "$OUT/first-run.txt" | awk '{s+=$1; if($1>m)m=$1} END {printf "first run: mean %.1f ms, worst %.1f ms over %d copies\n", s/NR, m, NR}'
if [ -L "$BUILD/bin/python3" ] && [[ "$(cd "$BUILD/bin" && readlink -f python3)" != "$(cd "$BUILD" && pwd -P)"/* ]]; then
  echo "note: $LABEL's interpreter lives outside the build, so this is not a true first run" >&2
fi
