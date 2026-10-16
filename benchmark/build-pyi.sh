#!/usr/bin/env bash
# Build the PyInstaller candidates from benchmark/entry.py.
#   MODE=onedir  build-pyi.sh  -> builds/pyi-onedir/arize-harness/arize-harness   (default)
#   MODE=onefile build-pyi.sh  -> builds/pyi-onefile/arize-harness
# Hidden imports and data files come from build_inputs.py, never by hand. build/, the
# spec file and PyInstaller's warn-*.txt (missing-module report) go to .workbench/pyi-<mode>/.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd); REPO=$(cd "$HERE/.." && pwd)
MODE=${MODE:-onedir}
case "$MODE" in onedir|onefile) ;; *) echo "MODE must be onedir or onefile" >&2; exit 2 ;; esac
LABEL=pyi-$MODE
DIST=$HERE/builds/$LABEL
if [ "$MODE" = onedir ]; then DEST=$DIST/arize-harness; EXE=$DEST/arize-harness
else DEST=$DIST/arize-harness; EXE=$DEST; fi
# shellcheck source=build-common.sh
. "$HERE/build-common.sh"

SECONDS=0
clean_dest "$DIST"
ARGS=()
while read -r m; do ARGS+=(--hidden-import "$m"); done < <(inputs modules)
while IFS=$'\t' read -r src dest; do ARGS+=(--add-data "$src:$(dirname "$dest")"); done < <(inputs data)

"$PY" -m PyInstaller --noconfirm --clean --log-level WARN "--$MODE" --name arize-harness \
  --paths "$REPO" --distpath "$DIST" --workpath "$WORKBENCH/build" --specpath "$WORKBENCH" \
  "${ARGS[@]}" "$HERE/entry.py"

# PyInstaller only warns about modules it couldn't find. Surface any that are ours
# (core.* / tracing.*), since those are the ones that would break a hook at runtime.
WARN=$WORKBENCH/build/arize-harness/warn-arize-harness.txt
if [ -f "$WARN" ] && grep -E "missing module named '?(core|tracing)[.']" "$WARN"; then
  echo "PyInstaller could not find the modules above (full report: $WARN)" >&2
fi

finish "$EXE"
