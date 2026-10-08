#!/usr/bin/env bash
# Start a local package registry and release server for rehearsing releases.
#
#   registry  pypiserver on :8080 serving $LOCAL_PYPI_DIR. No login, overwrite
#             allowed — local only; real PyPI never allows overwriting.
#   releases  static server on :8000 serving $LOCAL_RELEASE_DIR, laid out like
#             GitHub Releases: releases/download/<tag>/... plus a `latest` file.
#             Installers that download release files can use it as their base URL.
#
# Both run in the foreground; Ctrl-C stops both. Docker is not needed.
# See "Testing releases locally" in CONTRIBUTING.md.
set -euo pipefail

PYPI_DIR="${LOCAL_PYPI_DIR:-$HOME/local-pypi}"
RELEASE_DIR="${LOCAL_RELEASE_DIR:-$HOME/local-releases}"
REGISTRY_PORT="${LOCAL_REGISTRY_PORT:-8080}"
RELEASE_PORT="${LOCAL_RELEASE_PORT:-8000}"
# Loopback by default: an unauthenticated server that accepts overwrites should
# not be reachable from the network. Docker Desktop still reaches it through
# host.docker.internal. On a Linux Docker host, set LOCAL_BIND=0.0.0.0.
BIND="${LOCAL_BIND:-127.0.0.1}"

die() { echo "local-registry: $*" >&2; exit 1; }
port_in_use() { (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null; }

command -v uvx >/dev/null || die "uvx not found; install uv: https://docs.astral.sh/uv/"
command -v python3 >/dev/null || die "python3 not found"
for port in "$REGISTRY_PORT" "$RELEASE_PORT"; do
    port_in_use "$port" && die "port $port is already in use"
done
mkdir -p "$PYPI_DIR" "$RELEASE_DIR"

registry_pid="" release_pid=""
cleanup() {
    kill $registry_pid $release_pid 2>/dev/null || true
    wait 2>/dev/null || true
}
trap cleanup EXIT
trap 'exit 130' INT TERM

uvx --from pypiserver pypi-server run \
    -i "$BIND" -p "$REGISTRY_PORT" -a . -P . --overwrite "$PYPI_DIR" &
registry_pid=$!
python3 -m http.server "$RELEASE_PORT" --bind "$BIND" --directory "$RELEASE_DIR" &
release_pid=$!

# The first run downloads pypiserver; wait until both answer before announcing.
for _ in $(seq 60); do
    curl -fsS -o /dev/null "http://localhost:$REGISTRY_PORT/simple/" 2>/dev/null &&
        curl -fsS -o /dev/null "http://localhost:$RELEASE_PORT/" 2>/dev/null && break
    sleep 1
done

cat <<EOF

  registry  http://localhost:$REGISTRY_PORT/simple/   ($PYPI_DIR)
  releases  http://localhost:$RELEASE_PORT/           ($RELEASE_DIR)

  Publish with scripts/publish-local.sh. Ctrl-C stops both.

EOF

# macOS ships bash 3.2, which has no `wait -n`; poll so either exiting stops both.
while kill -0 "$registry_pid" 2>/dev/null && kill -0 "$release_pid" 2>/dev/null; do
    sleep 1
done
die "a server exited unexpectedly"
