#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Phase 6.1A · Standalone retrofit script for already-installed 0.6.x machines.

    Background:
        Auto Update (manifest.json + zip) only swaps EmployeeAgent.exe
        contents in C:\Program Files\EmployeeAgent\. It DOES NOT re-run
        the Inno post-install.ps1, so the scheduled task config from the
        original install (e.g. 0.6.0's single AtLogOn trigger,
        RestartCount=3, no recovery task) stays unchanged.

        Running this script as Administrator brings an existing 0.6.x
        machine up to 6.1A reliability config WITHOUT needing to re-run
        the full installer:

          A. Main task: AtLogOn + every-10min repeat, RestartCount=999/5min
          B. New recovery task 'EmployeeAgentRecovery' (SYSTEM, every 5min)
          E. Windows Defender exclusions (best-effort)

        Safe to re-run: each step uses -Force / idempotent registration.

.USAGE
        Right-click PowerShell → Run as Administrator, then:

            Set-ExecutionPolicy Bypass -Scope Process -Force
            .\apply_6_1a_hardening.ps1

        Verbose log goes to stdout. The recovery script is written to
        C:\ProgramData\EmployeeAgent\recovery.ps1.

.NOTES
        ASCII-only for Windows PowerShell 5.1 locale safety.
#>
param(
    [string]$InstallDir   = (Join-Path $env:ProgramFiles "EmployeeAgent"),
    [string]$DataDir      = (Join-Path $env:ProgramData "EmployeeAgent"),
    [string]$TaskName     = "EmployeeAgent",
    [string]$RecoveryTask = "EmployeeAgentRecovery"
)

$ErrorActionPreference = "Stop"

Write-Host "==> Phase 6.1A retrofit: applying hardening to existing install" -ForegroundColor Cyan
Write-Host "    InstallDir : $InstallDir"
Write-Host "    DataDir    : $DataDir"

# -- Sanity: agent must already be installed -------------------------------
$exePath = Join-Path $InstallDir "EmployeeAgent.exe"
if (-not (Test-Path $exePath)) {
    throw "EmployeeAgent.exe not found at $exePath -- this script retrofits an existing install. Use EmployeeAgentSetup_v0.6.1.exe for fresh installs."
}
if (-not (Test-Path $DataDir)) {
    Write-Host "  - DataDir missing, creating: $DataDir"
    New-Item -ItemType Directory -Force -Path $DataDir | Out-Null
}
New-Item -ItemType Directory -Force -Path (Join-Path $DataDir "logs")  | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $DataDir "state") | Out-Null

# ============================================================================
# Phase 6.1A · E: Windows Defender exclusions (best-effort)
# ============================================================================
Write-Host "  - Adding Windows Defender exclusions (best-effort)"
try {
    Add-MpPreference -ExclusionPath $InstallDir -ErrorAction Stop
    Add-MpPreference -ExclusionPath $DataDir    -ErrorAction Stop
    Add-MpPreference -ExclusionProcess "EmployeeAgent.exe" -ErrorAction Stop
    Write-Host "    Defender exclusions applied"
} catch {
    Write-Host "    Defender exclusion failed (likely 3rd-party AV or policy): $($_.Exception.Message)"
}

# ============================================================================
# Phase 6.1A · A: Re-register main task with reinforced settings
# ============================================================================
Write-Host "  - Re-registering main scheduled task '$TaskName' with 6.1A settings"

# Stop any running instance + the task so we can replace cleanly.
$proc = Get-Process -Name "EmployeeAgent" -ErrorAction SilentlyContinue
if ($proc) {
    Write-Host ("    - Stopping {0} running EmployeeAgent process(es) before re-registering task" -f $proc.Count)
    try { Stop-Process -Name "EmployeeAgent" -Force -ErrorAction SilentlyContinue } catch {}
    Start-Sleep -Seconds 2
}
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    try {
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    } catch {}
}

$action = New-ScheduledTaskAction -Execute $exePath -WorkingDirectory $InstallDir

# A1: AtLogOn + Once-with-RepetitionPattern every 10 min
$logonTrigger = New-ScheduledTaskTrigger -AtLogOn
$logonTrigger.Delay = "PT30S"
# NOTE: RepetitionDuration uses 9999 days (~27y) — Task Scheduler stores this
# internally as int32 seconds. 9999d*86400 = 8.64e8 fits in int32; the
# previously-used FromDays(36500) overflowed and produced
# "Duration:P36500D out of range" at Register-ScheduledTask. 27 years is
# more than this software's expected lifetime.
$repeatTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Minutes 10) `
    -RepetitionDuration (New-TimeSpan -Days 9999)
$triggers  = @($logonTrigger, $repeatTrigger)

$principal = New-ScheduledTaskPrincipal -GroupId "Users" -RunLevel Limited

# A2: RestartCount 999, Interval 5min
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 5) `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0)

Register-ScheduledTask -TaskName $TaskName `
    -Action $action -Trigger $triggers -Principal $principal -Settings $settings `
    -Description "Phase 6.1A retrofit: multi-trigger + RestartCount=999." `
    -Force | Out-Null
Write-Host "    Main task re-registered"

# ============================================================================
# Phase 6.1A · B: Write recovery.ps1 and register recovery task
# ============================================================================
# Inline the recovery.ps1 contents here so this retrofit script is fully
# self-contained (no need to ship recovery.ps1 alongside).
$recoveryScript = Join-Path $DataDir "recovery.ps1"
$recoveryBody = @'
# EmployeeAgent External Recovery Watchdog (Phase 6.1A)
# Runs every 5 minutes as NT AUTHORITY\SYSTEM.
# See client/installer/recovery.ps1 in source for full docs.

$ErrorActionPreference = "Continue"

$InstallDir    = Join-Path $env:ProgramFiles  "EmployeeAgent"
$DataDir       = Join-Path $env:ProgramData   "EmployeeAgent"
$TaskName      = "EmployeeAgent"
$ExePath       = Join-Path $InstallDir "EmployeeAgent.exe"
$LogDir        = Join-Path $DataDir    "logs"
$StateDir      = Join-Path $DataDir    "state"
$LogPath       = Join-Path $LogDir     "recovery.log"
$LogRotated    = Join-Path $LogDir     "recovery.log.1"
$HeartbeatPath = Join-Path $StateDir   "heartbeat.json"
$ExeMissFlag   = Join-Path $StateDir   "exe_missing.flag"
$MaxHeartbeatAgeSeconds = 600
$LogRotateBytes         = 1MB

function Write-Recovery {
    param([string]$Level, [string]$Message)
    $line = "{0} [{1}] {2}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Level, $Message
    try {
        if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Force -Path $LogDir | Out-Null }
        if (Test-Path $LogPath) {
            $size = (Get-Item $LogPath).Length
            if ($size -gt $LogRotateBytes) {
                try { Move-Item -Path $LogPath -Destination $LogRotated -Force } catch {}
            }
        }
        Add-Content -Path $LogPath -Value $line -Encoding UTF8
    } catch {}
}

try { if (-not (Test-Path $StateDir)) { New-Item -ItemType Directory -Force -Path $StateDir | Out-Null } } catch {}

$actions = @()
$age = 0

if (-not (Test-Path $ExePath)) {
    Write-Recovery "ERROR" "EmployeeAgent.exe missing at $ExePath (suspected AV removal or manual delete)"
    try { Set-Content -Path $ExeMissFlag -Value (Get-Date -Format "yyyy-MM-dd HH:mm:ss") -Encoding ASCII } catch {}
    return
} else {
    if (Test-Path $ExeMissFlag) {
        try { Remove-Item -Path $ExeMissFlag -Force -ErrorAction SilentlyContinue } catch {}
        Write-Recovery "INFO" "EmployeeAgent.exe restored - cleared exe_missing.flag"
    }
}

$task = $null
try { $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop } catch {
    Write-Recovery "ERROR" "Main scheduled task '$TaskName' not registered: $($_.Exception.Message)"
    return
}

if ($task.State -eq "Disabled") {
    Write-Recovery "WARN" "Main task disabled - re-enabling"
    try {
        Enable-ScheduledTask -TaskName $TaskName -ErrorAction Stop | Out-Null
        $actions += "enabled"
    } catch {
        Write-Recovery "ERROR" "Enable-ScheduledTask failed: $($_.Exception.Message)"
    }
}

$proc = Get-Process -Name "EmployeeAgent" -ErrorAction SilentlyContinue
if (-not $proc) {
    Write-Recovery "WARN" "EmployeeAgent process not running - starting task"
    try {
        Start-ScheduledTask -TaskName $TaskName -ErrorAction Stop
        $actions += "started"
    } catch {
        Write-Recovery "ERROR" "Start-ScheduledTask failed: $($_.Exception.Message)"
    }
} else {
    if (Test-Path $HeartbeatPath) {
        try {
            $mtime = (Get-Item $HeartbeatPath).LastWriteTime
            $age = [int]((Get-Date) - $mtime).TotalSeconds
            if ($age -gt $MaxHeartbeatAgeSeconds) {
                Write-Recovery "ERROR" ("heartbeat stale age={0}s threshold={1}s pid={2} - kill + restart" -f $age, $MaxHeartbeatAgeSeconds, $proc.Id)
                try {
                    Stop-Process -Name "EmployeeAgent" -Force -ErrorAction SilentlyContinue
                    Start-Sleep -Seconds 2
                    Start-ScheduledTask -TaskName $TaskName -ErrorAction Stop
                    $actions += "kill+restart(heartbeat_stale)"
                } catch {
                    Write-Recovery "ERROR" "kill+restart failed: $($_.Exception.Message)"
                }
            }
        } catch {
            Write-Recovery "WARN" "heartbeat.json read failed: $($_.Exception.Message)"
        }
    }
}

$procState = if ($proc) { "pid=" + $proc.Id } else { "down" }
$summary = "OK exe=present task={0} proc={1}" -f $task.State, $procState
if ($actions.Count -gt 0) { $summary += " actions=" + ($actions -join ",") }
Write-Recovery "INFO" $summary
'@
Write-Host "  - Writing recovery.ps1 to $recoveryScript"
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($recoveryScript, $recoveryBody, $utf8NoBom)

Write-Host "  - Registering recovery task '$RecoveryTask' (SYSTEM, every 5min)"
$recoveryAction = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument ("-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"{0}`"" -f $recoveryScript)
$rt1 = New-ScheduledTaskTrigger -AtStartup
# See note above re: 9999d vs 36500d int32 overflow.
$rt2 = New-ScheduledTaskTrigger -Once -At (Get-Date).AddSeconds(60) `
    -RepetitionInterval (New-TimeSpan -Minutes 5) `
    -RepetitionDuration (New-TimeSpan -Days 9999)
$rTriggers  = @($rt1, $rt2)
$rPrincipal = New-ScheduledTaskPrincipal -UserId "NT AUTHORITY\SYSTEM" -RunLevel Highest -LogonType ServiceAccount
$rSettings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 4)
Register-ScheduledTask -TaskName $RecoveryTask `
    -Action $recoveryAction -Trigger $rTriggers `
    -Principal $rPrincipal -Settings $rSettings `
    -Description "Phase 6.1A: external watchdog that re-launches EmployeeAgent if killed / disabled / heartbeat stale." `
    -Force | Out-Null
Write-Host "    Recovery task registered"

# ============================================================================
# Final: kick the main task once so the agent comes back up immediately
# ============================================================================
try {
    Start-ScheduledTask -TaskName $TaskName -ErrorAction Stop
    Write-Host "  - Main task started"
} catch {
    Write-Host "  - Start failed (RecoveryTask will retry within 5 min): $($_.Exception.Message)"
}

Write-Host "==> Retrofit complete." -ForegroundColor Green
Write-Host ""
Write-Host "Verify with:"
Write-Host "  (Get-ScheduledTask EmployeeAgent).Triggers.Count                # expect 2"
Write-Host "  (Get-ScheduledTask EmployeeAgent).Settings.RestartCount         # expect 999"
Write-Host "  Get-ScheduledTask EmployeeAgentRecovery                         # expect Ready, SYSTEM"
Write-Host "  Get-MpPreference | Select -Expand ExclusionPath                 # expect EmployeeAgent entries"
Write-Host "  Get-Content C:\ProgramData\EmployeeAgent\logs\recovery.log -Tail 5  # within 5 min"
