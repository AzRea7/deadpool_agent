#!/usr/bin/env bash
# Chaos: fill / on one Linux instance so the detector sees a disk filling (RB-009/RB-010).
# Runs ON an instance via Run Command:
#
#   az vmss run-command invoke -g rg-fleet-health -n vmss-fleet-lx --instance-id 0 \
#     --command-id RunShellScript --scripts @chaos/fill_disk.sh --parameters 92 0
#   ... --parameters cleanup                 # remove everything this script wrote
#
# Arguments: TARGET_USED_PERCENT (default 92) and GROW_MINUTES (default 0). GROW_MINUTES > 0
# fills one step per minute so the 6h growth-slope rule can fire before the 90% line.
# Writes only under /var/chaos, and never past 97% used.
set -euo pipefail
DIR=/var/chaos
HARD_STOP=97

used_pct() { df --output=pcent / | tail -1 | tr -dc '0-9'; }

if [ "${1:-}" = cleanup ]; then
  rm -rf "$DIR"
  echo "Cleaned up $DIR. / used = $(used_pct)%"
  exit 0
fi

TARGET=${1:-92}
GROW_MINUTES=${2:-0}
if [ "$TARGET" -gt "$HARD_STOP" ]; then echo "Target above the $HARD_STOP% safety stop" >&2; exit 1; fi

mkdir -p "$DIR"
size_kb=$(df --output=size -k / | tail -1 | tr -dc '0-9')
used_kb=$(df --output=used -k / | tail -1 | tr -dc '0-9')
need_kb=$((size_kb * TARGET / 100 - used_kb))
if [ "$need_kb" -le 0 ]; then echo "/ already at $(used_pct)%, nothing to do."; exit 0; fi

steps=$(( GROW_MINUTES > 0 ? GROW_MINUTES : 1 ))
chunk_kb=$(( (need_kb + steps - 1) / steps ))
echo "/ used = $(used_pct)%. Writing $((need_kb / 1024)) MB in $steps step(s)."
for ((i = 0; i < steps; i++)); do
  [ "$(used_pct)" -ge "$TARGET" ] && break
  fallocate -l "${chunk_kb}K" "$DIR/fill_$(printf '%04d' $i).bin"
  if [ "$GROW_MINUTES" -gt 0 ] && [ "$i" -lt $((steps - 1)) ]; then sleep 60; fi
done
echo "Done. / used = $(used_pct)%. Undo with: cleanup"
