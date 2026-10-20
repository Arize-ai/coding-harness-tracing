#!/usr/bin/env bash
# Build the Nuitka standalone candidate from benchmark/build/entry.py for one architecture.
#   ARCH=arm64  build/nuitka.sh -> builds/nuitka-arm64/arize-harness.bin   (default)
#   ARCH=x86_64 build/nuitka.sh -> builds/nuitka-x86_64/arize-harness.bin  (re-runs itself under arch -x86_64)
# build/universal.sh merges the two. Modules and data files come from inputs.py, never by hand.
# Nuitka's build directory and compilation report go to .workbench/nuitka-<arch>/.
set -euo pipefail
# Never prompt: under --quiet, a Nuitka prompt (e.g. "download ccache?") waits forever on a stdin
# that is open but not a TTY (CI, agent shells). With /dev/null it takes the default (no) and goes on.
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

buildtools "$ARCH"
SECONDS=0  # build time: the build itself, not setting up the build tools
# Capture the lists first: a failure inside < <(...) would go unnoticed and build without them.
# Before clean_dest, so a build that stops here keeps the previous good bundle. finish() reuses
# INPUT_DATA instead of running inputs.py again.
modules=$(inputs modules) || { echo "BUILD STOPPED: inputs.py modules failed" >&2; exit 1; }
INPUT_DATA=$(inputs data) || { echo "BUILD STOPPED: inputs.py data failed" >&2; exit 1; }
clean_dest "$DEST"
# if, not [ -n ] && ...: under set -e a false test as the loop's last command ends the script.
ARGS=()
while read -r m; do
  if [ -n "$m" ]; then ARGS+=("--include-module=$m"); fi
done <<<"$modules"
while IFS=$'\t' read -r src dest; do
  if [ -n "$src" ]; then ARGS+=("--include-data-files=$src=$dest"); fi
done <<<"$INPUT_DATA"

# Nuitka finds core/ and tracing/ through PYTHONPATH; run from .workbench so its
# build directory lands there. No --assume-yes-for-downloads: if Nuitka wants to
# fetch something, that's a finding, not something to accept silently.
# No ccache: every build compiles from scratch, so build times compare across runs.
rm -rf "$WORKBENCH/arize-harness.dist"
(cd "$WORKBENCH" && PYTHONPATH=$REPO "$PY" -m nuitka --mode=standalone \
  --output-dir="$WORKBENCH" --output-folder-name=arize-harness.dist --output-filename=arize-harness.bin \
  --report="$WORKBENCH/compilation-report.xml" --quiet \
  --disable-cache=ccache \
  "${ARGS[@]}" "$HERE/entry.py")

rm -rf "$DEST"
mv "$WORKBENCH/arize-harness.dist" "$DEST"
archs=$(lipo -archs "$EXE")
[ "$archs" = "$ARCH" ] || { echo "BUILD FAILED: $EXE is '$archs', want $ARCH" >&2; exit 1; }
finish "$EXE"
