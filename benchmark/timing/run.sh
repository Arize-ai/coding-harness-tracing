#!/usr/bin/env bash
# Day 1-2 driver. Fill in a build path once it exists; missing builds are skipped.
# First runs are timed twice: on writable copies, and on read-only ones like a root-owned
# MDM install (first-run-ro.txt). Build pbs / pbs-nocompile with build-pbs.sh.
# Each build then gets per-hook timings (UPS/Pre/Post/Stop, back to back and after 2 s idle,
# results/<label>/hooks.json, ~5 min per build). HOOKS_ONLY=1 runs just that step; SKIP_HOOKS=1 skips it.
# Record build time when you build, e.g. for nuitka:
#   SECONDS=0; <build command>; mkdir -p results/nuitka; echo $SECONDS > results/nuitka/build_seconds
set -uo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
B=${BUILDS:-$HERE/../builds}
RESULTS=${RESULTS:-$HERE/../results}  # hooks.json and table.md; bench.sh/first-run.sh always use benchmark/results
#        label        build dir                     executable inside it
CANDIDATES=(
  "venv312      $B/venv312                    bin/arize-harness"
  "pyi-onedir   $B/pyi-onedir/arize-harness   arize-harness"
  "pyi-onefile  $B/pyi-onefile                arize-harness"
  "nuitka       $B/nuitka/arize-harness.dist  arize-harness.bin"
  "pbs          $B/pbs                        bin/arize-harness"
  "pbs-nocompile $B/pbs-nocompile             bin/arize-harness"
  # Check 8 universal build (CHECKS.md): lipo-merged arm64 + x86_64 Nuitka builds from uv 3.12.10, and
  # the arm64 half on its own, so going universal is compared without a Python patch-version change.
  "nuitka-universal     $B/nuitka-universal     arize-harness.bin"
  "nuitka-arm64-3.12.10 $B/nuitka-arm64-3.12.10 arize-harness.bin"
)
for row in "${CANDIDATES[@]}"; do
  read -r label dir exe <<<"$row"
  [ -x "$dir/$exe" ] || { echo "== $label: not built, skipping"; continue; }
  if [ -n "${HOOKS_ONLY:-}" ]; then
    python3 "$HERE/../workload/workload.py" hooks --cmd "$dir/$exe" --label "$label" --results "$RESULTS" \
      || echo "== $label: hooks gate failed (INVALID)"
    continue
  fi
  # A failed gate still records results (shown as INVALID); skip first-run for it.
  BUILD_ROOT=$dir "$HERE/bench.sh" "$label" "$dir/$exe" || { echo "== $label: INVALID, skipping first run"; continue; }
  "$HERE/first-run.sh" "$label" "$dir" "$exe"
  READONLY=1 "$HERE/first-run.sh" "$label" "$dir" "$exe"
  [ -n "${SKIP_HOOKS:-}" ] || python3 "$HERE/../workload/workload.py" hooks --cmd "$dir/$exe" --label "$label" \
    --results "$RESULTS" || echo "== $label: hooks gate failed (INVALID)"
done
"$HERE/summarize.py" "$RESULTS" > "$RESULTS/table.md" && cat "$RESULTS/table.md"
