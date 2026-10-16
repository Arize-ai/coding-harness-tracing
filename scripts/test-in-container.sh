#!/usr/bin/env bash
# Run scripts/smoke-install.sh inside a clean Linux container, for a distro you
# do not have locally. CI's linux-distro-offline job runs this same script.
#
#   scripts/test-in-container.sh [ubuntu|fedora] [harness]
#
#   CONTAINER_ENGINE=docker   use this engine (default: podman, else docker)
#   SMOKE_SHELL=1             after the run, pass or fail, open a shell in the
#                             container with the install kept for inspection
#
# Distro defaults to ubuntu, harness to claude. The checkout is mounted
# read-only and copied inside the container, so nothing is written back.
# Podman is free and needs no Docker Desktop license. Any docker CLI works too,
# for example on Colima. See "Testing an install locally" in CONTRIBUTING.md.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Pinned, so every run tests with the same uv. Bump it on purpose.
UV_VERSION="0.11.24"

die() { echo "test-in-container: $*" >&2; exit 1; }

distro="${1:-ubuntu}"
harness="${2:-claude}"

# Fully qualified image names: Podman refuses or prompts for short names
# unless the host has a registry configured for them.
# Only what the smoke test needs, so the image stays close to stock.
case "$distro" in
    # Noninteractive, or with a terminal attached tzdata waits for a timezone
    # at a prompt hidden by the >/dev/null.
    ubuntu) image="docker.io/library/ubuntu:24.04"
            prep="export DEBIAN_FRONTEND=noninteractive && apt-get update -qq && apt-get install -y -qq ca-certificates python3 python3-venv >/dev/null" ;;
    # No curl: uv comes from PyPI and a --wheel-dir install downloads nothing.
    # Installing curl on Fedora can also conflict with the image's curl-minimal.
    fedora) image="docker.io/library/fedora:44"
            prep="dnf install -y -q python3 >/dev/null" ;;
    *)      die "unknown distro '$distro' (expected ubuntu or fedora)" ;;
esac

engine="${CONTAINER_ENGINE:-}"
if [[ -z "$engine" ]]; then
    if command -v podman >/dev/null; then engine=podman
    elif command -v docker >/dev/null; then engine=docker
    else die "no container engine found. Install Podman: brew install podman && podman machine init && podman machine start"
    fi
fi
command -v "$engine" >/dev/null || die "$engine not found"
if ! "$engine" info >/dev/null 2>&1; then
    [[ "$engine" == podman && "$(uname)" == Darwin ]] &&
        die "Podman is not running. Start it with: podman machine start (the first time: podman machine init)"
    die "$engine is not running. Start it and retry, or run scripts/smoke-install.sh directly."
fi

tty_flag=()
[[ -t 0 && -t 1 ]] && tty_flag=(-it)
[[ "${SMOKE_SHELL:-}" == 1 && ${#tty_flag[@]} -eq 0 ]] && die "SMOKE_SHELL=1 needs a terminal"

# uv comes from PyPI at an exact version in its own venv, linked onto PATH. Its
# venv is not put on PATH itself, because install.sh would then pick the venv's
# python3 in place of the distro's. SMOKE_SHELL implies SMOKE_KEEP, so the
# install is still there to inspect. label=disable lets the container read the
# mount on an SELinux host such as Fedora, without relabeling the checkout as
# :z would. Where SELinux is off it does nothing.
exec "$engine" run --rm "${tty_flag[@]+"${tty_flag[@]}"}" \
    --security-opt label=disable \
    -v "$ROOT:/src:ro" \
    -e UV_VERSION="$UV_VERSION" \
    -e SMOKE_SHELL="${SMOKE_SHELL:-}" \
    -e SMOKE_KEEP="${SMOKE_SHELL:+1}" \
    "$image" bash -c '
        set -euo pipefail
        '"$prep"'
        python3 -m venv /opt/uv
        /opt/uv/bin/pip install --quiet --disable-pip-version-check "uv==$UV_VERSION"
        ln -s /opt/uv/bin/uv /usr/local/bin/uv
        mkdir /tmp/src
        tar -C /src --exclude=.git --exclude=.venv -cf - . | tar -C /tmp/src -xf -
        rc=0
        /tmp/src/scripts/smoke-install.sh "$1" || rc=$?
        if [[ "$SMOKE_SHELL" == 1 ]]; then
            echo
            echo "test-in-container: smoke test exited $rc. Opening a shell; exit to remove the container."
            exec bash
        fi
        exit "$rc"
    ' bash "$harness"
