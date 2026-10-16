#!/usr/bin/env bash
# Day 1-2 driver. Fill in a build path once it exists; missing builds are skipped.
# First runs are timed twice: on writable copies, and on read-only ones like a root-owned
# MDM install (first-run-ro.txt). Build pbs / pbs-nocompile with build-pbs.sh.
# Record build time when you build, e.g. for nuitka:
#   SECONDS=0; <build command>; mkdir -p results/nuitka; echo $SECONDS > results/nuitka/build_seconds
set -uo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
B=${BUILDS:-$HERE/builds}
#        label        build dir                     executable inside it
CANDIDATES=(
  "venv312      $B/venv312                    bin/arize-harness"
  "pyi-onedir   $B/pyi-onedir/arize-harness   arize-harness"
  "pyi-onefile  $B/pyi-onefile                arize-harness"
  "nuitka       $B/nuitka/arize-harness.dist  arize-harness.bin"
  "pbs          $B/pbs                        bin/arize-harness"
  "pbs-nocompile $B/pbs-nocompile             bin/arize-harness"
)
for row in "${CANDIDATES[@]}"; do
  read -r label dir exe <<<"$row"
  [ -x "$dir/$exe" ] || { echo "== $label: not built, skipping"; continue; }
  # A failed gate still records results (shown as INVALID); skip first-run for it.
  BUILD_ROOT=$dir "$HERE/bench.sh" "$label" "$dir/$exe" || { echo "== $label: INVALID, skipping first run"; continue; }
  "$HERE/first-run.sh" "$label" "$dir" "$exe"
  READONLY=1 "$HERE/first-run.sh" "$label" "$dir" "$exe"
done
"$HERE/summarize.py" "$HERE/results" > "$HERE/results/table.md" && cat "$HERE/results/table.md"
