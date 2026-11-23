# Phase 0 proof: the baseline alone keeps a filled log disk under threshold (no agent).
# Runs ON an instance via Run Command:
#
#   az vmss run-command invoke -g rg-fleet-health -n vmss-fleet --instance-id 0 \
#     --command-id RunPowerShellScript --scripts @baseline/test_fill_disk.ps1 \
#     --parameters Step=fill
#   (wait ~15 min: the disk-free alert should fire)
#   ... same command with Step=verify
#
# fill:   writes backdated "old" IIS logs until F: free space drops below $TargetFreePercent,
#         plus two recent logs that retention must keep.
# verify: runs the Fleet-LogRetention scheduled task, then checks free space recovered,
#         old logs are gone and recent logs survived. Prints PASS or FAIL.
param(
    [ValidateSet('fill', 'verify')] [string]$Step = 'fill',
    [int]$TargetFreePercent = 10,
    [int]$ThresholdPercent = 15
)
$ErrorActionPreference = 'Stop'
$dir = 'F:\iislogs\W3SVC1'
New-Item -ItemType Directory -Force $dir | Out-Null

function Get-FreePercent {
    $v = Get-Volume -DriveLetter F
    [math]::Round(100 * $v.SizeRemaining / $v.Size, 1)
}

if ($Step -eq 'fill') {
    Write-Output "Before fill: F: free = $(Get-FreePercent)%"
    $i = 0
    while ((Get-FreePercent) -gt $TargetFreePercent) {
        $f = Join-Path $dir ("u_ex_fill_{0:D3}.log" -f $i)
        fsutil file createnew $f (256MB) | Out-Null
        (Get-Item $f).LastWriteTime = (Get-Date).AddDays(-10)
        $i++
    }
    foreach ($n in 1..2) {
        $f = Join-Path $dir "u_ex_recent_$n.log"
        fsutil file createnew $f (10MB) | Out-Null
    }
    Write-Output "Wrote $i old 256 MB logs + 2 recent logs. F: free = $(Get-FreePercent)%"
    return
}

$before = Get-FreePercent
Start-ScheduledTask -TaskName 'Fleet-LogRetention'
do { Start-Sleep 5 } while ((Get-ScheduledTask -TaskName 'Fleet-LogRetention').State -eq 'Running')
$after = Get-FreePercent
$oldLeft = @(Get-ChildItem $dir -Filter 'u_ex_fill_*.log').Count
$recentLeft = @(Get-ChildItem $dir -Filter 'u_ex_recent_*.log').Count
Get-Content C:\fleet\log_retention.log -Tail 3
Write-Output "F: free before = $before%, after = $after%, old left = $oldLeft, recent left = $recentLeft"
if ($after -ge $ThresholdPercent -and $oldLeft -eq 0 -and $recentLeft -eq 2) {
    Write-Output 'PASS: baseline brought the disk back above threshold and kept recent logs'
} else {
    Write-Output 'FAIL'
}
Remove-Item (Join-Path $dir 'u_ex_recent_*.log') -Force  # tidy up test files
