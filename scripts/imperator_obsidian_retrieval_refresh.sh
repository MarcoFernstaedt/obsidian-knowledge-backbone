#!/usr/bin/env bash
set -euo pipefail
umask 077

: "${OBSIDIAN_KB_CONFIG:?set OBSIDIAN_KB_CONFIG to the fixed private TOML config}"
: "${OBSIDIAN_KB_BIN:?set OBSIDIAN_KB_BIN to the absolute installed imperator-knowledge executable}"
[[ "$OBSIDIAN_KB_BIN" = /* && -x "$OBSIDIAN_KB_BIN" ]] || { printf 'invalid OBSIDIAN_KB_BIN\n' >&2; exit 2; }
[[ "$OBSIDIAN_KB_CONFIG" = /* && -f "$OBSIDIAN_KB_CONFIG" && ! -L "$OBSIDIAN_KB_CONFIG" ]] || {
  printf 'invalid OBSIDIAN_KB_CONFIG (class=config_security)\n' >&2; exit 2;
}
config_uid=$(stat -c '%u' -- "$OBSIDIAN_KB_CONFIG") || exit 2
config_mode=$(stat -c '%a' -- "$OBSIDIAN_KB_CONFIG") || exit 2
[[ "$config_uid" = "$(id -u)" ]] && (( (8#$config_mode & 077) == 0 )) || {
  printf 'invalid OBSIDIAN_KB_CONFIG (class=config_security)\n' >&2; exit 2;
}

STATE_HOME="${XDG_STATE_HOME:-$HOME/.local/state}/imperator/obsidian-retrieval"
CACHE_HOME="${XDG_CACHE_HOME:-$HOME/.cache}/imperator/obsidian-retrieval"
mkdir -p -m 700 "$STATE_HOME" "$CACHE_HOME"
LOCK="$STATE_HOME/refresh.lock"
LOG="$CACHE_HOME/refresh.log"
[[ -f "$LOG" ]] && [[ $(wc -c <"$LOG") -gt 1048576 ]] && mv -f "$LOG" "$LOG.1"

set +e
output=$(flock -n "$LOCK" timeout --signal=TERM 10m "$OBSIDIAN_KB_BIN" index --full-reconcile --json 2>/dev/null)
status=$?
set -e
if [[ -n "$output" ]]; then
  summary=$(printf '%s' "$output" | python3 -c 'import json,sys
try:
 d=json.load(sys.stdin)
 keys=("changed","excluded","unchanged","removed","active_notes","chunks","pending_vectors","pending_tombstones","source_drift_count")
 print(" ".join(f"{k}={int(d[k])}" for k in keys if isinstance(d.get(k),int)))
except Exception:
 pass')
  [[ -n "$summary" ]] && printf 'refresh %s exit=%s\n' "$summary" "$status" >>"$LOG"
fi
if [[ $status -ne 0 ]]; then
  printf 'obsidian retrieval refresh failed (class=exit_%s)\n' "$status" >&2
  exit "$status"
fi
