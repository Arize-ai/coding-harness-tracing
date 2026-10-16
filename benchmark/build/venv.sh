#!/usr/bin/env bash
# The venv312 baseline: this checkout pip-installed into a venv on the same pinned arm64
# interpreter the Nuitka build uses, the way the hooks run today.
#   build/venv.sh -> builds/venv312/bin/arize-harness
# Left writable, like a user's own venv: it writes __pycache__ on first use.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd); BENCH=$(cd "$HERE/.." && pwd); REPO=$(cd "$BENCH/.." && pwd)
LABEL=venv312
DEST=$BENCH/builds/venv312
EXE=$DEST/bin/arize-harness
# shellcheck source=common.sh
. "$HERE/common.sh"

SECONDS=0
clean_dest "$DEST"
uv venv -q --managed-python --no-config -p "$(pinned_python arm64)" "$DEST"
PY=$DEST/bin/python
check_python "$PY" arm64
# -c: the same python-dotenv as the build tools, without installing Nuitka here.
uv pip install -q --python "$PY" -c "$REQUIREMENTS" \
  --reinstall-package coding-harness-tracing --refresh-package coding-harness-tracing "$REPO"

echo "-- smoke test: $EXE"
"$PY" -I "$INPUTS" check "$EXE" || { echo "BUILD FAILED SMOKE TEST: $LABEL" >&2; exit 1; }
mkdir -p "$BENCH/results/$LABEL"
echo "$SECONDS" > "$BENCH/results/$LABEL/build_seconds"
echo "built $LABEL in ${SECONDS}s: $EXE"
