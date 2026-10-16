#!/usr/bin/env bash
# Build the Nuitka standalone candidate from benchmark/entry.py.
#   build-nuitka.sh -> builds/nuitka/arize-harness.dist/arize-harness.bin
# Modules and data files come from build_inputs.py, never by hand. Nuitka's build
# directory and compilation report go to .workbench/nuitka/.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd); REPO=$(cd "$HERE/.." && pwd)
LABEL=nuitka
DIST=$HERE/builds/nuitka
DEST=$DIST/arize-harness.dist
EXE=$DEST/arize-harness.bin
# shellcheck source=build-common.sh
. "$HERE/build-common.sh"

SECONDS=0
clean_dest "$DIST"
ARGS=()
while read -r m; do ARGS+=("--include-module=$m"); done < <(inputs modules)
while IFS=$'\t' read -r src dest; do ARGS+=("--include-data-files=$src=$dest"); done < <(inputs data)

# Nuitka finds core/ and tracing/ through PYTHONPATH; run from .workbench so its
# build directory lands there. No --assume-yes-for-downloads: if Nuitka wants to
# fetch something, that's a finding, not something to accept silently.
(cd "$WORKBENCH" && PYTHONPATH=$REPO "$PY" -m nuitka --mode=standalone \
  --output-dir="$WORKBENCH" --output-folder-name=arize-harness.dist --output-filename=arize-harness.bin \
  --report="$WORKBENCH/compilation-report.xml" --quiet \
  "${ARGS[@]}" "$HERE/entry.py")

rm -rf "$DIST" && mkdir -p "$DIST"
mv "$WORKBENCH/arize-harness.dist" "$DEST"
finish "$EXE"
