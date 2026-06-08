#!/usr/bin/env bash
set -euo pipefail

HOOK_BIN="${ARIZE_HOOK_CURSOR:-$HOME/.arize/harness/venv/bin/arize-hook-cursor}"
LOG_FILE="${ARIZE_LOG_FILE:-$HOME/.arize/harness/logs/cursor.log}"

if [[ ! -x "$HOOK_BIN" ]]; then
    mkdir -p "$(dirname "$LOG_FILE")"
    printf '[arize] Cursor hook binary not found: %s\n' "$HOOK_BIN" >> "$LOG_FILE"
    exit 0
fi

exec "$HOOK_BIN"
