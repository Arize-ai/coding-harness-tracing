#!/usr/bin/env bash
# Time every build that exists, then write results/bench-result.json and results/table.md.
#   timing/run.sh [--quick]          extra arguments go to hooks.py
# Per build: first launches of fresh copies (first-run.sh), writable and read-only like a
# root-owned MDM install. Then all builds together: the five Claude hooks, back to back and
# after 0.5 s idle, builds alternating (hooks.py, ~10 min for three builds). HOOKS_ONLY=1 skips
# the first runs and keeps their earlier results. Build first: build/venv.sh, build/nuitka.sh
# (ARCH=arm64 and ARCH=x86_64), build/universal.sh; missing builds are skipped.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
B=$HERE/../builds
#        label             build dir           executable inside it
CANDIDATES=(
  "venv312          $B/venv312          bin/arize-harness"
  "nuitka-arm64     $B/nuitka-arm64     arize-harness.bin"
  "nuitka-universal $B/nuitka-universal arize-harness.bin"
)
ARGS=()
for row in "${CANDIDATES[@]}"; do
  read -r label dir exe <<<"$row"
  [ -x "$dir/$exe" ] || { echo "== $label: not built, skipping"; continue; }
  ARGS+=(--build "$label" "$dir" "$exe")
  [ -n "${HOOKS_ONLY:-}" ] && continue
  echo "== $label: first runs"
  "$HERE/first-run.sh" "$label" "$dir" "$exe"
  READONLY=1 "$HERE/first-run.sh" "$label" "$dir" "$exe"
done
[ ${#ARGS[@]} -gt 0 ] || { echo "nothing built" >&2; exit 1; }
echo "== hooks: ${#ARGS[@]} builds"
rc=0
python3 "$HERE/hooks.py" "$@" "${ARGS[@]}" || rc=$?  # a failed gate still writes the result (INVALID)
"$HERE/summarize.py" "$HERE/../results/bench-result.json" > "$HERE/../results/table.md"
cat "$HERE/../results/table.md"
exit $rc
