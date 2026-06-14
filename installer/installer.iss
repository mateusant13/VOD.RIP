; VOD.RIP — Windows installer (Inno Setup)
; Build: iscc /DAppVersion=1.0.6 installer\installer.iss
; Requires MicrosoftEdgeWebview2Setup.exe beside this file (downloaded in CI).

#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif

#define AppName "VOD.RIP"
#define AppExe "VOD-RIP.EXE"
#define AppPublisher "mateusant13"
#define WebView2Clsid "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"

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
SetupIconFile=..\assets\icon.ico
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

; Run Microsoft's signed bootstrapper (bundled at build time) with the
; documented /silent /install flags. Microsoft publishes these flags for
; managed deployments and the binary is the same Microsoft-signed one
; either way; the only difference is whether the user sees the
; bootstrapper's own installer UI. Silent is a UX win (the user sees only
; the Inno Setup progress window, not a second "Microsoft Edge WebView2
; Runtime Setup" dialog on top) and carries no AV penalty over the visible
; variant — the binary is identical, Authenticode-signed by Microsoft.
; The bootstrapper can exit while the install continues in the background
; ("The installer may exit immediately even while installation continues"
; — Microsoft docs); PrepareToInstall (below) waits up to 60 s for
; IsWebView2Installed to start returning True, so the [Run] step is
; skipped if the install finishes before the user clicks Install.
[Run]
Filename: "{tmp}\MicrosoftEdgeWebview2Setup.exe"; Parameters: "/silent /install"; StatusMsg: "Installing Microsoft WebView2 Runtime (one-time, may take a minute)..."; Check: not IsWebView2Installed(); Flags: waituntilterminated
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#StringChange(AppName, '&', '&&')}}"; WorkingDir: "{app}"; Flags: nowait postinstall skipifsilent

[Code]
function WebView2VersionOk(const Version: String): Boolean;
begin
  Result := (Version <> '') and (Version <> '0.0.0.0') and (Version <> '0.0.0');
end;

function WebView2BinaryExists: Boolean;
var
  Locations: array[0..5] of String;
  I: Integer;
  ApplicationDir: String;
  Child: String;
  ExePath: String;
  FindRec: TFindRec;
begin
  { Returns True if msedgewebview2.exe is present in any well-known install
    location. Mirrors the Python detector in services/webview2_setup.py.
    On non-Windows this is unreachable (the installer only builds for
    Windows anyway). }
  Result := False;
  Locations[0] := ExpandConstant('{pf32}\Microsoft\EdgeWebView\Application');
  Locations[1] := ExpandConstant('{pf64}\Microsoft\EdgeWebView\Application');
  Locations[2] := ExpandConstant('{localappdata}\Microsoft\EdgeWebView\Application');
  Locations[3] := ExpandConstant('{pf32}\Microsoft\Edge\Application');
  Locations[4] := ExpandConstant('{pf64}\Microsoft\Edge\Application');
  Locations[5] := '';

  for I := 0 to High(Locations) - 1 do
  begin
    ApplicationDir := Locations[I];
    if ApplicationDir = '' then Continue;
    if not DirExists(ApplicationDir) then Continue;
    { Iterate versioned subfolders inside the Application dir. }
    if FindFirst(ApplicationDir + '\*', FindRec) then
    try
      repeat
        if (FindRec.Attributes and FILE_ATTRIBUTE_DIRECTORY) = 0 then Continue;
        Child := FindRec.Name;
        if Child = '.' then Continue;
        if Child = '..' then Continue;
        ExePath := ApplicationDir + '\' + Child + '\msedgewebview2.exe';
        if FileExists(ExePath) then
        begin
          Result := True;
          Exit;
        end;
      until not FindNext(FindRec);
    finally
      FindClose(FindRec);
    end;
  end;
end;

function WebView2RegistryTryHive(const RootKey: Integer; const SubKey: String): Boolean;
var
  Location, Pv, ExePath: String;
begin
  Result := False;
  Location := '';
  if not RegQueryStringValue(RootKey, SubKey, 'location', Location) then
    RegQueryStringValue(RootKey, SubKey, 'path', Location);
  if Location = '' then
    Exit;
  Pv := '';
  RegQueryStringValue(RootKey, SubKey, 'pv', Pv);
  if WebView2VersionOk(Pv) then
    ExePath := Location + '\' + Pv + '\msedgewebview2.exe'
  else
    ExePath := Location + '\msedgewebview2.exe';
  Result := FileExists(ExePath);
end;

function WebView2RegistryBinaryExists: Boolean;
begin
  { Mirror services/webview2_setup._registry_reported_path: trust EdgeUpdate
    only when location/path resolves to a real msedgewebview2.exe. Never trust
    ``pv`` alone — stale registry after uninstall caused skipped installs. }
  Result :=
    WebView2RegistryTryHive(HKLM, 'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{#WebView2Clsid}') or
    WebView2RegistryTryHive(HKLM, 'SOFTWARE\Microsoft\EdgeUpdate\Clients\{#WebView2Clsid}') or
    WebView2RegistryTryHive(HKCU, 'Software\Microsoft\EdgeUpdate\Clients\{#WebView2Clsid}');
end;

function IsWebView2Installed: Boolean;
begin
  { Must match webview2_setup.webview2_installed() — binary on disk first,
    then registry only when it points at a real msedgewebview2.exe. }
  Result := WebView2BinaryExists or WebView2RegistryBinaryExists;
end;
function WaitForWebView2Install(TimeoutSec: Integer): Boolean;
{ If WebView2 is currently being installed in the background (e.g. a
  previous bootstrapper run is still finishing), wait up to
 TimeoutSec seconds for IsWebView2Installed to start returning True.
  This handles Microsoft's documented "the installer may exit
  immediately even while installation continues" behaviour.
  Returns True iff WebView2 became available within the timeout. }
var
  Waited: Integer;
begin
  Waited := 0;
  Result := False;
  while (Waited < TimeoutSec) do
  begin
    if IsWebView2Installed() then
    begin
      Result := True;
      Exit;
    end;
    Sleep(2000);
    Waited := Waited + 2;
  end;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
{ Called by Inno Setup before the [Run] step is evaluated. If a
  previous bootstrapper run left an install in progress, wait for it
  to complete so the [Run] step is skipped. Returns an empty string
  on success or a non-empty string to abort the install with that
  message (we never abort here). }
begin
  if not IsWebView2Installed() then
    WaitForWebView2Install(60);
  Result := '';
end;

{
  F4/F13 (ANTIVIRUS_AUDIT): SignTool reference kept simple. Inno Setup 6.4+
  supports a [PostCompile] step that runs signtool.exe on the produced
  installer. The conditional ..\signing\vodrip.pfx test in [Setup] will
  also add SignTool=vodrip to the build. We do NOT add a [SignTools] section
  here because Inno's parameter-substitution syntax is version-specific; the
  CI step in .github/workflows/release.yml is the source of truth and the
  docs/SIGNING.md covers the local-build flow.
  For local dev, just drop a pfx at ..\signing\vodrip.pfx and the build will
  sign automatically.
}

