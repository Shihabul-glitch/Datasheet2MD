<#
.SYNOPSIS
    Build Datasheet2MD.exe with PyInstaller.

.DESCRIPTION
    Packages ONLY the GUI frontend (CustomTkinter + tkinterdnd2). Docling, PyTorch
    and the Docling models are intentionally NOT bundled: the executable calls the
    `docling` command already installed on this computer.

    Release -> dist\Datasheet2MD.exe        (one file, no console window)
    Debug   -> dist\Datasheet2MD-Debug.exe  (one file, with a console for diagnostics)

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File build\build.ps1
    powershell -ExecutionPolicy Bypass -File build\build.ps1 -Mode Both
    powershell -ExecutionPolicy Bypass -File build\build.ps1 -Mode Debug -SkipTests
#>
[CmdletBinding()]
param(
    [ValidateSet("Release", "Debug", "Both")]
    [string]$Mode = "Release",
    [switch]$SkipTests,
    [switch]$SkipInstall
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
Write-Host "== Datasheet2MD build ($Mode) in $Root" -ForegroundColor Cyan

# 1. Virtual environment (a dedicated venv keeps Docling/torch out of the bundle)
$Py = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Py)) {
    Write-Host "== Creating virtual environment .venv" -ForegroundColor Cyan
    python -m venv (Join-Path $Root ".venv")
    if ($LASTEXITCODE -ne 0) { throw "Could not create the virtual environment (is Python on PATH?)" }
}

# 2. Build dependencies
if (-not $SkipInstall) {
    Write-Host "== Installing/updating build dependencies" -ForegroundColor Cyan
    & $Py -m pip install --disable-pip-version-check -q --upgrade pip
    & $Py -m pip install --disable-pip-version-check -q -r (Join-Path $Root "requirements.txt")
    if ($LASTEXITCODE -ne 0) { throw "pip install failed" }
}

# Unit tests
if (-not $SkipTests) {
    Write-Host "== Running unit tests" -ForegroundColor Cyan
    & $Py -m pytest
    if ($LASTEXITCODE -ne 0) { throw "Unit tests failed - not building." }
}

# 3. Clean old build artifacts
$Work = Join-Path $Root "build\pyinstaller"
$Dist = Join-Path $Root "dist"
Write-Host "== Cleaning old build artifacts" -ForegroundColor Cyan
foreach ($p in @($Work, (Join-Path $Dist "Datasheet2MD.exe"), (Join-Path $Dist "Datasheet2MD-Debug.exe"))) {
    if (Test-Path $p) { Remove-Item -Recurse -Force $p }
}
New-Item -ItemType Directory -Force $Dist | Out-Null

# 4. Build
$Icon = Join-Path $Root "assets\app.ico"
$Common = @(
    "--noconfirm", "--clean", "--onefile",
    "--icon", $Icon,
    "--add-data", "$Icon;assets",
    "--collect-data", "customtkinter",      # themes (json) and fonts
    "--collect-all", "tkinterdnd2",         # native tkdnd library for drag & drop
    "--workpath", $Work,
    "--specpath", $Work,
    "--distpath", $Dist,
    "--paths", $Root,
    (Join-Path $Root "main.py")
)

function Invoke-Build([string]$Name, [string[]]$Extra) {
    Write-Host "== PyInstaller: $Name" -ForegroundColor Cyan
    & $Py -m PyInstaller --name $Name @Extra @Common
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed for $Name" }
    $exe = Join-Path $Dist "$Name.exe"
    if (-not (Test-Path $exe)) { throw "Expected output not found: $exe" }
    $mb = [math]::Round((Get-Item $exe).Length / 1MB, 1)
    Write-Host "   -> $exe ($mb MB)" -ForegroundColor Green
}

if ($Mode -in @("Release", "Both")) { Invoke-Build "Datasheet2MD" @("--noconsole") }
if ($Mode -in @("Debug", "Both"))   { Invoke-Build "Datasheet2MD-Debug" @("--console") }

Write-Host "== Done" -ForegroundColor Green
