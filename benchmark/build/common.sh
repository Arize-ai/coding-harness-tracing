# Shared by the build scripts in build/ (sourced, not run).
# Expects HERE (build/), BENCH (benchmark/), REPO, LABEL, DEST (the directory or file the build produces).

# Every build comes from this interpreter, named exactly: uv's python-build-standalone 3.12.10 for
# each architecture. --managed-python everywhere, so uv never settles for another 3.12.10 it finds
# (python.org's OpenSSL only looks for certificates inside Python.framework; see README.md).
PY_VERSION=3.12.10
PY_ARM64=cpython-$PY_VERSION-macos-aarch64-none
PY_X86_64=cpython-$PY_VERSION-macos-x86_64-none
# uv downloads the interpreter and the build tools. Before 0.13.4 its bundled root certificates still
# trust the distrusted eMudhra/emSign CAs (#175), so an older uv stops the build.
UV_MIN=0.13.4
# uv also picks the python-build-standalone release behind PY_VERSION, so another uv can mean another
# interpreter build (and another minos, which universal.sh checks). Warn, don't stop: see README.md.
UV_VERSION=0.13.5
uv_have=$(uv --version | awk '{print $2}')
version_lt() {  # a b: true if version a is older than b, comparing dot-separated numbers
  awk -v a="$1" -v b="$2" 'BEGIN {
    n = split(a, x, "."); m = split(b, y, ".")
    for (i = 1; i <= (n > m ? n : m); i++) { if (x[i] + 0 < y[i] + 0) exit 0; if (x[i] + 0 > y[i] + 0) exit 1 }
    exit 1
  }'
}
if version_lt "$uv_have" "$UV_MIN"; then
  echo "BUILD STOPPED: uv $uv_have is older than $UV_MIN, whose bundled root certificates no longer trust" \
    "the distrusted eMudhra/emSign CAs (see README.md); update uv" >&2
  exit 1
fi
[ "$uv_have" = "$UV_VERSION" ] || echo "warning: uv $uv_have, the PR's numbers were taken with uv $UV_VERSION" >&2
REQUIREMENTS=$BENCH/requirements-build.txt
WORKBENCH=$REPO/.workbench/$LABEL   # build/, spec files, reports: never the repo root
INPUTS=$HERE/inputs.py
unset INPUT_DATA BUILD_INFO  # set by the build scripts for finish(); never taken from the environment

# Stop unless <python> is the pinned interpreter for <arch> and its OpenSSL reads the system CA file.
# A build inherits its interpreter's CA path, so a wrong one only shows up as failed HTTPS sends.
check_python() {  # python, arch
  "$1" -I - "$PY_VERSION" "$2" <<'PY' || { echo "BUILD STOPPED: $1 is not the pinned build interpreter" >&2; exit 1; }
import os, platform, ssl, sys
want, arch = sys.argv[1:]
bad = []
if platform.python_version() != want:
    bad.append(f"version {platform.python_version()}, want {want}")
if platform.machine() != arch:
    bad.append(f"arch {platform.machine()}, want {arch}")
cafile = ssl.get_default_verify_paths().openssl_cafile
if os.path.realpath(cafile) != os.path.realpath("/etc/ssl/cert.pem"):  # uv's reports /private/etc/...
    bad.append(f"openssl_cafile {cafile}, want /etc/ssl/cert.pem")
for b in bad:
    print(f"check_python: {sys.executable}: {b}", file=sys.stderr)
sys.exit(1 if bad else 0)
PY
}

# Install the pinned interpreter (never linked into ~/.local/bin) and print its uv name.
pinned_python() {  # arch
  local name
  case $1 in
    arm64) name=$PY_ARM64 ;;
    x86_64) name=$PY_X86_64 ;;
    *) echo "unknown arch: $1 (arm64 or x86_64)" >&2; exit 1 ;;
  esac
  uv python install -q --managed-python --no-bin "$name" >&2
  echo "$name"
}

# The build tools for <arch> (requirements-build.txt plus this checkout) in .workbench/bt-<arch>;
# sets BT and PY. BUILDTOOLS=<venv> uses that venv as is, but it still has to pass check_python.
# The checkout is reinstalled every time: uv would otherwise reuse a cached build of it.
buildtools() {  # arch
  local name
  BT=${BUILDTOOLS:-$REPO/.workbench/bt-$1}
  PY=$BT/bin/python
  if [ -z "${BUILDTOOLS:-}" ]; then
    name=$(pinned_python "$1")
    [ -x "$PY" ] || uv venv -q --managed-python --no-config -p "$name" "$BT"
    uv pip install -q --python "$PY" -r "$REQUIREMENTS" \
      --reinstall-package coding-harness-tracing --refresh-package coding-harness-tracing "$REPO"
  fi
  check_python "$PY" "$1"
}

# Build-time lists, generated from the code (see inputs.py). -I: never pick up a
# stray core/ from the cwd. The data listing reports missing paths on stderr itself.
inputs() { "$PY" -I "$INPUTS" "$@"; }

# What this build is made from (inputs.py info), read before building so an edit made during the
# build can't be credited to it. finish() writes it next to build_seconds; universal.sh compares it.
read_build_info() {
  BUILD_INFO=$(inputs info "$uv_have") || { echo "BUILD STOPPED: inputs.py info failed" >&2; exit 1; }
}
build_info_file() { echo "$BENCH/results/$LABEL/build_info.json"; }

clean_dest() {  # earlier builds are read-only
  [ -e "$1" ] && chmod -R u+w "$1" && rm -rf "$1"
  # Only a build that passes finish() has a build_info.json, so universal.sh never merges a failed one.
  rm -f "$(build_info_file)"
  mkdir -p "$(dirname "$1")" "$WORKBENCH"
}

finish() {  # executable
  local exe=$1
  echo "-- smoke test: $exe"
  if ! "$PY" -I "$INPUTS" check "$exe"; then
    echo "BUILD FAILED SMOKE TEST: $LABEL (output left writable in $DEST)" >&2
    exit 1
  fi
  # The smoke test runs hooks, not setup, so it can't see a missing or wrong data file: compare
  # each one with its source. nuitka.sh sets INPUT_DATA (possibly empty); universal.sh doesn't.
  local data src dest
  if [ -n "${INPUT_DATA+set}" ]; then
    data=$INPUT_DATA
  else
    data=$(inputs data) || { echo "BUILD FAILED: inputs.py data failed (output left writable in $DEST)" >&2; exit 1; }
  fi
  while IFS=$'\t' read -r src dest; do
    if [ -n "$dest" ] && ! cmp -s "$src" "$(dirname "$exe")/$dest"; then
      echo "BUILD FAILED: data file missing or different in bundle: $dest (output left writable in $DEST)" >&2
      exit 1
    fi
  done <<<"$data"
  chmod -R a-w "$DEST"  # like a root-owned MDM install
  mkdir -p "$BENCH/results/$LABEL"
  echo "$SECONDS" > "$BENCH/results/$LABEL/build_seconds"
  echo "$BUILD_INFO" > "$(build_info_file)"
  echo "built $LABEL in ${SECONDS}s, read-only: $exe"
}
