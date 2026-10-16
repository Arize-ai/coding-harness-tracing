#!/usr/bin/env bash
# Build the python-build-standalone (pbs) candidate: a copied pbs interpreter with
# arize-harness installed into it and a relocatable launcher.
#   build-pbs.sh            -> builds/pbs            (stdlib + package precompiled)
#   COMPILE=0 build-pbs.sh  -> builds/pbs-nocompile  (no .pyc at all, for comparison)
#
# Precompiling matters for MDM: a .pkg installs root-owned files, so at runtime users
# can't write __pycache__ and an uncompiled build recompiles every module on every hook.
# unchecked-hash .pyc also skip the per-import source mtime check.
#
# The interpreter defaults to uv's managed CPython, which is itself a pbs build, so
# nothing is downloaded. Pass PBS_SRC=<unpacked pbs install_only dir> to use another one.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd); REPO=$(cd "$HERE/.." && pwd)
COMPILE=${COMPILE:-1}
LABEL=pbs; [ "$COMPILE" = 1 ] || LABEL=pbs-nocompile
DEST=$HERE/builds/$LABEL
PBS_SRC=${PBS_SRC:-$(cd "$(dirname "$(uv python find 3.12)")/.." && pwd -P)}

SECONDS=0
[ -d "$DEST" ] && chmod -R u+w "$DEST" && rm -rf "$DEST"
mkdir -p "$(dirname "$DEST")"
cp -R "$PBS_SRC" "$DEST"
rm -f "$DEST"/lib/python3.*/EXTERNALLY-MANAGED          # our bundle, not a shared system Python
find "$DEST" -name __pycache__ -type d -prune -exec rm -rf {} +  # start from source-only, like a fresh pbs

uv pip install -q --python "$DEST/bin/python3" "$REPO"

# pip's console script hardcodes this build path in its shebang, which breaks once an
# installer puts the bundle elsewhere. exec replaces the shell, so no extra process sits
# between the harness and Python (Claude's session key uses the grandparent PID).
# -I: don't put the hook's cwd (the user's project) on sys.path, ignore PYTHON* env vars.
# ${0%/*}, not $(dirname "$0"): dirname is a separate process and cost ~30 ms per hook.
# (Like dirname, this assumes the launcher is run by its real path, not via a symlink.)
cat > "$DEST/bin/arize-harness" <<'SH'
#!/bin/sh
exec "${0%/*}/python3" -I -m core.cli "$@"
SH
chmod +x "$DEST/bin/arize-harness"

if [ "$COMPILE" = 1 ]; then
  # -q: only report errors. A few stdlib files are not valid Python on purpose (e.g. lib2to3
  # test data); compileall returns 1 for those, so check the count of compiled core modules instead.
  "$DEST/bin/python3" -I -m compileall -q -j0 --invalidation-mode unchecked-hash "$DEST/lib" >/dev/null || true
  CORE_PYC=$(find "$DEST"/lib/python3.*/site-packages/core -name '*.pyc' | wc -l | tr -d ' ')
  [ "$CORE_PYC" -gt 0 ] || { echo "compileall produced no core .pyc" >&2; exit 1; }
fi

"$DEST/bin/arize-harness" hook --list >/dev/null  # smoke test
[ "$COMPILE" = 1 ] || find "$DEST" -name __pycache__ -type d -prune -exec rm -rf {} +  # the smoke test wrote some

# Leave the build read-only, like a root-owned MDM install. Otherwise the first warmup
# run writes __pycache__ and pbs-nocompile silently turns into pbs.
chmod -R a-w "$DEST"

mkdir -p "$HERE/results/$LABEL"
echo "$SECONDS" > "$HERE/results/$LABEL/build_seconds"
echo "built $DEST in ${SECONDS}s, read-only ($(find "$DEST" -name '*.pyc' | wc -l | tr -d ' ') .pyc files)"
