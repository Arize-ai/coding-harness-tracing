#!/usr/bin/env bash
# Install smoke test: build a wheel from this checkout, install it with
# install.sh --wheel-dir --non-interactive into a throwaway HOME, then check
# status, update and uninstall. CI's unix-offline and linux-distro-offline jobs
# run this script.
#
#   scripts/smoke-install.sh [harness]      harness defaults to claude
#
#   UV_PYTHON=3.9   build and install with this Python, as CI does
#   SMOKE_KEEP=1    skip the uninstall and keep the throwaway HOME
#   SETUPTOOLS_SCM_PRETEND_VERSION=X   build as version X instead of the
#                   checkout's git version
#
# Never writes to your ~/.claude, ~/.arize or the checkout. See CONTRIBUTING.md.
set -euo pipefail

HARNESS="${1:-claude}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
KEEP="${SMOKE_KEEP:-}"

# With SMOKE_KEEP=1, print where the install is and how to inspect it.
kept() {
    [[ -n "${work:-}" ]] || return 0
    local inst="$work/home/.arize/harness/install.sh"
    if [[ -x "$inst" ]]; then
        echo "  HOME=$work/home"
        echo "  cd \"$work\" && HOME=\"$work/home\" \"$inst\" status"
    else
        echo "  $work (nothing installed yet)"
    fi
    echo "  rm -rf \"$work\""
}
die() {
    echo "smoke-install: FAIL: $*" >&2
    if [[ "$KEEP" == 1 && -n "${work:-}" ]]; then
        echo "smoke-install: kept for inspection" >&2
        kept >&2
    fi
    exit 1
}
step() { echo; echo "smoke-install: $*"; }

command -v uv >/dev/null || die "uv not found. Install it: https://docs.astral.sh/uv/"

# The build copy below has no .git, so compute the version from the checkout
# and hand it to the build. Done before the throwaway HOME and PIP_CONFIG_FILE,
# so a pip mirror still works. test-in-container.sh passes it in already.
if [[ -z "${SETUPTOOLS_SCM_PRETEND_VERSION:-}" ]]; then
    SETUPTOOLS_SCM_PRETEND_VERSION="$(cd "$ROOT" && uv tool run --quiet --from 'setuptools-scm>=8,<9' python -m setuptools_scm)" ||
        die "could not compute the version from $ROOT"
else
    # core/_version.py keeps an override exactly as given, while the wheel's
    # metadata is normalized, so accept only the normalized form.
    normalized="$(uv tool run --quiet --from 'setuptools-scm>=8,<9' python -c \
        'import sys; from packaging.version import Version; print(Version(sys.argv[1]))' \
        "$SETUPTOOLS_SCM_PRETEND_VERSION")" ||
        die "could not check SETUPTOOLS_SCM_PRETEND_VERSION=$SETUPTOOLS_SCM_PRETEND_VERSION (not a PEP 440 version?)"
    [[ "$normalized" == "$SETUPTOOLS_SCM_PRETEND_VERSION" ]] ||
        die "set SETUPTOOLS_SCM_PRETEND_VERSION=$normalized, the normalized form of $SETUPTOOLS_SCM_PRETEND_VERSION"
fi
export SETUPTOOLS_SCM_PRETEND_VERSION
echo "smoke-install: building version $SETUPTOOLS_SCM_PRETEND_VERSION"

work="$(mktemp -d "${TMPDIR:-/tmp}/smoke-install.XXXXXX")"
work="$(cd "$work" && pwd -P)"
[[ "$KEEP" == 1 ]] || trap 'rm -rf "$work"' EXIT
# copilot's installer writes .github/hooks/ under the cwd; stay out of the repo.
cd "$work"

# install.sh uses the first python3 on PATH, so put UV_PYTHON's there.
if [[ -n "${UV_PYTHON:-}" ]]; then
    py="$(uv python find "$UV_PYTHON" 2>/dev/null)" ||
        { uv python install "$UV_PYTHON" && py="$(uv python find "$UV_PYTHON")"; } ||
        die "no Python matching UV_PYTHON=$UV_PYTHON"
    mkdir -p "$work/bin"
    ln -s "$py" "$work/bin/python3"
    export PATH="$work/bin:$PATH"
else
    py="$(command -v python3)" || die "python3 not found"
fi
echo "smoke-install: $HARNESS with $("$py" --version 2>&1) ($py)"

step "building the wheel"
# Build from a copy, so build/ and *.egg-info never land in the checkout.
# Root-level dirs are filtered here rather than with tar --exclude, which on
# macOS would also drop nested dirs with the same names.
mkdir -p "$work/src" "$work/wheels"
top=()
shopt -s dotglob nullglob
for path in "$ROOT"/*; do
    case "${path##*/}" in .git | .venv | venv | build | dist) ;; *) top+=("${path##*/}") ;; esac
done
shopt -u dotglob nullglob
tar -C "$ROOT" -cf - --exclude='*.egg-info' --exclude=__pycache__ --exclude=node_modules "${top[@]}" |
    tar -C "$work/src" -xf -
uv build --quiet --wheel --out-dir "$work/wheels" "$work/src" || die "uv build failed"
wheel=("$work/wheels"/*.whl)
[[ ${#wheel[@]} -eq 1 && -f "${wheel[0]}" ]] || die "expected exactly one wheel in $work/wheels"

# --wheel-dir installs with --no-index, so fetch the wheel's dependencies plus
# certifi (install.sh adds it on macOS, and only warns if missing). Uses an
# ephemeral pip, since a uv-created venv has none.
uv run --quiet --python "$py" --with pip --no-project --isolated \
    python -m pip download --quiet --disable-pip-version-check \
    --only-binary :all: -d "$work/wheels" "${wheel[0]}" certifi ||
    die "could not download the dependency wheels"
ls -1 "$work/wheels"

# Use the throwaway HOME, and drop settings that could load code or wheels from
# outside the wheel dir (PYTHONPATH, PIP_*, pip.conf) and hide a broken wheel.
# Done after the download, so a pip mirror still works there. UV_PYTHON stays.
export HOME="$work/home"
mkdir -p "$HOME"
unset CODEX_HOME DEVIN_PROJECT_DIR PYTHONPATH PYTHONHOME PYTHONSTARTUP PYTHONUSERBASE VIRTUAL_ENV
while IFS= read -r var; do unset "$var"; done < <(compgen -v | grep -E '^(ARIZE|PHOENIX|OTEL|PIP)_' || true)
export PIP_CONFIG_FILE=/dev/null

creds="$work/creds.env"
printf '%s\n' ARIZE_API_KEY=ci-not-a-real-key ARIZE_SPACE_ID=ci-space > "$creds"
installed="$HOME/.arize/harness"

step "installing with install.sh $HARNESS --wheel-dir --non-interactive"
ARIZE_ENV_FILE="$creds" "$ROOT/install.sh" "$HARNESS" --wheel-dir "$work/wheels" --non-interactive ||
    die "install.sh $HARNESS exited $?"

step "checking the install"
"$installed/install.sh" status --json > "$work/status.json" ||
    { rc=$?; cat "$work/status.json"; die "status --json exited $rc (1 = nothing configured, 2 = hooks missing)"; }
echo "status --json: healthy"

# Exit 0 only means the configured harnesses are healthy, so also check this one
# is listed with its hooks registered. claude's config key is claude-code.
config_key="$HARNESS"
[[ "$HARNESS" == claude ]] && config_key=claude-code
"$py" - "$work/status.json" "$config_key" <<'EOF' ||
import json, sys
status = json.load(open(sys.argv[1]))
sys.exit(not any(h["name"] == sys.argv[2] and h["registered"] is True for h in status["harnesses"]))
EOF
    { cat "$work/status.json"; die "status --json does not list $config_key with its hooks registered"; }
echo "$HARNESS is configured and its hooks are registered"

[[ ! -e "$installed/pyproject.toml" && ! -e "$installed/.git" ]] ||
    die "$installed holds a source tree. Wheel mode must not fetch the repo"
echo "no source tree in $installed"

# -I: read the venv's install only, never the cwd or PYTHON* settings.
reported="$("$installed/venv/bin/python" -I -c "import core; print(core.version())")" ||
    die "core.version() failed in the installed venv"
[[ "$reported" == "$SETUPTOOLS_SCM_PRETEND_VERSION" ]] ||
    die "installed package reports version $reported, but $SETUPTOOLS_SCM_PRETEND_VERSION was built"
echo "installed package reports version $reported, as built"

if [[ "$(uname)" == Darwin ]]; then
    "$installed/venv/bin/python" -c "import certifi" 2>/dev/null ||
        die "certifi is not in the venv, so hooks would fail SSL verification on macOS"
    echo "certifi installed for the macOS SSL fix"
fi

if "$installed/install.sh" update > "$work/update.log" 2>&1; then
    cat "$work/update.log"
    die "update exited 0. An offline install must refuse to update"
fi
grep -q "no source tree to update" "$work/update.log" ||
    { cat "$work/update.log"; die "update failed, but not with the offline-install refusal"; }
echo "update refused, as intended"

if [[ "$KEEP" == 1 ]]; then
    echo
    echo "smoke-install: OK ($HARNESS), kept for inspection (uninstall skipped)"
    kept
    exit 0
fi

step "uninstalling"
"$installed/install.sh" uninstall > "$work/uninstall.log" 2>&1 ||
    { rc=$?; cat "$work/uninstall.log"; die "uninstall exited $rc"; }
[[ ! -e "$installed/venv" ]] ||
    { cat "$work/uninstall.log"; die "$installed/venv survived the uninstall"; }
echo "uninstall removed the venv"

echo
echo "smoke-install: OK ($HARNESS)"
