#!/bin/bash
# Arize coding harness tracing: MDM install for macOS (Iru Custom Script, runs as root).
# Installs a checksum-verified uv and a system-wide arize-harness, then sets up
# the listed harnesses for every user account that has an email.
# Exit codes: 0 done, 1 install or a user's setup failed, 2 bad input or environment.
#
# Email map file (EMAIL_MAP_FILE): one "username email" pair per line, e.g.
#   alice alice@example.com
# Blank lines and lines starting with # are ignored; the first line for a user wins.

# ---------------------------------------------------------------------------
# Settings. IT sets these in the library item; an empty value falls back to
# the environment variable named on the right.
# ---------------------------------------------------------------------------
API_KEY=""          # ARIZE_API_KEY: Arize API key (never printed)
SPACE_ID=""         # ARIZE_SPACE_ID: Arize space ID
OTLP_ENDPOINT=""    # ARIZE_OTLP_ENDPOINT: optional, setup's default when empty
PACKAGE_SOURCE=""   # ARIZE_MDM_PACKAGE_SOURCE: wheel URL (http/https) or local wheel path
UV_SOURCE=""        # ARIZE_MDM_UV_SOURCE: empty for the pinned uv release, or a local uv-<target>.tar.gz
HARNESSES=""        # ARIZE_MDM_HARNESSES: space- or comma-separated, e.g. "claude-code cursor codex"
EMAIL_MAP_FILE=""   # ARIZE_MDM_EMAIL_MAP_FILE: file of "username email" lines
IRU_PLIST=""        # ARIZE_MDM_IRU_PLIST: Iru global variables plist
PYTHON_MIRROR=""    # UV_PYTHON_INSTALL_MIRROR: optional mirror for uv's managed Python
LOG_PROMPTS=""      # ARIZE_LOG_PROMPTS: optional, "true" captures prompt text
LOG_TOOL_DETAILS="" # ARIZE_LOG_TOOL_DETAILS: optional, "true" captures tool commands, paths and URLs
LOG_TOOL_CONTENT="" # ARIZE_LOG_TOOL_CONTENT: optional, "true" captures tool output

: "${API_KEY:=${ARIZE_API_KEY:-}}"
: "${SPACE_ID:=${ARIZE_SPACE_ID:-}}"
: "${OTLP_ENDPOINT:=${ARIZE_OTLP_ENDPOINT:-}}"
: "${PACKAGE_SOURCE:=${ARIZE_MDM_PACKAGE_SOURCE:-}}"
: "${UV_SOURCE:=${ARIZE_MDM_UV_SOURCE:-}}"
: "${HARNESSES:=${ARIZE_MDM_HARNESSES:-}}"
: "${EMAIL_MAP_FILE:=${ARIZE_MDM_EMAIL_MAP_FILE:-}}"
: "${IRU_PLIST:=${ARIZE_MDM_IRU_PLIST:-/Library/Managed Preferences/io.kandji.globalvariables.plist}}"
: "${PYTHON_MIRROR:=${UV_PYTHON_INSTALL_MIRROR:-}}"
: "${LOG_PROMPTS:=${ARIZE_LOG_PROMPTS:-}}"
: "${LOG_TOOL_DETAILS:=${ARIZE_LOG_TOOL_DETAILS:-}}"
: "${LOG_TOOL_CONTENT:=${ARIZE_LOG_TOOL_CONTENT:-}}"

set -u
umask 022

UV_VERSION="0.13.5"
# https://github.com/astral-sh/uv/releases/download/0.13.5/uv-aarch64-apple-darwin.tar.gz.sha256
UV_SHA256_AARCH64="743f85035a1996ecb350659e01cd9faea9709829be09b1b4b03c73eaf3c9cd73"
# https://github.com/astral-sh/uv/releases/download/0.13.5/uv-x86_64-apple-darwin.tar.gz.sha256
UV_SHA256_X86_64="b0f52b0e3945b4107d89024ea38e625683011fbfc05a0d98f9fa2d6dcee28c66"

# Harness names setup accepts, except copilot, whose hooks are per repo.
SUPPORTED_HARNESSES="claude claude-code codex cursor gemini antigravity opencode omp kiro devin"

# Set only by tests.
SYSTEM_ROOT="${ARIZE_MDM_SYSTEM_ROOT:-}"
CREDENTIALS_DIR="${ARIZE_MDM_CREDENTIALS_DIR:-$SYSTEM_ROOT/Library/Application Support/Arize/harness}"
PLISTBUDDY="${ARIZE_MDM_PLISTBUDDY:-/usr/libexec/PlistBuddy}"

ARIZE_DIR="$SYSTEM_ROOT/opt/arize"
UV_BIN="$ARIZE_DIR/bin/uv"
TOOL_DIR="$ARIZE_DIR/tools"
PYTHON_DIR="$ARIZE_DIR/python"
BIN_DIR="$SYSTEM_ROOT/usr/local/bin"
WHEEL_MARK="$ARIZE_DIR/mdm-wheel.sha256"
WHEEL_DIR="$ARIZE_DIR/wheel"  # the wheel uv's receipt points to, for `uv tool upgrade`
HARNESS_BIN="$BIN_DIR/arize-harness"

log() { echo "arize-mdm: $*"; }
fail() { log "error: $2" >&2; exit "$1"; }

WORK=""
COMMITTING=0
CREATED=""  # paths this run created, newest first

cleanup() {
    [ -n "$WORK" ] && rm -rf "$WORK"
    return 0
}

# Adds each path that doesn't exist yet to CREATED.
note_missing() {
    local path
    for path in "$@"; do
        [ -e "$path" ] || [ -L "$path" ] || CREATED="$path
$CREATED"
    done
}

rollback() {
    # Removes only what this run created; an existing install is left as it was.
    [ "$COMMITTING" = 1 ] || return 0
    local path link
    case "
$CREATED" in
        *"
$TOOL_DIR
"*)
            for link in "$BIN_DIR"/*; do
                [ -L "$link" ] || continue
                case "$(readlink "$link")" in "$TOOL_DIR"/*) rm -f "$link" ;; esac
            done
            ;;
    esac
    while IFS= read -r path; do
        [ -n "$path" ] || continue
        case "$path" in
            "$ARIZE_DIR" | "$ARIZE_DIR"/*) rm -rf "$path" ;;
            # Shared folders such as /opt and /usr/local/bin: only if empty.
            *) rmdir "$path" 2>/dev/null ;;
        esac
    done <<EOF_CREATED
$CREATED
EOF_CREATED
    return 0
}

on_exit() {
    local rc=$?
    [ "$rc" != 0 ] && rollback
    cleanup
    exit "$rc"
}
trap on_exit EXIT

sha256_of() { shasum -a 256 "$1" | awk '{print $1}'; }

# ---------------------------------------------------------------------------
# Preflight: checks every input; changes nothing.
# ---------------------------------------------------------------------------
preflight() {
    [ "$(id -u)" = 0 ] || fail 2 "must run as root"
    [ "$(uname -s)" = Darwin ] || fail 2 "macOS only"
    [ -n "$API_KEY" ] || fail 2 "missing API key (API_KEY or ARIZE_API_KEY)"
    [ -n "$SPACE_ID" ] || fail 2 "missing space ID (SPACE_ID or ARIZE_SPACE_ID)"
    [ -n "$PACKAGE_SOURCE" ] || fail 2 "missing package source (PACKAGE_SOURCE)"

    # Values go single-quoted into each credentials file.
    local v
    for v in "$API_KEY" "$SPACE_ID" "$OTLP_ENDPOINT" "$LOG_PROMPTS" "$LOG_TOOL_DETAILS" "$LOG_TOOL_CONTENT"; do
        case "$v" in
            *"'"* | *\\* | *"
"*) fail 2 "API key, space ID, endpoint and ARIZE_LOG_* values can't contain quotes, backslashes or line breaks" ;;
        esac
    done

    if [ -n "$EMAIL_MAP_FILE" ] && { [ ! -f "$EMAIL_MAP_FILE" ] || [ ! -r "$EMAIL_MAP_FILE" ]; }; then
        fail 2 "email map file missing or unreadable: $EMAIL_MAP_FILE"
    fi

    local h
    HARNESS_LIST=""
    set -f
    for h in $(echo "$HARNESSES" | tr ',' ' '); do
        [ "$h" = claude ] && h=claude-code
        case " $HARNESS_LIST " in *" $h "*) continue ;; esac
        HARNESS_LIST="${HARNESS_LIST:+$HARNESS_LIST }$h"
    done
    [ -n "$HARNESS_LIST" ] || fail 2 "empty harness list (HARNESSES)"
    for h in $HARNESS_LIST; do
        [ "$h" = copilot ] && fail 2 "copilot is not supported: its hooks are per repo"
        case " $SUPPORTED_HARNESSES " in
            *" $h "*) ;;
            *) fail 2 "unknown harness: $h (supported: $SUPPORTED_HARNESSES)" ;;
        esac
    done
    set +f

    case "$PACKAGE_SOURCE" in
        http://* | https://*) PACKAGE_KIND=url ;;
        *://*) fail 2 "unsupported PACKAGE_SOURCE form (use a wheel URL or a local wheel path)" ;;
        *.whl) PACKAGE_KIND=file ;;
        *) fail 2 "unsupported PACKAGE_SOURCE form (use a wheel URL or a local wheel path)" ;;
    esac

    if [ "$PACKAGE_KIND" = file ] && [ ! -f "$PACKAGE_SOURCE" ]; then
        fail 2 "wheel not found: $PACKAGE_SOURCE"
    fi

    case "$(uname -m)" in
        arm64) UV_TARGET=aarch64-apple-darwin; UV_SHA256="$UV_SHA256_AARCH64" ;;
        x86_64) UV_TARGET=x86_64-apple-darwin; UV_SHA256="$UV_SHA256_X86_64" ;;
        *) fail 2 "unsupported CPU: $(uname -m)" ;;
    esac
    return 0
}

# ---------------------------------------------------------------------------
# Staging: downloads and verifies into a temp folder.
# ---------------------------------------------------------------------------
stage_uv() {
    local archive="$WORK/uv.tar.gz"
    if [ -n "$UV_SOURCE" ]; then
        [ -f "$UV_SOURCE" ] || fail 1 "uv file not found: $UV_SOURCE"
        cp "$UV_SOURCE" "$archive" || fail 1 "cannot read uv file: $UV_SOURCE"
    else
        local url="https://github.com/astral-sh/uv/releases/download/$UV_VERSION/uv-$UV_TARGET.tar.gz"
        curl -fsSL --retry 3 -o "$archive" "$url" || fail 1 "cannot download uv from $url"
    fi
    [ "$(sha256_of "$archive")" = "$UV_SHA256" ] || fail 1 "uv checksum mismatch; nothing installed"
    mkdir "$WORK/uv" && tar -xzf "$archive" -C "$WORK/uv" || fail 1 "cannot unpack uv"
    STAGED_UV="$(find "$WORK/uv" -type f -name uv | head -n 1)"
    [ -n "$STAGED_UV" ] && "$STAGED_UV" --version >/dev/null 2>&1 || fail 1 "unpacked uv does not run"
}

stage_wheel() {
    local name
    if [ "$PACKAGE_KIND" = url ]; then
        name="${PACKAGE_SOURCE%%[?#]*}"
        name="${name##*/}"
    else
        name="${PACKAGE_SOURCE##*/}"
    fi
    WHEEL_NAME="$name"
    STAGED_WHEEL="$WORK/$name"
    if [ "$PACKAGE_KIND" = url ]; then
        curl -fsSL --retry 3 -o "$STAGED_WHEEL" "$PACKAGE_SOURCE" || fail 1 "cannot download the wheel from PACKAGE_SOURCE"
    else
        [ -f "$PACKAGE_SOURCE" ] || fail 1 "wheel not found: $PACKAGE_SOURCE"
        cp "$PACKAGE_SOURCE" "$STAGED_WHEEL" || fail 1 "cannot read wheel: $PACKAGE_SOURCE"
    fi
    unzip -tq "$STAGED_WHEEL" >/dev/null 2>&1 || fail 1 "not a valid wheel: $name"
    WHEEL_SHA256="$(sha256_of "$STAGED_WHEEL")"
}

# ---------------------------------------------------------------------------
# Install: uv into /opt/arize/bin, then arize-harness as a system-wide uv tool.
# ---------------------------------------------------------------------------
install_uv() {
    mkdir -p "$ARIZE_DIR/bin" || fail 1 "cannot create $ARIZE_DIR/bin"
    if ! cmp -s "$STAGED_UV" "$UV_BIN"; then
        cp "$STAGED_UV" "$UV_BIN.new" && chmod 0755 "$UV_BIN.new" && mv -f "$UV_BIN.new" "$UV_BIN" \
            || fail 1 "cannot install uv to $UV_BIN"
        log "installed uv $UV_VERSION at $UV_BIN"
    fi
}

system_install_current() {
    [ -f "$WHEEL_MARK" ] && [ "$(cat "$WHEEL_MARK")" = "$WHEEL_SHA256" ] || return 1
    cmp -s "$STAGED_WHEEL" "$WHEEL_DIR/$WHEEL_NAME" || return 1
    case "$(readlink "$HARNESS_BIN")" in "$TOOL_DIR"/*) ;; *) return 1 ;; esac
    "$HARNESS_BIN" --version >/dev/null 2>&1
}

# Installs wheel $1 as the system-wide uv tool and checks that it runs.
uv_tool_install() {
    # --reinstall replaces an install of the same version; UV_NO_CONFIG ignores uv.toml files.
    (umask 022 && env \
        UV_NO_CONFIG=1 \
        UV_TOOL_DIR="$TOOL_DIR" \
        UV_TOOL_BIN_DIR="$BIN_DIR" \
        UV_PYTHON_INSTALL_DIR="$PYTHON_DIR" \
        UV_PYTHON_PREFERENCE=only-managed \
        UV_COMPILE_BYTECODE=1 \
        "$UV_BIN" tool install --force --reinstall --python 3.12 "$1") \
        && "$HARNESS_BIN" --version >/dev/null 2>&1
}

# Copies the wheel matching WHEEL_MARK to $WORK/prev and sets PREV_WHEEL (empty if none).
save_previous_wheel() {
    PREV_WHEEL=""
    [ -f "$WHEEL_MARK" ] || return 0
    local mark old
    mark="$(cat "$WHEEL_MARK")"
    for old in "$WHEEL_DIR"/*.whl; do
        [ -f "$old" ] && [ "$(sha256_of "$old")" = "$mark" ] || continue
        mkdir -p "$WORK/prev" && cp -p "$old" "$WORK/prev/" || fail 1 "cannot back up $old"
        PREV_WHEEL="$WORK/prev/${old##*/}"
        return 0
    done
}

system_install() {
    if system_install_current; then
        log "system install is current"
        return 0
    fi
    if [ -n "$PYTHON_MIRROR" ]; then
        export UV_PYTHON_INSTALL_MIRROR="$PYTHON_MIRROR"
    fi
    mkdir -p "$BIN_DIR" "$WHEEL_DIR" || fail 1 "cannot create $BIN_DIR or $WHEEL_DIR"
    save_previous_wheel
    local wheel="$WHEEL_DIR/$WHEEL_NAME" old
    cp "$STAGED_WHEEL" "$wheel.new" && mv -f "$wheel.new" "$wheel" || fail 1 "cannot write $wheel"
    if ! uv_tool_install "$wheel"; then
        if [ -n "$PREV_WHEEL" ]; then
            # Reinstall the previous wheel.
            rm -f "$wheel"
            old="$WHEEL_DIR/${PREV_WHEEL##*/}"
            cp -p "$PREV_WHEEL" "$old" && uv_tool_install "$old" \
                || log "error: could not restore the previous install" >&2
        fi
        fail 1 "uv tool install failed, or the installed $HARNESS_BIN does not run"
    fi
    for old in "$WHEEL_DIR"/*.whl; do
        [ "$old" = "$wheel" ] || rm -f "$old"
    done
    echo "$WHEEL_SHA256" >"$WHEEL_MARK.new" && mv -f "$WHEEL_MARK.new" "$WHEEL_MARK" \
        || fail 1 "cannot write $WHEEL_MARK"
    log "system install ready: $HARNESS_BIN"
}

# ---------------------------------------------------------------------------
# Per-user setup: a credentials file and `arize-harness setup` for each user.
# ---------------------------------------------------------------------------

# Prints "name<TAB>home" for each real account: UID >= 501, not _-prefixed, a shell other than false/nologin, a home folder.
list_users() {
    local name uid home shell
    dscl . -list /Users UniqueID | while read -r name uid; do
        case "$name" in "" | _*) continue ;; esac
        case "$uid" in "" | *[!0-9]*) continue ;; esac
        [ "$uid" -ge 501 ] || continue
        shell="$(dscl . -read "/Users/$name" UserShell 2>/dev/null | sed -n 's/^UserShell: *//p')"
        case "$shell" in "" | */false | */nologin) continue ;; esac
        # A value with spaces comes on the next line, indented by one space.
        home="$(dscl . -read "/Users/$name" NFSHomeDirectory 2>/dev/null \
            | sed -e '1s/^NFSHomeDirectory://' -e 's/^ //' | tr -d '\n')"
        [ -n "$home" ] && [ -d "$home" ] || continue
        printf '%s\t%s\n' "$name" "$home"
    done
}

# Prints user $1's email, or nothing. Sources: EMAIL_MAP_FILE.
email_for() {
    [ -n "$EMAIL_MAP_FILE" ] || return 0
    awk -v user="$1" '{ sub(/\r$/, "") } /^[ \t]*(#|$)/ { next } $1 == user { print $2; exit }' "$EMAIL_MAP_FILE"
}

# True if $1 has one @, not at either end, and no quote or backslash.
usable_email() {
    case "$1" in
        *"'"* | *\\* | @* | *@ | *@*@*) return 1 ;;
        *@*) return 0 ;;
    esac
    return 1
}

# Writes user $1's credentials file for email $2 unless it is already current.
write_credentials() {
    local user="$1" email="$2"
    local file="$CREDENTIALS_DIR/$user.env" staged="$WORK/$user.env" tmp="$CREDENTIALS_DIR/.$user.env.new"
    {
        echo "ARIZE_BACKEND=arize"
        echo "ARIZE_API_KEY='$API_KEY'"
        echo "ARIZE_SPACE_ID='$SPACE_ID'"
        [ -z "$OTLP_ENDPOINT" ] || echo "ARIZE_OTLP_ENDPOINT='$OTLP_ENDPOINT'"
        echo "ARIZE_USER_ID='$email'"
        [ -z "$LOG_PROMPTS" ] || echo "ARIZE_LOG_PROMPTS='$LOG_PROMPTS'"
        [ -z "$LOG_TOOL_DETAILS" ] || echo "ARIZE_LOG_TOOL_DETAILS='$LOG_TOOL_DETAILS'"
        [ -z "$LOG_TOOL_CONTENT" ] || echo "ARIZE_LOG_TOOL_CONTENT='$LOG_TOOL_CONTENT'"
    } >"$staged" || return 1
    if [ -f "$file" ] && [ ! -L "$file" ] && cmp -s "$staged" "$file" \
        && [ "$(stat -f '%Su:%Sg %Lp' "$file")" = "root:wheel 400" ] \
        && [ "$(ls -led "$file" | sed -n 's/^ *[0-9][0-9]*: //p')" = "user:$user allow read" ]; then
        return 0
    fi
    rm -f "$tmp"
    # Owner, mode and ACL go on after the move, which replaces the file; until then only root can read it.
    (umask 077 && cat "$staged" >"$tmp") && mv -f "$tmp" "$file" || { rm -f "$tmp"; return 1; }
    chown root:wheel "$file" && chmod 0400 "$file" && chmod -N "$file" && chmod +a "$user allow read" "$file" \
        || return 1
    log "wrote credentials for $user"
}

# Runs `arize-harness setup <harness> -y` as user $1 (home $2) for each listed harness.
setup_harnesses() {
    local user="$1" home="$2" h rc=0
    set -f
    for h in $HARNESS_LIST; do
        if ! (cd "$home" && sudo -H -u "$user" env HOME="$home" ARIZE_ENV_FILE="$CREDENTIALS_DIR/$user.env" \
            "$HARNESS_BIN" setup "$h" -y </dev/null); then
            log "error: setup $h failed for $user" >&2
            rc=1
        fi
    done
    set +f
    return "$rc"
}

# Sets up every real account with an email; returns 1 if any user failed.
setup_users() {
    local user home email rc=0 found=0
    if [ ! -d "$CREDENTIALS_DIR" ]; then
        mkdir -p "$CREDENTIALS_DIR" && chmod 0755 "$CREDENTIALS_DIR" || fail 1 "cannot create $CREDENTIALS_DIR"
    fi
    while IFS="$(printf '\t')" read -r user home; do
        [ -n "$user" ] || continue
        found=1
        email="$(email_for "$user")"
        if [ -z "$email" ]; then
            log "skipping $user: no email"
            continue
        fi
        if ! usable_email "$email"; then
            log "error: skipping $user: not a usable email: $email" >&2
            rc=1
            continue
        fi
        if ! write_credentials "$user" "$email"; then
            log "error: cannot write credentials for $user" >&2
            rc=1
            continue
        fi
        setup_harnesses "$user" "$home" || rc=1
    done <<EOF_USERS
$(list_users)
EOF_USERS
    [ "$found" = 1 ] || log "no user accounts found"
    return "$rc"
}

main() {
    preflight
    WORK="$(mktemp -d "${TMPDIR:-/tmp}/arize-mdm.XXXXXX")" || fail 1 "cannot create a temp folder"
    stage_uv
    stage_wheel

    note_missing "$SYSTEM_ROOT/opt" "$SYSTEM_ROOT/usr" "$SYSTEM_ROOT/usr/local" "$ARIZE_DIR" "$ARIZE_DIR/bin" "$TOOL_DIR" "$PYTHON_DIR" "$BIN_DIR" "$WHEEL_DIR" "$WHEEL_MARK"
    COMMITTING=1
    install_uv
    system_install
    COMMITTING=0

    # Past the commit window: a user's failure never rolls back the system install.
    setup_users
    exit "$?"
}

main "$@"
