#!/usr/bin/env bash
# Time every build that exists, then write bench-result.json and table.md to the results directory.
#   timing/run.sh [--quick] [--results DIR]   other arguments go to hooks.py
# --results DIR (default benchmark/results) holds everything this run writes and reads back: first
# runs, bench-result.json, table.md. Build records (build_seconds, build_info.json) stay in
# benchmark/results, where the build scripts write them.
# Per build: first launches of fresh copies (first-run.sh), writable and read-only like a
# root-owned MDM install. Then all builds together: the five Claude hooks, back to back and
# after 0.5 s idle, builds alternating (hooks.py, ~10 min for three builds). HOOKS_ONLY=1 skips
# the first runs and keeps their earlier results. A failed first run doesn't stop the script: the
# build shows as INVALID in the table. Build first: build/venv.sh, build/nuitka.sh
# (ARCH=arm64 and ARCH=x86_64), build/universal.sh; missing builds are skipped.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
B=$HERE/../builds
RESULTS=$HERE/../results
PASS=()  # arguments for hooks.py, without --results: run.sh passes its own
while [ $# -gt 0 ]; do
  case $1 in
    --results) [ $# -ge 2 ] || { echo "run.sh: --results needs a directory" >&2; exit 1; }; RESULTS=$2; shift 2 ;;
    --results=*) RESULTS=${1#--results=}; shift ;;
    *) PASS+=("$1"); shift ;;
  esac
done
mkdir -p "$RESULTS"
RESULTS=$(cd "$RESULTS" && pwd)
ARGS=()
# Each build is builds/<label>, as the build scripts name them. dir and exe are set per label, never
# split out of one string, so a checkout path with spaces in it still works.
for label in venv312 nuitka-arm64 nuitka-universal; do
  dir=$B/$label
  case $label in
    venv312) exe=bin/arize-harness ;;
    nuitka-arm64 | nuitka-universal) exe=arize-harness.bin ;;
    *) echo "run.sh: no executable known for $label" >&2; exit 1 ;;
  esac
  [ -x "$dir/$exe" ] || { echo "== $label: not built, skipping"; continue; }
  ARGS+=(--build "$label" "$dir" "$exe")
  [ -n "${HOOKS_ONLY:-}" ] && continue
  echo "== $label: first runs"
  BENCH_RESULTS=$RESULTS "$HERE/first-run.sh" "$label" "$dir" "$exe" || echo "== $label: first run failed, INVALID"
  BENCH_RESULTS=$RESULTS READONLY=1 "$HERE/first-run.sh" "$label" "$dir" "$exe" ||
    echo "== $label: first run (read-only) failed, INVALID"
done
[ ${#ARGS[@]} -gt 0 ] || { echo "nothing built" >&2; exit 1; }
echo "== hooks: $((${#ARGS[@]} / 4)) builds"  # 4 words per build: --build label dir exe
rc=0
# An earlier run's result must never stand in for this one's, e.g. after hooks.py crashes.
rm -f "$RESULTS/bench-result.json" "$RESULTS/table.md"
# ${PASS[@]+...}: bash 3.2 (macOS) treats an empty array as unset under set -u.
python3 "$HERE/hooks.py" ${PASS[@]+"${PASS[@]}"} --results "$RESULTS" "${ARGS[@]}" ||
  rc=$?  # a failed gate still writes the result (INVALID)
[ -f "$RESULTS/bench-result.json" ] ||
  { echo "run.sh: hooks.py exited $rc without writing $RESULTS/bench-result.json" >&2; exit "$((rc ? rc : 1))"; }
"$HERE/summarize.py" "$RESULTS/bench-result.json" >"$RESULTS/table.md"
cat "$RESULTS/table.md"
exit $rc
