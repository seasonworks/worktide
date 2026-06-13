#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Phase 5.5I · Inno Setup pre-uninstall hook.

    Stops + unregisters the scheduled task and kills the running process
    so that Inno Setup can then delete files in $InstallDir without
    SHARING VIOLATION.

    Inno itself removes the program directory; this script intentionally
    *keeps* the data directory ($env:ProgramData\EmployeeAgent) so that
    re-installs (or accidental uninstalls) don't destroy logs / state /
    buffer.db / lifecycle records. Operators wanting a full purge can run
    [client/packaging/uninstall.ps1] -RemoveData manually.

.NOTES
    ASCII-only for Windows PowerShell 5.1 locale safety.
#>
param(
    [string]$TaskName     = "EmployeeAgent",
    [string]$RecoveryTask = "EmployeeAgentRecovery"
)

# Best-effort: never fail uninstall on a hook error
$ErrorActionPreference = "Continue"

Write-Host "==> Phase 6.1A pre-uninstall: stopping agent + recovery task" -ForegroundColor Cyan

# 1a) Unregister recovery task FIRST so its 5-minute tick can't relaunch the
#     main task mid-uninstall (small window but real).
$rec = Get-ScheduledTask -TaskName $RecoveryTask -ErrorAction SilentlyContinue
if ($rec) {
    Write-Host "  - Unregistering recovery task '$RecoveryTask'"
    try {
        Stop-ScheduledTask -TaskName $RecoveryTask -ErrorAction SilentlyContinue
    } catch {}
    Unregister-ScheduledTask -TaskName $RecoveryTask -Confirm:$false -ErrorAction SilentlyContinue
} else {
    Write-Host "  - Recovery task '$RecoveryTask' not found, skipping"
}

# 1b) Unregister main scheduled task (skip if absent)
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($task) {
    Write-Host "  - Unregistering scheduled task '$TaskName'"
    try {
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    } catch {}
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
} else {
    Write-Host "  - Scheduled task '$TaskName' not found, skipping"
}

# 2) Stop running process(es); give the OS a moment to release the EXE handle
$procs = Get-Process -Name "EmployeeAgent" -ErrorAction SilentlyContinue
if ($procs) {
    Write-Host ("  - Stopping {0} running EmployeeAgent process(es)" -f $procs.Count)
    $procs | Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
}

# 3) Scrub legacy autostart leftovers (defense-in-depth; should be no-op here
#    because we install via scheduled task, not Run/Startup; but Phase 5.1 R5
#    policy is: 'uninstall' must mean 'this software will not auto-start again').
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

Write-Host "==> Pre-uninstall hook done. Inno Setup will now remove program files." -ForegroundColor Green
Write-Host "    NOTE: ProgramData\EmployeeAgent is preserved (logs / config / state)."
