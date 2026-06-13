# ===========================================================================
# Phase 6.1A · EmployeeAgent External Recovery Watchdog
#
# Runs every 5 minutes as NT AUTHORITY\SYSTEM (registered by post-install.ps1
# as scheduled task 'EmployeeAgentRecovery'). Independent of the Python
# interpreter / agent process / user session.
#
# Checks (in order):
#   1. EmployeeAgent.exe present?                  (ERROR + state\exe_missing.flag)
#   2. Main scheduled task 'EmployeeAgent' exists? (ERROR)
#   3. Main task disabled?                          (WARN + Enable-ScheduledTask)
#   4. EmployeeAgent process running?               (WARN + Start-ScheduledTask)
#   5. heartbeat.json mtime > 10 minutes?           (ERROR + Stop-Process + Start)
#
# All actions are best-effort: every block is wrapped to never raise.
# Log: C:\ProgramData\EmployeeAgent\logs\recovery.log  (rotated at 1 MiB)
#
# ASCII-only (Windows PowerShell 5.1 GBK locale safety).
# ===========================================================================

$ErrorActionPreference = "Continue"

# --- Paths ----------------------------------------------------------------
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

$MaxHeartbeatAgeSeconds = 600   # 10 minutes
$LogRotateBytes         = 1MB

# --- Logging -------------------------------------------------------------
function Write-Recovery {
    param([string]$Level, [string]$Message)
    $line = "{0} [{1}] {2}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Level, $Message
    try {
        if (-not (Test-Path $LogDir)) {
            New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
        }
        # Rotate at 1 MiB (overwrite single .1 file; tiny disk footprint)
        if (Test-Path $LogPath) {
            $size = (Get-Item $LogPath).Length
            if ($size -gt $LogRotateBytes) {
                try { Move-Item -Path $LogPath -Destination $LogRotated -Force } catch {}
            }
        }
        Add-Content -Path $LogPath -Value $line -Encoding UTF8
    } catch {
        # Logging itself must never crash recovery.ps1 — silently swallow
    }
}

# --- Ensure state dir exists (needed for flags, even on first run) -------
try {
    if (-not (Test-Path $StateDir)) {
        New-Item -ItemType Directory -Force -Path $StateDir | Out-Null
    }
} catch {}

# Track whether we did anything this run (for the OK-summary at the end)
$actions = @()

# === Step 1: EXE present? ===============================================
if (-not (Test-Path $ExePath)) {
    Write-Recovery "ERROR" "EmployeeAgent.exe missing at $ExePath (suspected AV removal or manual delete)"
    try {
        Set-Content -Path $ExeMissFlag -Value (Get-Date -Format "yyyy-MM-dd HH:mm:ss") -Encoding ASCII
    } catch {}
    # Cannot recover without the binary; 6.1B will add reinstall-from-cache.
    # Exit silently — next run will re-check.
    return
} else {
    # If flag exists from a previous run but EXE is back, clear it
    if (Test-Path $ExeMissFlag) {
        try { Remove-Item -Path $ExeMissFlag -Force -ErrorAction SilentlyContinue } catch {}
        Write-Recovery "INFO" "EmployeeAgent.exe restored — cleared exe_missing.flag"
    }
}

# === Step 2: Main task exists? ==========================================
$task = $null
try {
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
} catch {
    Write-Recovery "ERROR" "Main scheduled task '$TaskName' not registered: $($_.Exception.Message)"
    # Cannot Start-ScheduledTask without it; 6.1B will add re-register flow.
    return
}

# === Step 3: Main task disabled? ========================================
if ($task.State -eq "Disabled") {
    Write-Recovery "WARN" "Main task disabled — re-enabling"
    try {
        Enable-ScheduledTask -TaskName $TaskName -ErrorAction Stop | Out-Null
        $actions += "enabled"
    } catch {
        Write-Recovery "ERROR" "Enable-ScheduledTask failed: $($_.Exception.Message)"
    }
}

# === Step 4: Process running? ===========================================
$proc = Get-Process -Name "EmployeeAgent" -ErrorAction SilentlyContinue
if (-not $proc) {
    Write-Recovery "WARN" "EmployeeAgent process not running — starting task"
    try {
        Start-ScheduledTask -TaskName $TaskName -ErrorAction Stop
        $actions += "started"
    } catch {
        Write-Recovery "ERROR" "Start-ScheduledTask failed: $($_.Exception.Message)"
    }
    # Don't check heartbeat in this run — the process was down anyway
} else {
    # === Step 5: heartbeat fresh? =======================================
    if (Test-Path $HeartbeatPath) {
        try {
            $mtime = (Get-Item $HeartbeatPath).LastWriteTime
            $age = [int]((Get-Date) - $mtime).TotalSeconds
            if ($age -gt $MaxHeartbeatAgeSeconds) {
                Write-Recovery "ERROR" ("heartbeat stale age={0}s threshold={1}s pid={2} — kill + restart" `
                    -f $age, $MaxHeartbeatAgeSeconds, $proc.Id)
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
    # Absent heartbeat.json is OK on a fresh install — agent may not have
    # written one yet. Step 4 covered "process not running"; we don't need
    # to also flag "process running but no heartbeat" since that resolves
    # itself within ~30s of normal operation.
}

# === Summary: write one line per run so log proves recovery.ps1 is alive ==
# Keep it terse to bound log growth — 5 min interval * 1 line ~ 105 lines/day
$procState = if ($proc) { "pid=" + $proc.Id } else { "down" }
$summary = "OK exe=present task={0} proc={1}" -f $task.State, $procState
if ($actions.Count -gt 0) {
    $summary += " actions=" + ($actions -join ",")
}
Write-Recovery "INFO" $summary
