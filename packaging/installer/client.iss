; Client installer. Build the exe first (from the repo root):
;   pyinstaller packaging/client.spec
; then compile this with Inno Setup 6 (ISCC.exe or the IDE).
;
; com0com is NOT bundled here — get a signed build from
; https://com0com.sourceforge.net (or a maintained fork) and drop its
; setup exe at packaging\installer\redist\com0com-setup.exe before
; compiling. The #if below means: if you never place that file there,
; the install step simply doesn't exist in the compiled installer — no
; missing-file error at install time, the user just installs com0com
; themselves afterward. The silent-install flag below (/S) matches the
; classic com0com NSIS-based installer; verify against whatever build
; you use — some forks ship an MSI instead, which needs
; "msiexec /i ... /quiet".

#define HasCom0comRedist FileExists("redist\com0com-setup.exe")

#define MyAppName "HAM Radio Remote Client"
#define MyAppVersion "1.1.0"
#define MyAppPublisher "Your Callsign / Club"
#define MyAppExeName "HAM-Radio-Client.exe"

[Setup]
AppId={{B6E1B1A0-6A9E-4C7E-9E2F-HAMRADIOCLIENT}}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\HAM Radio Remote Client
DefaultGroupName=HAM Radio Remote
DisableProgramGroupPage=yes
OutputDir=..\..\dist_installers
OutputBaseFilename=HAM-Radio-Client-Setup
Compression=lzma
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=admin
; Lets a silent re-run (the auto-updater) close a running client first.
AppMutex=HAMRadioClientMutex
UninstallDisplayIcon={app}\{#MyAppExeName}

[Languages]
Name: "bulgarian"; MessagesFile: "compiler:Languages\Bulgarian.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Files]
Source: "..\..\dist\HAM-Radio-Client\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\..\client\config.json"; DestDir: "{app}"; Flags: onlyifdoesntexist
#if HasCom0comRedist
Source: "redist\com0com-setup.exe"; DestDir: "{tmp}"; Flags: deleteafterinstall; Check: not IsCom0comInstalled
#endif

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Конфигурация (config.json)"; Filename: "{app}\config.json"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"

[Run]
#if HasCom0comRedist
Filename: "{tmp}\com0com-setup.exe"; Parameters: "/S"; StatusMsg: "Инсталиране на com0com виртуален COM порт драйвер..."; Check: not IsCom0comInstalled; Flags: waituntilterminated
#endif
; Deliberately NOT registered as a startup app — nothing here auto-runs at boot.

[UninstallDelete]
Type: filesandordirs; Name: "{app}"

[Code]
function IsCom0comInstalled: Boolean;
begin
  Result := RegKeyExists(HKLM, 'SYSTEM\CurrentControlSet\Services\com0com') or
            RegKeyExists(HKLM, 'SOFTWARE\com0com');
end;
