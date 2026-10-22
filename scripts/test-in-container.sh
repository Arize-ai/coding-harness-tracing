#!/usr/bin/env bash
# Run scripts/smoke-install.sh in a clean Linux container. CI's
# linux-distro-offline job runs this script.
#
#   scripts/test-in-container.sh [ubuntu|fedora] [harness]
#
#   CONTAINER_ENGINE=docker   engine to use (default: podman, else docker)
#   SMOKE_SHELL=1             open a shell in the container afterwards
#
# Distro defaults to ubuntu, harness to claude. The checkout is mounted
# read-only. See CONTRIBUTING.md.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Pinned, so every run uses the same uv.
UV_VERSION="0.13.5"
PIP_REQUIREMENT="pip @ https://files.pythonhosted.org/packages/3f/06/3e4420302363fbb71be3c7f35ce042f54bfca330e3e66b3a2e3c4cca579f/pip-26.2.2-py3-none-any.whl#sha256=fd7f4dfa8190e014013b9f08f3e973f4e4c26ffd179a204895533a7254813a70"

die() { echo "test-in-container: $*" >&2; exit 1; }

distro="${1:-ubuntu}"
harness="${2:-claude}"

# Full image names, since Podman rejects short ones by default. Install only
# what the smoke test needs, so the image stays close to stock.
case "$distro" in
    # Noninteractive, or tzdata hangs on a hidden timezone prompt.
    ubuntu) image="docker.io/library/ubuntu:24.04"
            prep="export DEBIAN_FRONTEND=noninteractive && apt-get update -qq && apt-get install -y -qq ca-certificates python3 python3-venv >/dev/null" ;;
    # No curl: nothing needs it, and it conflicts with curl-minimal.
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

# uv gets its own venv, linked onto PATH, so install.sh still picks the
# distro's python3. label=disable lets SELinux hosts read the mount without
# relabeling it.
# SMOKE_SHELL implies SMOKE_KEEP.
exec "$engine" run --rm "${tty_flag[@]+"${tty_flag[@]}"}" \
    --security-opt label=disable \
    -v "$ROOT:/src:ro" \
    -e UV_VERSION="$UV_VERSION" \
    -e PIP_REQUIREMENT="$PIP_REQUIREMENT" \
    -e SMOKE_SHELL="${SMOKE_SHELL:-}" \
    -e SMOKE_KEEP="${SMOKE_SHELL:+1}" \
    "$image" bash -c '
        set -euo pipefail
        '"$prep"'
        python3 -m venv /opt/uv
        /opt/uv/bin/python -m pip install --isolated --quiet --disable-pip-version-check --no-deps --upgrade "$PIP_REQUIREMENT"
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
