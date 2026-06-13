#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Install EmployeeAgent: copy program, create data dir, register logon auto-start task.
.DESCRIPTION
    Run as Administrator (install-time only; the agent itself runs as a standard user).
    Transparent and compliant: the auto-start entry is visible, runs non-elevated,
    and the process is not hidden.
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\install.ps1
.NOTES
    ASCII-only on purpose so it parses correctly under Windows PowerShell 5.1
    regardless of system locale/encoding. User-facing docs live in README.md.
#>
param(
    # PyInstaller onedir output
    [string]$SourceDir = (Join-Path $PSScriptRoot "..\dist\EmployeeAgent"),
    # Program location (read-only)
    [string]$InstallDir = (Join-Path $env:ProgramFiles "EmployeeAgent"),
    # Writable data dir (config.json + logs)
    [string]$DataDir = (Join-Path $env:ProgramData "EmployeeAgent"),
    [string]$TaskName = "EmployeeAgent"
)

$ErrorActionPreference = "Stop"

Write-Host "==> Installing EmployeeAgent" -ForegroundColor Cyan

# Phase 5.1 R1: Upgrade path — stop existing task + process BEFORE Copy-Item.
# Without this, if EmployeeAgent.exe is currently running the next Copy-Item hits
# SHARING VIOLATION and leaves a half-new InstallDir. Also lets the named mutex
# Global\EmployeeAgent free up so the new agent can acquire on next start.
$existingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existingTask) {
    Write-Host "  - Upgrade path: stopping existing scheduled task"
    try { Stop-ScheduledTask -TaskName $TaskName -ErrorAction Stop } catch {}
}
$existingProcs = Get-Process -Name "EmployeeAgent" -ErrorAction SilentlyContinue
if ($existingProcs) {
    Write-Host ("  - Upgrade path: stopping {0} running EmployeeAgent process(es)" -f $existingProcs.Count)
    $existingProcs | Stop-Process -Force -ErrorAction SilentlyContinue
    # Give the OS a moment to fully reap the processes (close file handles, free mutex).
    # The last in-flight sample (< one flush_interval, default 15s) may be lost; this is
    # acceptable per V11: window_buffer.db is crash-safe and pending/uploaded data persist.
    Start-Sleep -Seconds 2
}

# 1) Validate and copy the program to Program Files
if (-not (Test-Path $SourceDir)) {
    throw "Program directory not found: $SourceDir. Build the onedir with PyInstaller first (under client/)."
}
$exeSource = Join-Path $SourceDir "EmployeeAgent.exe"
if (-not (Test-Path $exeSource)) {
    throw "EmployeeAgent.exe not found: $exeSource"
}
Write-Host "  - Copying program to $InstallDir"
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
Copy-Item -Path (Join-Path $SourceDir "*") -Destination $InstallDir -Recurse -Force
$exePath = Join-Path $InstallDir "EmployeeAgent.exe"

# 2) Create data + logs dirs, grant Users group Modify (agent writes logs/config at runtime)
Write-Host "  - Creating data dir $DataDir and setting permissions"
New-Item -ItemType Directory -Force -Path $DataDir | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $DataDir "logs") | Out-Null
# *S-1-5-32-545 = BUILTIN\Users (locale-independent); (OI)(CI)M = inherit + Modify
icacls $DataDir /grant "*S-1-5-32-545:(OI)(CI)M" /T | Out-Null

# 3) Place default config.json (only if missing, never overwrite admin's config)
#    Phase 4.2 / 4.3 validation defaults:
#      - server_url points at port 9100 (isolated test server) to avoid
#        polluting the existing production 9000 instance during pilot
#      - window.enabled = true so window_tracker + window_uploader actually run
#      - all other window/* fields match client/config.example.json
$configPath = Join-Path $DataDir "config.json"
if (-not (Test-Path $configPath)) {
    Write-Host "  - Writing default config.json (edit server_url / employee_name as needed)"
    $defaultConfig = @'
{
  "server_url": "http://localhost:9100",
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
    # Set-Content -Encoding UTF8 在 PowerShell 5.1 下会写 UTF-8 BOM，
    # Python 的 json.load 不接受 BOM 会直接 JSONDecodeError 崩启动。
    # 强制用无 BOM UTF-8。PS 7+ 可用 utf8NoBOM；这里写法兼容 PS 5.1。
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($configPath, $defaultConfig, $utf8NoBom)
} else {
    Write-Host "  - config.json already exists, keeping it"
}

# 4) Register scheduled task: logon trigger + restart-on-failure, Users group, non-elevated
Write-Host "  - Registering scheduled task '$TaskName' (start at logon + restart on failure)"
# Phase 5.1 R3: pin WorkingDirectory to InstallDir so the PyInstaller onedir bootloader
# always finds _internal\ via relative cwd, even if some launcher leaves cwd as C:\Windows\System32.
$action = New-ScheduledTaskAction -Execute $exePath -WorkingDirectory $InstallDir
$trigger = New-ScheduledTaskTrigger -AtLogOn
# Phase 5.1 R2: delay 30s after logon — lets desktop, network and AV scanners
# settle before we start hammering the SQLite buffer and uploading to the server.
$trigger.Delay = "PT30S"
# Users group + Limited: runs for any logged-on user with their standard rights (no elevation)
$principal = New-ScheduledTaskPrincipal -GroupId "Users" -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0)   # 0 = no time limit (long-running)

Register-ScheduledTask -TaskName $TaskName `
    -Action $action -Trigger $trigger -Principal $principal -Settings $settings `
    -Description "Employee activity monitoring agent (transparent, non-elevated)." `
    -Force | Out-Null

Write-Host "==> Install complete." -ForegroundColor Green
Write-Host "    Program: $exePath"
Write-Host "    Config : $configPath"
Write-Host "    Logs   : $(Join-Path $DataDir 'logs\agent.log')"
Write-Host "    The task starts at next user logon. To start now: Start-ScheduledTask -TaskName $TaskName"
