#!/usr/bin/env bash
# Merge builds/nuitka-arm64 and builds/nuitka-x86_64 into builds/nuitka-universal and verify it.
#   ARCH=arm64 build/nuitka.sh; ARCH=x86_64 build/nuitka.sh; build/universal.sh
# Nuitka can't build universal itself (--macos-target-arch takes arm64 or x86_64), and there is no
# universal python-build-standalone, so: two single-arch builds, lipo per Mach-O file, re-sign ad hoc.
# Verification: both halves were built from this checkout's sources (build_info.json "source");
# every Mach-O file is "x86_64 arm64"; inputs.py check and a dry-run span pass
# natively and under arch -x86_64; minos is EXPECT_MINOS_ARM64 (default 11.0) and
# EXPECT_MINOS_X86_64 (default 10.15).
set -euo pipefail
exec </dev/null
HERE=$(cd "$(dirname "$0")" && pwd); BENCH=$(cd "$HERE/.." && pwd); REPO=$(cd "$BENCH/.." && pwd)
# BUILDTOOLS names one venv, and one venv can't be both arm64 and x86_64: without this the
# x86_64 interpreter check fails and blames the interpreter.
if [ -n "${BUILDTOOLS:-}" ]; then
  echo "UNIVERSAL STOPPED: universal.sh needs one build-tools venv per architecture; unset BUILDTOOLS" >&2
  exit 1
fi
# The pinned interpreter's minos. A deliberate uv or Python bump can change it: see README.md.
DEFAULT_MINOS_ARM64=11.0 DEFAULT_MINOS_X86_64=10.15
EXPECT_MINOS_ARM64=${EXPECT_MINOS_ARM64:-$DEFAULT_MINOS_ARM64}
EXPECT_MINOS_X86_64=${EXPECT_MINOS_X86_64:-$DEFAULT_MINOS_X86_64}
# Say so in the log when the environment changes what the minos check accepts.
minos_override=""
if [ "$EXPECT_MINOS_ARM64" != "$DEFAULT_MINOS_ARM64" ] || [ "$EXPECT_MINOS_X86_64" != "$DEFAULT_MINOS_X86_64" ]; then
  minos_override=" (override)"
fi
LABEL=nuitka-universal
ARM=$BENCH/builds/nuitka-arm64
X86=$BENCH/builds/nuitka-x86_64
DEST=$BENCH/builds/$LABEL
EXE=$DEST/arize-harness.bin
# shellcheck source=common.sh
. "$HERE/common.sh"

fail() { echo "UNIVERSAL FAILED: $*" >&2; exit 1; }
is_macho() { file -b "$1" | grep -q '^Mach-O'; }
minos() {  # arch, file: LC_BUILD_VERSION minos, or LC_VERSION_MIN_MACOSX version
  otool -arch "$1" -l "$2" | awk '/cmd LC_BUILD_VERSION/ {b = 1} b && $1 == "minos" {print $2; exit}
                                  /cmd LC_VERSION_MIN_MACOSX/ {v = 1} v && $1 == "version" {print $2; exit}'
}

for d in "$ARM" "$X86"; do
  [ -x "$d/arize-harness.bin" ] || fail "no build in $d (run ARCH=arm64 / ARCH=x86_64 build/nuitka.sh first)"
done
buildtools x86_64; PY_X86=$PY
buildtools arm64   # PY is the native driver from here on

# lipo merges any two builds that pass the checks below, even from different sources, and finish()
# and inputs.py check compare against this checkout: so both halves must come from it.
read_build_info
half() {  # arch -> "<source> <commit>" from its build_info.json
  local f=$BENCH/results/nuitka-$1/build_info.json
  [ -f "$f" ] || fail "no $f: the $1 half has no record of its sources (rebuild it: ARCH=$1 build/nuitka.sh)"
  "$PY" -I -c 'import json, sys; i = json.load(open(sys.argv[1])); print(i["source"], i["commit"])' "$f"
}
# One assignment each: in `a=$(..) b=$(..)` only the last substitution's status counts under set -e.
src_arm=$(half arm64)
src_x86=$(half x86_64)
want=$("$PY" -I -c 'import json, sys; print(json.loads(sys.argv[1])["source"])' "$BUILD_INFO")
short() { echo "${1:0:12} (${1#* })"; }  # "<source> <commit>" -> "<source:12> (<commit>)"
[ "${src_arm%% *}" = "${src_x86%% *}" ] ||
  fail "halves built from different sources: arm64 $(short "$src_arm"), x86_64 $(short "$src_x86"); rebuild both"
[ "${src_arm%% *}" = "$want" ] ||
  fail "halves built from other sources than this checkout: $(short "$src_arm"), checkout ${want:0:12}; rebuild both"
echo "-- verify: both halves built from this checkout's sources (${want:0:12})"
# Build time: both halves plus the merge, not setting up the build tools.
SECONDS=$(($(cat "$BENCH/results/nuitka-arm64/build_seconds") + $(cat "$BENCH/results/nuitka-x86_64/build_seconds")))

# Same files on both sides, and nothing but regular files and directories.
listing() { (cd "$1" && find . ! -type d | sort); }
diff <(listing "$ARM") <(listing "$X86") || fail "the two builds have different files"
[ -z "$(find "$ARM" "$X86" ! -type d ! -type f)" ] || fail "symlinks or special files in a build"

clean_dest "$DEST"
macho=()
while IFS= read -r f; do
  f=${f#./}
  mkdir -p "$DEST/$(dirname "$f")"
  if is_macho "$ARM/$f"; then
    lipo -create "$ARM/$f" "$X86/$f" -output "$DEST/$f"
    chmod "$(stat -f %Lp "$ARM/$f")" "$DEST/$f"
    macho+=("$f")
  else
    cmp -s "$ARM/$f" "$X86/$f" || fail "not Mach-O and not identical in both builds: $f"
    cp -p "$ARM/$f" "$DEST/$f"
  fi
done < <(listing "$ARM")
chmod -R u+w "$DEST"

# lipo invalidates the signatures. Libraries first, then the executable that loads them.
for f in "${macho[@]}"; do
  [ "$f" = arize-harness.bin ] || codesign --force --sign - --timestamp=none "$DEST/$f"
done
codesign --force --sign - --timestamp=none "$EXE"
for f in "${macho[@]}"; do codesign --verify --strict "$DEST/$f" || fail "codesign --verify --strict: $f"; done
echo "merged ${#macho[@]} Mach-O file(s) with lipo, copied $(($(listing "$ARM" | wc -l) - ${#macho[@]})) identical file(s), re-signed ad hoc"

echo "-- verify: architectures and minos (want $EXPECT_MINOS_ARM64 / $EXPECT_MINOS_X86_64)$minos_override"
for f in "${macho[@]}"; do
  archs=$(lipo -archs "$DEST/$f") arm=$(minos arm64 "$DEST/$f") x86=$(minos x86_64 "$DEST/$f")
  echo "  $f: $archs, minos arm64 $arm, x86_64 $x86"
  [ "$archs" = "x86_64 arm64" ] || fail "$f is '$archs', want 'x86_64 arm64'"
  [ "$arm" = "$EXPECT_MINOS_ARM64" ] && [ "$x86" = "$EXPECT_MINOS_X86_64" ] ||
    fail "$f minos arm64 $arm / x86_64 $x86, want $EXPECT_MINOS_ARM64 / $EXPECT_MINOS_X86_64 (EXPECT_MINOS_ARM64 / EXPECT_MINOS_X86_64)"
done

for mode in native x86_64; do
  if [ "$mode" = native ]; then
    run=(env) driver=$PY want=arm64
  else
    run=(arch -x86_64) driver=$PY_X86 want=i386
  fi
  # A process running under Rosetta starts the x86_64 slice of a universal child.
  child=$("${run[@]}" "$driver" -I -c 'import subprocess; print(subprocess.run(["/usr/bin/arch"], capture_output=True, text=True).stdout.strip())')
  echo "-- verify: $mode (children run as $child)"
  [ "$child" = "$want" ] || fail "$mode driver starts children as $child, want $want"
  "${run[@]}" "$driver" -I "$INPUTS" check "$EXE" || fail "inputs.py check ($mode)"
  "${run[@]}" "$driver" -I "$BENCH/checks/equiv.py" dryrun "$EXE" || fail "dry-run span ($mode)"
done

finish "$EXE"
