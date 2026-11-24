#!/usr/bin/env bash
# Baseline Apache log retention for Linux hosts (ADR-001): deletes Apache logs older than DAYS.
# No agent, no LLM. Runs hourly from a systemd timer installed by baseline/bicep/cloud-init.yaml.
#
# Dry run by default: without --apply it only lists what it would delete.
# Deletes rotated logs (access.log.3, error.log.12.gz, ...) and stale *.log files that no
# process has open. A log Apache still holds open is never deleted: removing it frees no
# space until Apache reopens its logs (RB-010), so that is logrotate's job, not this script's.
set -euo pipefail

DAYS=7
LOG_ROOT=/var/log/apache2
APPLY=0
while [ $# -gt 0 ]; do
  case "$1" in
    --days) DAYS=$2; shift 2 ;;
    --log-root) LOG_ROOT=$2; shift 2 ;;
    --apply) APPLY=1; shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

if [ ! -d "$LOG_ROOT" ]; then
  echo "Log root $LOG_ROOT not found, nothing to do."
  exit 0
fi

candidates=()
while IFS= read -r -d '' f; do
  case "$f" in
    *.log)  # current-style name: only if nothing has it open
      if command -v fuser >/dev/null && fuser -s "$f" 2>/dev/null; then continue; fi ;;
  esac
  candidates+=("$f")
done < <(find "$LOG_ROOT" -type f \( -name '*.log' -o -name '*.log.[0-9]*' \) -mtime +"$DAYS" -print0)

bytes=0
for f in "${candidates[@]}"; do bytes=$((bytes + $(stat -c %s "$f"))); done
mode=DRY-RUN; [ "$APPLY" -eq 1 ] && mode=APPLY
mb=$(awk -v b="$bytes" 'BEGIN { printf "%.1f", b / 1048576 }')
printf '[%s] %d files older than %d days, %s MB under %s\n' \
  "$mode" "${#candidates[@]}" "$DAYS" "$mb" "$LOG_ROOT"

for f in "${candidates[@]}"; do
  if [ "$APPLY" -eq 1 ]; then rm -f -- "$f"; else echo "would delete: $f"; fi
done
