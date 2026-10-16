#!/usr/bin/env bash
# Build the Nuitka standalone candidate from benchmark/build/entry.py for one architecture.
#   ARCH=arm64  build/nuitka.sh -> builds/nuitka-arm64/arize-harness.bin   (default)
#   ARCH=x86_64 build/nuitka.sh -> builds/nuitka-x86_64/arize-harness.bin  (re-runs itself under arch -x86_64)
# build/universal.sh merges the two. Modules and data files come from inputs.py, never by hand.
# Nuitka's build directory and compilation report go to .workbench/nuitka-<arch>/.
set -euo pipefail
# Under --quiet, Nuitka's "download ccache?" prompt waits forever on a stdin that is open but not
# a TTY (CI, agent shells). With /dev/null it takes the default (no) and goes on.
exec </dev/null
ARCH=${ARCH:-arm64}
case $ARCH in
  arm64 | x86_64) ;;
  *) echo "ARCH must be arm64 or x86_64, not $ARCH" >&2; exit 1 ;;
esac
if [ "$ARCH" = x86_64 ] && [ "$(uname -m)" != x86_64 ]; then
  # Rosetta: the build tools, the compiler and the smoke test all run as x86_64.
  exec arch -x86_64 /bin/bash "$0" "$@"
fi
HERE=$(cd "$(dirname "$0")" && pwd); BENCH=$(cd "$HERE/.." && pwd); REPO=$(cd "$BENCH/.." && pwd)
LABEL=nuitka-$ARCH
DEST=$BENCH/builds/$LABEL
EXE=$DEST/arize-harness.bin
# shellcheck source=common.sh
. "$HERE/common.sh"

SECONDS=0
buildtools "$ARCH"
clean_dest "$DEST"
ARGS=()
while read -r m; do ARGS+=("--include-module=$m"); done < <(inputs modules)
while IFS=$'\t' read -r src dest; do ARGS+=("--include-data-files=$src=$dest"); done < <(inputs data)

# Nuitka finds core/ and tracing/ through PYTHONPATH; run from .workbench so its
# build directory lands there. No --assume-yes-for-downloads: if Nuitka wants to
# fetch something, that's a finding, not something to accept silently.
rm -rf "$WORKBENCH/arize-harness.dist"
(cd "$WORKBENCH" && PYTHONPATH=$REPO "$PY" -m nuitka --mode=standalone \
  --output-dir="$WORKBENCH" --output-folder-name=arize-harness.dist --output-filename=arize-harness.bin \
  --report="$WORKBENCH/compilation-report.xml" --quiet \
  "${ARGS[@]}" "$HERE/entry.py")

rm -rf "$DEST"
mv "$WORKBENCH/arize-harness.dist" "$DEST"
archs=$(lipo -archs "$EXE")
[ "$archs" = "$ARCH" ] || { echo "BUILD FAILED: $EXE is '$archs', want $ARCH" >&2; exit 1; }
finish "$EXE"
