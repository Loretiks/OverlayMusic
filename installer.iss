; Inno Setup script for SpotifyOverlay
; Build: run `build-installer.bat` (it calls ISCC.exe).

#define MyAppName       "Spotify Overlay"
#define MyAppShortName  "SpotifyOverlay"
#define MyAppVersion    "1.0.0"
#define MyAppPublisher  "Ilyushka"
#define MyAppExeName    "SpotifyOverlay.exe"

[Setup]
; Stable AppId — keep this exact value across releases so upgrades work.
AppId={{A7E13B91-8C29-4B65-A4F2-2F58E1D3A8B6}}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppShortName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
DisableDirPage=auto

OutputDir=installer
OutputBaseFilename={#MyAppShortName}-Setup-{#MyAppVersion}

Compression=lzma2/ultra
SolidCompression=yes
WizardStyle=modern
ShowLanguageDialog=no

; Per-user install — no admin prompt, but allow elevation if user prefers Program Files.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog

ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName}

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; \
    Description: "{cm:CreateDesktopIcon}"; \
    GroupDescription: "{cm:AdditionalIcons}"; \
    Flags: unchecked
Name: "autostart"; \
    Description: "Запускать при входе в Windows"; \
    GroupDescription: "Дополнительно:"; \
    Flags: unchecked

[Files]
Source: "dist\SpotifyOverlay.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; \
    ValueType: string; ValueName: "SpotifyOverlay"; \
    ValueData: """{app}\{#MyAppExeName}"""; \
    Tasks: autostart; \
    Flags: uninsdeletevalue

[Run]
Filename: "{app}\{#MyAppExeName}"; \
    Description: "Запустить {#MyAppName}"; \
    Flags: nowait postinstall skipifsilent

[UninstallRun]
; Try to stop the app on uninstall (ignore failure — process may not be running).
Filename: "taskkill.exe"; Parameters: "/F /IM {#MyAppExeName}"; \
    Flags: runhidden; RunOnceId: "KillSpotifyOverlay"
