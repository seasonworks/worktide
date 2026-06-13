#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Phase 6.1A · Inno Setup post-install hook (Agent Reliability Phase 1).

    Files have already been extracted to $InstallDir by Inno Setup; this
    script does the runtime-side work:

      1. Create data dir + permissions + default config (unchanged from 6.0D)
      2. Best-effort Windows Defender exclusions (NEW · E)
      3. Register the main scheduled task 'EmployeeAgent' with:
         - AtLogOn trigger (unchanged) — 30s delay
         - Every-10-minutes repeat trigger (NEW · A1) — mutex de-dupes
         - RestartCount 999 / RestartInterval 5min (NEW · A2)
      4. Write recovery.ps1 to ProgramData (NEW · B1)
      5. Register external recovery task 'EmployeeAgentRecovery' running
         as NT AUTHORITY\SYSTEM every 5 minutes (NEW · B2)
      6. First-boot Start-ScheduledTask with 3x retry @ 5s (NEW · C1)

    All hardening operations are best-effort: a failure in step 2/4/5/6
    never blocks step 3 (which is the absolute minimum for the agent to
    run at all). RecoveryTask will pick up from any partial state on its
    next 5-minute tick.

.NOTES
    ASCII-only for Windows PowerShell 5.1 locale safety.
#>
param(
    [string]$InstallDir   = (Join-Path $env:ProgramFiles "EmployeeAgent"),
    [string]$DataDir      = (Join-Path $env:ProgramData "EmployeeAgent"),
    [string]$TaskName     = "EmployeeAgent",
    [string]$RecoveryTask = "EmployeeAgentRecovery",
    # Phase 6.4A: Inno writes the entered employee name to {tmp}\employee_name.txt
    # (UTF-8 no BOM) and passes the path here. Optional — manual reruns can omit.
    [string]$EmployeeNameFile = ""
)

$ErrorActionPreference = "Stop"

Write-Host "==> Phase 6.1A post-install: configuring runtime" -ForegroundColor Cyan

# -- Sanity: files were extracted ------------------------------------------
$exePath = Join-Path $InstallDir "EmployeeAgent.exe"
if (-not (Test-Path $exePath)) {
    throw "Inno extracted nothing: $exePath missing"
}

# -- Create ProgramData + logs dir -----------------------------------------
Write-Host "  - Creating data dir $DataDir and setting permissions"
New-Item -ItemType Directory -Force -Path $DataDir | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $DataDir "logs")  | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $DataDir "state") | Out-Null
# BUILTIN\Users (locale-independent) gets inherit + Modify on data dir
icacls $DataDir /grant "*S-1-5-32-545:(OI)(CI)M" /T | Out-Null

# -- Default config (only if absent — never overwrite admin's config) ------
$configPath = Join-Path $DataDir "config.json"
if (-not (Test-Path $configPath)) {
    Write-Host "  - Writing default config.json"
    $defaultConfig = @'
{
  "server_url": "https://api.example.com",
  "api_path": "/api/v1/activity/report",
  "windows_api_path": "/api/v1/windows/report",
  "report_interval_seconds": 30,
  "request_timeout_seconds": 10,
  "employee_name": "",
  "screenshot": {
    "enabled": false,
    "interval_seconds": 300
  },
  "window": {
    "enabled": true,
    "sample_interval_seconds": 2,
    "idle_threshold_seconds": 60,
    "flush_interval_seconds": 15,
    "title_max_length": 200,
    "upload_interval_seconds": 30,
    "upload_batch_size": 500,
    "upload_backoff_seconds": [5, 30, 120, 300],
    "upload_max_attempts": 5,
    "buffer_max_rows": 100000,
    "buffer_retention_hours": 24,
    "buffer_path": ""
  }
}
'@
    # Force UTF-8 *without BOM*: PS 5.1 Set-Content -Encoding UTF8 writes BOM,
    # which crashes Python json.load on startup.
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($configPath, $defaultConfig, $utf8NoBom)
} else {
    Write-Host "  - config.json already exists, keeping it"
}

# ============================================================================
# Phase 6.4A · Merge employee_name from Inno wizard into config.json
#
# Rules:
#   1. If $EmployeeNameFile is empty / missing / empty content -> no change
#      (manual rerun without wizard, or empty wizard submission)
#   2. If config.json's employee_name is already non-empty -> NEVER overwrite
#      (admin may have edited it via the dashboard; auto-update reruns must
#       not clobber that)
#   3. Otherwise, set employee_name to the wizard value
# Always write UTF-8 without BOM (Python json.load chokes on BOM).
# ============================================================================
$wizardName = ""
if ($EmployeeNameFile -and (Test-Path $EmployeeNameFile)) {
    try {
        $wizardName = (Get-Content -Path $EmployeeNameFile -Raw -Encoding UTF8).Trim()
    } catch {
        Write-Host "  - Failed to read employee_name file: $($_.Exception.Message)"
        $wizardName = ""
    }
}

if ($wizardName) {
    try {
        $cfg = Get-Content -Path $configPath -Raw -Encoding UTF8 | ConvertFrom-Json
        $existingName = ""
        if ($cfg.PSObject.Properties.Name -contains 'employee_name') {
            $existingName = ("" + $cfg.employee_name).Trim()
        }
        if ($existingName) {
            Write-Host "  - employee_name already set to '$existingName', NOT overwriting (Phase 6.4A guard)"
        } else {
            Write-Host "  - Setting employee_name='$wizardName' from installer wizard"
            $cfg | Add-Member -NotePropertyName employee_name -NotePropertyValue $wizardName -Force
            $json = $cfg | ConvertTo-Json -Depth 10
            $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
            [System.IO.File]::WriteAllText($configPath, $json, $utf8NoBom)
        }
    } catch {
        Write-Host "  - employee_name merge failed (non-fatal): $($_.Exception.Message)"
    }
} else {
    Write-Host "  - No employee_name from wizard (auto-update / manual rerun)"
}

# ============================================================================
# Phase 6.1A · E: Windows Defender exclusions (best-effort)
# ============================================================================
Write-Host "  - Adding Windows Defender exclusions (best-effort)"
try {
    Add-MpPreference -ExclusionPath $InstallDir -ErrorAction Stop
    Add-MpPreference -ExclusionPath $DataDir    -ErrorAction Stop
    Add-MpPreference -ExclusionProcess "EmployeeAgent.exe" -ErrorAction Stop
    Write-Host "    Defender exclusions applied: $InstallDir, $DataDir, EmployeeAgent.exe"
} catch {
    # GPO-controlled environments / 3rd-party AV / missing Defender module
    # all land here — exclusion failure must not block install. RecoveryTask
    # will detect AV deletion via state\exe_missing.flag in that case.
    Write-Host "    Defender exclusion failed (likely 3rd-party AV or policy): $($_.Exception.Message)"
}

# ============================================================================
# Phase 6.1A · A: Main scheduled task with reinforced settings
# ============================================================================
Write-Host "  - Registering main scheduled task '$TaskName' (with 10min repeat)"
$action = New-ScheduledTaskAction -Execute $exePath -WorkingDirectory $InstallDir

# A1 · Multi-trigger: AtLogOn (existing) + Once-with-RepetitionPattern every 10 min
$logonTrigger = New-ScheduledTaskTrigger -AtLogOn
$logonTrigger.Delay = "PT30S"

# RepetitionPattern needs a "Once at <some time>" base. Use Get-Date so the
# pattern starts immediately. RepetitionDuration uses 9999 days (~27 years).
# NOTE: Task Scheduler stores duration as int32 seconds; 36500 days overflows
# (3.15e9 > 2.15e9), causing "Duration:P36500D out of range". 9999*86400 fits.
# 27 years is more than this software's expected lifetime.
# The Mutex (Global\EmployeeAgent) ensures only one agent runs even when
# this trigger fires while an instance is already up.
$repeatTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Minutes 10) `
    -RepetitionDuration (New-TimeSpan -Days 9999)

$triggers  = @($logonTrigger, $repeatTrigger)
$principal = New-ScheduledTaskPrincipal -GroupId "Users" -RunLevel Limited

# A2 · RestartCount 3 -> 999, Interval 1min -> 5min. With watchdog
# miss_count_to_exit=2 filtering transient jitter, only sustained system-level
# issues will burn through these — and at 5min apart, even 999 retries spans
# ~3.5 days, plenty of time for operators to notice.
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 5) `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0)   # 0 = unlimited

Register-ScheduledTask -TaskName $TaskName `
    -Action $action -Trigger $triggers -Principal $principal -Settings $settings `
    -Description "Employee activity monitoring agent (Phase 6.1A: multi-trigger + RestartCount=999)." `
    -Force | Out-Null

# ============================================================================
# Phase 6.1A · B: External Recovery Watchdog Task
# ============================================================================
# Deploy recovery.ps1 to ProgramData (not Program Files) so an AV that nukes
# Program Files\EmployeeAgent\ still leaves the recovery script — and the
# recovery script will then detect the missing EXE and flag it.
$recoveryScript = Join-Path $DataDir "recovery.ps1"
$recoverySrc    = Join-Path $PSScriptRoot "recovery.ps1"
if (Test-Path $recoverySrc) {
    Write-Host "  - Deploying recovery.ps1 to $recoveryScript"
    Copy-Item -Path $recoverySrc -Destination $recoveryScript -Force
} else {
    # post-install was launched without recovery.ps1 alongside it (developer
    # invocation, perhaps). Skip — recovery task won't be useful but main
    # task is already installed.
    Write-Host "  - recovery.ps1 source missing at $recoverySrc, skipping recovery task"
    $recoveryScript = $null
}

if ($recoveryScript) {
    Write-Host "  - Registering recovery task '$RecoveryTask' (SYSTEM, every 5min)"
    try {
        # Action: powershell.exe runs recovery.ps1. -NoProfile is essential —
        # without it, Windows PowerShell loads the SYSTEM profile which on
        # some machines has slow modules (Az, AWS, etc.) and adds latency.
        $recoveryAction = New-ScheduledTaskAction `
            -Execute "powershell.exe" `
            -Argument ("-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"{0}`"" -f $recoveryScript)

        # Two triggers: AtStartup (so it kicks in immediately on boot before
        # any user logs in) + Once now with 5min repeat (so the install gets
        # the first health check within minutes).
        $rt1 = New-ScheduledTaskTrigger -AtStartup
        # See main-task block above: 9999d avoids int32 seconds overflow.
        $rt2 = New-ScheduledTaskTrigger -Once -At (Get-Date).AddSeconds(60) `
            -RepetitionInterval (New-TimeSpan -Minutes 5) `
            -RepetitionDuration (New-TimeSpan -Days 9999)
        $rTriggers = @($rt1, $rt2)

        $rPrincipal = New-ScheduledTaskPrincipal `
            -UserId "NT AUTHORITY\SYSTEM" -RunLevel Highest -LogonType ServiceAccount

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
    } catch {
        Write-Host "    Recovery task registration failed: $($_.Exception.Message)"
        # Non-fatal: main task is up, just no external watchdog
    }
}

# ============================================================================
# Phase 6.1A · C: First-boot Start with 3x retry
# ============================================================================
# Spec #4: 'install must auto-complete: first launch'. Previously a single
# Start-ScheduledTask attempt; now retry up to 3x at 5s intervals, then give
# up gracefully (RecoveryTask will take over within 5 minutes).
$started = $false
for ($i = 1; $i -le 3; $i++) {
    try {
        Start-ScheduledTask -TaskName $TaskName -ErrorAction Stop
        Write-Host "  - Scheduled task started (attempt $i)"
        $started = $true
        break
    } catch {
        Write-Host "    First-boot attempt $i/3 failed: $($_.Exception.Message)"
        if ($i -lt 3) {
            Start-Sleep -Seconds 5
        }
    }
}
if (-not $started) {
    Write-Host "  - All 3 first-boot attempts failed; RecoveryTask will retry within 5 minutes"
}

Write-Host "==> Post-install complete." -ForegroundColor Green
Write-Host "    Program       : $exePath"
Write-Host "    Config        : $configPath"
Write-Host "    Logs          : $(Join-Path $DataDir 'logs\agent.log')"
Write-Host "    Recovery log  : $(Join-Path $DataDir 'logs\recovery.log')"
Write-Host "    Main task     : $TaskName"
Write-Host "    Recovery task : $RecoveryTask"
