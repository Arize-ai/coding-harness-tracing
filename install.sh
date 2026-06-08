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
    git_sync_harness_repo "$INSTALL_BRANCH" && return 0
    install_repo_tarball
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

    if ! "$pip" install --quiet certifi 2>/dev/null; then
        warn "Could not install certifi — SSL verification may fail on macOS"
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
    if venv_python &>/dev/null && ! venv_pip &>/dev/null; then
        warn "Existing venv is missing pip — recreating ${VENV_DIR}"
        rm -rf "$VENV_DIR"
    fi
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
    "$pip" install --quiet "$INSTALL_DIR" 2>/dev/null || { err "Failed to install coding-harness-tracing package"; return 1; }

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
        *)       return 1 ;;
    esac
}

install_harness() {
    local cmd="$1" skills="$2"
    shift 2
    local passthrough=("$@")
    local dir; dir=$(harness_dir "$cmd") || { err "Unknown harness: ${cmd}"; usage; exit 1; }
    header "Installing ${cmd} tracing"
    local python_cmd; python_cmd=$(find_python) || { err "No Python 3.9+ found"; exit 1; }
    info "Found Python: ${python_cmd} ($("$python_cmd" --version 2>&1))"
    install_repo
    setup_venv "$python_cmd"
    local vp; vp=$(venv_python) || { err "Venv python not found after setup"; exit 1; }
    local install_py="${INSTALL_DIR}/${dir}/install.py"
    [[ -f "$install_py" ]] || { err "Harness install script not found: ${install_py}"; exit 1; }
    if [[ "$skills" == true ]]; then
        if [[ ${#passthrough[@]} -gt 0 ]]; then
            run_with_tty "$vp" "$install_py" install --with-skills "${passthrough[@]}"
        else
            run_with_tty "$vp" "$install_py" install --with-skills
        fi
    else
        if [[ ${#passthrough[@]} -gt 0 ]]; then
            run_with_tty "$vp" "$install_py" install "${passthrough[@]}"
        else
            run_with_tty "$vp" "$install_py" install
        fi
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
  update      Update the installed coding-harness-tracing and re-register all harnesses
  uninstall <harness>   Tear down one harness
  uninstall             Full wipe: venv + repo + shared config

Flags:
  --with-skills         Symlink harness skills into .agents/skills/
  --project-hooks       Cursor only: write repo-local .cursor/hooks.json
  --cloud-agent         Cursor only: project hooks plus Cloud Agent bootstrap
  --branch NAME         Install from a specific git branch (default: main)

EOF
}

# -- Main dispatch -----------------------------------------------------------
main() {
    local cmd="${1:-}"; shift || true
    local subcmd="" with_skills=false
    local passthrough=()
    local args=("$@") i=0
    while [[ $i -lt ${#args[@]} ]]; do
        case "${args[$i]}" in
            --with-skills) with_skills=true ;;
            --project-hooks|--cloud-agent) passthrough+=("${args[$i]}") ;;
            --branch)
                i=$((i + 1))
                INSTALL_BRANCH="${args[$i]:-main}"
                TARBALL_URL="https://github.com/Arize-ai/coding-harness-tracing/archive/refs/heads/${INSTALL_BRANCH}.tar.gz"
                ;;
            *) [[ -z "$subcmd" ]] && subcmd="${args[$i]}" ;;
        esac
        i=$((i + 1))
    done

    case "$cmd" in
        claude|codex|copilot|cursor|gemini|kiro)
            if [[ "$cmd" != "cursor" && ${#passthrough[@]} -gt 0 ]]; then
                err "--project-hooks and --cloud-agent are only supported for cursor"
                exit 1
            fi
            if [[ ${#passthrough[@]} -gt 0 ]]; then
                install_harness "$cmd" "$with_skills" "${passthrough[@]}"
            else
                install_harness "$cmd" "$with_skills"
            fi
            ;;
        uninstall)
            if [[ -n "$subcmd" ]]; then
                local dir; dir=$(harness_dir "$subcmd") || { err "Unknown harness: ${subcmd}"; usage; exit 1; }
                local vp; vp=$(venv_python) || { err "Venv not found — nothing to uninstall"; exit 1; }
                header "Uninstalling ${subcmd} tracing"
                run_with_tty "$vp" "${INSTALL_DIR}/${dir}/install.py" uninstall
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
                        local dir; dir=$(harness_dir "$key") || { warn "Unknown harness: ${key} (skipping)"; continue; }
                        if [[ -f "${INSTALL_DIR}/${dir}/install.py" ]]; then
                            info "Uninstalling ${key} tracing..."
                            run_with_tty "$vp" "${INSTALL_DIR}/${dir}/install.py" uninstall || warn "${key} uninstall failed (continuing)"
                        fi
                    done <<< "$harnesses"
                fi
                "$vp" -m core.setup.wipe
            fi
            ;;
        update)
            header "Updating coding-harness-tracing"
            if [[ -d "${INSTALL_DIR}/.git" ]]; then
                info "Pulling latest changes..."
                git -C "$INSTALL_DIR" pull --ff-only 2>/dev/null || {
                    warn "git pull failed — falling back to tarball re-extract"; install_repo_tarball; }
            else install_repo_tarball; fi
            local pip; pip=$(venv_pip) || { err "Venv not found — run install first"; exit 1; }
            info "Reinstalling coding-harness-tracing..."
            "$pip" install --quiet -U "$INSTALL_DIR" 2>/dev/null || { err "Failed to reinstall package"; exit 1; }
            local vp; vp=$(venv_python) || { err "venv python not found"; exit 1; }
            local harnesses
            harnesses=$("$vp" -c 'from core.setup import list_installed_harnesses as L; print("\n".join(L()))' 2>/dev/null) || true
            if [[ -n "$harnesses" ]]; then
                while IFS= read -r key; do
                    local dir; dir=$(harness_dir "$key") || { warn "Unknown harness: ${key} (skipping)"; continue; }
                    if [[ -f "${INSTALL_DIR}/${dir}/install.py" ]]; then
                        info "Re-registering ${key}..."; run_with_tty "$vp" "${INSTALL_DIR}/${dir}/install.py" install
                    else warn "Harness directory not found: ${dir}"; fi
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
