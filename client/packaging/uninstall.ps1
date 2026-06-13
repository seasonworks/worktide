#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Uninstall EmployeeAgent: unregister task, stop process, remove program dir.
.DESCRIPTION
    Keeps the data dir (config/logs) by default; pass -RemoveData to delete it too.
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\uninstall.ps1
    powershell -ExecutionPolicy Bypass -File .\uninstall.ps1 -RemoveData
.NOTES
    ASCII-only so it parses correctly under Windows PowerShell 5.1 in any locale.
#>
param(
    [string]$InstallDir = (Join-Path $env:ProgramFiles "EmployeeAgent"),
    [string]$DataDir = (Join-Path $env:ProgramData "EmployeeAgent"),
    [string]$TaskName = "EmployeeAgent",
    [switch]$RemoveData
)

$ErrorActionPreference = "Stop"

Write-Host "==> Uninstalling EmployeeAgent" -ForegroundColor Cyan

# 1) Unregister scheduled task (skip if absent)
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($task) {
    Write-Host "  - Unregistering scheduled task '$TaskName'"
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
} else {
    Write-Host "  - Scheduled task '$TaskName' not found, skipping"
}

# 2) Stop the running process if any
$proc = Get-Process -Name "EmployeeAgent" -ErrorAction SilentlyContinue
if ($proc) {
    Write-Host "  - Stopping running EmployeeAgent process"
    $proc | Stop-Process -Force
}

# 3) Remove program directory
if (Test-Path $InstallDir) {
    Write-Host "  - Removing program dir $InstallDir"
    Remove-Item -Path $InstallDir -Recurse -Force
}

# 4) Optionally remove data directory
if ($RemoveData -and (Test-Path $DataDir)) {
    Write-Host "  - Removing data dir $DataDir"
    Remove-Item -Path $DataDir -Recurse -Force
} elseif (Test-Path $DataDir) {
    Write-Host "  - Keeping data dir $DataDir (pass -RemoveData to delete)"
}

# 5) Phase 5.1 R5: scrub potential autostart leftovers in HKLM/HKCU\Run and Startup folders.
# We don't install into those locations, but cover the case where an older version or external
# tool seeded an entry — we want "uninstall" to mean "this software won't autostart again",
# not "the named entries we know about are gone".
$runKeys = @(
    'HKLM:\Software\Microsoft\Windows\CurrentVersion\Run',
    'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
)
foreach ($key in $runKeys) {
    if (Test-Path $key) {
        $prop = Get-ItemProperty -Path $key -Name "EmployeeAgent" -ErrorAction SilentlyContinue
        if ($prop) {
            Write-Host "  - Removing leftover Run entry: $key\EmployeeAgent"
            Remove-ItemProperty -Path $key -Name "EmployeeAgent" -Force -ErrorAction SilentlyContinue
        }
    }
}
$startupShortcuts = @(
    (Join-Path ([Environment]::GetFolderPath('Startup'))       "EmployeeAgent.lnk"),
    (Join-Path ([Environment]::GetFolderPath('CommonStartup')) "EmployeeAgent.lnk")
)
foreach ($lnk in $startupShortcuts) {
    if (Test-Path $lnk) {
        Write-Host "  - Removing leftover Startup shortcut: $lnk"
        Remove-Item -Path $lnk -Force -ErrorAction SilentlyContinue
    }
}

Write-Host "==> Uninstall complete." -ForegroundColor Green
