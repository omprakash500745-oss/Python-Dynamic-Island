Dynamic Island Windows Installer
=================================

Build output:
  output\DynamicIslandSetup.exe

The setup installs Dynamic Island for the current Windows user and does not require administrator access. The installer bundles the application and its Python dependencies; the target PC does not need Python or pip. Windows PowerShell is used for built-in media and notification APIs and is included with supported Windows installations.

First run:
  The app asks whether to open Windows notification settings. Notification access is controlled by Windows and must be approved by the user. The prompt is optional; the app can still show its system-monitoring alerts without access to other apps' notifications.

Build requirements (developer PC only):
  - Windows 10/11, 64-bit
  - Python 3
  - Inno Setup 6
  - Internet access to install the pinned Python build requirements

Build from PowerShell:
  .\build.ps1

The installer is per-user and unsigned. Windows SmartScreen may show a warning until the executable is signed with a publisher certificate.
