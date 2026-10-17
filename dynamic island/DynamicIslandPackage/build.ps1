$ErrorActionPreference = "Stop"

$packageDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$sourceFile = Join-Path (Split-Path -Parent $packageDir) "dynamic_notch.py"
$requirementsFile = Join-Path $packageDir "requirements-build.txt"
$venvDir = Join-Path $packageDir ".venv"
$venvPython = Join-Path $venvDir "Scripts\python.exe"
$distDir = Join-Path $packageDir "dist"
$outputDir = Join-Path $packageDir "output"
$installerScript = Join-Path $packageDir "DynamicIsland.iss"
$appExe = Join-Path $distDir "DynamicIsland.exe"

if (-not (Test-Path $sourceFile)) {
    throw "Application source not found: $sourceFile"
}

if (-not (Test-Path $venvPython)) {
    $pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($pythonCommand) {
        & $pythonCommand.Source -m venv $venvDir
    } else {
        $pythonLauncher = Get-Command py.exe -ErrorAction SilentlyContinue
        if (-not $pythonLauncher) {
            throw "Python 3 is required to build the installer. Install Python 3 and rerun this script."
        }
        & $pythonLauncher.Source -3 -m venv $venvDir
    }
}

& $venvPython -m pip install --disable-pip-version-check -r $requirementsFile
if ($LASTEXITCODE -ne 0) { throw "Could not install build requirements." }

& $venvPython -m PyInstaller --noconfirm --clean --onefile --windowed `
    --name DynamicIsland `
    --distpath $distDir `
    --workpath (Join-Path $packageDir "build") `
    --specpath $packageDir `
    $sourceFile
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed." }

$compilerCandidates = @(
    (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe"),
    (Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe"),
    (Join-Path $env:ProgramFiles "Inno Setup 6\ISCC.exe")
)
$compilerCommand = Get-Command ISCC.exe -ErrorAction SilentlyContinue
$compiler = if ($compilerCommand) { $compilerCommand.Source } else {
    $compilerCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
}
if (-not $compiler) {
    throw "Inno Setup 6 is required to create the setup EXE. Install it and rerun this script."
}

New-Item -ItemType Directory -Path $outputDir -Force | Out-Null
& $compiler "/DAppExe=$appExe" "/DOutputDir=$outputDir" $installerScript
if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed." }

Write-Host "Installer created: $(Join-Path $outputDir 'DynamicIslandSetup.exe')"
