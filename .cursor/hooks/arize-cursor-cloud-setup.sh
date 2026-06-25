#!/usr/bin/env bash
set -euo pipefail

if [[ -z "${PHOENIX_ENDPOINT:-}" && ( -z "${ARIZE_API_KEY:-}" || -z "${ARIZE_SPACE_ID:-}" ) ]]; then
    echo "[arize] Warning: set ARIZE_API_KEY+ARIZE_SPACE_ID or PHOENIX_ENDPOINT as Cursor Cloud secrets." >&2
fi

run_as_root() {
    if [[ "${EUID:-$(id -u)}" -eq 0 ]]; then
        "$@"
    elif command -v sudo >/dev/null 2>&1; then
        sudo "$@"
    else
        return 1
    fi
}

ensure_python_venv_support() {
    if ! command -v python3 >/dev/null 2>&1; then
        return 0
    fi

    probe_dir="$(mktemp -d)"
    if python3 -m venv "$probe_dir/venv" >/dev/null 2>&1; then
        rm -rf "$probe_dir"
        return 0
    fi
    rm -rf "$probe_dir"

    if ! command -v apt-get >/dev/null 2>&1; then
        echo "[arize] Warning: python3 venv support is missing and apt-get is unavailable." >&2
        return 0
    fi

    echo "[arize] Installing python3-venv for Cursor Cloud bootstrap..." >&2
    run_as_root apt-get update
    if ! run_as_root env DEBIAN_FRONTEND=noninteractive apt-get install -y python3-venv; then
        versioned_pkg="$(python3 - <<'PY'
import sys
print(f"python{sys.version_info.major}.{sys.version_info.minor}-venv")
PY
)"
        run_as_root env DEBIAN_FRONTEND=noninteractive apt-get install -y "$versioned_pkg"
    fi
}

remove_incomplete_harness_venv() {
    venv_dir="$HOME/.arize/harness/venv"
    if [[ -d "$venv_dir" && ! -x "$venv_dir/bin/pip" && ! -x "$venv_dir/Scripts/pip.exe" ]]; then
        echo "[arize] Removing incomplete harness venv at $venv_dir" >&2
        rm -rf "$venv_dir"
    fi
}

ensure_python_venv_support
remove_incomplete_harness_venv

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
