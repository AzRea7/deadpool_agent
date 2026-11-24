#!/usr/bin/env bash
# Phase 0 proof for Linux: the baseline alone keeps a filled log disk under threshold.
# Runs ON an instance via Run Command:
#
#   az vmss run-command invoke -g rg-fleet-health -n vmss-fleet-lx --instance-id 0 \
#     --command-id RunShellScript --scripts @baseline/test_fill_disk.sh --parameters fill
#   (wait ~15 min: the disk alert should fire)
#   ... same command with --parameters verify
#
# fill:   writes backdated rotated Apache logs until /data drops below TARGET_FREE% free,
#         plus two recent logs that retention must keep.
# verify: runs the fleet-log-retention service, then checks free space recovered, old logs
#         are gone and recent logs survived. Prints PASS or FAIL.
set -euo pipefail
STEP=${1:-fill}
TARGET_FREE=10
THRESHOLD=15
DIR=/data/apache-logs

free_pct() { df --output=pcent /data | tail -1 | tr -dc '0-9' | awk '{print 100 - $1}'; }

if [ "$STEP" = fill ]; then
  echo "Before fill: /data free = $(free_pct)%"
  i=0
  while [ "$(free_pct)" -gt "$TARGET_FREE" ]; do
    f="$DIR/access.log.fill$(printf '%03d' $i)"
    fallocate -l 256M "$f"
    touch -d '10 days ago' "$f"
    i=$((i + 1))
  done
  for n in 1 2; do fallocate -l 10M "$DIR/access.log.recent$n"; done
  echo "Wrote $i old 256 MB logs + 2 recent logs. /data free = $(free_pct)%"
  exit 0
fi

before=$(free_pct)
systemctl start fleet-log-retention.service   # oneshot: returns when the run finishes
after=$(free_pct)
old_left=$(find "$DIR" -name 'access.log.fill*' | wc -l)
recent_left=$(find "$DIR" -name 'access.log.recent*' | wc -l)
journalctl -u fleet-log-retention.service -n 3 --no-pager
echo "/data free before = $before%, after = $after%, old left = $old_left, recent left = $recent_left"
if [ "$after" -ge "$THRESHOLD" ] && [ "$old_left" -eq 0 ] && [ "$recent_left" -eq 2 ]; then
  echo "PASS: baseline brought the disk back above threshold and kept recent logs"
else
  echo "FAIL"
fi
rm -f "$DIR"/access.log.recent*   # tidy up test files
