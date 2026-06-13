; ============================================================================
; Phase 6.1B + 6.4A · EmployeeAgentSetup.iss
;   End-user installer for EmployeeAgent v0.6.2.
;
;   Inherits 6.1A reliability (scheduled task hardening + Recovery watchdog
;   + Defender exclusions + firstboot retry) PLUS:
;     - Phase 6.1B: client+server anti-downgrade guard (already in EXE)
;     - Phase 6.4A: Employee Name wizard page (Inno custom page →
;                   {tmp}\employee_name.txt → post-install.ps1 -EmployeeNameFile)
;
;   Forward semantics: 0.6.1 → 0.6.2 in-place keeps machine.json /
;   machine_id / hw_fingerprint / legacy_machine_id (preserved by
;   pre-uninstall.ps1 — ProgramData not removed on uninstall).
;
;   Downgrade 0.6.2 → 0.6.x is now actively REFUSED by both client and
;   server (semver guard). DO NOT downgrade 0.6.x → 0.5.x (server
;   employee row keyed by uuid4).
;
; Build:  ISCC.exe EmployeeAgentSetup.iss
; Output: dist/EmployeeAgentSetup_v0.6.2.exe
; ============================================================================

#define AppName        "EmployeeAgent"
#define AppVersion     "0.6.2"
#define AppPublisher   "example.com"
#define AppExeName     "EmployeeAgent.exe"
#define AppURL         "https://example.com"
#define TaskName       "EmployeeAgent"
#define RecoveryTask   "EmployeeAgentRecovery"

[Setup]
AppId={{8F4E2A1C-9D5B-4F3E-A6C7-EE5A4B3D2F11}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} v{#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}
AppUpdatesURL={#AppURL}
VersionInfoVersion=0.6.2.0
VersionInfoCompany={#AppPublisher}
VersionInfoProductName={#AppName}
VersionInfoProductVersion={#AppVersion}
VersionInfoDescription=Employee activity monitoring agent (transparent, non-elevated)

; --- Install location: Program Files\EmployeeAgent ---
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
; Show directory page but don't allow ridiculous changes
DisableDirPage=auto

; --- Admin elevation required (we register a scheduled task + write Program Files) ---
PrivilegesRequired=admin
PrivilegesRequiredOverridesAllowed=dialog

; --- Setup UI ---
WizardStyle=modern
ShowLanguageDialog=auto
SetupLogging=yes
; If EmployeeAgent.exe is running, Inno auto-handles (CurStepChanged below also stops it)
CloseApplications=force
RestartApplications=no

; --- Output binary ---
OutputDir={#SourcePath}\..\..\dist
OutputBaseFilename=EmployeeAgentSetup_v{#AppVersion}
Compression=lzma2/ultra
SolidCompression=yes
LZMAUseSeparateProcess=yes

; --- Uninstaller in Add/Remove Programs ---
UninstallDisplayName={#AppName} v{#AppVersion}
UninstallDisplayIcon={app}\{#AppExeName}

; --- Architecture: ship as x64 onedir from PyInstaller ---
ArchitecturesInstallIn64BitMode=x64compatible
ArchitecturesAllowed=x64compatible

[Languages]
; Inno Setup 6 默认只随官方 distribution 自带 Default.isl 等少数 Latin-script
; 语言；中文 UI 需要从 jrsoftware/issrc 第三方仓库取 .isl 文件单独装入
; compiler\Languages\，本期不引入第三方资源。终端用户都能识别英文 wizard。
Name: "english"; MessagesFile: "compiler:Default.isl"

[Files]
; --- Main payload: PyInstaller onedir (Phase 5.4) ---
Source: "..\dist\EmployeeAgent\EmployeeAgent.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\dist\EmployeeAgent\_internal\*";       DestDir: "{app}\_internal"; \
    Flags: ignoreversion recursesubdirs createallsubdirs

; --- Hooks: extract to {tmp}, deleted after install / uninstall ---
; recovery.ps1 sits next to post-install.ps1 in {tmp} so post-install can
; resolve it via $PSScriptRoot and copy it to ProgramData (the script must
; live OUTSIDE Program Files so AV that nukes the install dir still leaves
; the recovery watchdog behind).
Source: "post-install.ps1";  DestDir: "{tmp}"; Flags: deleteafterinstall
Source: "recovery.ps1";      DestDir: "{tmp}"; Flags: deleteafterinstall
Source: "pre-uninstall.ps1"; DestDir: "{app}"; Flags: ignoreversion uninsneveruninstall

[Run]
; --- After file copy: configure ProgramData + scheduled task + first launch ---
; Phase 6.4A: -EmployeeNameFile points to {tmp}\employee_name.txt (UTF-8) which
; was written by InitializeWizard from the custom Employee Name page. Using a
; file instead of an inline parameter sidesteps PowerShell quote-escaping bugs
; around Chinese names and accidental injection via punctuation.
Filename: "powershell.exe"; \
    Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{tmp}\post-install.ps1"" -EmployeeNameFile ""{tmp}\employee_name.txt"""; \
    Flags: runhidden waituntilterminated; \
    StatusMsg: "Configuring runtime (data dir, scheduled task, first launch)..."

[UninstallRun]
; --- Before file removal: stop task + process so Inno can delete files cleanly ---
Filename: "powershell.exe"; \
    Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\pre-uninstall.ps1"""; \
    Flags: runhidden waituntilterminated; \
    RunOnceId: "WorktideStopAgent"

[UninstallDelete]
; Inno already removes {app}; the runtime data dir is intentionally kept.

[Code]
// ---------------------------------------------------------------------------
// Phase 6.4A · Employee Name wizard page
//   Forces fresh installs to capture "whose computer is this?" so the admin
//   dashboard never shows machine GUIDs again. Auto-update path (updater.py)
//   does NOT re-run this installer, so existing employees keep whatever name
//   they had — no clobber risk. Reinstalling preserves naming via the same
//   "don't overwrite non-empty" guard in post-install.ps1.
// ---------------------------------------------------------------------------
var
  EmployeeNamePage: TInputQueryWizardPage;

procedure InitializeWizard;
begin
  EmployeeNamePage := CreateInputQueryPage(wpWelcome,
    'Employee Information / 员工信息',
    'Whose computer is this?  /  这台电脑是谁的?',
    'Enter the employee name. It is shown in the admin dashboard.' + #13#10 +
    '输入员工姓名 (管理后台会显示)。例如: Alice / Bob / Carol。');
  EmployeeNamePage.Add('Employee Name / 员工姓名:', False);
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  V: String;
begin
  Result := True;
  if (EmployeeNamePage <> nil) and (CurPageID = EmployeeNamePage.ID) then
  begin
    V := Trim(EmployeeNamePage.Values[0]);
    if V = '' then
    begin
      MsgBox('Employee name is required.' + #13#10 + '请输入员工姓名。',
        mbError, MB_OK);
      Result := False;
    end;
  end;
end;

// Write the entered name to {tmp}\employee_name.txt as raw UTF-8 bytes
// (no BOM). post-install.ps1 reads it with Get-Content -Raw -Encoding UTF8.
//
// Implementation: SaveStringToUTF8File would be the obvious choice but is
// only available in Inno Setup 6.2.0+. Older Inno 6.x builds rejecting that
// helper would break this whole install path. We use the universally-
// available combo of Utf8Encode (converts UnicodeString -> UTF-8 AnsiString)
// + SaveStringToFile (writes the AnsiString as raw bytes). Net effect:
// identical bytes on disk as SaveStringToUTF8File, no BOM, no Inno version
// floor higher than 6.0.0.
procedure WriteEmployeeNameFile;
var
  Name: String;
  FilePath: String;
begin
  if EmployeeNamePage = nil then Exit;
  Name := Trim(EmployeeNamePage.Values[0]);
  FilePath := ExpandConstant('{tmp}\employee_name.txt');
  if not SaveStringToFile(FilePath, Utf8Encode(Name), False) then
    Log('[6.4A] Failed to write ' + FilePath);
end;

// ---------------------------------------------------------------------------
// Phase 5.1 R1 belt-and-braces: stop existing task + process BEFORE file copy.
// Without this, Inno's [Files] step can hit SHARING VIOLATION on _internal\*
// even with CloseApplications=force, because the agent re-spawns via the task
// trigger while Inno is mid-copy.
// ---------------------------------------------------------------------------
procedure StopExistingAgent;
var
  ResultCode: Integer;
begin
  Log('[6.1A] Stopping existing EmployeeAgent + Recovery task + process (pre-copy)');
  // 6.1A: also disable EmployeeAgentRecovery so its 5-min tick can't relaunch
  // the agent mid file-copy (otherwise SHARING VIOLATION on _internal\*).
  Exec(ExpandConstant('{cmd}'), '/c schtasks /End /TN "{#RecoveryTask}" >nul 2>nul', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Exec(ExpandConstant('{cmd}'), '/c schtasks /Change /TN "{#RecoveryTask}" /DISABLE >nul 2>nul', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Exec(ExpandConstant('{cmd}'), '/c schtasks /End /TN "{#TaskName}" >nul 2>nul', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Exec(ExpandConstant('{cmd}'), '/c schtasks /Change /TN "{#TaskName}" /DISABLE >nul 2>nul', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Exec(ExpandConstant('{cmd}'), '/c taskkill /F /IM EmployeeAgent.exe >nul 2>nul', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Sleep(2000);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssInstall then
  begin
    StopExistingAgent;
    WriteEmployeeNameFile;
  end;
end;
