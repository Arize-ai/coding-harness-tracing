#!/usr/bin/env bash
# Wheel install smoke test: `uv tool install` a wheel, then set up, check and
# uninstall every harness, each in a throwaway HOME. CI's wheel-smoke job runs it.
#
#   scripts/smoke-wheel.sh <wheel> [<dependency wheel dir>]
#
# A dependency wheel dir (holding python-dotenv) means no network. Exits 0 when
# every check passes, 1 naming each failed harness and check, 2 on bad arguments.
#
#   UV_PYTHON=3.11  install with this Python
#
# check_install takes any arize-harness path, so other install types can reuse it.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
USAGE="usage: scripts/smoke-wheel.sh <wheel> [<dependency wheel dir>]"

usage_error() {
    echo "smoke-wheel: $*" >&2
    echo "$USAGE" >&2
    exit 2
}
die() { echo "smoke-wheel: FAIL: $*" >&2; exit 1; }

# --- Arguments ---------------------------------------------------------------

[[ $# -ge 1 && $# -le 2 ]] || usage_error "expected a wheel and an optional dependency wheel dir"
[[ -f "$1" && -r "$1" ]] || usage_error "wheel '$1' is missing or unreadable"
[[ "$1" == *.whl ]] || usage_error "'$1' is not a .whl file"
wheel="$(cd "$(dirname "$1")" && pwd -P)/$(basename "$1")"
deps=""
if [[ $# -eq 2 ]]; then
    [[ -d "$2" && -r "$2" && -x "$2" ]] || usage_error "dependency wheel dir '$2' is missing or unreadable"
    deps="$(cd "$2" && pwd -P)"
fi
# coding_harness_tracing-<version>-py3-none-any.whl
version="$(basename "$wheel" .whl)"
version="${version#*-}"
version="${version%%-*}"

# --- Prerequisites -----------------------------------------------------------

command -v uv >/dev/null || die "uv not found on PATH. Install it: https://docs.astral.sh/uv/"
if [[ -n "$deps" ]]; then
    compgen -G "$deps/python_dotenv-*.whl" >/dev/null ||
        die "dependency wheel dir $deps has no python-dotenv wheel"
fi

work="$(mktemp -d "${TMPDIR:-/tmp}/smoke-wheel.XXXXXX")"
work="$(cd "$work" && pwd -P)"
trap 'rm -rf "$work"' EXIT
cd "$work"

# Before the environment is cleared, so UV_PYTHON counts. --system: never a project or active venv.
py="$(UV_CACHE_DIR="$work/uv-cache" UV_NO_CONFIG=1 uv python find --system --no-python-downloads "${UV_PYTHON:->=3.11}" 2>/dev/null)" ||
    die "no Python 3.11+ found${UV_PYTHON:+ for UV_PYTHON=$UV_PYTHON}"
"$py" -c 'import sys; sys.exit(sys.version_info < (3, 11))' ||
    die "$py is $("$py" --version 2>&1); Python 3.11+ is needed"

# --- Throwaway environment ---------------------------------------------------

# As smoke-install.sh: drop settings that load code from elsewhere or move harness configs.
unset CODEX_HOME DEVIN_PROJECT_DIR PYTHONPATH PYTHONHOME PYTHONSTARTUP PYTHONUSERBASE VIRTUAL_ENV
while IFS= read -r var; do unset "$var"; done < <(compgen -v | grep -E '^(ARIZE|PHOENIX|OTEL|PIP|UV|XDG)_' || true)
export PIP_CONFIG_FILE=/dev/null NO_COLOR=1
export HOME="$work/install-home"
mkdir -p "$HOME"
# setup refuses to write from anything under the temp dir, and $work is there.
mkdir -p "$work/tmp"
export TMPDIR="$work/tmp" TEMP="$work/tmp" TMP="$work/tmp"
export UV_NO_CONFIG=1 UV_PYTHON_DOWNLOADS=never UV_CACHE_DIR="$work/uv-cache"
export UV_TOOL_DIR="$work/tools" UV_TOOL_BIN_DIR="$work/bin"

offline=()
[[ -n "$deps" ]] && offline=(--offline --no-index --find-links "$deps")

printf '%s\n' ARIZE_API_KEY=ci-not-a-real-key ARIZE_SPACE_ID=ci-space > "$work/creds.env"

# The checks import this checkout's core, which needs python-dotenv.
uv venv --quiet --python "$py" "$work/checker" || die "could not create the checker venv"
uv pip install --quiet --python "$work/checker/bin/python" ${offline[@]+"${offline[@]}"} python-dotenv ||
    die "could not install python-dotenv for the checks"
check() { "$work/checker/bin/python" -I -B "$ROOT/scripts/smoke_wheel_check.py" "$@"; }

read -r -a HARNESSES <<< "$(check harnesses)"
echo "smoke-wheel: $(basename "$wheel") with $("$py" --version 2>&1) ($py)${deps:+, offline from $deps}"
echo "smoke-wheel: harnesses: ${HARNESSES[*]}"

# --- Install -----------------------------------------------------------------

# Prints the arize-harness path the checks run against.
install_uv_tool() {
    uv tool install --quiet --python "$py" ${offline[@]+"${offline[@]}"} "$wheel" > "$work/install.log" 2>&1 ||
        { cat "$work/install.log" >&2; die "uv tool install $(basename "$wheel") failed"; }
    [[ -x "$UV_TOOL_BIN_DIR/arize-harness" ]] || die "uv tool install made no arize-harness in $UV_TOOL_BIN_DIR"
    echo "$UV_TOOL_BIN_DIR/arize-harness"
}

# --- Checks ------------------------------------------------------------------

failures=()
fail() { failures+=("$1: $2"); echo "  FAIL $1: $2" >&2; }

home="" proj="" log=""
fresh() {
    home="$work/$1/home" proj="$work/$1/project" log="$work/$1/log"
    mkdir -p "$home" "$proj"
    : > "$log"
}
ah() {
    (cd "$proj" && HOME="$home" ARIZE_ENV_FILE="$work/creds.env" "$cmd" "$@" < /dev/null) >> "$log" 2>&1
}
in_home() { (cd "$proj" && HOME="$home" "$@" < /dev/null); }
show_log() { sed 's/^/    | /' "$log" >&2; }

# Checks that hold for any install type.
check_install() {
    cmd="$1"
    local h rc

    fresh hook-list
    rc=0
    "$cmd" hook --list < /dev/null > "$work/hook-list.txt" 2>> "$log" || rc=$?
    if [[ $rc -ne 0 ]]; then
        show_log; fail all "hook --list exited $rc"
    elif ! check hook-list "$work/hook-list.txt" "${HARNESSES[@]}" 2>> "$log"; then
        show_log; fail all "hook --list does not list every harness"
    fi

    for h in "${HARNESSES[@]}"; do
        echo "smoke-wheel: $h"
        fresh "$h"
        ah setup "$h" --non-interactive --backend arize ||
            { rc=$?; show_log; fail "$h" "setup exited $rc"; continue; }
        in_home check entries "$cmd" "$h" 2>> "$log" ||
            { show_log; fail "$h" "hook entries do not all run '$cmd' hook"; continue; }
        ah status || { rc=$?; show_log; fail "$h" "status exited $rc"; continue; }
        in_home "$cmd" status --json > "$work/$h/status.json" 2>> "$log" ||
            { rc=$?; show_log; fail "$h" "status --json exited $rc"; continue; }
        check json "$work/$h/status.json" 2>> "$log" ||
            { show_log; fail "$h" "status --json is not JSON"; continue; }
        check snapshot "$work/$h/before.json" "$home" "$proj" 2>> "$log" ||
            { show_log; fail "$h" "snapshot before setup re-run failed"; continue; }
        ah setup "$h" --non-interactive --backend arize ||
            { rc=$?; show_log; fail "$h" "setup re-run exited $rc"; continue; }
        check snapshot "$work/$h/after.json" "$home" "$proj" 2>> "$log" ||
            { show_log; fail "$h" "snapshot after setup re-run failed"; continue; }
        check same "$work/$h/before.json" "$work/$h/after.json" 2>> "$log" ||
            { show_log; fail "$h" "setup re-run changed files"; continue; }
        ah uninstall "$h" || { rc=$?; show_log; fail "$h" "uninstall exited $rc"; continue; }
        in_home check no-entries "$h" 2>> "$log" ||
            { show_log; fail "$h" "uninstall left an entry of ours"; continue; }
        echo "  ok"
    done

    echo "smoke-wheel: all harnesses in one HOME"
    fresh all
    for h in "${HARNESSES[@]}"; do
        ah setup "$h" --non-interactive --backend arize ||
            { rc=$?; show_log; fail all "setup $h exited $rc"; return; }
    done
    check snapshot "$work/all/before.json" "$home" "$proj" 2>> "$log" ||
        { show_log; fail all "snapshot before bare uninstall failed"; return; }
    rc=0
    ah uninstall || rc=$?
    [[ $rc -eq 2 ]] || { show_log; fail all "bare uninstall exited $rc, not 2"; }
    check snapshot "$work/all/after.json" "$home" "$proj" 2>> "$log" ||
        { show_log; fail all "snapshot after bare uninstall failed"; return; }
    check same "$work/all/before.json" "$work/all/after.json" 2>> "$log" ||
        { show_log; fail all "bare uninstall changed files"; }
    ah uninstall --all || { rc=$?; show_log; fail all "uninstall --all exited $rc"; return; }
    in_home check no-entries "${HARNESSES[@]}" 2>> "$log" ||
        { show_log; fail all "uninstall --all left an entry of ours"; return; }
    echo "  ok"
}

# The checks that only hold for a uv tool install.
check_uv_tool() {
    cmd="$1"
    local out rc
    echo "smoke-wheel: uv tool install checks"
    out="$("$cmd" --version < /dev/null 2>&1)" ||
        { rc=$?; fail uv-tool "--version exited $rc: $out"; }
    [[ "$out" == "arize-harness $version" ]] ||
        fail uv-tool "--version printed '$out', not 'arize-harness $version'"

    fresh update
    ah setup claude --non-interactive --backend arize || { rc=$?; show_log; fail uv-tool "setup claude exited $rc"; return; }
    check snapshot "$work/update/before.json" "$home" "$proj" 2>> "$log" ||
        { show_log; fail uv-tool "snapshot before update failed"; return; }
    rc=0
    ah update || rc=$?
    [[ $rc -eq 1 ]] || { show_log; fail uv-tool "update exited $rc, not 1"; }
    grep -q "uv tool upgrade coding-harness-tracing" "$log" ||
        { show_log; fail uv-tool "update did not print the uv tool upgrade hint"; }
    check snapshot "$work/update/after.json" "$home" "$proj" 2>> "$log" ||
        { show_log; fail uv-tool "snapshot after update failed"; return; }
    check same "$work/update/before.json" "$work/update/after.json" 2>> "$log" ||
        { show_log; fail uv-tool "update changed files"; }
    echo "  ok"
}

arize_harness="$(install_uv_tool)"
echo "smoke-wheel: installed $arize_harness"
check_uv_tool "$arize_harness"
check_install "$arize_harness"

echo
if [[ ${#failures[@]} -gt 0 ]]; then
    echo "smoke-wheel: FAIL (${#failures[@]}):" >&2
    printf '  %s\n' "${failures[@]}" >&2
    exit 1
fi
echo "smoke-wheel: OK"
