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

; F4/F13 (ANTIVIRUS_AUDIT): The installer and the packaged EXE are unsigned,
; which is the single largest source of "Windows protected your PC"
; SmartScreen warnings and `PUA:Win32/UnsignedInstaller` flags. To sign:
;   1. Acquire an EV/OV Authenticode certificate (DigiCert, Sectigo, GlobalSign).
;   2. Set the environment variables:
;        VODRIP_CERT_FILE   - path to .pfx
;        VODRIP_CERT_PWD    - .pfx password
;        VODRIP_TIMESTAMP   - timestamp server (default: http://timestamp.digicert.com)
;   3. The [Setup] block below references the placeholder SignTool name
;      "vodrip" if VODRIP_CERT_FILE is set, and ISCC's [Code] section picks
;      the right SignTool definition from [Files] automatically. See
;      docs/SIGNING.md (created in this change) for the CI step that calls
;      signtool directly for the PyInstaller bootloader.
;   4. Sign the PyInstaller bootloader (VOD-RIP.exe) post-build with the
;      same cert; PyInstaller's bootloader can be Authenticode-signed.
; Without these env vars, SignTool is not added and the build proceeds
; unsigned (as before). The conditional is mandatory because we do NOT
; want a broken/cancelled cert password to fail the build.
#ifexist "..\signing\vodrip.pfx"
  SignTool=vodrip
  SignedUninstaller=yes
#endif
[Setup]
AppId={{A4B8C2E1-9F3D-4A2B-8C1E-0123456789AB}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL=https://github.com/mateusant13/VOD.RIP
AppSupportURL=https://github.com/mateusant13/VOD.RIP/issues
SetupIconFile=..\assets\setup-icon.ico
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
RestartApplications=no

[Icons]

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

; Run Microsoft's signed bootstrapper (bundled at build time). Visible installer UI — not silent/hidden (AV-friendly).
[Run]
Filename: "{tmp}\MicrosoftEdgeWebview2Setup.exe"; Parameters: "/install"; StatusMsg: "Installing Microsoft WebView2 Runtime..."; Check: not IsWebView2Installed(); Flags: waituntilterminated
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#StringChange(AppName, '&', '&&')}}"; WorkingDir: "{app}"; Flags: nowait postinstall skipifsilent

[Code]
function WebView2VersionOk(const Version: String): Boolean;
begin
  Result := (Version <> '') and (Version <> '0.0.0.0') and (Version <> '0.0.0');
end;

function IsWebView2Installed: Boolean;
var
  Version: String;
begin
  Result := RegQueryStringValue(
    HKLM,
    'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{#WebView2Clsid}',
    'pv', Version) and WebView2VersionOk(Version);

  if not Result then
    Result := RegQueryStringValue(
      HKLM,
      'SOFTWARE\Microsoft\EdgeUpdate\Clients\{#WebView2Clsid}',
      'pv', Version) and WebView2VersionOk(Version);

  if not Result then
    Result := RegQueryStringValue(
      HKCU,
      'Software\Microsoft\EdgeUpdate\Clients\{#WebView2Clsid}',
      'pv', Version) and WebView2VersionOk(Version);
end;


; F4/F13 (ANTIVIRUS_AUDIT): SignTool reference kept simple. Inno Setup 6.4+
; supports a [PostCompile] step that runs signtool.exe on the produced
; installer. The conditional `..\signing\vodrip.pfx` test in [Setup] will
; also add SignTool=vodrip to the build. We do NOT add a [SignTools] section
; here because Inno's parameter-substitution syntax is version-specific; the
; CI step in `.github/workflows/release.yml` is the source of truth and the
; docs/SIGNING.md (added in this change) covers the local-build flow.
; For local dev, just drop a pfx at ..\signing\vodrip.pfx and the build will
; sign automatically.

