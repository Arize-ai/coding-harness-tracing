#!/usr/bin/env bash
# Probe builds for checks 3/4: same tool flags and build_inputs.py lists as build-nuitka.sh /
# build-pyi.sh, plus probe_entry.py and the four modules it imports dynamically (a hand-written
# include, so these builds do not count as "clean"). Output: $OUT/{nuitka,pyi-onedir}.
#   REPO=<checkout with benchmark/build_inputs.py> OUT=<dir> build-probe.sh
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
REPO=${REPO:-$(cd "$HERE/../.." && pwd)}
OUT=${OUT:?set OUT}
PY=${BUILDTOOLS:-$REPO/.workbench/buildtools}/bin/python
inputs() { "$PY" -I "$REPO/benchmark/build_inputs.py" "$@"; }
EXTRA=(core.setup.status tracing.opencode.install tracing.omp.install tracing.devin.install)
mkdir -p "$OUT/work"

NK=(); while read -r m; do NK+=("--include-module=$m"); done < <(inputs modules; printf '%s\n' "${EXTRA[@]}")
while IFS=$'\t' read -r src dest; do NK+=("--include-data-files=$src=$dest"); done < <(inputs data)
(cd "$OUT/work" && PYTHONPATH=$REPO "$PY" -m nuitka --mode=standalone --output-dir="$OUT/work" \
  --output-folder-name=nuitka.dist --output-filename=arize-harness.bin --quiet "${NK[@]}" "$HERE/probe_entry.py")
rm -rf "$OUT/nuitka"; mv "$OUT/work/nuitka.dist" "$OUT/nuitka"

PI=(); while read -r m; do PI+=(--hidden-import "$m"); done < <(inputs modules; printf '%s\n' "${EXTRA[@]}")
while IFS=$'\t' read -r src dest; do PI+=(--add-data "$src:$(dirname "$dest")"); done < <(inputs data)
"$PY" -m PyInstaller --noconfirm --clean --log-level WARN --onedir --name arize-harness --paths "$REPO" \
  --distpath "$OUT/pyi-dist" --workpath "$OUT/work/pyi" --specpath "$OUT/work" "${PI[@]}" "$HERE/probe_entry.py"
rm -rf "$OUT/pyi-onedir"; mv "$OUT/pyi-dist/arize-harness" "$OUT/pyi-onedir"
echo "probe builds: $OUT/nuitka/arize-harness.bin $OUT/pyi-onedir/arize-harness"
