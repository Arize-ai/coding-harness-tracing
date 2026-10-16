# Shared by the build scripts in build/ (sourced, not run).
# Expects HERE (build/), BENCH (benchmark/), REPO, LABEL, DEST (the directory or file the build produces).
BT=${BUILDTOOLS:-$REPO/.workbench/buildtools}
PY=$BT/bin/python
WORKBENCH=$REPO/.workbench/$LABEL   # build/, spec files, reports: never the repo root
INPUTS=$HERE/inputs.py

# Build-time lists, generated from the code (see inputs.py). -I: never pick up a
# stray core/ from the cwd. The data listing reports missing paths on stderr itself.
inputs() { "$PY" -I "$INPUTS" "$@"; }

clean_dest() {  # earlier builds are read-only
  [ -e "$1" ] && chmod -R u+w "$1" && rm -rf "$1"
  mkdir -p "$(dirname "$1")" "$WORKBENCH"
}

finish() {  # executable
  local exe=$1
  echo "-- smoke test: $exe"
  if ! "$PY" -I "$INPUTS" check "$exe"; then
    echo "BUILD FAILED SMOKE TEST: $LABEL (output left writable in $DEST)" >&2
    exit 1
  fi
  chmod -R a-w "$DEST"  # like a root-owned MDM install
  mkdir -p "$BENCH/results/$LABEL"
  echo "$SECONDS" > "$BENCH/results/$LABEL/build_seconds"
  echo "built $LABEL in ${SECONDS}s, read-only: $exe"
}
