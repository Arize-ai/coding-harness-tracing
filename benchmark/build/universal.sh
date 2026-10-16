#!/usr/bin/env bash
# Merge builds/nuitka-arm64 and builds/nuitka-x86_64 into builds/nuitka-universal and verify it.
#   ARCH=arm64 build/nuitka.sh; ARCH=x86_64 build/nuitka.sh; build/universal.sh
# Nuitka can't build universal itself (--macos-target-arch takes arm64 or x86_64), and there is no
# universal python-build-standalone, so: two single-arch builds, lipo per Mach-O file, re-sign ad hoc.
# Verification: every Mach-O file is "x86_64 arm64"; inputs.py check and a dry-run span pass
# natively and under arch -x86_64; minos is 11.0 (arm64) and 10.15 (x86_64).
set -euo pipefail
exec </dev/null
HERE=$(cd "$(dirname "$0")" && pwd); BENCH=$(cd "$HERE/.." && pwd); REPO=$(cd "$BENCH/.." && pwd)
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
# Build time: both halves plus the merge.
SECONDS=$(($(cat "$BENCH/results/nuitka-arm64/build_seconds") + $(cat "$BENCH/results/nuitka-x86_64/build_seconds")))
buildtools x86_64; PY_X86=$PY
buildtools arm64   # PY is the native driver from here on

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

echo "-- verify: architectures and minos"
for f in "${macho[@]}"; do
  archs=$(lipo -archs "$DEST/$f") arm=$(minos arm64 "$DEST/$f") x86=$(minos x86_64 "$DEST/$f")
  echo "  $f: $archs, minos arm64 $arm, x86_64 $x86"
  [ "$archs" = "x86_64 arm64" ] || fail "$f is '$archs', want 'x86_64 arm64'"
  [ "$arm" = 11.0 ] && [ "$x86" = 10.15 ] || fail "$f minos arm64 $arm / x86_64 $x86, want 11.0 / 10.15"
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
