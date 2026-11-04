#!/bin/bash
# Arize Coding Harness Tracing — Thin shell router
#
# Handles Python discovery, repo clone/tarball, venv creation, and pip install.
# All harness-specific logic lives in tracing/<harness>/install.py.
#
# Usage:
#   curl -sSL .../install.sh | bash -s -- claude [--with-skills] [--branch NAME]
#   ./install.sh uninstall [<harness>]
#   ./install.sh update

set -euo pipefail

REPO_URL="https://github.com/Arize-ai/coding-harness-tracing.git"
INSTALL_BRANCH="${ARIZE_INSTALL_BRANCH:-main}"
TARBALL_URL="https://github.com/Arize-ai/coding-harness-tracing/archive/refs/heads/${INSTALL_BRANCH}.tar.gz"
INSTALL_DIR="${HOME}/.arize/harness"
VENV_DIR="${INSTALL_DIR}/venv"
PIP_VERSION="26.2.2"
PIP_WHEEL="pip-${PIP_VERSION}-py3-none-any.whl"
PIP_SHA256="fd7f4dfa8190e014013b9f08f3e973f4e4c26ffd179a204895533a7254813a70"
PIP_WHEEL_URL="https://files.pythonhosted.org/packages/3f/06/3e4420302363fbb71be3c7f35ce042f54bfca330e3e66b3a2e3c4cca579f/${PIP_WHEEL}#sha256=${PIP_SHA256}"
CERTIFI_VERSION="2026.10.23"
CERTIFI_WHEEL="certifi-${CERTIFI_VERSION}-py3-none-any.whl"
CERTIFI_SHA256="e9b54de76e530bb6cf0ccd036787103acb58fd42ba930ea8bf948ddbaa2f61d1"
CERTIFI_WHEEL_URL="https://files.pythonhosted.org/packages/32/d1/a3652fe2c19134d0d15922177821d863cbd7e842d53a21bbe7e8e3a1b5ac/${CERTIFI_WHEEL}#sha256=${CERTIFI_SHA256}"
# When set, install from local wheels in this directory instead of fetching the
# repo. Lets a caller that already ships the wheels install with no network at
# all — and with no remote code execution for a permission layer to object to.
WHEEL_DIR="${ARIZE_WHEEL_DIR:-}"

# -- Terminal helpers --------------------------------------------------------
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; BOLD='\033[1m'; NC='\033[0m'
[[ -n "${NO_COLOR:-}" ]] || [[ ! -t 1 ]] && { RED=""; GREEN=""; YELLOW=""; BLUE=""; BOLD=""; NC=""; }

info()   { echo -e "${GREEN}[arize]${NC} $*"; }
warn()   { echo -e "${YELLOW}[arize]${NC} $*"; }
err()    { echo -e "${RED}[arize]${NC} $*" >&2; }
header() { echo -e "\n${BOLD}${BLUE}$*${NC}\n"; }
command_exists() { command -v "$1" &>/dev/null; }

# TTY input for curl|bash scenarios
_tty_in=""
if [[ -t 0 ]]; then _tty_in="/dev/stdin"
elif (exec 3< /dev/tty) 2>/dev/null; then exec 3<&-; _tty_in="/dev/tty"; fi

# Run a command with stdin wired to the user's TTY when possible.
# Under `curl | bash`, our own stdin is the pipe — not a terminal — so any
# subprocess that calls input() (e.g. tracing/<harness>/install.py) would hit
# EOFError on the very first prompt. Redirecting from _tty_in lets Python read
# from the actual terminal. No-op in non-interactive environments without a TTY.
run_with_tty() {
    if [[ -n "$_tty_in" ]]; then
        "$@" < "$_tty_in"
    else
        "$@"
    fi
}

# -- Python discovery --------------------------------------------------------
find_python() {
    local candidates=(python3 python /usr/bin/python3 /usr/local/bin/python3 "$HOME/.local/bin/python3")
    [[ -d "$HOME/.pyenv/shims" ]] && candidates+=("$HOME/.pyenv/shims/python3")
    [[ -x "/opt/homebrew/bin/python3" ]] && candidates+=("/opt/homebrew/bin/python3")
    local conda_base
    conda_base=$(conda info --base 2>/dev/null) && [[ -n "$conda_base" ]] && candidates+=("${conda_base}/bin/python3")
    for p in "${candidates[@]}"; do
        local resolved
        if [[ "$p" == /* ]]; then resolved="$p"
        else resolved=$(command -v "$p" 2>/dev/null || true); fi
        [[ -z "$resolved" || ! -f "$resolved" ]] && continue
        "$resolved" -c "import sys; assert sys.version_info >= (3, 11)" 2>/dev/null && { echo "$resolved"; return 0; }
    done
    return 1
}

# -- Venv helpers ------------------------------------------------------------
venv_python() {
    [[ -x "${VENV_DIR}/bin/python" ]] && { echo "${VENV_DIR}/bin/python"; return; }
    [[ -x "${VENV_DIR}/Scripts/python.exe" ]] && { echo "${VENV_DIR}/Scripts/python.exe"; return; }
    return 1
}
venv_pip() {
    [[ -x "${VENV_DIR}/bin/pip" ]] && { echo "${VENV_DIR}/bin/pip"; return; }
    [[ -x "${VENV_DIR}/Scripts/pip.exe" ]] && { echo "${VENV_DIR}/Scripts/pip.exe"; return; }
    return 1
}

# Hash-check a wheel in WHEEL_DIR before anything is installed from it.
verify_wheel() {
    local vp="$1" name="$2" expected="$3" wheel="${WHEEL_DIR}/$2"
    [[ -f "$wheel" ]] || { err "Missing ${name} in ${WHEEL_DIR}"; return 1; }
    "$vp" - "$wheel" "$expected" <<'PYEOF' || { err "${name} failed SHA-256 verification"; return 1; }
import hashlib
import pathlib
import sys

wheel = pathlib.Path(sys.argv[1])
expected = sys.argv[2]
sys.exit(hashlib.sha256(wheel.read_bytes()).hexdigest() != expected)
PYEOF
}
verify_pip_wheel()     { verify_wheel "$1" "$PIP_WHEEL" "$PIP_SHA256"; }
verify_certifi_wheel() { verify_wheel "$1" "$CERTIFI_WHEEL" "$CERTIFI_SHA256"; }

# Every download trusts the OS store, never a bundle named by the environment
# (an old install's sitecustomize.py pointed these at the pre-distrust bundle).
# Wraps pip, curl, wget and git alike.
without_ca_overrides() { env -u SSL_CERT_FILE -u REQUESTS_CA_BUNDLE -u CURL_CA_BUNDLE -u PIP_CERT -u GIT_SSL_CAINFO "$@"; }

# PIP_CONFIG_FILE=/dev/null drops global and venv config, which --isolated keeps.
bootstrap_pip() {
    local vp
    vp=$(venv_python) || { err "Venv Python not found"; return 1; }
    info "Securing pip ${PIP_VERSION}..."
    local source="$PIP_WHEEL_URL"
    if [[ -n "$WHEEL_DIR" ]]; then
        verify_pip_wheel "$vp" || return 1
        source="${WHEEL_DIR}/${PIP_WHEEL}"
    fi
    without_ca_overrides PIP_CONFIG_FILE=/dev/null "$vp" -m pip install --isolated --quiet --disable-pip-version-check --no-deps --upgrade "$source" || {
        err "Failed to install verified pip ${PIP_VERSION}"
        return 1
    }
}

# -- Repository download ----------------------------------------------------
git_sync_harness_repo() {
    local branch="$1"
    [[ -d "${INSTALL_DIR}/.git" ]] || return 1
    info "Syncing with origin/${branch}..."
    without_ca_overrides git -C "$INSTALL_DIR" fetch --depth 1 origin "$branch" 2>/dev/null \
        && git -C "$INSTALL_DIR" checkout -B "$branch" FETCH_HEAD 2>/dev/null && return 0
    without_ca_overrides git -C "$INSTALL_DIR" fetch origin "$branch" 2>/dev/null \
        && git -C "$INSTALL_DIR" checkout -B "$branch" FETCH_HEAD 2>/dev/null && return 0
    warn "git fetch/checkout failed — trying pull --ff-only"
    without_ca_overrides git -C "$INSTALL_DIR" pull --ff-only origin "$branch" 2>/dev/null && return 0
    without_ca_overrides git -C "$INSTALL_DIR" pull --ff-only 2>/dev/null && return 0
    return 1
}

install_repo_tarball() {
    local tarball_url="${1:-$TARBALL_URL}"
    info "Downloading coding-harness-tracing tarball..."
    local tmp_tar; tmp_tar="$(mktemp)"
    if command_exists curl; then without_ca_overrides curl -sSfL "$tarball_url" -o "$tmp_tar"
    elif command_exists wget; then without_ca_overrides wget -qO "$tmp_tar" "$tarball_url"
    else rm -f "$tmp_tar"; err "Neither curl nor wget found — cannot download"; exit 1; fi
    mkdir -p "$INSTALL_DIR"
    tar xzf "$tmp_tar" --strip-components=1 -C "$INSTALL_DIR"
    rm -f "$tmp_tar"
    info "Extracted to ${INSTALL_DIR}"
}

install_repo() {
    # Wheel mode fetches nothing. The wheel carries every module the harness
    # needs, so there is no source tree to place — but install.sh itself has to
    # land in INSTALL_DIR, because `status`, `update` and `uninstall` are all
    # documented as running from there and repo mode gets it via the extract.
    if [[ -n "$WHEEL_DIR" ]]; then
        mkdir -p "$INSTALL_DIR"
        # -ef, not cmp: cmp comes from diffutils, which minimal images lack.
        if [[ -f "${BASH_SOURCE[0]}" && ! "${BASH_SOURCE[0]}" -ef "${INSTALL_DIR}/install.sh" ]]; then
            cp "${BASH_SOURCE[0]}" "${INSTALL_DIR}/install.sh" && chmod +x "${INSTALL_DIR}/install.sh"
        fi
        return 0
    fi
    git_sync_harness_repo "$INSTALL_BRANCH" && return 0
    install_repo_tarball
}

# Invoke a harness's install.py. Repo mode runs the file from the source tree;
# wheel mode has no source tree, so it runs the same code as a module. Both
# resolve `core.*` from site-packages either way — the package is pip-installed,
# never on sys.path by accident — so these are equivalent, not a fallback.
run_harness_py() {
    local key="$1" vp="$2"; shift 2
    local dir; dir=$(harness_dir "$key") || return 1
    if [[ -f "${INSTALL_DIR}/${dir}/install.py" ]]; then
        run_with_tty "$vp" "${INSTALL_DIR}/${dir}/install.py" "$@"
    else
        run_with_tty "$vp" -m "${dir//\//.}.install" "$@"
    fi
}

# -- Venv setup --------------------------------------------------------------

# Fix SSL certificate verification on macOS.
#
# Python.org installers ship their own OpenSSL that doesn't trust the macOS
# system keychain, so urllib (used by every arize-hook-*) fails with
# "CERTIFICATE_VERIFY_FAILED" against https://otlp.arize.com.
#
# Fix: install the pinned certifi into the venv and write a sitecustomize.py that sets
# SSL_CERT_FILE before any hook code runs. Every step is fatal: a venv whose
# hooks cannot verify TLS is not an install worth keeping.
_fix_macos_ssl_certs() {
    local pip="$1"
    local vp
    vp=$(venv_python) || { err "Venv Python not found"; return 1; }

    local source="$CERTIFI_WHEEL_URL"
    if [[ -n "$WHEEL_DIR" ]]; then
        verify_certifi_wheel "$vp" || return 1
        source="${WHEEL_DIR}/${CERTIFI_WHEEL}"
    fi
    if ! without_ca_overrides "$pip" install --quiet --no-deps --upgrade "$source"; then
        err "Could not install ${CERTIFI_WHEEL}"
        [[ -n "$WHEEL_DIR" ]] && err "Bundle it in ${WHEEL_DIR}."
        return 1
    fi

    local site_dir sc
    "$vp" -c "import certifi; certifi.where()" 2>/dev/null || { err "certifi did not install into the venv"; return 1; }
    site_dir=$("$vp" -c "import site; print(site.getsitepackages()[0])" 2>/dev/null) || { err "Venv site-packages not found"; return 1; }
    sc="${site_dir}/sitecustomize.py"

    cat > "$sc" <<'PYEOF'
# Arize Coding Harness Tracing: point Python's SSL stack at certifi's CA bundle on macOS.
# This runs automatically at interpreter startup, before any hook code.
import os as _os
try:
    import certifi as _certifi
    _bundle = _certifi.where()
    _os.environ.setdefault("SSL_CERT_FILE", _bundle)
    _os.environ.setdefault("REQUESTS_CA_BUNDLE", _bundle)
except ImportError:
    pass
PYEOF
    info "SSL certificates configured via certifi"
}

# Install the package into the venv. Extra args go to pip (`-U` for update).
# Shared so install and update cannot drift: they were the same wheel/repo branch
# twice, differing only by -U, and a flag added to one would have missed the other.
pip_install_harness() {
    local pip="$1"; shift
    if [[ -n "$WHEEL_DIR" ]]; then
        # --no-index so a missing wheel fails loudly instead of quietly reaching
        # PyPI, which would defeat the point of installing offline.
        without_ca_overrides "$pip" install --quiet "$@" --no-index --find-links "$WHEEL_DIR" coding-harness-tracing \
            || { err "Failed to install coding-harness-tracing from ${WHEEL_DIR}"; return 1; }
    else
        without_ca_overrides "$pip" install --quiet "$@" "$INSTALL_DIR" 2>/dev/null \
            || { err "Failed to install coding-harness-tracing package"; return 1; }
    fi
}

# Build a fresh venv at VENV_DIR. Extra args go to pip (`-U` for update).
build_venv() {
    local python_cmd="$1"; shift
    info "Creating venv..."
    "$python_cmd" -m venv "$VENV_DIR" 2>/dev/null || {
        err "Failed to create venv with $python_cmd"
        err "You may need to install the venv module: apt install python3-venv (Debian/Ubuntu)"
        return 1
    }
    bootstrap_pip || return 1
    local pip; pip=$(venv_pip) || { err "pip not found in venv"; return 1; }
    if [[ "$(uname)" == "Darwin" ]]; then _fix_macos_ssl_certs "$pip" || return 1; fi
    info "Installing coding-harness-tracing into venv..."
    pip_install_harness "$pip" "$@" || return 1
    local vp; vp=$(venv_python) || return 1
    "$vp" -c "import core" 2>/dev/null || { err "Installed package does not import"; return 1; }
}

# The previous venv is set aside, never deleted, until the new one is built and
# imports. A failed download or wheelhouse puts it straight back, so hooks keep
# their runtime. Always rebuilt: a venv is never upgraded in place, so nothing
# from an older install (pip, certifi, sitecustomize.py) can survive.
setup_venv() {
    local previous="${VENV_DIR}.previous"
    rm -rf "$previous"
    [[ -e "$VENV_DIR" ]] && mv "$VENV_DIR" "$previous"
    if build_venv "$@"; then
        rm -rf "$previous"
        info "Venv ready at ${VENV_DIR}"
        return 0
    fi
    rm -rf "$VENV_DIR"
    [[ -e "$previous" ]] && { mv "$previous" "$VENV_DIR"; err "Previous venv restored unchanged."; }
    return 1
}

# -- Harness name mapping ----------------------------------------------------
#
# Accepts both the CLI name and the config key. They are the same for every
# harness except Claude Code, which writes HARNESS_NAME "claude-code" while its
# CLI name is "claude". `update` and full `uninstall` discover harnesses via
# list_installed_harnesses(), which yields *config keys*, so without the alias
# both skipped Claude Code entirely — a full uninstall wiped the venv and left
# its hooks in ~/.claude/settings.json pointing at the deleted path.
# install.bat has accepted both spellings all along.
harness_dir() {
    case "$1" in
        claude|claude-code)  echo "tracing/claude_code" ;;
        codex)   echo "tracing/codex" ;;
        copilot) echo "tracing/copilot" ;;
        cursor)  echo "tracing/cursor" ;;
        gemini)  echo "tracing/gemini" ;;
        kiro)    echo "tracing/kiro" ;;
        antigravity) echo "tracing/antigravity" ;;
        opencode) echo "tracing/opencode" ;;
        omp)     echo "tracing/omp" ;;
        devin)   echo "tracing/devin" ;;
        *)       return 1 ;;
    esac
}

install_harness() {
    local cmd="$1" skills="$2"
    harness_dir "$cmd" >/dev/null || { err "Unknown harness: ${cmd}"; usage; exit 1; }
    header "Installing ${cmd} tracing"
    local python_cmd; python_cmd=$(find_python) || { err "No Python 3.11+ found"; exit 1; }
    info "Found Python: ${python_cmd} ($("$python_cmd" --version 2>&1))"
    install_repo
    setup_venv "$python_cmd"
    local vp; vp=$(venv_python) || { err "Venv python not found after setup"; exit 1; }
    info "Migrating legacy config.yaml to config.json (if present)..."
    "$vp" -m core.config migrate || true
    if [[ "$skills" == true ]]; then
        run_harness_py "$cmd" "$vp" install --with-skills
    else
        run_harness_py "$cmd" "$vp" install
    fi
    info "Setup complete!"
}

usage() {
    cat <<'EOF'

Arize Coding Harness Tracing Installer

Usage: install.sh <command> [flags]

Commands:
  claude      Install and configure tracing for Claude Code / Agent SDK
  codex       Install and configure tracing for OpenAI Codex CLI
  copilot     Install and configure tracing for GitHub Copilot (VS Code + CLI)
  cursor      Install and configure tracing for Cursor IDE
  gemini      Install and configure tracing for Gemini CLI
  kiro        Install and configure tracing for Kiro CLI
  antigravity Install and configure tracing for Google Antigravity CLI/IDE
  opencode    Install and configure tracing for opencode
  omp         Install and configure tracing for Oh My Pi (omp)
  devin       Install and configure tracing for Devin CLI
  status      Report configured harnesses and whether their hooks are wired up
  update      Update the installed coding-harness-tracing and re-register all harnesses
  uninstall <harness>   Tear down one harness
  uninstall             Full wipe: venv + repo + shared config

Flags:
  --with-skills         Symlink harness skills into .agents/skills/
  --branch NAME         Install from a specific git branch (default: main)
  --wheel-dir DIR       Install from local wheels in DIR instead of downloading
                        the repo. No network and no remote code execution; also
                        settable as ARIZE_WHEEL_DIR. macOS also needs a
                        certifi wheel alongside it.
  --json                With `status`: emit machine-readable JSON. Exit code is
                        0 all wired up, 1 nothing configured, 2 hooks missing.
  --non-interactive, -y Ask nothing; use defaults, saved values, and settings
                        from the environment or ARIZE_ENV_FILE. Missing
                        required values are an error.

Non-interactive install:
  Values come from the environment, or from a dotenv file named with
  ARIZE_ENV_FILE — which keeps the API key out of the command line and shell
  history. A named file outranks the environment, so there is no automatic
  ./.env search: a cloned repo's dotenv must not get to choose the endpoint
  your credentials are sent to.

  ARIZE_API_KEY, ARIZE_SPACE_ID     Arize AX credentials (both required)
  PHOENIX_ENDPOINT, PHOENIX_API_KEY Phoenix credentials
  ARIZE_BACKEND                     arize|phoenix (default: inferred — a space
                                    ID means Arize AX, a Phoenix endpoint
                                    means Phoenix)
  ARIZE_PROJECT_NAME                AX project from ARIZE_ENV_FILE only
                                    (default: harness/<email>; harness ID
                                    if no email is available)
  PHOENIX_PROJECT, PHOENIX_PROJECT_NAME
                                    Phoenix project from ARIZE_ENV_FILE only
                                    (default: harness ID; PHOENIX_PROJECT wins)
  ARIZE_USER_ID                     Optional user ID stamped on spans
  ARIZE_OTLP_ENDPOINT               Override otlp.arize.com:443
  ARIZE_LOG_PROMPTS                 Set true to capture prompt text (off here)
  ARIZE_LOG_TOOL_DETAILS            Set true to capture tool commands and paths
  ARIZE_LOG_TOOL_CONTENT            Set true to capture tool output

  Example — credentials straight from a dotenv file, nothing exported:
    ax api-keys create --env-file ~/.arize/onboarding.env
    echo 'ARIZE_SPACE_ID=<space-id>' >> ~/.arize/onboarding.env
    ARIZE_ENV_FILE=~/.arize/onboarding.env ./install.sh claude --non-interactive

EOF
}

# -- Main dispatch -----------------------------------------------------------
main() {
    local cmd="${1:-}"; shift || true
    local subcmd="" with_skills=false status_args=""
    local args=("$@") i=0
    while [[ $i -lt ${#args[@]} ]]; do
        case "${args[$i]}" in
            --with-skills) with_skills=true ;;
            --non-interactive|-y) export ARIZE_NONINTERACTIVE=1 ;;
            --json) status_args="--json" ;;
            --branch)
                i=$((i + 1))
                INSTALL_BRANCH="${args[$i]:-main}"
                TARBALL_URL="https://github.com/Arize-ai/coding-harness-tracing/archive/refs/heads/${INSTALL_BRANCH}.tar.gz"
                ;;
            --wheel-dir)
                i=$((i + 1))
                WHEEL_DIR="${args[$i]:-}"
                [[ -d "$WHEEL_DIR" ]] || { err "--wheel-dir needs a directory; got '${WHEEL_DIR}'"; exit 1; }
                WHEEL_DIR="$(cd "$WHEEL_DIR" && pwd)"
                compgen -G "${WHEEL_DIR}/coding_harness_tracing-*.whl" >/dev/null \
                    || { err "No coding_harness_tracing-*.whl in ${WHEEL_DIR}"; exit 1; }
                ;;
            *) [[ -z "$subcmd" ]] && subcmd="${args[$i]}" ;;
        esac
        i=$((i + 1))
    done

    case "$cmd" in
        claude|codex|copilot|cursor|gemini|kiro|antigravity|opencode|omp|devin)
            install_harness "$cmd" "$with_skills"
            ;;
        uninstall)
            if [[ -n "$subcmd" ]]; then
                harness_dir "$subcmd" >/dev/null || { err "Unknown harness: ${subcmd}"; usage; exit 1; }
                local vp; vp=$(venv_python) || { err "Venv not found — nothing to uninstall"; exit 1; }
                header "Uninstalling ${subcmd} tracing"
                run_harness_py "$subcmd" "$vp" uninstall
            else
                local vp; vp=$(venv_python) || {
                    warn "Venv not found — removing install directory"; rm -rf "$INSTALL_DIR"
                    info "Uninstall complete."; return 0; }
                header "Full uninstall"
                # Run each installed harness's uninstall first so external
                # registrations (settings.json hooks, config.toml notify,
                # cursor hooks.json, .github/hooks/*) are cleaned before the
                # shared runtime is wiped. wipe.py deliberately does not
                # touch those files.
                local harnesses
                harnesses=$("$vp" -c 'from core.setup import list_installed_harnesses as L; print("\n".join(L()))' 2>/dev/null) || true
                if [[ -n "$harnesses" ]]; then
                    while IFS= read -r key; do
                        harness_dir "$key" >/dev/null || { warn "Unknown harness: ${key} (skipping)"; continue; }
                        info "Uninstalling ${key} tracing..."
                        run_harness_py "$key" "$vp" uninstall || warn "${key} uninstall failed (continuing)"
                    done <<< "$harnesses"
                fi
                "$vp" -m core.setup.wipe
            fi
            ;;
        status)
            local vp; vp=$(venv_python) || { err "Venv not found — nothing installed"; exit 1; }
            "$vp" -m core.setup.status $status_args
            ;;
        update)
            header "Updating coding-harness-tracing"
            local python_cmd
            python_cmd=$(find_python) || { err "No Python 3.11+ found"; exit 1; }
            # Re-registering runs each harness's installer, which prompts for the
            # project name. With no terminal to answer on that used to die with an
            # EOFError, so fall back to stored values there — and only there, so an
            # interactive update keeps every prompt it has today.
            [[ -n "$_tty_in" ]] || export ARIZE_NONINTERACTIVE=1
            # A wheel install has no repo to pull and no newer wheel to hand us.
            # Silently converting it to a network install would change how it was
            # installed behind the user's back, so refuse and say who can update.
            if [[ -z "$WHEEL_DIR" && ! -d "${INSTALL_DIR}/.git" && ! -f "${INSTALL_DIR}/pyproject.toml" ]]; then
                err "This looks like an offline install with no source tree to update."
                err "Re-run the installer that created it (for npx evals, update that), or"
                err "pass --wheel-dir <dir> with a newer wheel."
                exit 1
            fi
            if [[ -n "$WHEEL_DIR" ]]; then
                info "Updating from local wheels in ${WHEEL_DIR}..."
            elif [[ -d "${INSTALL_DIR}/.git" ]]; then
                info "Pulling latest changes..."
                without_ca_overrides git -C "$INSTALL_DIR" pull --ff-only 2>/dev/null || {
                    warn "git pull failed — falling back to tarball re-extract"; install_repo_tarball; }
            else install_repo_tarball; fi
            setup_venv "$python_cmd" -U || exit 1
            local vp; vp=$(venv_python) || { err "venv python not found"; exit 1; }
            info "Migrating legacy config.yaml to config.json (if present)..."
            "$vp" -m core.config migrate || true
            local harnesses
            harnesses=$("$vp" -c 'from core.setup import list_installed_harnesses as L; print("\n".join(L()))' 2>/dev/null) || true
            if [[ -n "$harnesses" ]]; then
                while IFS= read -r key; do
                    harness_dir "$key" >/dev/null || { warn "Unknown harness: ${key} (skipping)"; continue; }
                    # Keep going, as the uninstall loop does: one harness whose
                    # registration fails should not abandon the rest half-updated.
                    info "Re-registering ${key}..."
                    run_harness_py "$key" "$vp" install || warn "${key} re-registration failed (continuing)"
                done <<< "$harnesses"
            else info "No installed harnesses found to re-register"; fi
            info "Update complete."
            ;;
        -h|--help|help) usage ;;
        "") usage; exit 1 ;;
        *) err "Unknown command: ${cmd}"; usage; exit 1 ;;
    esac
}

main "$@"
