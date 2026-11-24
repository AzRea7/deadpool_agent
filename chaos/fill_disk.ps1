# Chaos: fill C: on one instance so the detector sees a disk filling (scenario RB-001/RB-002).
# Runs ON an instance via Run Command:
#
#   az vmss run-command invoke -g rg-fleet-health -n vmss-fleet --instance-id 0 \
#     --command-id RunPowerShellScript --scripts @chaos/fill_disk.ps1 \
#     --parameters TargetUsedPercent=92
#   ... --parameters Cleanup=true            # remove everything this script wrote
#
# -GrowMinutes > 0 fills gradually (one step per minute) so the 6h growth-slope rule can fire
# before the 90% line. Writes only under C:\chaos, and never past 97% used.
param(
    [int]$TargetUsedPercent = 92,
    [int]$GrowMinutes = 0,
    [switch]$Cleanup
)
$ErrorActionPreference = 'Stop'
$dir = 'C:\chaos'
$hardStop = 97

function Get-UsedPercent {
    $v = Get-Volume -DriveLetter C
    [math]::Round(100 * (1 - $v.SizeRemaining / $v.Size), 1)
}

if ($Cleanup) {
    if (Test-Path $dir) { Remove-Item $dir -Recurse -Force }
    Write-Output "Cleaned up $dir. C: used = $(Get-UsedPercent)%"
    return
}
if ($TargetUsedPercent -gt $hardStop) { throw "Target above the $hardStop% safety stop" }

New-Item -ItemType Directory -Force $dir | Out-Null
$v = Get-Volume -DriveLetter C
$needBytes = [int64]($v.Size * $TargetUsedPercent / 100) - ($v.Size - $v.SizeRemaining)
if ($needBytes -le 0) {
    Write-Output "C: already at $(Get-UsedPercent)%, nothing to do."
    return
}

$steps = [math]::Max(1, $GrowMinutes)
$chunk = [int64][math]::Ceiling($needBytes / $steps)
Write-Output "C: used = $(Get-UsedPercent)%. Writing $([math]::Round($needBytes / 1GB, 2)) GB in $steps step(s)."
for ($i = 0; $i -lt $steps; $i++) {
    if ((Get-UsedPercent) -ge $TargetUsedPercent) { break }
    fsutil file createnew (Join-Path $dir ("fill_{0:D4}.bin" -f $i)) $chunk | Out-Null
    if ($GrowMinutes -gt 0 -and $i -lt $steps - 1) { Start-Sleep -Seconds 60 }
}
Write-Output "Done. C: used = $(Get-UsedPercent)%. Undo with -Cleanup."
