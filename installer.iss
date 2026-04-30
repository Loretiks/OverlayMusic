; Inno Setup script for OverlayMusic
; Build: run `build-installer.bat` (it calls ISCC.exe).

#define MyAppName       "Overlay Music"
#define MyAppShortName  "OverlayMusic"
#define MyAppVersion    "1.1.0"
#define MyAppPublisher  "Melanholy"
#define MyAppExeName    "OverlayMusic.exe"

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

; Admin install — нужен и для записи в Program Files, и для создания
; Scheduled Task с /RL HIGHEST (только админы могут это сделать).
PrivilegesRequired=admin

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
    GroupDescription: "{cm:AdditionalIcons}"
Name: "autostart"; \
    Description: "Автозапуск при входе в Windows (от имени администратора, без UAC-промпта)"; \
    GroupDescription: "Дополнительно:"

[Files]
Source: "dist\OverlayMusic.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
; Создаём Scheduled Task: запуск с highest privileges при логине пользователя.
; /RL HIGHEST + /SC ONLOGON = запускается автоматически как админ без UAC-промпта.
Filename: "{sys}\schtasks.exe"; \
    Parameters: "/Create /TN ""{#MyAppShortName}"" /TR ""\""{app}\{#MyAppExeName}\"""" /SC ONLOGON /RL HIGHEST /F"; \
    Tasks: autostart; \
    Flags: runhidden

; После установки запускаем приложение (от имени админа, через ту же задачу).
Filename: "{sys}\schtasks.exe"; \
    Parameters: "/Run /TN ""{#MyAppShortName}"""; \
    Tasks: autostart; \
    Flags: runhidden nowait postinstall skipifsilent
; Если автостарт не выбран — просто запускаем .exe (запросит UAC).
Filename: "{app}\{#MyAppExeName}"; \
    Description: "Запустить {#MyAppName}"; \
    Flags: nowait postinstall skipifsilent unchecked

[UninstallRun]
; Сначала останавливаем процесс.
Filename: "taskkill.exe"; Parameters: "/F /IM {#MyAppExeName}"; \
    Flags: runhidden; RunOnceId: "KillOverlayMusic"
; Удаляем Scheduled Task.
Filename: "{sys}\schtasks.exe"; Parameters: "/Delete /TN ""{#MyAppShortName}"" /F"; \
    Flags: runhidden; RunOnceId: "DelOverlayMusicTask"
