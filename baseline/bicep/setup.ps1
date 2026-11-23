# Instance setup for the baseline scale set (ADR-001). Delivered as VMSS customData and run by
# the Custom Script Extension on every instance, including after a reimage or automatic repair.
# Idempotent: safe to run again.
$ErrorActionPreference = 'Stop'
$logDir = 'F:\iislogs'
New-Item -ItemType Directory -Force C:\fleet | Out-Null
Start-Transcript -Path C:\fleet\setup.log -Append

# 1. Data disk: initialize the raw LUN 0 disk as F: ("logs"). IIS logs live here, not on C:.
if (-not (Get-Volume -DriveLetter F -ErrorAction SilentlyContinue)) {
    $raw = Get-Disk | Where-Object PartitionStyle -eq 'RAW' | Select-Object -First 1
    if (-not $raw) { throw 'No raw data disk found for F:' }
    Initialize-Disk -Number $raw.Number -PartitionStyle GPT -PassThru |
        New-Partition -DriveLetter F -UseMaximumSize |
        Format-Volume -FileSystem NTFS -NewFileSystemLabel logs -Confirm:$false | Out-Null
}
New-Item -ItemType Directory -Force $logDir | Out-Null

# 2. IIS, with site logs moved to the data disk.
Install-WindowsFeature Web-Server -IncludeManagementTools | Out-Null
Import-Module WebAdministration
Set-WebConfigurationProperty -PSPath 'MACHINE/WEBROOT/APPHOST' `
    -Filter 'system.applicationHost/sites/siteDefaults/logFile' -Name directory -Value $logDir

# 3. Log retention script (copied from baseline/log_retention.ps1 at deploy time).
$retention = @'
__RETENTION_SCRIPT__
'@
Set-Content -Path C:\fleet\log_retention.ps1 -Value $retention -Encoding UTF8

# 4. Hourly scheduled task, running as SYSTEM, that really deletes (-Apply).
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument (
    '-NoProfile -ExecutionPolicy Bypass -Command "& C:\fleet\log_retention.ps1 ' +
    "-Days __RETENTION_DAYS__ -LogRoot $logDir -Apply *>> C:\fleet\log_retention.log" + '"')
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).Date -RepetitionInterval (New-TimeSpan -Hours 1)
Register-ScheduledTask -TaskName 'Fleet-LogRetention' -Action $action -Trigger $trigger `
    -User 'SYSTEM' -RunLevel Highest -Force | Out-Null

Write-Output 'Fleet baseline setup complete.'
Stop-Transcript
