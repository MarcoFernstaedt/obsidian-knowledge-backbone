#!/usr/bin/env bash
set -euo pipefail
umask 077

: "${OBSIDIAN_KB_CONFIG:?set OBSIDIAN_KB_CONFIG to the fixed private TOML config}"
: "${OBSIDIAN_KB_BIN:?set OBSIDIAN_KB_BIN to the absolute installed imperator-knowledge executable}"
[[ "$OBSIDIAN_KB_BIN" = /* && -x "$OBSIDIAN_KB_BIN" ]] || { printf 'invalid OBSIDIAN_KB_BIN\n' >&2; exit 2; }

STATE_HOME="${XDG_STATE_HOME:-$HOME/.local/state}/imperator/obsidian-retrieval"
CACHE_HOME="${XDG_CACHE_HOME:-$HOME/.cache}/imperator/obsidian-retrieval"
mkdir -p -m 700 "$STATE_HOME" "$CACHE_HOME"
LOCK="$STATE_HOME/refresh.lock"
LOG="$CACHE_HOME/refresh.log"
[[ -f "$LOG" ]] && [[ $(wc -c <"$LOG") -gt 1048576 ]] && mv -f "$LOG" "$LOG.1"

# The application also owns an SQLite-adjacent lock; this wrapper lock bounds scheduler overlap.
set +e
flock -n "$LOCK" timeout --signal=TERM 10m "$OBSIDIAN_KB_BIN" index --config "$OBSIDIAN_KB_CONFIG" --full-reconcile --json >>"$LOG" 2>&1
status=$?
set -e
if [[ $status -ne 0 ]]; then
  printf 'obsidian retrieval refresh failed (class=exit_%s)\n' "$status" >&2
  exit "$status"
fi
