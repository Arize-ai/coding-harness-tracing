#!/usr/bin/env bash
# MDM install smoke test: runs scripts/mdm/macos/install.sh as real root on a
# fresh Mac with two test users, then checks the system install and each user's
# credentials and harness setup. CI's mdm-macos job runs this script.
#
#   scripts/smoke-mdm-macos.sh <wheel>
#
#   SMOKE_MDM_FORCE=1   run outside CI
#
# Writes to /opt/arize, /usr/local/bin and /Library, creates user accounts and
# leaves them all in place, so it only runs in CI or with SMOKE_MDM_FORCE=1, on a
# throwaway machine.
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
NEW_KEY="smoke-mdm-rotated-key-never-printed"
VERSION="$(basename "$WHEEL" | cut -d- -f2)"
CREDS="/Library/Application Support/Arize/harness"
USERS="smoke-alice smoke-bob"
PY=/opt/arize/tools/coding-harness-tracing/bin/python
work="$(mktemp -d)"
# The checks run as each test user, so they need a copy of the checkout every user can read.
checks="$(mktemp -d /tmp/smoke-mdm-checks.XXXXXX)"
LOG="$work/mdm.log"
trap 'rm -rf "$work" "$checks"' EXIT

installed() { [[ -e /opt/arize ]] || ls /usr/local/bin/arize-* >/dev/null 2>&1; }
installed && die "/opt/arize or /usr/local/bin/arize-* already exists; needs a fresh machine"

email() { echo "$1@example.com"; }

# add_user NAME UID: a standard account with a home folder.
add_user() {
    dscl . -read "/Users/$1" >/dev/null 2>&1 && die "user $1 already exists"
    dscl . -list /Users UniqueID | awk -v uid="$2" '$2 == uid { found = 1 } END { exit !found }' &&
        die "UID $2 is taken"
    sudo dscl . -create "/Users/$1"
    sudo dscl . -create "/Users/$1" UserShell /bin/zsh
    sudo dscl . -create "/Users/$1" UniqueID "$2"
    sudo dscl . -create "/Users/$1" PrimaryGroupID 20
    sudo dscl . -create "/Users/$1" NFSHomeDirectory "/Users/$1"
    sudo createhomedir -c -u "$1" >/dev/null
}

# as_user USER CMD...: runs CMD as USER from their home folder, with their HOME.
as_user() {
    local user="$1"
    shift
    sudo -H -u "$user" /bin/sh -c 'cd "$HOME" && exec "$@"' sh "$@"
}
check_as() { local user="$1"; shift; as_user "$user" "$PY" -I -B "$checks/scripts/smoke_wheel_check.py" "$@"; }
config_value() { as_user "$1" plutil -extract "$2" raw -o - .arize/harness/config.json; }

# mdm SOURCE [VAR=value ...]: runs install.sh as root, appending its output to $LOG.
mdm() {
    local source="$1" rc=0
    shift
    echo "--- install.sh PACKAGE_SOURCE=$source $*" >>"$LOG"
    sudo env ARIZE_API_KEY="$KEY" ARIZE_SPACE_ID=smoke-space \
        ARIZE_MDM_HARNESSES="$HARNESSES" ARIZE_MDM_EMAIL_MAP_FILE="$work/emails.txt" \
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

# Every file and link of the install and the test users, with owner, mode, size and mtime; logs left out.
snapshot() {
    local homes=() user
    for user in $USERS; do homes+=("/Users/$user"); done
    sudo find /opt/arize /usr/local/bin/arize-* "$CREDS" "${homes[@]}" \
        -path '*/.arize/harness/logs' -prune -o \( -type f -o -type l \) -print0 |
        sudo xargs -0 stat -f '%N%t%Su %p %z %m' | sort
}

step "create test users"
uid=5001
for user in $USERS; do
    add_user "$user" "$uid"
    uid=$((uid + 1))
    echo "$user $(email "$user")" >>"$work/emails.txt"
done
cp -R "$ROOT/core" "$ROOT/tracing" "$ROOT/scripts" "$checks/"
chmod -R a+rX "$checks"

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
grep -q "skipping $(id -un): no email" "$LOG" || die "$(id -un) has no email but wasn't skipped"

for user in $USERS; do
    step "$user: credentials file"
    file="$CREDS/$user.env"
    [[ "$(sudo stat -f '%Su:%Sg %Lp' "$file")" == "root:wheel 400" ]] || die "$file isn't root:wheel 0400"
    ls -led "$file" | grep -q "user:$user allow read" || die "$file has no read ACL for $user"
    as_user "$user" grep -qF "ARIZE_USER_ID='$(email "$user")'" "$file" || die "$user can't read $file, or it has the wrong email"
    cat "$file" >/dev/null 2>&1 && die "$(id -un) can read $file"
    for other in $USERS; do
        [[ "$other" == "$user" ]] || ! as_user "$other" cat "$file" >/dev/null 2>&1 || die "$other can read $file"
    done

    step "$user: harness setup"
    check_as "$user" entries "$BIN" $HARNESSES || die "$user's hooks don't all run $BIN"
    [[ "$(config_value "$user" user_id)" == "$(email "$user")" ]] || die "$user's saved user ID isn't their email"
    for h in $HARNESSES; do
        [[ "$(config_value "$user" "harnesses.$h.project_name")" == "$h/$(email "$user")" ]] ||
            die "$user's $h project isn't $h/$(email "$user")"
    done
    check_as "$user" claude-turn "$BIN" || die "$user's Claude Code hooks logged no dry-run span"
done

step "a re-run changes nothing"
before="$(snapshot)"
expect_exit 0 "$WHEEL"
grep -q "system install is current" "$LOG" || die "the re-run reinstalled"
[[ "$(snapshot)" == "$before" ]] || die "the re-run changed files"

step "a new key changes only the credentials files and saved keys"
before="$(snapshot)"
expect_exit 0 "$WHEEL" ARIZE_API_KEY="$NEW_KEY"
after="$(snapshot)"
[[ "$(cut -f1 <<<"$before")" == "$(cut -f1 <<<"$after")" ]] || die "the new key added or removed files"
changed="$(diff <(echo "$before") <(echo "$after") | sed -n 's/^> //p' | cut -f1 | sort)"
expected="$(for user in $USERS; do echo "$CREDS/$user.env"; echo "/Users/$user/.arize/harness/config.json"; done | sort)"
[[ "$changed" == "$expected" ]] || die "the new key changed: $changed"
for user in $USERS; do
    for h in $HARNESSES; do
        [[ "$(config_value "$user" "harnesses.$h.api_key")" == "$NEW_KEY" ]] || die "$user's $h key wasn't replaced"
    done
done

step "a failed upgrade exits 1 and keeps the working install"
expect_exit 1 "$(rebuild_wheel crashes)"
"$BIN" --version | grep -qF "$VERSION" || die "the failed upgrade broke $BIN"
[[ "$(ls /opt/arize/wheel)" == "$(basename "$WHEEL")" ]] || die "the failed upgrade left the wrong wheel in /opt/arize/wheel"

step "the API key is never printed"
grep -qF -e "$KEY" -e "$NEW_KEY" "$LOG" && die "install.sh printed the API key"

step "all checks passed"
