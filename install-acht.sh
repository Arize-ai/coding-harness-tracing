#!/bin/bash
# Installer for acht — the Arize Coding Harness Tracing CLI.
#
# Usage:
#   curl -sSL https://raw.githubusercontent.com/Arize-ai/coding-harness-tracing/main/install-acht.sh | bash
#
# Environment variables:
#   AX_TRACE_VERSION   Pin to a specific version (e.g. v0.1.0). Default: latest.
#   AX_TRACE_INSTALL_DIR   Install directory. Default: ~/.local/bin.

set -euo pipefail

REPO="Arize-ai/coding-harness-tracing"
INSTALL_DIR="${AX_TRACE_INSTALL_DIR:-$HOME/.local/bin}"
VERSION="${AX_TRACE_VERSION:-}"

# -- detect OS + arch --------------------------------------------------------
detect_platform() {
    local os arch
    os="$(uname -s | tr '[:upper:]' '[:lower:]')"
    arch="$(uname -m)"
    case "$arch" in
        x86_64|amd64) arch="amd64" ;;
        arm64|aarch64) arch="arm64" ;;
        *) echo "error: unsupported architecture: $arch" >&2; exit 1 ;;
    esac
    case "$os" in
        linux|darwin) ;;
        *) echo "error: unsupported OS: $os" >&2; exit 1 ;;
    esac
    echo "${os}_${arch}"
}

# -- pick a sha256 verification command -------------------------------------
# macOS ships `shasum -a 256` by default; Linux ships `sha256sum`. Pick whichever
# is available, fail closed if neither is present (verification is mandatory).
sha256_check() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum -c -
    elif command -v shasum >/dev/null 2>&1; then
        shasum -a 256 -c -
    else
        echo "error: no sha256 verification tool found (need sha256sum or shasum)" >&2
        return 1
    fi
}

# -- resolve version (latest if unset) --------------------------------------
# Walk paginated releases until we find one tagged cmd/acht/v*. The repo
# also ships non-acht releases, so the first page is not guaranteed to
# contain our tag.
resolve_version() {
    if [[ -n "$VERSION" ]]; then echo "$VERSION"; return; fi
    local api="https://api.github.com/repos/$REPO/releases"
    local page=1 tag=""
    while [[ $page -le 5 ]]; do
        local body
        body="$(curl -sSL --retry 3 --retry-delay 2 "$api?per_page=100&page=$page")"
        tag="$(echo "$body" | grep -oE '"tag_name": *"cmd/acht/v[^"]+"' | head -1 | sed 's|.*"cmd/acht/\(v[^"]*\)"|\1|')"
        if [[ -n "$tag" ]]; then echo "$tag"; return; fi
        # If page returned fewer than 100 entries, we've hit the end.
        local count
        count="$(echo "$body" | grep -c '"tag_name":' || true)"
        if [[ "$count" -lt 100 ]]; then break; fi
        page=$((page + 1))
    done
    echo "error: could not resolve latest acht version" >&2
    exit 1
}

# -- download + verify ------------------------------------------------------
download_release() {
    local version="$1" platform="$2"
    local base="https://github.com/$REPO/releases/download/cmd/acht/${version}"
    local archive="acht_${version#v}_${platform}.tar.gz"
    local checksums="checksums.txt"

    curl -sSLf --retry 3 --retry-delay 2 "$base/$archive" -o "$TMPDIR_INSTALL/$archive"
    curl -sSLf --retry 3 --retry-delay 2 "$base/$checksums" -o "$TMPDIR_INSTALL/$checksums"

    # Verify SHA256. GoReleaser checksum format is "<hash>  <file>"; match the
    # exact filename at end-of-line so we don't accept substring matches.
    (cd "$TMPDIR_INSTALL" && grep -F -- "  $archive" "$checksums" | sha256_check) || {
        echo "error: SHA256 verification failed for $archive" >&2
        exit 1
    }

    tar -xzf "$TMPDIR_INSTALL/$archive" -C "$TMPDIR_INSTALL"
    echo "$TMPDIR_INSTALL/acht"
}

# -- install + PATH check ---------------------------------------------------
install_binary() {
    local src="$1"
    mkdir -p "$INSTALL_DIR"
    cp "$src" "$INSTALL_DIR/acht"
    chmod +x "$INSTALL_DIR/acht"
    echo "[acht] Installed to $INSTALL_DIR/acht"

    case ":$PATH:" in
        *":$INSTALL_DIR:"*) ;;
        *)
            echo ""
            echo "[acht] $INSTALL_DIR is not on your PATH. Add this line to your shell rc:"
            echo "  export PATH=\"$INSTALL_DIR:\$PATH\""
            echo ""
            ;;
    esac
}

main() {
    TMPDIR_INSTALL="$(mktemp -d)"
    trap 'rm -rf "$TMPDIR_INSTALL"' EXIT

    local platform version binary
    platform="$(detect_platform)"
    version="$(resolve_version)"
    echo "[acht] Installing acht $version for $platform"
    binary="$(download_release "$version" "$platform")"
    install_binary "$binary"
    echo ""
    echo "[acht] Run 'acht add claude' to get started"
}

main "$@"
