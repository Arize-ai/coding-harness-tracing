#!/bin/bash
# Arize coding harness tracing: MDM uninstall for macOS (Iru Custom Script, runs as root).
# Runs `arize-harness uninstall --all` as each user, then removes the credentials files and the system install.
# Exit codes: 0 done, 1 a user's uninstall or a removal failed, 2 bad environment.

# ---------------------------------------------------------------------------
# Settings. IT sets these in the library item; an empty value falls back to
# the environment variable named on the right.
# ---------------------------------------------------------------------------
FORCE=""  # ARIZE_MDM_FORCE: 1 removes the system install even if a user's uninstall failed

: "${FORCE:=${ARIZE_MDM_FORCE:-}}"

set -u

# Set only by tests.
SYSTEM_ROOT="${ARIZE_MDM_SYSTEM_ROOT:-}"
CREDENTIALS_DIR="${ARIZE_MDM_CREDENTIALS_DIR:-$SYSTEM_ROOT/Library/Application Support/Arize/harness}"

# Same paths as install.sh (tests/mdm_macos checks they match).
# shellcheck disable=SC2034
{
ARIZE_DIR="$SYSTEM_ROOT/opt/arize"
TOOL_DIR="$ARIZE_DIR/tools"
PYTHON_DIR="$ARIZE_DIR/python"
BIN_DIR="$SYSTEM_ROOT/usr/local/bin"
WHEEL_MARK="$ARIZE_DIR/mdm-wheel.sha256"
WHEEL_DIR="$ARIZE_DIR/wheel"
HARNESS_BIN="$BIN_DIR/arize-harness"
}

log() { echo "arize-mdm: $*"; }
fail() { log "error: $2" >&2; exit "$1"; }

preflight() {
    [ "$(id -u)" = 0 ] || fail 2 "must run as root"
    [ "$(uname -s)" = Darwin ] || fail 2 "macOS only"
}

# Value of key $2 in account $1's record; dscl puts values with spaces on the next line.
account_value() {
    dscl . -read "/Users/$1" "$2" 2>/dev/null | sed -e "1s/^$2://" -e 's/^ //' | tr -d '\n'
}

# Prints "name<TAB>home" for each real account: UID >= 501, not _-prefixed, a login shell, an existing home.
list_users() {
    local name uid home shell
    dscl . -list /Users UniqueID | while read -r name uid; do
        case "$name" in _* | "") continue ;; esac
        case "$uid" in *[!0-9]* | "") continue ;; esac
        [ "$uid" -ge 501 ] || continue
        shell="$(account_value "$name" UserShell)"
        case "$shell" in "" | */false | */nologin) continue ;; esac
        home="$(account_value "$name" NFSHomeDirectory)"
        [ -n "$home" ] && [ -d "$home" ] || continue
        printf '%s\t%s\n' "$name" "$home"
    done
}

system_harness_installed() {
    [ -L "$HARNESS_BIN" ] || return 1
    case "$(readlink "$HARNESS_BIN")" in "$TOOL_DIR"/*) ;; *) return 1 ;; esac
    [ -x "$HARNESS_BIN" ]
}

# Runs a full uninstall as each user; sets FAILED=1 if any fails.
uninstall_users() {
    if ! system_harness_installed; then
        log "no system arize-harness at $HARNESS_BIN; skipping per-user uninstall"
        return 0
    fi
    local name home users
    users="$(list_users)"
    while IFS="$(printf '\t')" read -r name home; do
        [ -n "$name" ] || continue
        if (cd "$home" && sudo -n -u "$name" -H "$HARNESS_BIN" uninstall --all) </dev/null; then
            log "uninstalled for $name"
        else
            log "error: uninstall failed for $name" >&2
            FAILED=1
            FAILED_USERS="${FAILED_USERS:+$FAILED_USERS }$name"
        fi
    done <<EOF_USERS
$users
EOF_USERS
}

remove_credentials() {
    local file
    # .<user>.env.new is install.sh's temp file, left if it was interrupted mid-write.
    for file in "$CREDENTIALS_DIR"/*.env "$CREDENTIALS_DIR"/.*.env.new; do
        [ -e "$file" ] || [ -L "$file" ] || continue
        if rm -f "$file"; then
            log "removed $file"
        else
            log "error: cannot remove $file" >&2
            FAILED=1
        fi
    done
    [ -d "$CREDENTIALS_DIR" ] && rmdir "$CREDENTIALS_DIR" 2>/dev/null && log "removed $CREDENTIALS_DIR"
    [ -d "$CREDENTIALS_DIR" ] && log "kept $CREDENTIALS_DIR: not empty"
    return 0
}

remove_system_install() {
    local link
    for link in "$BIN_DIR"/*; do
        [ -L "$link" ] || continue
        case "$(readlink "$link")" in "$TOOL_DIR"/*) rm -f "$link" && log "removed $link" ;; esac
    done
    if [ -e "$ARIZE_DIR" ]; then
        rm -rf "$ARIZE_DIR" || fail 1 "cannot remove $ARIZE_DIR"
        log "removed $ARIZE_DIR"
    fi
}

main() {
    preflight
    FAILED=0
    FAILED_USERS=""
    uninstall_users
    remove_credentials
    if [ -n "$FAILED_USERS" ] && [ "$FORCE" != 1 ]; then
        fail 1 "kept the system install so a re-run can retry $FAILED_USERS; set FORCE=1 (or ARIZE_MDM_FORCE=1) to remove it anyway"
    fi
    remove_system_install
    [ -z "$FAILED_USERS" ] || fail 1 "removed the system install; remove the hooks of $FAILED_USERS by hand"
    [ "$FAILED" = 0 ] || fail 1 "finished, but at least one step failed (see above)"
    log "done"
    exit 0
}

main "$@"
