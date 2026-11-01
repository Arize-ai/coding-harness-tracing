#!/usr/bin/env bash
# First launch of a freshly copied build: what a user sees right after an MDM install,
# before macOS has scanned (and cached a verdict for) the new binaries. Warm hyperfine
# runs hide this. Each trial copies the build to a new path and times exactly one run.
#   first-run.sh <label> <build dir> <executable path relative to build dir> [trials]
#
# READONLY=1 makes each copy read-only first, like a root-owned .pkg install where users
# can't write __pycache__, and writes first-run-ro.txt instead of first-run.txt.
# BENCH_RESULTS=DIR writes to DIR/<label>/ instead of benchmark/results/<label>/ (run.sh --results).
#
# Every copy's run has to exit 0 within 30 s: a build that crashes on its first launch is fast
# too. A copy that doesn't is left out of the times and its reason goes to first-run.failed
# (first-run-ro.failed), which timing/hooks.py turns into INVALID; the script then exits 1.
#
# A pip console script (venv312's bin/arize-harness) has the ORIGINAL interpreter's
# absolute path in its shebang, so running the copy would run the old, already-scanned
# Python. For those we run the copied bin/python3 instead. A venv's bin/python3 is itself
# a symlink to uv's shared interpreter, so venv312's first-run number is never a true
# first run; only self-contained builds give a meaningful one.
set -euo pipefail
LABEL=$1; BUILD=$2; EXE=$3; TRIALS=${4:-5}
HERE=$(cd "$(dirname "$0")" && pwd); OUT=${BENCH_RESULTS:-$HERE/../results}/$LABEL; mkdir -p "$OUT"
RESULT=$OUT/first-run.txt FAILED=$OUT/first-run.failed
if [ "${READONLY:-}" = 1 ]; then RESULT=$OUT/first-run-ro.txt FAILED=$OUT/first-run-ro.failed; fi
rm -f "$FAILED"
FAKE_HOME=$(mktemp -d); WORK=$(mktemp -d)
trap 'chmod -R u+w "$WORK" 2>/dev/null; rm -rf "$FAKE_HOME" "$WORK"' EXIT
export HOME=$FAKE_HOME ARIZE_DRY_RUN=true
for i in $(seq "$TRIALS"); do
  # cp -R keeps the read-only modes finish() puts on Nuitka bundles: make each copy writable first.
  COPY=$WORK/build-$i; cp -R "$BUILD" "$COPY"; chmod -R u+w "$COPY"
  [ "${READONLY:-}" = 1 ] && chmod -R a-w "$COPY"
  if head -1 "$COPY/$EXE" | grep -q '^#!/.*python'; then
    # -I: the hook's cwd must not put the repo's own core/ on sys.path.
    RUN=("$COPY/bin/python3" -I -m core.cli)
  else
    RUN=("$COPY/$EXE")
  fi
  # The time on stdout, or why the run failed on stderr (exit 1). An if, because under set -e a
  # failing subshell would end the loop, and with it the remaining copies.
  if ! (cd "$WORK" && python3 - "${RUN[@]}" 2>"$WORK/why" <<'PY'
import subprocess, sys, time
t = time.perf_counter()
try:
    p = subprocess.run(
        sys.argv[1:] + ["hook", "claude", "session-start"],
        input=b"{}",
        stdout=subprocess.DEVNULL,  # the times go to stdout
        stderr=subprocess.PIPE,
        timeout=30,
    )
except subprocess.TimeoutExpired as e:
    sys.exit(f"timed out after {e.timeout:g} s")
ms = (time.perf_counter() - t) * 1000
if p.returncode:
    how = f"exit {p.returncode}" if p.returncode > 0 else f"killed by signal {-p.returncode}"
    last = p.stderr.decode(errors="replace").strip().splitlines()[-1:]
    sys.exit(how + (f": {last[0]}" if last else ""))
print(f"{ms:.1f}")
PY
  ); then
    echo "copy $i: $(head -1 "$WORK/why")" >>"$FAILED"
  fi
  chmod -R u+w "$COPY"; rm -rf "$COPY"
done | tee "$RESULT" | awk -v ro="${READONLY:+ (read-only)}" \
  '{s+=$1; if($1>m)m=$1} END {if (NR) printf "first run%s: mean %.1f ms, worst %.1f ms over %d copies\n", ro, s/NR, m, NR}'
if [ -L "$BUILD/bin/python3" ] && [[ "$(cd "$BUILD/bin" && readlink -f python3)" != "$(cd "$BUILD" && pwd -P)"/* ]]; then
  echo "note: $LABEL's interpreter lives outside the build, so this is not a true first run" >&2
fi
if [ -s "$FAILED" ]; then
  echo "FIRST RUN FAILED: $LABEL${READONLY:+ (read-only)}, $(wc -l <"$FAILED" | tr -d ' ')/$TRIALS copies:" >&2
  sed 's/^/  /' "$FAILED" >&2
  exit 1
fi
