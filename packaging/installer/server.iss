; Server installer. Build the exe first (from the repo root):
;   pyinstaller packaging/server.spec
; then compile this with Inno Setup 6 (ISCC.exe or the IDE).
;
; PostgreSQL is NOT bundled here — get the official installer from
; postgresql.org and drop it at packaging\installer\redist\postgresql-setup.exe
; if you want the "Install PostgreSQL" task offered below. The #if below
; means: if you never place that file there, the task/step simply don't
; exist in the compiled installer — no missing-file error at install
; time. Either way, without it the server still installs and runs fine
; (NullDb fallback); point server\config.json at an existing PostgreSQL
; instance whenever you're ready for admin-panel logins and config storage.

#define HasPostgresRedist FileExists("redist\postgresql-setup.exe")

#define MyAppName "HAM Radio Remote Server"
#define MyAppVersion "1.2.14"
#define MyAppPublisher "Your Callsign / Club"
#define MyAppExeName "HAM-Radio-Server.exe"

[Setup]
AppId={{2B9F1E1E-2B7A-4E3B-9B0C-HAMRADIOSERVER}}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\HAM Radio Remote Server
DefaultGroupName=HAM Radio Remote
DisableProgramGroupPage=yes
OutputDir=..\..\dist_installers
OutputBaseFilename=HAM-Radio-Server-Setup
Compression=lzma
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=admin
; Lets a silent re-run (the auto-updater) close a running server first.
AppMutex=HAMRadioServerMutex
UninstallDisplayIcon={app}\{#MyAppExeName}

[Languages]
Name: "bulgarian"; MessagesFile: "compiler:Languages\Bulgarian.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

#if HasPostgresRedist
[Tasks]
Name: "installpostgres"; Description: "Инсталирай PostgreSQL локално"; Flags: unchecked; Check: not IsPostgresInstalled
#endif

[Files]
Source: "..\..\dist\HAM-Radio-Server\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\..\server\config.json"; DestDir: "{app}"; Flags: onlyifdoesntexist
Source: "..\..\server\db\schema.sql"; DestDir: "{app}\db"; Flags: ignoreversion
#if HasPostgresRedist
Source: "redist\postgresql-setup.exe"; DestDir: "{tmp}"; Flags: deleteafterinstall; Tasks: installpostgres
#endif

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Конфигурация (config.json)"; Filename: "{app}\config.json"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"

[Run]
#if HasPostgresRedist
; Unattended PostgreSQL install — verify these flags against whatever
; installer build you actually place in redist/ before shipping; the
; EnterpriseDB Windows installer supports --mode unattended plus
; --serverport/--superpassword etc., but exact flags vary by version.
Filename: "{tmp}\postgresql-setup.exe"; Parameters: "--mode unattended --unattendedmodeui minimal"; StatusMsg: "Инсталиране на PostgreSQL..."; Tasks: installpostgres; Flags: waituntilterminated
#endif
; Deliberately NOT registered as a startup app / service — the project
; plan requires the server to be started manually every time.

[UninstallDelete]
Type: filesandordirs; Name: "{app}"

[Code]
// Shared by the Task's Check (above) and CurUninstallStepChanged (below) —
// finds any installed program whose display name contains "PostgreSQL"
// by walking the Uninstall registry key, rather than hardcoding a
// version-specific key (which varies by PostgreSQL release).
function FindPostgresUninstallString(var UninstallString: String): Boolean;
var
  KeyPath: String;
  Names: TArrayOfString;
  I: Integer;
  DisplayName: String;
begin
  Result := False;
  KeyPath := 'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall';
  if RegGetSubkeyNames(HKLM64, KeyPath, Names) then
  begin
    for I := 0 to GetArrayLength(Names) - 1 do
    begin
      if RegQueryStringValue(HKLM64, KeyPath + '\' + Names[I], 'DisplayName', DisplayName) then
      begin
        if Pos('PostgreSQL', DisplayName) > 0 then
        begin
          if RegQueryStringValue(HKLM64, KeyPath + '\' + Names[I], 'UninstallString', UninstallString) then
          begin
            Result := True;
            Exit;
          end;
        end;
      end;
    end;
  end;
end;

function IsPostgresInstalled: Boolean;
var
  Unused: String;
begin
  Result := FindPostgresUninstallString(Unused);
end;

var
  KeepPostgres: Boolean;

function InitializeUninstall(): Boolean;
begin
  KeepPostgres := MsgBox(
    'Да запазя ли PostgreSQL инсталацията и базата с логове?' + #13#10 +
    '(Избери "Не" само ако PostgreSQL е бил инсталиран единствено за това приложение.)',
    mbConfirmation, MB_YESNO) = IDYES;
  Result := True;
end;

// Not tested against a real PostgreSQL install here — verify before
// relying on it; the unattended uninstall flag varies by installer build.
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  UninstallString: String;
  ResultCode: Integer;
begin
  if (CurUninstallStep = usPostUninstall) and (not KeepPostgres) then
  begin
    if FindPostgresUninstallString(UninstallString) then
      Exec(RemoveQuotes(UninstallString), '--mode unattended', '', SW_SHOW, ewWaitUntilTerminated, ResultCode);
  end;
end;
