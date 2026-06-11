; VOD.RIP — Windows installer (Inno Setup)
; Build: iscc /DAppVersion=1.0.3 installer\installer.iss

#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif

#define AppName "VOD.RIP"
#define AppExe "VOD-RIP.EXE"
#define AppPublisher "mateusant13"

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

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"; WorkingDir: "{app}"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon; WorkingDir: "{app}"

[Run]
Filename: "{app}\{#AppExe}"; Description: "Launch {#AppName}"; Flags: nowait postinstall skipifsilent
