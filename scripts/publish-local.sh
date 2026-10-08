#!/usr/bin/env bash
# Build the current checkout and publish it to the local registry and release
# folder from scripts/local-registry.sh. Republishing the same version
# overwrites it. See "Testing releases locally" in CONTRIBUTING.md.
set -euo pipefail

REGISTRY_PORT="${LOCAL_REGISTRY_PORT:-8080}"
RELEASE_DIR="${LOCAL_RELEASE_DIR:-$HOME/local-releases}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

die() { echo "publish-local: $*" >&2; exit 1; }
# Standard sha256sum output: "<hex>  <filename>". macOS has shasum instead.
sha256() { if command -v sha256sum >/dev/null; then sha256sum "$@"; else shasum -a 256 "$@"; fi; }

curl -fsS -o /dev/null "http://localhost:$REGISTRY_PORT/simple/" 2>/dev/null ||
    die "no registry on :$REGISTRY_PORT — start it with scripts/local-registry.sh"

dist="$(mktemp -d)"
trap 'rm -rf "$dist"' EXIT
uv build "$ROOT" --out-dir "$dist"

# Read the version off the built wheel rather than pyproject.toml, so this
# keeps working once the version is derived at build time.
wheels=("$dist"/*.whl)
[[ ${#wheels[@]} -eq 1 && -f "${wheels[0]}" ]] || die "expected exactly one wheel in $dist"
version="$(basename "${wheels[0]}" | cut -d- -f2)"
tag="v$version"

# pypiserver runs without auth; dummy credentials stop uv from prompting.
uv publish --publish-url "http://localhost:$REGISTRY_PORT/" \
    --username local --password local "$dist"/*

out="$RELEASE_DIR/releases/download/$tag"
mkdir -p "$out"
rm -f "$out"/*
cp "$dist"/* "$out"/
(cd "$out" && sha256 *.whl *.tar.gz > SHA256SUMS)
printf '%s\n' "$tag" > "$RELEASE_DIR/latest"

cat <<EOF

Published $tag
  registry  http://localhost:$REGISTRY_PORT/simple/coding-harness-tracing/
  release   $out

Install exactly this build into a throwaway HOME (--reinstall bypasses uv's
cache after republishing the same version):
  T=\$(mktemp -d)
  HOME=\$T UV_TOOL_DIR=\$T/tools UV_TOOL_BIN_DIR=\$T/bin uv tool install --reinstall \\
    --index http://localhost:$REGISTRY_PORT/simple/ coding-harness-tracing==$version
EOF
