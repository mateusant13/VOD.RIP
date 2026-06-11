; VOD.RIP — Windows installer (Inno Setup)
; Build: iscc /DAppVersion=1.0.6 installer\installer.iss
; Requires MicrosoftEdgeWebview2Setup.exe beside this file (downloaded in CI).

#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif

#define AppName "VOD.RIP"
#define AppExe "VOD-RIP.EXE"
#define AppPublisher "mateusant13"
#define WebView2Clsid "{F3017226-FE2A-4295-8BDF-00B3D09F7BF5}"

[Setup]
AppId={{A4B8C2E1-9F3D-4A2B-8C1E-0123456789AB}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={localappdata}\VOD.RIP
DefaultGroupName={#AppName}
DisableProgramGroupPage=no
OutputBaseFilename=VOD.RIP-{#AppVersion}-Setup
OutputDir=..\release
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\{#AppExe}
CloseApplications=force
RestartApplications=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked

[Files]
Source: "staging\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "MicrosoftEdgeWebview2Setup.exe"; DestDir: "{tmp}"; Flags: deleteafterinstall

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"; WorkingDir: "{app}"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon; WorkingDir: "{app}"

[Run]
Filename: "{tmp}\MicrosoftEdgeWebview2Setup.exe"; Parameters: "/silent /install"; StatusMsg: "Installing Microsoft WebView2 Runtime..."; Check: not IsWebView2Installed(); Flags: waituntilterminated runhidden

[Code]
function IsWebView2Installed: Boolean;
var
  Version: String;
begin
  Result := RegQueryStringValue(
    HKLM,
    'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{#WebView2Clsid}',
    'pv', Version);
  if Result and ((Version = '') or (Version = '0.0.0.0') or (Version = '0.0.0')) then
    Result := False;

  if not Result then
  begin
    Result := RegQueryStringValue(
      HKCU,
      'Software\Microsoft\EdgeUpdate\Clients\{#WebView2Clsid}',
      'pv', Version);
    if Result and ((Version = '') or (Version = '0.0.0.0') or (Version = '0.0.0')) then
      Result := False;
  end;
end;
