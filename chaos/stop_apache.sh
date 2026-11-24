#!/usr/bin/env bash
# Chaos: stop Apache on one Linux instance so the health probe fails (RB-011).
# Runs ON an instance via Run Command:
#
#   az vmss run-command invoke -g rg-fleet-health -n vmss-fleet-lx --instance-id 1 \
#     --command-id RunShellScript --scripts @chaos/stop_apache.sh
#   ... --parameters restore                 # start it again
#
# Note: automatic instance repairs (baseline) replace the instance if the probe stays down
# past the repair grace period. For agent demos, restore Apache or suspend repairs first.
set -euo pipefail
UNIT=apache2

if [ "${1:-}" = restore ]; then
  systemctl start "$UNIT"
else
  systemctl stop "$UNIT"
fi
sleep 2
echo "$UNIT is now $(systemctl is-active "$UNIT" || true)."
