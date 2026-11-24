#!/bin/bash
# Arize coding harness tracing: MDM install for macOS (Iru Custom Script, runs as root).
# Installs a checksum-verified uv and a system-wide arize-harness.
# Exit codes: 0 done, 1 install failed, 2 bad input or environment.

# ---------------------------------------------------------------------------
# Settings. IT sets these in the library item; an empty value falls back to
# the environment variable named on the right.
# ---------------------------------------------------------------------------
API_KEY=""          # ARIZE_API_KEY: Arize API key (never printed)
SPACE_ID=""         # ARIZE_SPACE_ID: Arize space ID
OTLP_ENDPOINT=""    # ARIZE_OTLP_ENDPOINT: optional, setup's default when empty
PACKAGE_SOURCE=""   # ARIZE_MDM_PACKAGE_SOURCE: wheel URL (http/https), local wheel path, or coding-harness-tracing==X.Y.Z
UV_SOURCE=""        # ARIZE_MDM_UV_SOURCE: empty for the pinned uv release, or a local uv-<target>.tar.gz
HARNESSES=""        # ARIZE_MDM_HARNESSES: space- or comma-separated, e.g. "claude-code cursor codex"
EMAIL_MAP_FILE=""   # ARIZE_MDM_EMAIL_MAP_FILE: file of "username email" lines
IRU_PLIST=""        # ARIZE_MDM_IRU_PLIST: Iru global variables plist
PYTHON_MIRROR=""    # UV_PYTHON_INSTALL_MIRROR: optional mirror for uv's managed Python

: "${API_KEY:=${ARIZE_API_KEY:-}}"
: "${SPACE_ID:=${ARIZE_SPACE_ID:-}}"
: "${OTLP_ENDPOINT:=${ARIZE_OTLP_ENDPOINT:-}}"
: "${PACKAGE_SOURCE:=${ARIZE_MDM_PACKAGE_SOURCE:-}}"
: "${UV_SOURCE:=${ARIZE_MDM_UV_SOURCE:-}}"
: "${HARNESSES:=${ARIZE_MDM_HARNESSES:-}}"
: "${EMAIL_MAP_FILE:=${ARIZE_MDM_EMAIL_MAP_FILE:-}}"
: "${IRU_PLIST:=${ARIZE_MDM_IRU_PLIST:-/Library/Managed Preferences/io.kandji.globalvariables.plist}}"
: "${PYTHON_MIRROR:=${UV_PYTHON_INSTALL_MIRROR:-}}"

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
WHEEL_MARK="$ARIZE_DIR/mdm-wheel.sha256"  # the installed wheel's SHA-256, or the PyPI requirement
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
        coding-harness-tracing==[0-9]*)
            case "${PACKAGE_SOURCE#coding-harness-tracing==}" in
                *[!0-9A-Za-z.+!-]*) fail 2 "unsupported PACKAGE_SOURCE version: $PACKAGE_SOURCE" ;;
            esac
            PACKAGE_KIND=pypi ;;
        http://* | https://*) PACKAGE_KIND=url ;;
        *.whl) case "$PACKAGE_SOURCE" in *://*) ;; *) PACKAGE_KIND=file ;; esac ;;
    esac
    [ -n "${PACKAGE_KIND:-}" ] \
        || fail 2 "unsupported PACKAGE_SOURCE form (use a wheel URL, a local wheel path, or coding-harness-tracing==X.Y.Z)"

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
    if [ "$PACKAGE_KIND" = pypi ]; then
        INSTALL_MARK="$PACKAGE_SOURCE"
        return 0
    fi
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
    INSTALL_MARK="$(sha256_of "$STAGED_WHEEL")"
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
    [ -f "$WHEEL_MARK" ] && [ "$(cat "$WHEEL_MARK")" = "$INSTALL_MARK" ] || return 1
    [ "$PACKAGE_KIND" = pypi ] || cmp -s "$STAGED_WHEEL" "$WHEEL_DIR/$WHEEL_NAME" || return 1
    case "$(readlink "$HARNESS_BIN")" in "$TOOL_DIR"/*) ;; *) return 1 ;; esac
    "$HARNESS_BIN" --version >/dev/null 2>&1
}

# Installs $1 (a wheel path or PyPI requirement) as the system-wide uv tool and checks that it runs.
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

# Sets PREV_TARGET to the current install's PyPI requirement, or to a copy of its wheel in
# $WORK/prev; empty if there is none.
save_previous_install() {
    PREV_TARGET=""
    [ -f "$WHEEL_MARK" ] || return 0
    local mark old
    mark="$(cat "$WHEEL_MARK")"
    case "$mark" in
        coding-harness-tracing==*) PREV_TARGET="$mark"; return 0 ;;
    esac
    for old in "$WHEEL_DIR"/*.whl; do
        [ -f "$old" ] && [ "$(sha256_of "$old")" = "$mark" ] || continue
        mkdir -p "$WORK/prev" && cp -p "$old" "$WORK/prev/" || fail 1 "cannot back up $old"
        PREV_TARGET="$WORK/prev/${old##*/}"
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
    save_previous_install
    local target="$PACKAGE_SOURCE" wheel="" old
    if [ "$PACKAGE_KIND" != pypi ]; then
        wheel="$WHEEL_DIR/$WHEEL_NAME"
        target="$wheel"
        cp "$STAGED_WHEEL" "$wheel.new" && mv -f "$wheel.new" "$wheel" || fail 1 "cannot write $wheel"
    fi
    if ! uv_tool_install "$target"; then
        if [ -n "$PREV_TARGET" ]; then
            # Reinstall the previous version.
            [ -n "$wheel" ] && rm -f "$wheel"
            old="$PREV_TARGET"
            case "$old" in
                "$WORK"/prev/*) old="$WHEEL_DIR/${PREV_TARGET##*/}"; cp -p "$PREV_TARGET" "$old" ;;
            esac && uv_tool_install "$old" || log "error: could not restore the previous install" >&2
        fi
        fail 1 "uv tool install failed, or the installed $HARNESS_BIN does not run"
    fi
    for old in "$WHEEL_DIR"/*.whl; do
        [ -e "$old" ] && [ "$old" != "$wheel" ] && rm -f "$old"
    done
    echo "$INSTALL_MARK" >"$WHEEL_MARK.new" && mv -f "$WHEEL_MARK.new" "$WHEEL_MARK" \
        || fail 1 "cannot write $WHEEL_MARK"
    log "system install ready: $HARNESS_BIN"
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

    # Per-user setup goes here.

    exit 0
}

main "$@"
