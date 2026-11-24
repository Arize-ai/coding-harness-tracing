#!/usr/bin/env bash
# MDM install smoke test: runs scripts/mdm/macos/install.sh as real root on a
# fresh Mac, then checks the system install from a normal user. CI's mdm-macos
# job runs this script.
#
#   scripts/smoke-mdm-macos.sh <wheel>
#
#   SMOKE_MDM_FORCE=1   run outside CI
#
# Writes to /opt/arize and /usr/local/bin and leaves the install in place, so it
# only runs in CI or with SMOKE_MDM_FORCE=1, on a throwaway machine.
set -euo pipefail

die() { echo "smoke-mdm: FAIL: $*" >&2; exit 1; }
step() { echo; echo "smoke-mdm: $*"; }

[[ $# == 1 ]] || { echo "usage: scripts/smoke-mdm-macos.sh <wheel>" >&2; exit 2; }
WHEEL="$(cd "$(dirname "$1")" && pwd)/$(basename "$1")"
[[ -f "$WHEEL" && "$WHEEL" == *.whl ]] || { echo "smoke-mdm: not a wheel: $1" >&2; exit 2; }
[[ "$(uname -s)" == Darwin ]] || { echo "smoke-mdm: macOS only" >&2; exit 2; }
[[ -n "${CI:-}${SMOKE_MDM_FORCE:-}" ]] ||
    { echo "smoke-mdm: writes to /opt and /usr/local/bin; run in CI or set SMOKE_MDM_FORCE=1" >&2; exit 2; }
[[ "$(id -u)" != 0 ]] || { echo "smoke-mdm: run as a normal user with sudo, not as root" >&2; exit 2; }
sudo -n true 2>/dev/null || { echo "smoke-mdm: needs passwordless sudo" >&2; exit 2; }

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="$ROOT/scripts/mdm/macos/install.sh"
# Every harness install.sh accepts, without the claude alias. copilot isn't one: its hooks are
# per repository, so a per-user MDM setup can't trace it.
HARNESSES="$(sed -n 's/^SUPPORTED_HARNESSES="\(.*\)"$/\1/p' "$SCRIPT" | tr ' ' '\n' | grep -vx claude | xargs)"
[[ -n "$HARNESSES" ]] || die "no SUPPORTED_HARNESSES in $SCRIPT"
BIN=/usr/local/bin/arize-harness
KEY="smoke-mdm-key-never-printed"
VERSION="$(basename "$WHEEL" | cut -d- -f2)"
work="$(mktemp -d)"
LOG="$work/mdm.log"
trap 'rm -rf "$work"' EXIT

installed() { [[ -e /opt/arize ]] || ls /usr/local/bin/arize-* >/dev/null 2>&1; }
installed && die "/opt/arize or /usr/local/bin/arize-* already exists; needs a fresh machine"

# mdm SOURCE [VAR=value ...]: runs install.sh as root, appending its output to $LOG.
mdm() {
    local source="$1" rc=0
    shift
    echo "--- install.sh PACKAGE_SOURCE=$source $*" >>"$LOG"
    sudo env ARIZE_API_KEY="$KEY" ARIZE_SPACE_ID=smoke-space \
        ARIZE_MDM_HARNESSES="$HARNESSES" \
        ARIZE_MDM_PACKAGE_SOURCE="$source" "$@" /bin/bash "$SCRIPT" >>"$LOG" 2>&1 || rc=$?
    return "$rc"
}

# expect_exit CODE SOURCE [VAR=value ...]
expect_exit() {
    local want="$1" rc=0
    shift
    mdm "$@" || rc=$?
    [[ "$rc" == "$want" ]] || { tail -n 30 "$LOG" >&2; die "install.sh exited $rc, expected $want"; }
}

# rebuild_wheel KIND: a copy of $WHEEL that fails to resolve (unresolvable) or to run (crashes).
rebuild_wheel() {
    local kind="$1" tmp="$work/rebuild-$1" out="$work/$1"
    mkdir -p "$tmp" "$out"
    (cd "$tmp" && unzip -q "$WHEEL")
    case "$kind" in
        unresolvable)
            local meta
            meta="$(ls "$tmp"/*.dist-info/METADATA)"
            awk '{ print } /^Version: / { print "Requires-Dist: arize-mdm-no-such-package" }' "$meta" >"$meta.new"
            mv "$meta.new" "$meta"
            ;;
        crashes) echo 'raise SystemExit(3)' >"$tmp/core/cli.py" ;;
    esac
    (cd "$tmp" && zip -qrX "$out/$(basename "$WHEEL")" .)
    echo "$out/$(basename "$WHEEL")"
}

snapshot() {
    {
        find /opt/arize -print0 | xargs -0 stat -f '%N %Su %p %z %m'
        stat -f '%N %Su %p %z %m' /usr/local/bin/arize-*
    } | sort
}

step "a missing input exits 2 and installs nothing"
expect_exit 2 "$WHEEL" ARIZE_API_KEY=
installed && die "a missing input left files behind"

step "a uv that doesn't match the pinned checksum exits 1 and installs nothing"
case "$(uname -m)" in arm64) target=aarch64-apple-darwin ;; *) target=x86_64-apple-darwin ;; esac
curl -fsSL --retry 3 -o "$work/uv.tar.gz" "https://github.com/astral-sh/uv/releases/download/0.13.5/uv-$target.tar.gz"
printf x >>"$work/uv.tar.gz"
expect_exit 1 "$WHEEL" ARIZE_MDM_UV_SOURCE="$work/uv.tar.gz"
grep -q "uv checksum mismatch" "$LOG" || die "no checksum mismatch reported"
installed && die "a bad uv left files behind"

step "a failed first install exits 1 and leaves nothing"
expect_exit 1 "$(rebuild_wheel unresolvable)"
installed && die "a failed first install left files behind"

step "install $VERSION"
expect_exit 0 "$WHEEL"
[[ "$(stat -f %Su /opt/arize/tools/coding-harness-tracing)" == root ]] || die "the install isn't owned by root"
"$BIN" --version | grep -qF "$VERSION" || die "$BIN --version, run as $(id -un), doesn't report $VERSION"

step "a re-run changes nothing"
before="$(snapshot)"
expect_exit 0 "$WHEEL"
grep -q "system install is current" "$LOG" || die "the re-run reinstalled"
[[ "$(snapshot)" == "$before" ]] || die "the re-run changed files"

step "a failed upgrade exits 1 and keeps the working install"
expect_exit 1 "$(rebuild_wheel crashes)"
"$BIN" --version | grep -qF "$VERSION" || die "the failed upgrade broke $BIN"
[[ "$(ls /opt/arize/wheel)" == "$(basename "$WHEEL")" ]] || die "the failed upgrade left the wrong wheel in /opt/arize/wheel"

step "the API key is never printed"
grep -qF "$KEY" "$LOG" && die "install.sh printed the API key"

step "all checks passed"
