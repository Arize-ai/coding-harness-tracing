#!/usr/bin/env bash
set -euo pipefail

if [[ -z "${PHOENIX_ENDPOINT:-}" && ( -z "${ARIZE_API_KEY:-}" || -z "${ARIZE_SPACE_ID:-}" ) ]]; then
    echo "[arize] Warning: set ARIZE_API_KEY+ARIZE_SPACE_ID or PHOENIX_ENDPOINT as Cursor Cloud secrets." >&2
fi

branch="${ARIZE_INSTALL_BRANCH:-main}"
url="${ARIZE_INSTALL_URL:-https://raw.githubusercontent.com/Arize-ai/coding-harness-tracing/${branch}/install.sh}"
tmp="$(mktemp)"
cleanup() { rm -f "$tmp"; }
trap cleanup EXIT

if command -v curl >/dev/null 2>&1; then
    curl -sSfL "$url" -o "$tmp"
elif command -v wget >/dev/null 2>&1; then
    wget -qO "$tmp" "$url"
else
    echo "[arize] Neither curl nor wget found; cannot install Cursor tracing." >&2
    exit 1
fi

bash "$tmp" cursor --cloud-agent --branch "$branch"
