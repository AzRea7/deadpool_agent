# Chaos: stop an IIS app pool so the health probe fails (scenario RB-003).
# Runs ON an instance via Run Command:
#
#   az vmss run-command invoke -g rg-fleet-health -n vmss-fleet --instance-id 1 \
#     --command-id RunPowerShellScript --scripts @chaos/stop_pool.ps1
#   ... --parameters Restore=true            # start it again
#
# Note: automatic instance repairs (baseline) will replace the instance if the probe stays
# down past the repair grace period. That is the baseline working; for agent demos, restore
# the pool or suspend repairs first.
param(
    [string]$PoolName = 'DefaultAppPool',
    [switch]$Restore
)
$ErrorActionPreference = 'Stop'
Import-Module WebAdministration

if ($Restore) {
    Start-WebAppPool -Name $PoolName
} else {
    Stop-WebAppPool -Name $PoolName
}
Start-Sleep -Seconds 2
$state = (Get-WebAppPoolState -Name $PoolName).Value
Write-Output "App pool $PoolName is now $state."
