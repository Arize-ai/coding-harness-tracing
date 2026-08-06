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

tty_input() {
    local prompt="$1" reply=""
    [[ -n "$_tty_in" ]] && read -rp "$prompt" reply < "$_tty_in"
    echo "$reply"
}

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

tty_read_masked_line() {
    REPLY=""
    [[ -n "${_tty_in:-}" ]] || return 1
    local prompt="$1" char
    printf '%s' "$prompt" >&2
    while IFS= read -rs -n 1 char < "$_tty_in"; do
        if [[ -z "$char" || "$char" == $'\n' || "$char" == $'\r' ]]; then
            printf '\n' >&2; return 0
        fi
        if [[ "$char" == $'\177' || "$char" == $'\b' ]]; then
            [[ -n "$REPLY" ]] && { REPLY="${REPLY%?}"; printf '\b \b' >&2; }
            continue
        fi
        [[ "$char" =~ [[:cntrl:]] ]] && continue
        REPLY+="$char"; printf '*' >&2
    done
    printf '\n' >&2
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
        "$resolved" -c "import sys; assert sys.version_info >= (3, 9)" 2>/dev/null && { echo "$resolved"; return 0; }
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

# -- Repository download ----------------------------------------------------
git_sync_harness_repo() {
    local branch="$1"
    [[ -d "${INSTALL_DIR}/.git" ]] || return 1
    info "Syncing with origin/${branch}..."
    git -C "$INSTALL_DIR" fetch --depth 1 origin "$branch" 2>/dev/null \
        && git -C "$INSTALL_DIR" checkout -B "$branch" FETCH_HEAD 2>/dev/null && return 0
    git -C "$INSTALL_DIR" fetch origin "$branch" 2>/dev/null \
        && git -C "$INSTALL_DIR" checkout -B "$branch" FETCH_HEAD 2>/dev/null && return 0
    warn "git fetch/checkout failed — trying pull --ff-only"
    git -C "$INSTALL_DIR" pull --ff-only origin "$branch" 2>/dev/null && return 0
    git -C "$INSTALL_DIR" pull --ff-only 2>/dev/null && return 0
    return 1
}

install_repo_tarball() {
    local tarball_url="${1:-$TARBALL_URL}"
    info "Downloading coding-harness-tracing tarball..."
    local tmp_tar; tmp_tar="$(mktemp)"
    if command_exists curl; then curl -sSfL "$tarball_url" -o "$tmp_tar"
    elif command_exists wget; then wget -qO "$tmp_tar" "$tarball_url"
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
        if [[ -f "${BASH_SOURCE[0]}" ]] && ! cmp -s "${BASH_SOURCE[0]}" "${INSTALL_DIR}/install.sh"; then
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
# Fix: install certifi into the venv and write a sitecustomize.py that sets
# SSL_CERT_FILE before any hook code runs. Idempotent — safe to call repeatedly.
_fix_macos_ssl_certs() {
    local pip="$1"
    local vp
    vp=$(venv_python 2>/dev/null) || return 0

    local offline=()
    [[ -n "$WHEEL_DIR" ]] && offline=(--no-index --find-links "$WHEEL_DIR")
    if ! "$pip" install --quiet "${offline[@]+"${offline[@]}"}" certifi 2>/dev/null; then
        warn "Could not install certifi — SSL verification may fail on macOS"
        [[ -n "$WHEEL_DIR" ]] && warn "Bundle a certifi wheel in ${WHEEL_DIR} to fix this offline."
        return 0
    fi

    local certifi_where site_dir sc
    certifi_where=$("$vp" -c "import certifi; print(certifi.where())" 2>/dev/null) || return 0
    [[ -z "$certifi_where" ]] && return 0

    site_dir=$("$vp" -c "import site; print(site.getsitepackages()[0])" 2>/dev/null) || return 0
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

setup_venv() {
    local python_cmd="$1"
    if ! venv_python &>/dev/null; then
        info "Creating venv..."
        "$python_cmd" -m venv "$VENV_DIR" 2>/dev/null || {
            err "Failed to create venv with $python_cmd"
            err "You may need to install the venv module: apt install python3-venv (Debian/Ubuntu)"
            return 1
        }
    fi
    local pip; pip=$(venv_pip) || { err "pip not found in venv"; return 1; }
    info "Installing coding-harness-tracing into venv..."
    if [[ -n "$WHEEL_DIR" ]]; then
        # --no-index so a missing wheel fails loudly instead of quietly reaching
        # PyPI, which would defeat the point of installing offline.
        "$pip" install --quiet --no-index --find-links "$WHEEL_DIR" coding-harness-tracing \
            || { err "Failed to install coding-harness-tracing from ${WHEEL_DIR}"; return 1; }
    else
        "$pip" install --quiet "$INSTALL_DIR" 2>/dev/null || { err "Failed to install coding-harness-tracing package"; return 1; }
    fi

    [[ "$(uname)" == "Darwin" ]] && _fix_macos_ssl_certs "$pip"

    info "Venv ready at ${VENV_DIR}"
}

# -- Harness name mapping ----------------------------------------------------
harness_dir() {
    case "$1" in
        claude)  echo "tracing/claude_code" ;;
        codex)   echo "tracing/codex" ;;
        copilot) echo "tracing/copilot" ;;
        cursor)  echo "tracing/cursor" ;;
        gemini)  echo "tracing/gemini" ;;
        kiro)    echo "tracing/kiro" ;;
        opencode) echo "tracing/opencode" ;;
        omp)     echo "tracing/omp" ;;
        *)       return 1 ;;
    esac
}

install_harness() {
    local cmd="$1" skills="$2"
    harness_dir "$cmd" >/dev/null || { err "Unknown harness: ${cmd}"; usage; exit 1; }
    header "Installing ${cmd} tracing"
    local python_cmd; python_cmd=$(find_python) || { err "No Python 3.9+ found"; exit 1; }
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
  opencode    Install and configure tracing for opencode
  omp         Install and configure tracing for Oh My Pi (omp)
  status      Report configured harnesses and whether their hooks are wired up
  update      Update the installed coding-harness-tracing and re-register all harnesses
  uninstall <harness>   Tear down one harness
  uninstall             Full wipe: venv + repo + shared config

Flags:
  --with-skills         Symlink harness skills into .agents/skills/
  --branch NAME         Install from a specific git branch (default: main)
  --wheel-dir DIR       Install from local wheels in DIR instead of downloading
                        the repo. No network and no remote code execution; also
                        settable as ARIZE_WHEEL_DIR. Bundle a certifi wheel
                        alongside it to keep macOS SSL working offline.
  --json                With `status`: emit machine-readable JSON. Exit code is
                        0 all wired up, 1 nothing configured, 2 hooks missing.
  --non-interactive, -y Ask nothing; read every value from the environment or a
                        .env file. Missing required values are an error.

Non-interactive install:
  Values are read from the environment first, then from ./.env or ./.env.local
  (override the path with ARIZE_ENV_FILE). Because credentials can come from a
  file, the API key never has to appear in a command line or shell history.

  ARIZE_API_KEY, ARIZE_SPACE_ID     Arize AX credentials (both required)
  PHOENIX_ENDPOINT, PHOENIX_API_KEY Phoenix credentials
  ARIZE_BACKEND                     arize|phoenix (default: inferred — a space
                                    ID means Arize AX, a Phoenix endpoint
                                    means Phoenix)
  ARIZE_PROJECT_NAME                Project name (default: the harness name)
  ARIZE_USER_ID                     Optional user ID stamped on spans
  ARIZE_OTLP_ENDPOINT               Override otlp.arize.com:443
  ARIZE_LOG_PROMPTS                 Set false to omit prompt text
  ARIZE_LOG_TOOL_DETAILS            Set false to omit tool commands and paths
  ARIZE_LOG_TOOL_CONTENT            Set false to omit tool output

  Example — credentials straight from a dotenv file, nothing exported:
    ax api-keys create --env-file .env   # writes ARIZE_API_KEY
    echo 'ARIZE_SPACE_ID=<space-id>' >> .env
    ./install.sh claude --non-interactive

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
        claude|codex|copilot|cursor|gemini|kiro|opencode|omp)
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
                git -C "$INSTALL_DIR" pull --ff-only 2>/dev/null || {
                    warn "git pull failed — falling back to tarball re-extract"; install_repo_tarball; }
            else install_repo_tarball; fi
            local pip; pip=$(venv_pip) || { err "Venv not found — run install first"; exit 1; }
            info "Reinstalling coding-harness-tracing..."
            if [[ -n "$WHEEL_DIR" ]]; then
                "$pip" install --quiet -U --no-index --find-links "$WHEEL_DIR" coding-harness-tracing \
                    || { err "Failed to reinstall from ${WHEEL_DIR}"; exit 1; }
            else
                "$pip" install --quiet -U "$INSTALL_DIR" 2>/dev/null || { err "Failed to reinstall package"; exit 1; }
            fi
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
