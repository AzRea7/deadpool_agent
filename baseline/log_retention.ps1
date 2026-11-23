# Baseline IIS log retention (ADR-001): deletes IIS logs older than $Days.
# No agent, no LLM. Runs as a scheduled task on each scale-set instance.
#
# Dry run by default: without -Apply it only lists what it would delete (Remove-Item -WhatIf).
# The scheduled task installed by baseline/bicep/setup.ps1 passes -Apply.
# Only *.log files under $LogRoot are ever touched.
param(
    [int]$Days = 7,
    [string]$LogRoot = 'C:\inetpub\logs\LogFiles',
    [switch]$Apply
)

$ErrorActionPreference = 'Stop'
if (-not (Test-Path $LogRoot)) {
    Write-Output "Log root $LogRoot not found, nothing to do."
    return
}

$cutoff = (Get-Date).AddDays(-$Days)
$old = @(Get-ChildItem $LogRoot -Recurse -File -Filter *.log |
    Where-Object LastWriteTime -lt $cutoff)
$bytes = ($old | Measure-Object Length -Sum).Sum
$mode = if ($Apply) { 'APPLY' } else { 'DRY-RUN' }
Write-Output ("[{0}] {1} files older than {2} days, {3:N1} MB under {4}" -f `
    $mode, $old.Count, $Days, ($bytes / 1MB), $LogRoot)

if ($Apply) {
    $old | Remove-Item -Force
} else {
    $old | Remove-Item -WhatIf
}
