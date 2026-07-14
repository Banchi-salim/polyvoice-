; PolyVoice.iss — Inno Setup script
; Compile with Inno Setup Compiler (ISCC.exe) or the GUI "Compile" button

#define MyAppName "PolyVoice"
#define MyAppVersion "1.0.0"
#define MyAppPublisher "Saleem_Banchi"
#define MyAppExeName "PolyVoice.exe"
#define MyDistDir "dist\PolyVoice"

[Setup]
AppId={{3A25CC64-D17A-48A2-A919-902DE31F53E5}}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
; Per-user install by default so the app and installer are easier to share.
DefaultDirName={userpf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=installer_output
OutputBaseFilename={#MyAppName}_Setup_{#MyAppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional icons:"

[Files]
; Explicit per-pattern list. Don't use a single recursive wildcard
; here — that would ship anything PyInstaller happened to COLLECT,
; and a future accidental `datas=` entry in the spec could silently
; include a dev data directory. With explicit entries, anyone reading
; the .iss can confirm at a glance what gets installed.
Source: "{#MyDistDir}\PolyVoice.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#MyDistDir}\_internal\*"; DestDir: "{app}\_internal"; Flags: ignoreversion recursesubdirs createallsubdirs
; README is optional — produced by the spec when dist/PolyVoice.README.txt
; is present next to the spec. The Check function makes the entry a no-op
; when the file is missing, so a build without the README still succeeds.
Source: "{#MyDistDir}\README.txt"; DestDir: "{app}"; Flags: ignoreversion; Check: FileExists(AddBackslash('{#MyDistDir}') + 'README.txt')
; Defensive: nothing else from {#MyDistDir}\ should be packaged. If
; you find yourself needing to add another entry here, make sure the
; path comes from a curated PyInstaller COLLECT output, not a dev dir
; that happens to live under dist\.

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Clean up whatever the app extracted to %APPDATA% at first run
Type: filesandordirs; Name: "{userappdata}\{#MyAppName}"
