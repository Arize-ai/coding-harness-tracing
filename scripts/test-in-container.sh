#!/usr/bin/env bash
# Run an install inside a clean Linux container against the local registry and
# release server from scripts/local-registry.sh.
#
#   scripts/test-in-container.sh [ubuntu|fedora] [-- command ...]
#
# With no command: checks both servers are reachable, installs uv, then installs
# the exact version named in the release folder's `latest` file — the build
# publish-local.sh just published — from the local registry. The container sees
# LOCAL_REGISTRY_URL and LOCAL_RELEASE_URL. Only this helper needs Docker.
set -euo pipefail

REGISTRY_PORT="${LOCAL_REGISTRY_PORT:-8080}"
RELEASE_PORT="${LOCAL_RELEASE_PORT:-8000}"

die() { echo "test-in-container: $*" >&2; exit 1; }

distro="ubuntu"
if [[ $# -gt 0 && "$1" != "--" ]]; then
    distro="$1"
    shift
fi
[[ "${1:-}" == "--" ]] && shift

case "$distro" in
    ubuntu) image="ubuntu:24.04"
            prep="apt-get update -qq && apt-get install -y -qq curl ca-certificates >/dev/null" ;;
    fedora) image="fedora:44"
            prep="true" ;;
    *)      die "unknown distro '$distro' (expected ubuntu or fedora)" ;;
esac

command -v docker >/dev/null || die "docker not found. Only this helper needs Docker."
docker info >/dev/null 2>&1 || die "Docker is not running. Start it and retry; the registry and release server don't need it."

if [[ $# -eq 0 ]]; then
    set -- sh -c '
        set -e
        fail() { echo "test-in-container: $*" >&2; exit 1; }
        hint="Is scripts/local-registry.sh running, with the same LOCAL_REGISTRY_PORT/LOCAL_RELEASE_PORT as this helper? On a Linux Docker host, start it with LOCAL_BIND=0.0.0.0."
        curl -fsS -o /dev/null "$LOCAL_REGISTRY_URL" ||
            fail "cannot reach the registry at $LOCAL_REGISTRY_URL. $hint"
        echo "registry reachable: $LOCAL_REGISTRY_URL"
        curl -fsS -o /dev/null "$LOCAL_RELEASE_URL/" ||
            fail "cannot reach the release server at $LOCAL_RELEASE_URL. $hint"
        # Strip whitespace so a hand-edited or CRLF `latest` still pins cleanly.
        tag="$(curl -fsS "$LOCAL_RELEASE_URL/latest" 2>/dev/null | tr -d "[:space:]")"
        [ -n "$tag" ] ||
            fail "nothing published yet: $LOCAL_RELEASE_URL/latest is missing or empty. Run scripts/publish-local.sh first."
        echo "releases reachable: $LOCAL_RELEASE_URL (latest: $tag)"
        curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null
        export PATH="$HOME/.local/bin:$PATH"
        # Pin the version so a higher or stable version already in the registry
        # cannot stand in for the build under test.
        uv tool install --index "$LOCAL_REGISTRY_URL" "coding-harness-tracing==${tag#v}"
        command -v arize-config
    '
fi

tty_flag=""
[[ -t 0 && -t 1 ]] && tty_flag="-it"

# host-gateway makes host.docker.internal resolve on Linux too; Docker Desktop
# already provides it.
exec docker run --rm $tty_flag \
    --add-host=host.docker.internal:host-gateway \
    -e LOCAL_REGISTRY_URL="http://host.docker.internal:$REGISTRY_PORT/simple/" \
    -e LOCAL_RELEASE_URL="http://host.docker.internal:$RELEASE_PORT" \
    "$image" sh -c "$prep && exec \"\$@\"" sh "$@"
