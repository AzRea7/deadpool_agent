# Baseline IIS log retention (ADR-001): deletes IIS logs older than $Days.
# No agent, no LLM. Runs as a scheduled task on each scale-set instance.
# -WhatIf stays ON until this has been tested on the test scale set; remove it only after
# confirming the listed files are the expected ones.
param(
    [int]$Days = 7,
    [string]$LogRoot = 'C:\inetpub\logs\LogFiles'
)

Get-ChildItem $LogRoot -Recurse -Filter *.log |
    Where-Object LastWriteTime -lt (Get-Date).AddDays(-$Days) |
    Remove-Item -WhatIf
