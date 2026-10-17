#define AppName "Dynamic Island"
#define AppVersion "1.0.0"

#ifndef AppExe
  #error AppExe must point to the packaged DynamicIsland.exe
#endif
#ifndef OutputDir
  #define OutputDir "output"
#endif

[Setup]
AppId={{7B5A91D4-2B58-4BC7-9C24-2DF70CFA9E61}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=Dynamic Island
DefaultDirName={localappdata}\Programs\DynamicIsland
DefaultGroupName=Dynamic Island
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesInstallIn64BitMode=x64
OutputDir="{#OutputDir}"
OutputBaseFilename=DynamicIslandSetup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayName=Dynamic Island

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked
Name: "startup"; Description: "Start Dynamic Island when I sign in"; GroupDescription: "Additional options:"; Flags: unchecked

[Files]
Source: "{#AppExe}"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\Dynamic Island"; Filename: "{app}\DynamicIsland.exe"
Name: "{autodesktop}\Dynamic Island"; Filename: "{app}\DynamicIsland.exe"; Tasks: desktopicon
Name: "{userstartup}\Dynamic Island"; Filename: "{app}\DynamicIsland.exe"; Tasks: startup

[Run]
Filename: "{app}\DynamicIsland.exe"; Description: "Launch Dynamic Island"; Flags: postinstall nowait skipifsilent
