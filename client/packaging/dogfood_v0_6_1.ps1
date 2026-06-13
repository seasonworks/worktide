#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Phase 6.1A · Single-machine dogfood deploy of 0.6.1.

    Bypasses Auto Update (latest.json untouched, no other machine sees
    0.6.1) and pushes 0.6.1 onto just THIS machine + applies 6.1A
    scheduled-task hardening + Defender exclusions + recovery task.

    What this script does (in order):
      1. Stop main task + recovery task + kill running agent
      2. Backup current install dir to
           %PROGRAMDATA%\EmployeeAgent\updates\rollback\manual_<ts>
      3. Extract EmployeeAgent_v0.6.1.zip into a stage dir
      4. Verify the new EXE's FileVersion = 0.6.1.0
      5. Replace install_dir contents (ProgramData state is untouched —
         machine.json / config.json / logs / window_buffer.db all stay
         exactly where they are, so machine_id / hw_fp / legacy
         continuity is guaranteed)
      6. Invoke apply_6_1a_hardening.ps1 (alongside this script) to
         re-register tasks with 6.1A settings + start
      7. Print one-line verification

    Safe to re-run. ProgramData is preserved by step 2 (backup) and not
    touched by step 5.

.PARAMETER ZipPath
    Path to EmployeeAgent_v0.6.1.zip. Defaults to the same dir as this
    script. Override if you put it elsewhere.

.PARAMETER Rollback
    If specified, restores the most recent manual_<ts> backup back to
    install_dir. Run this if 0.6.1 misbehaves; it brings the agent back
    to whatever was there before this script ran.

.USAGE
    Right-click PowerShell → Run as Administrator, then:

        Set-ExecutionPolicy Bypass -Scope Process -Force
        .\dogfood_v0_6_1.ps1

    Required files alongside this script:
        EmployeeAgent_v0.6.1.zip      (~11.5 MB)
        apply_6_1a_hardening.ps1      (the retrofit script)

.NOTES
    ASCII-only for Windows PowerShell 5.1 locale safety.
#>
param(
    [string]$ZipPath     = (Join-Path $PSScriptRoot "EmployeeAgent_v0.6.1.zip"),
    [string]$InstallDir  = (Join-Path $env:ProgramFiles  "EmployeeAgent"),
    [string]$DataDir     = (Join-Path $env:ProgramData   "EmployeeAgent"),
    [string]$TaskName    = "EmployeeAgent",
    [string]$RecoveryTask = "EmployeeAgentRecovery",
    [switch]$Rollback
)

$ErrorActionPreference = "Stop"

# ============================================================================
# Rollback mode
# ============================================================================
if ($Rollback) {
    Write-Host "==> Phase 6.1A dogfood rollback" -ForegroundColor Yellow
    $rbRoot = Join-Path $DataDir "updates\rollback"
    if (-not (Test-Path $rbRoot)) {
        throw "no rollback dir at $rbRoot"
    }
    $latest = Get-ChildItem -Path $rbRoot -Directory -Filter "manual_*" -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if (-not $latest) {
        throw "no manual_<ts> backups in $rbRoot"
    }
    Write-Host "  Restoring from: $($latest.FullName)"

    # Stop everything first
    schtasks /End /TN $RecoveryTask | Out-Null 2>&1
    schtasks /End /TN $TaskName     | Out-Null 2>&1
    Stop-Process -Name "EmployeeAgent" -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 3

    # Wipe + copy backup back
    if (Test-Path $InstallDir) {
        Get-ChildItem -Path $InstallDir -Force | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
    } else {
        New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
    }
    Copy-Item -Recurse -Path (Join-Path $latest.FullName "*") -Destination $InstallDir -Force

    # Re-start
    Start-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    $exe = Get-Item (Join-Path $InstallDir "EmployeeAgent.exe")
    Write-Host "==> Rollback complete. FileVersion = $($exe.VersionInfo.FileVersion)" -ForegroundColor Green
    return
}

# ============================================================================
# Normal mode: deploy 0.6.1
# ============================================================================
Write-Host "==> Phase 6.1A dogfood deploy of 0.6.1 (single-machine)" -ForegroundColor Cyan
Write-Host "    Zip         : $ZipPath"
Write-Host "    InstallDir  : $InstallDir"
Write-Host "    DataDir     : $DataDir (preserved)"

if (-not (Test-Path $ZipPath)) {
    throw "EmployeeAgent_v0.6.1.zip not found at: $ZipPath. Copy it alongside this script."
}

$Retrofit = Join-Path $PSScriptRoot "apply_6_1a_hardening.ps1"
if (-not (Test-Path $Retrofit)) {
    throw "apply_6_1a_hardening.ps1 not found at: $Retrofit. Copy it alongside this script."
}

if (-not (Test-Path (Join-Path $InstallDir "EmployeeAgent.exe"))) {
    throw "no existing EmployeeAgent install at $InstallDir — this script upgrades an existing install. For fresh installs use the .exe installer."
}

# ----------------------------------------------------------------------------
# Step 1: stop main + recovery + kill process
# ----------------------------------------------------------------------------
Write-Host "  [1/7] Stopping main + recovery tasks + agent process"
try { Stop-ScheduledTask -TaskName $RecoveryTask -ErrorAction SilentlyContinue } catch {}
try { Disable-ScheduledTask -TaskName $RecoveryTask -ErrorAction SilentlyContinue | Out-Null } catch {}
try { Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue } catch {}
try { Disable-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue | Out-Null } catch {}
Stop-Process -Name "EmployeeAgent" -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 3

# ----------------------------------------------------------------------------
# Step 2: backup current install_dir
# ----------------------------------------------------------------------------
$rbRoot = Join-Path $DataDir "updates\rollback"
New-Item -ItemType Directory -Force -Path $rbRoot | Out-Null
$ts = Get-Date -Format "yyyyMMdd_HHmmss"
$backup = Join-Path $rbRoot ("manual_{0}" -f $ts)
Write-Host "  [2/7] Backup: $InstallDir -> $backup"
Copy-Item -Recurse -Path $InstallDir -Destination $backup -Force

# ----------------------------------------------------------------------------
# Step 3: extract zip to stage dir
# ----------------------------------------------------------------------------
$stage = Join-Path $env:TEMP ("EmployeeAgent_v0.6.1_stage_{0}" -f $ts)
if (Test-Path $stage) { Remove-Item -Recurse -Force $stage }
New-Item -ItemType Directory -Force -Path $stage | Out-Null
Write-Host "  [3/7] Extracting zip to $stage"
Expand-Archive -Path $ZipPath -DestinationPath $stage -Force

$inner = Join-Path $stage "EmployeeAgent"
$stagedExe = Join-Path $inner "EmployeeAgent.exe"
if (-not (Test-Path $stagedExe)) {
    throw "zip layout unexpected: $stagedExe not found after extraction"
}

# ----------------------------------------------------------------------------
# Step 4: verify staged EXE = 0.6.1.0
# ----------------------------------------------------------------------------
$stagedVer = (Get-Item $stagedExe).VersionInfo.FileVersion
Write-Host "  [4/7] Staged EXE FileVersion = $stagedVer"
if ($stagedVer -ne "0.6.1.0") {
    throw "staged EXE FileVersion mismatch: got $stagedVer, expected 0.6.1.0"
}

# ----------------------------------------------------------------------------
# Step 5: replace install_dir contents (NOT the dir itself; preserve ACLs)
# ----------------------------------------------------------------------------
Write-Host "  [5/7] Replacing install_dir contents"
# Wipe old contents but keep $InstallDir itself (preserves Inno-set ACLs)
Get-ChildItem -Path $InstallDir -Force -ErrorAction SilentlyContinue |
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
Copy-Item -Recurse -Path (Join-Path $inner "*") -Destination $InstallDir -Force

$installedExe = Join-Path $InstallDir "EmployeeAgent.exe"
$installedVer = (Get-Item $installedExe).VersionInfo.FileVersion
if ($installedVer -ne "0.6.1.0") {
    throw "after copy, installed EXE FileVersion is $installedVer (expected 0.6.1.0)"
}
Write-Host "    installed FileVersion = $installedVer (OK)"

# ----------------------------------------------------------------------------
# Step 6: re-enable main task BEFORE running retrofit
# ----------------------------------------------------------------------------
try { Enable-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue | Out-Null } catch {}

# ----------------------------------------------------------------------------
# Step 7: invoke apply_6_1a_hardening.ps1 (re-registers both tasks + starts)
# ----------------------------------------------------------------------------
Write-Host "  [6/7] Running retrofit ($Retrofit)"
& $Retrofit

# ----------------------------------------------------------------------------
# Cleanup stage
# ----------------------------------------------------------------------------
try { Remove-Item -Recurse -Force $stage -ErrorAction SilentlyContinue } catch {}

# ----------------------------------------------------------------------------
# Final verification
# ----------------------------------------------------------------------------
Write-Host "  [7/7] Verification"
Start-Sleep -Seconds 3
$proc = Get-Process -Name "EmployeeAgent" -ErrorAction SilentlyContinue
if ($proc) {
    Write-Host "    PID=$($proc.Id) running (WorkingSet=$('{0:N1}' -f ($proc.WorkingSet64/1MB)) MB)"
} else {
    Write-Host "    Agent not running yet — RecoveryTask will pick up within 5 min"
}

$mainTriggers = (Get-ScheduledTask $TaskName).Triggers.Count
$mainRestart  = (Get-ScheduledTask $TaskName).Settings.RestartCount
$recTask      = Get-ScheduledTask $RecoveryTask -ErrorAction SilentlyContinue
$recState     = if ($recTask) { $recTask.State } else { "MISSING" }

Write-Host ""
Write-Host "==> Dogfood 0.6.1 deploy complete." -ForegroundColor Green
Write-Host ""
Write-Host "Quick verify (paste these into the same PS window):"
Write-Host "  Get-Item '$installedExe' | Select FileVersion,ProductVersion,Length"
Write-Host "  (Get-ScheduledTask $TaskName).Triggers.Count             # expect 2  (got $mainTriggers)"
Write-Host "  (Get-ScheduledTask $TaskName).Settings.RestartCount      # expect 999  (got $mainRestart)"
Write-Host "  Get-ScheduledTask $RecoveryTask                          # expect Ready,SYSTEM  (got $recState)"
Write-Host "  Get-MpPreference | Select -Expand ExclusionPath          # expect EmployeeAgent entries"
Write-Host ""
Write-Host "Rollback if needed:"
Write-Host "  .\dogfood_v0_6_1.ps1 -Rollback"
Write-Host ""
Write-Host "Backup is at: $backup"
