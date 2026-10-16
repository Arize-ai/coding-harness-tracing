#!/usr/bin/env bash
# Install smoke test: build a wheel from this checkout, install it with the real
# user installer (install.sh --wheel-dir --non-interactive) into a throwaway
# HOME, then check status, update and uninstall. The Linux and macOS
# counterpart of the windows-offline job in .github/workflows/ci.yml. CI's
# unix-offline and linux-distro-offline jobs run this same script.
#
#   scripts/smoke-install.sh [harness]      harness defaults to claude
#
#   UV_PYTHON=3.9   build and install with this Python, as CI does
#   SMOKE_KEEP=1    skip the uninstall and keep the throwaway HOME to inspect
#
# Your real ~/.claude, ~/.arize and the checkout itself are never written to.
# See "Testing an install locally" in CONTRIBUTING.md.
set -euo pipefail

HARNESS="${1:-claude}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
KEEP="${SMOKE_KEEP:-}"

# With SMOKE_KEEP=1, pass or fail, say where the install is and how to look at
# it. The HOME it names is the throwaway one, not the shell's. A run that fails
# before install.sh has nothing to run status against, so only the work dir.
kept() {
    [[ -n "${work:-}" ]] || return 0
    local inst="$work/home/.arize/harness/install.sh"
    if [[ -x "$inst" ]]; then
        echo "  HOME=$work/home"
        echo "  HOME=\"$work/home\" \"$inst\" status"
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

work="$(mktemp -d "${TMPDIR:-/tmp}/smoke-install.XXXXXX")"
work="$(cd "$work" && pwd -P)"
[[ "$KEEP" == 1 ]] || trap 'rm -rf "$work"' EXIT
# Run everything from the work dir. tracing/copilot/install.py writes
# .github/hooks/hooks.json under the cwd, and from the repo root that would
# land in this repo's .github/hooks/.
cd "$work"

# install.sh takes the first python3 on PATH, so put the chosen interpreter
# there. Otherwise UV_PYTHON would reach the build but not the installed venv.
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
# setuptools leaves build/ and *.egg-info next to pyproject.toml. They are
# gitignored, so git status would not show them, so build a copy instead of the
# checkout. tar keeps the tracing/*/core symlinks as symlinks.
# .git, venvs, build and dist are skipped at the checkout root only, so a
# nested directory that happens to share one of those names still reaches the
# wheel. That is done by listing the root here: macOS tar ignores a ./ anchor
# on --exclude and would drop the nested ones too.
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

# install.sh --wheel-dir installs with --no-index, so every dependency has to be
# in the folder. Resolve them from the built wheel, so a dependency added to
# pyproject.toml is picked up here too. certifi is not a package dependency, but
# install.sh installs it for the macOS SSL fix; without it the install only warns,
# so CI would never notice.
# A uv-created venv has no pip, and python3 is one whenever .venv is active or
# this script runs under `uv run`. Download through an ephemeral pip instead.
uv run --quiet --python "$py" --with pip --no-project --isolated \
    python -m pip download --quiet --disable-pip-version-check \
    --only-binary :all: -d "$work/wheels" "${wheel[0]}" certifi ||
    die "could not download the dependency wheels"
ls -1 "$work/wheels"

# Every per-user path now points at the throwaway HOME. Also drop settings that
# would send the installer elsewhere or change what it does. The PYTHON* and
# PIP_* settings would let the installed venv load code or packages from outside
# the wheel, for example the checkout on PYTHONPATH, and hide a module the wheel
# is missing. UV_PYTHON stays: it picks the interpreter on purpose.
# PIP_CONFIG_FILE=/dev/null also stops pip reading the global and site config
# files, such as /etc/pip.conf, whose find-links could supply a wheel missing
# from the wheel dir. Set after the download, so a configured mirror still works
# there.
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

# Exit 0 only says every configured harness is wired up. Check that the one
# under test is among them, with its hooks confirmed rather than unknown. The
# config key is the CLI name for every harness except claude (see harness_dir
# in install.sh).
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
