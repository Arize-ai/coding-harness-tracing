#!/usr/bin/env bash
# MDM smoke test: runs scripts/mdm/macos/install.sh as real root on a fresh Mac,
# checks the Iru email for the Mac's only account, then adds two test users and
# checks the system install and each user's credentials and harness setup. Last,
# runs uninstall.sh and checks nothing is left. CI's
# mdm-macos job runs this script.
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
UNINSTALL="$ROOT/scripts/mdm/macos/uninstall.sh"
BIN=/usr/local/bin/arize-harness
KEY="smoke-mdm-key-never-printed"
NEW_KEY="smoke-mdm-rotated-key-never-printed"
VERSION="$(basename "$WHEEL" | cut -d- -f2)"
CREDS="/Library/Application Support/Arize/harness"
HARNESSES="claude-code cursor codex"
USERS="smoke-alice smoke-bob"
ME="$(id -un)"
IRU_EMAIL="assigned@example.com"
PY=/opt/arize/tools/coding-harness-tracing/bin/python
work="$(mktemp -d)"
# The checks run as each test user, so they need a copy of the checkout every user can read.
checks="$(mktemp -d /tmp/smoke-mdm-checks.XXXXXX)"
LOG="$work/mdm.log"
IRU_PLIST="$work/iru.plist"  # created by the Iru step; until then there is no plist
: >"$work/emails.txt"
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
        ARIZE_MDM_HARNESSES="$HARNESSES" ARIZE_MDM_EMAIL_MAP_FILE="$work/emails.txt" ARIZE_MDM_IRU_PLIST="$IRU_PLIST" \
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

# expect_uninstall CODE: runs uninstall.sh as root, appending its output to $LOG.
expect_uninstall() {
    local rc=0
    echo "--- uninstall.sh" >>"$LOG"
    sudo /bin/bash "$UNINSTALL" >>"$LOG" 2>&1 || rc=$?
    [[ "$rc" == "$1" ]] || { tail -n 30 "$LOG" >&2; die "uninstall.sh exited $rc, expected $1"; }
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

# creds_email USER: the ARIZE_USER_ID in USER's credentials file.
creds_email() { sudo sed -n "s/^ARIZE_USER_ID='\(.*\)'$/\1/p" "$CREDS/$1.env"; }

cp -R "$ROOT/core" "$ROOT/tracing" "$ROOT/scripts" "$checks/"
chmod -R a+rX "$checks"

step "uninstall.sh uses install.sh's paths"
paths() { sed -n -E 's/ *#.*$//; /^(ARIZE_DIR|TOOL_DIR|PYTHON_DIR|BIN_DIR|HARNESS_BIN|WHEEL_DIR|WHEEL_MARK|CREDENTIALS_DIR)=/p' "$1" | sort; }
[[ "$(paths "$SCRIPT")" == "$(paths "$UNINSTALL")" ]] || die "uninstall.sh's paths differ from install.sh's"
[[ "$(paths "$UNINSTALL" | wc -l | tr -d ' ')" == 8 ]] || die "uninstall.sh is missing a path"

step "a missing input exits 2 and installs nothing"
expect_exit 2 "$WHEEL" ARIZE_API_KEY=
installed && die "a missing input left files behind"
expect_exit 2 "$WHEEL" ARIZE_MDM_EMAIL_MAP_FILE=
grep -q "no email source" "$LOG" || die "no email source wasn't reported"
installed && die "a missing email source left files behind"

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

step "$ME, the only account, gets the Iru email; a map entry wins over it"
plutil -create xml1 "$IRU_PLIST"
plutil -insert EMAIL -string "$IRU_EMAIL" "$IRU_PLIST"
echo "$ME $(email "$ME")" >"$work/lone-map.txt"
expect_exit 0 "$WHEEL" ARIZE_MDM_HARNESSES=claude-code ARIZE_MDM_EMAIL_MAP_FILE="$work/lone-map.txt"
grep -q "not using the Iru email" "$LOG" && die "$ME isn't the runner's only account"
[[ "$(creds_email "$ME")" == "$(email "$ME")" ]] || die "the map entry didn't win over the Iru email"
expect_exit 0 "$WHEEL" ARIZE_MDM_HARNESSES=claude-code
[[ "$(creds_email "$ME")" == "$IRU_EMAIL" ]] || die "$ME didn't get the Iru email"

step "create test users"
uid=5001
for user in $USERS; do
    add_user "$user" "$uid"
    uid=$((uid + 1))
    echo "$user $(email "$user")" >>"$work/emails.txt"
done

step "install $VERSION for the test users"
before_log="$(wc -l <"$LOG")"
expect_exit 0 "$WHEEL"
tail -n +"$((before_log + 1))" "$LOG" | grep -q "not using the Iru email" ||
    die "the Iru email was used with several accounts"
[[ "$(stat -f %Su /opt/arize/tools/coding-harness-tracing)" == root ]] || die "the install isn't owned by root"
"$BIN" --version | grep -qF "$VERSION" || die "$BIN --version, run as $(id -un), doesn't report $VERSION"
grep -q "skipping $ME: no email" "$LOG" || die "$ME has no email but wasn't skipped"

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

step "uninstall removes every user's setup, the credentials and the system install"
expect_uninstall 0
for user in $USERS; do
    sudo test -e "/Users/$user/.arize/harness" && die "$user's ~/.arize/harness is left"
    sudo grep -rlF "$BIN" "/Users/$user" --exclude-dir=Library >/dev/null 2>&1 && die "$user has a hook left that runs $BIN"
done
sudo test -e "$CREDS" && die "$CREDS is left"
installed && die "the system install is left"

step "a second uninstall exits 0"
expect_uninstall 0
grep -q "skipping per-user uninstall" "$LOG" || die "the second uninstall didn't skip the users"

step "the API key is never printed"
grep -qF -e "$KEY" -e "$NEW_KEY" "$LOG" && die "install.sh printed the API key"

step "all checks passed"
