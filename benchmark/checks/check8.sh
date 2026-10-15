#!/usr/bin/env bash
# Check 8 on a universal build, natively and under Rosetta.
#   DRIVER=<universal2 python with python-dotenv> REPO=<checkout> check8.sh <dist dir> <exe>
# DRIVER runs build_inputs.py/equiv.py; under `arch -x86_64` it runs as x86_64, so the build it
# spawns does too (children of a translated process prefer x86_64 slices).
set -uo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
DIST=$1 EXE=$2
DRIVER=${DRIVER:?set DRIVER} REPO=${REPO:?set REPO}

echo "## lipo -archs, every Mach-O file under $DIST"
n=0 bad=0
while IFS= read -r -d '' f; do
  file -b "$f" | grep -q Mach-O || continue
  n=$((n + 1)); a=$(lipo -archs "$f")
  [ "$a" = "x86_64 arm64" ] || { bad=$((bad + 1)); echo "  NOT UNIVERSAL: $a  ${f#"$DIST"/}"; }
done < <(find "$DIST" -type f -print0)
echo "  $n Mach-O files, $bad not 'x86_64 arm64'"

for mode in native x86_64; do
  if [ "$mode" = native ]; then run=(env); else run=(arch -x86_64); fi
  echo "## $mode (driver runs as: $("${run[@]}" "$DRIVER" -c 'import platform; print(platform.machine())'), sh child: $("${run[@]}" sh -c arch))"
  "${run[@]}" "$EXE" hook --list > /dev/null 2> /tmp/check8-err.txt
  echo "  hook --list: exit $? $(head -c 300 /tmp/check8-err.txt)"
  out=$("${run[@]}" "$DRIVER" -I "$REPO/benchmark/build_inputs.py" check "$EXE" 2>&1); rc=$?
  echo "  build_inputs check: exit $rc, $(tail -1 <<< "$out")"
  out=$("${run[@]}" "$DRIVER" -I "$HERE/equiv.py" dryrun "$EXE" 2>&1); rc=$?
  echo "  dry-run span: exit $rc, $(tail -1 <<< "$out")"
  echo "  https: $("${run[@]}" "$DRIVER" -I "$HERE/equiv.py" https "$EXE" 2>&1 | tail -1)"
done
