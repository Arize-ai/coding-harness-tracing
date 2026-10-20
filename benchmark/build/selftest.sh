#!/usr/bin/env bash
# Test the build gates themselves: break each one on purpose in a throwaway copy of the repo and
# check that the build stops with the message it should give.
#   build/selftest.sh   after ARCH=arm64 and ARCH=x86_64 build/nuitka.sh; exit 1 if any case fails
# Cases: (a) inputs.py modules fails, and the previous bundle is kept; (b) a DATA path that doesn't
# exist; (c) a data file removed from the bundle before finish(); (d) a data file changed in the
# bundle; (e) EXPECT_MINOS_ARM64=99.0 build/universal.sh; (f) tracing/devin/skills alone passes;
# (g) one hook module left out of inputs.py modules: the smoke test fails on that hook alone.
# Each case gets its own copy (no .git, .workbench, builds or results); this checkout is never
# changed. The arm64 cases reuse .workbench/bt-arm64 (or BUILDTOOLS=<venv>) as is; (e) runs
# universal.sh, which sets up both build-tools venvs inside its copy.
set -euo pipefail
exec </dev/null
HERE=$(cd "$(dirname "$0")" && pwd); BENCH=$(cd "$HERE/.." && pwd); REPO=$(cd "$BENCH/.." && pwd)
ARM=$BENCH/builds/nuitka-arm64
X86=$BENCH/builds/nuitka-x86_64
BT=${BUILDTOOLS:-$REPO/.workbench/bt-arm64}
for need in "$ARM/arize-harness.bin" "$X86/arize-harness.bin" "$BT/bin/python" \
  "$BENCH/results/nuitka-arm64/build_seconds" "$BENCH/results/nuitka-x86_64/build_seconds"; do
  [ -e "$need" ] || { echo "SELFTEST STOPPED: missing $need (run ARCH=arm64 / ARCH=x86_64 build/nuitka.sh first)" >&2; exit 1; }
done

TMP=$(mktemp -d)
trap 'chmod -R u+w "$TMP" 2>/dev/null; rm -rf "$TMP"' EXIT  # builds are read-only
cases=0 failed=0

copy() {  # case -> prints the path of a fresh copy of the repo
  rsync -a --exclude .git --exclude .venv --exclude .workbench \
    --exclude benchmark/builds --exclude benchmark/results "$REPO/" "$TMP/$1/"
  echo "$TMP/$1"
}
inject() {  # file, perl substitution: the case is meaningless if it doesn't change the file
  cp "$1" "$1.orig"
  perl -i -pe "$2" "$1"
  if cmp -s "$1" "$1.orig"; then echo "SELFTEST STOPPED: injection didn't change $1" >&2; exit 1; fi
  rm "$1.orig"
}
result() {  # case, ok (0/1), what
  cases=$((cases + 1))
  if [ "$2" = 0 ]; then echo "ok   $1: $3"; else echo "FAIL $1: $3"; failed=$((failed + 1)); fi
}
expect_fail() {  # case, expected text, command...: exit 1 and the text in its output
  local name=$1 want=$2 log=$TMP/$1.log rc=0
  shift 2
  "$@" >"$log" 2>&1 || rc=$?
  if [ "$rc" = 1 ] && grep -qF -- "$want" "$log"; then
    result "$name" 0 "exit 1, \"$want\""
  else
    result "$name" 1 "exit $rc, want exit 1 and \"$want\"; last lines of $name.log:"
    tail -5 "$log" | sed 's/^/       /'
  fi
}
build_arm64() { env BUILDTOOLS="$BT" ARCH=arm64 "$1/benchmark/build/nuitka.sh"; }  # copy

echo "-- (a) inputs.py modules fails"
d=$(copy a)
mkdir -p "$d/benchmark/builds" && cp -R "$ARM" "$d/benchmark/builds/"
inject "$d/benchmark/build/inputs.py" 's/^(\s+)print\("\\n"\.join\(modules\(\)\)\)/$1sys.exit("selftest: modules fails")/'
expect_fail a "BUILD STOPPED: inputs.py modules failed" build_arm64 "$d"
[ -x "$d/benchmark/builds/nuitka-arm64/arize-harness.bin" ] && kept=0 || kept=1
result a "$kept" "previous bundle kept"

echo "-- (b) a DATA path that doesn't exist"
d=$(copy b)
inject "$d/benchmark/build/inputs.py" 's|"tracing/devin/skills"\]|"tracing/devin/skills", "tracing/selftest-missing.ts"]|'
expect_fail b "BUILD STOPPED: inputs.py data failed" build_arm64 "$d"

echo "-- (c) a data file removed from the bundle before finish()"
d=$(copy c)
inject "$d/benchmark/build/nuitka.sh" 's|^finish "\$EXE"|rm "\$DEST/tracing/omp/plugin/arize-tracing.ts"\nfinish "\$EXE"|'
expect_fail c "BUILD FAILED: data file missing or different in bundle: tracing/omp/plugin/arize-tracing.ts" build_arm64 "$d"

echo "-- (d) a data file changed in the bundle before finish()"
d=$(copy d)
inject "$d/benchmark/build/nuitka.sh" 's|^finish "\$EXE"|echo "// selftest" >>"\$DEST/tracing/omp/plugin/arize-tracing.ts"\nfinish "\$EXE"|'
expect_fail d "BUILD FAILED: data file missing or different in bundle: tracing/omp/plugin/arize-tracing.ts" build_arm64 "$d"

echo "-- (e) EXPECT_MINOS_ARM64=99.0 build/universal.sh"
d=$(copy e)
mkdir -p "$d/benchmark/builds" "$d/benchmark/results"
cp -R "$ARM" "$X86" "$d/benchmark/builds/"
cp -R "$BENCH/results/nuitka-arm64" "$BENCH/results/nuitka-x86_64" "$d/benchmark/results/"
# Without BUILDTOOLS: one venv can't serve both arches, so universal.sh sets up its own.
expect_fail e "(EXPECT_MINOS_ARM64 / EXPECT_MINOS_X86_64)" env -u BUILDTOOLS EXPECT_MINOS_ARM64=99.0 "$d/benchmark/build/universal.sh"
grep -qF "(want 99.0 / 10.15) (override)" "$TMP/e.log" && seen=0 || seen=1
result e "$seen" "verify line shows \"(want 99.0 / 10.15) (override)\""

echo "-- (f) tracing/devin/skills alone"
d=$(copy f)
rc=0
"$BT/bin/python" -I "$d/benchmark/build/inputs.py" data >"$TMP/f.log" 2>&1 || rc=$?
[ "$rc" = 0 ] && grep -qF "data path missing, not included: tracing/devin/skills" "$TMP/f.log" && ok=0 || ok=1
result f "$ok" "inputs.py data exit $rc (want 0), devin path reported"

echo "-- (g) one hook module left out of inputs.py modules"
# Kiro has one hook, and only --include-module brings its handlers in (nothing imports them), so
# the binary lacks exactly that hook: the smoke test has to find one bad hook among all of them.
d=$(copy g)
drop=tracing.kiro.hooks.handlers
inject "$d/benchmark/build/inputs.py" "s/^(\\s+)return sorted\\(names\\)/\$1return sorted(names - {\"$drop\"})/"
expect_fail g "BUILD FAILED SMOKE TEST: nuitka-arm64" build_arm64 "$d"
n=$("$BT/bin/python" -I "$d/benchmark/build/inputs.py" hooks | wc -l)
n=$((n))  # wc pads with spaces
grep -qF "check: kiro: " "$TMP/g.log" && grep -qF "$((n - 1))/$n loaded" "$TMP/g.log" && ok=0 || ok=1
result g "$ok" "only the kiro hook failed ($((n - 1))/$n loaded)"
# Otherwise a module Nuitka bundled anyway would look like a gate that missed it.
report=$d/.workbench/nuitka-arm64/compilation-report.xml
[ -f "$report" ] && ! grep -qF "module name=\"$drop\"" "$report" && ok=0 || ok=1
result g "$ok" "$drop not in the compilation report"

echo "selftest: $((cases - failed))/$cases passed"
[ "$failed" = 0 ]
