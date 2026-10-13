#!/usr/bin/env bash
# Run scripts/smoke-install.sh inside a clean Linux container, for a distro you
# do not have locally.
#
#   scripts/test-in-container.sh [ubuntu|fedora] [harness]
#
# Distro defaults to ubuntu, harness to claude. The checkout is mounted
# read-only and copied inside the container, so nothing is written back.
# Only this helper needs Docker; smoke-install.sh runs fine without it.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Pinned, so every run tests with the same uv. Bump it on purpose.
UV_VERSION="0.11.24"

die() { echo "test-in-container: $*" >&2; exit 1; }

distro="${1:-ubuntu}"
harness="${2:-claude}"

case "$distro" in
    # Noninteractive, or with a terminal attached tzdata waits for a timezone
    # at a prompt hidden by the >/dev/null.
    ubuntu) image="ubuntu:24.04"
            prep="export DEBIAN_FRONTEND=noninteractive && apt-get update -qq && apt-get install -y -qq ca-certificates python3 python3-venv >/dev/null" ;;
    # No curl: uv comes from PyPI and a --wheel-dir install downloads nothing.
    # Installing curl on Fedora can also conflict with the image's curl-minimal.
    fedora) image="fedora:44"
            prep="dnf install -y -q python3 >/dev/null" ;;
    *)      die "unknown distro '$distro' (expected ubuntu or fedora)" ;;
esac

command -v docker >/dev/null || die "docker not found. Only this helper needs Docker."
docker info >/dev/null 2>&1 || die "Docker is not running. Start it and retry, or run scripts/smoke-install.sh directly."

tty_flag=()
[[ -t 0 && -t 1 ]] && tty_flag=(-it)

# uv comes from PyPI at an exact version in its own venv, linked onto PATH. Its
# venv is not put on PATH itself, because install.sh would then pick the venv's
# python3 in place of the distro's.
exec docker run --rm "${tty_flag[@]+"${tty_flag[@]}"}" \
    -v "$ROOT:/src:ro" \
    -e UV_VERSION="$UV_VERSION" \
    "$image" bash -c '
        set -euo pipefail
        '"$prep"'
        python3 -m venv /opt/uv
        /opt/uv/bin/pip install --quiet --disable-pip-version-check "uv==$UV_VERSION"
        ln -s /opt/uv/bin/uv /usr/local/bin/uv
        mkdir /tmp/src
        tar -C /src --exclude=.git --exclude=.venv -cf - . | tar -C /tmp/src -xf -
        exec /tmp/src/scripts/smoke-install.sh "$1"
    ' bash "$harness"
