# Shared by the build scripts in build/ (sourced, not run).
# Expects HERE (build/), BENCH (benchmark/), REPO, LABEL, DEST (the directory or file the build produces).

# Every build comes from this interpreter, named exactly: uv's python-build-standalone 3.12.10 for
# each architecture. --managed-python everywhere, so uv never settles for another 3.12.10 it finds
# (python.org's OpenSSL only looks for certificates inside Python.framework; see README.md).
PY_VERSION=3.12.10
PY_ARM64=cpython-$PY_VERSION-macos-aarch64-none
PY_X86_64=cpython-$PY_VERSION-macos-x86_64-none
REQUIREMENTS=$BENCH/requirements-build.txt
WORKBENCH=$REPO/.workbench/$LABEL   # build/, spec files, reports: never the repo root
INPUTS=$HERE/inputs.py

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

clean_dest() {  # earlier builds are read-only
  [ -e "$1" ] && chmod -R u+w "$1" && rm -rf "$1"
  mkdir -p "$(dirname "$1")" "$WORKBENCH"
}

finish() {  # executable
  local exe=$1
  echo "-- smoke test: $exe"
  if ! "$PY" -I "$INPUTS" check "$exe"; then
    echo "BUILD FAILED SMOKE TEST: $LABEL (output left writable in $DEST)" >&2
    exit 1
  fi
  chmod -R a-w "$DEST"  # like a root-owned MDM install
  mkdir -p "$BENCH/results/$LABEL"
  echo "$SECONDS" > "$BENCH/results/$LABEL/build_seconds"
  echo "built $LABEL in ${SECONDS}s, read-only: $exe"
}
