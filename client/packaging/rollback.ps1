#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Phase 5.4 · Roll back EmployeeAgent to a previously installed version.
.DESCRIPTION
    Reads %PROGRAMDATA%\EmployeeAgent\updates\rollback\<Version>\ and copies it
    back to %ProgramFiles%\EmployeeAgent\. v1 explicitly does NOT auto-rollback;
    this script is the manual escape hatch when an auto-update went bad.

    Decoupled from the Python client: pure PowerShell + file copy, so it still
    works even if the agent code is broken.
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\rollback.ps1 -Version 0.5.3
.NOTES
    Order: stop scheduled task -> kill any lingering process -> wipe install_dir
    -> copy backup -> restart task. ASCII-only on purpose to parse cleanly under
    Windows PowerShell 5.1 regardless of locale (same convention as install.ps1).
#>
param(
    [Parameter(Mandatory=$true)]
    [string]$Version,
    [string]$InstallDir = (Join-Path $env:ProgramFiles "EmployeeAgent"),
    [string]$DataDir = (Join-Path $env:ProgramData "EmployeeAgent"),
    [string]$TaskName = "EmployeeAgent"
)

$ErrorActionPreference = "Stop"
$BackupDir = Join-Path $DataDir "updates\rollback\$Version"

Write-Host "==> Rolling back EmployeeAgent to v$Version" -ForegroundColor Cyan
Write-Host "    InstallDir : $InstallDir"
Write-Host "    BackupSrc  : $BackupDir"
Write-Host ""

# 1) Validate backup exists
if (-not (Test-Path $BackupDir)) {
    throw "Backup not found: $BackupDir. Available backups:`n" +
          ((Get-ChildItem (Join-Path $DataDir 'updates\rollback') -EA SilentlyContinue |
            ForEach-Object { "  - $($_.Name)" }) -join "`n")
}

# 2) Stop scheduled task (best effort)
Write-Host "  - Stopping scheduled task '$TaskName'"
try { Stop-ScheduledTask -TaskName $TaskName -ErrorAction Stop } catch {}

# 3) Kill any lingering EmployeeAgent process
$procs = Get-Process -Name "EmployeeAgent" -EA SilentlyContinue
if ($procs) {
    Write-Host ("  - Stopping {0} running EmployeeAgent process(es)" -f $procs.Count)
    $procs | Stop-Process -Force -EA SilentlyContinue
    Start-Sleep -Seconds 2
}

# 4) Wipe current InstallDir
if (Test-Path $InstallDir) {
    Write-Host "  - Removing current install: $InstallDir"
    Remove-Item -Path $InstallDir -Recurse -Force
}

# 5) Copy backup → InstallDir
Write-Host "  - Copying backup back to $InstallDir"
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
Copy-Item -Path (Join-Path $BackupDir "*") -Destination $InstallDir -Recurse -Force

# 6) Restart scheduled task
$task = Get-ScheduledTask -TaskName $TaskName -EA SilentlyContinue
if ($task) {
    Write-Host "  - Starting scheduled task"
    Start-ScheduledTask -TaskName $TaskName
} else {
    Write-Host "  - WARN: scheduled task '$TaskName' not registered; restart via Task Scheduler manually" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "==> Rollback complete." -ForegroundColor Green
Write-Host "    Verify via Get-Process EmployeeAgent and Admin UI Device Health."
