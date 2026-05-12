; NP Create -- Windows installer (Inno Setup 6.x)
;
; What this builds
; ----------------
; A single self-contained ``NP-Create-Setup-<version>.exe`` that
; the customer double-clicks. The installer:
;
;   1. Shows a Thai/English language picker (Inno auto-detects).
;   2. Asks the customer to accept the EULA (installer-license.txt).
;   3. Installs to ``%LOCALAPPDATA%\NP Create`` -- per-user, no UAC
;      prompt, no admin password. We deliberately do *not* default
;      to ``Program Files`` because most customers run as a
;      Standard User on shared family PCs.
;   4. Lays down ``NP-Create.exe`` (PyInstaller bundle) + ``.tools\``
;      (adb, JDK 21, lspatch). Tools live next to the exe so the
;      bundled platform_tools.find_adb()/find_ffmpeg() resolver
;      keeps working unchanged.
;   5. Creates Desktop + Start Menu shortcuts.
;   6. Registers an uninstaller (Settings ▸ Apps ▸ NP Create).
;
; Build prerequisites (Windows host, one-time)
; --------------------------------------------
;   * Python 3.11 or 3.13 with ``pip install pyinstaller``
;   * Inno Setup 6: https://jrsoftware.org/isinfo.php
;
; Build steps
; -----------
;   1. ``python tools\build_pyinstaller.py``       (produces NP-Create.exe)
;   2. ``ISCC tools\installer.iss``                (compiles the installer)
;
; Output: ``vcam-pc\dist\installer\NP-Create-Setup-<version>.exe``
;
; Why we build the installer here, not in build_release.py
; --------------------------------------------------------
; Inno Setup is a Windows-native compiler. ``build_release.py``
; builds the cross-platform ZIPs from any host (the existing flow
; for power-user customers); the installer is the *consumer*-grade
; experience and naturally lives in its own pipeline. CI runs both
; in parallel on tag push (see ``.github/workflows/release.yml``).
;

#define MyAppName      "NP Create"
; MyAppVersion can be overridden from the ISCC command line, e.g.
;   ISCC /DMyAppVersion=1.7.6 tools\installer.iss
; Our build_installer.bat / GitHub Actions workflow injects the
; canonical value pulled out of ``src/branding.py``. The string
; below is just the fallback for "I ran ISCC by hand without
; passing /D". Keep it in sync with branding.py before tagging a
; release; CI will warn if the two diverge by more than a tag.
#ifndef MyAppVersion
#define MyAppVersion "1.8.12"
#endif
#define MyAppPublisher "NP Create Co., Ltd."
#define MyAppURL       "https://line.me/R/ti/p/@npcreate"
#define MyAppExeName   "NP-Create.exe"
#define MyOutputName   "NP-Create-Setup-" + MyAppVersion

; All [Setup] / [Files] paths in this script are relative to the
; .iss file location (vcam-pc/tools/). PROJ_ROOT walks one level
; up to vcam-pc/. WORKSPACE_ROOT walks one further to the repo
; root where .tools/ lives as a sibling of vcam-pc/.
#define PROJ_ROOT       ".."
#define WORKSPACE_ROOT  "..\.."

[Setup]
; AppId: keep this stable across versions or upgrade detection breaks.
AppId={{8E5C9F1A-3B7D-4E2A-9C8B-1D5F6E7A8B9C}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
DefaultDirName={localappdata}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
LicenseFile={#PROJ_ROOT}\tools\installer-license.txt
OutputDir={#PROJ_ROOT}\dist\installer
OutputBaseFilename={#MyOutputName}
SetupIconFile={#PROJ_ROOT}\assets\logo.ico
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
; Per-user install: no UAC prompt, lands in %LOCALAPPDATA%.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; Show the version in the Add/Remove Programs entry so support can
; tell which build the customer has at a glance.
VersionInfoVersion={#MyAppVersion}.0
VersionInfoCompany={#MyAppPublisher}
VersionInfoProductName={#MyAppName}
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName} {#MyAppVersion}
; Thai locale uses Buddhist calendar; force ISO date in logs to
; avoid confusing customers reading "2569" timestamps.
TimeStampsInUTC=yes

[Languages]
; Inno ships Thai.isl from version 6.2+. If you build on an older
; Inno, copy Thai.isl from https://jrsoftware.org/files/istrans/
; into your Inno Setup ``Languages`` directory first.
Name: "thai";    MessagesFile: "compiler:Languages\Thai.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[CustomMessages]
; Per-language strings for our custom shortcuts. Splitting them
; out (instead of putting Thai bytes inline in [Icons]) keeps the
; .iss file safe to edit on systems where the default text
; encoding is not UTF-8 -- a known Inno Setup foot-gun on older
; Windows builds where ISCC may parse the file as Windows-1252.
english.UserManual=User Manual
thai.UserManual=คู่มือการใช้งาน
english.ContactAdmin=Contact Admin (Line OA)
thai.ContactAdmin=ติดต่อแอดมิน Line OA

[Tasks]
; Desktop shortcut is opt-in but checked by default -- customer
; explicitly chose to install, they expect a shortcut.
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: checkablealone

[Files]
; Main app exe (PyInstaller --onefile output).
Source: "{#PROJ_ROOT}\dist\pyinstaller\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion

; Phone tools -- adb, JDK 21, lspatch. ~190 MB. Recursesubdirs
; preserves the directory tree under .tools\windows\.
Source: "{#WORKSPACE_ROOT}\.tools\windows\*"; DestDir: "{app}\.tools\windows"; Flags: ignoreversion recursesubdirs createallsubdirs

; vcam-app APK — the Xposed module that LSPatch fuses into TikTok
; during Patch. ``find_vcam_apk()`` looks here at runtime; missing
; means Patch fails with "vcam-app APK not found". ~7.5 MB.
Source: "{#WORKSPACE_ROOT}\apk\vcam-app-release.apk"; DestDir: "{app}\apk"; Flags: ignoreversion

; Documentation customers will reference. SALES_KIT_TH.md is
; admin-only (pricing scripts, Line templates) and MUST NOT ship.
Source: "{#PROJ_ROOT}\docs\MANUAL_TH.md"; DestDir: "{app}"; Flags: ignoreversion isreadme
Source: "installer-license.txt"; DestDir: "{app}"; DestName: "LICENSE_TH.txt"; Flags: ignoreversion

; Logo assets (used at runtime + by uninstaller display).
Source: "{#PROJ_ROOT}\assets\logo.ico"; DestDir: "{app}\assets"; Flags: ignoreversion
Source: "{#PROJ_ROOT}\assets\logo.png"; DestDir: "{app}\assets"; Flags: ignoreversion skipifsourcedoesntexist

[Icons]
Name: "{group}\{#MyAppName}";              Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\assets\logo.ico"
Name: "{group}\{cm:UserManual}";           Filename: "{app}\MANUAL_TH.md"
Name: "{group}\{cm:ContactAdmin}";         Filename: "{#MyAppURL}"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}";        Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\assets\logo.ico"; Tasks: desktopicon

[Run]
; Optional final-page checkbox: launch the app right after install.
; ``nowait`` so the installer wizard exits cleanly.
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#MyAppName}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; The app writes a few JSON files to {app} at runtime
; (config.json, profiles.json, customer_devices.json,
; license/.activated, .install_done). Wipe them on uninstall so
; reinstall starts from a clean slate.
Type: files;          Name: "{app}\config.json"
Type: files;          Name: "{app}\profiles.json"
Type: files;          Name: "{app}\customer_devices.json"
Type: files;          Name: "{app}\license\activated.json"
Type: files;          Name: "{app}\.install_done"
Type: dirifempty;     Name: "{app}\license"
Type: dirifempty;     Name: "{app}\videos"
Type: dirifempty;     Name: "{app}\logs"

[Code]
function InitializeSetup(): Boolean;
begin
  // Reserved for future preflight checks (Windows version, free
  // disk space, etc.). For now always allow install -- Inno's
  // built-in MinVersion handles the OS check.
  Result := True;
end;
