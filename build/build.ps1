<#
.SYNOPSIS
    Build Datasheet2MD.exe with PyInstaller.

.DESCRIPTION
    Packages ONLY the GUI/CLI frontend (CustomTkinter, tkinterdnd2) and the small
    engine-worker script. Docling, PyTorch and the Docling models are intentionally
    NOT bundled: the executable runs the Docling installation (and model cache)
    already present on the computer.

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
    [switch]$SkipInstall,
    [switch]$SkipSelfTest
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
    "--add-data", "$(Join-Path $Root 'core\docling_worker.py');core",  # runs inside Docling's Python
    "--exclude-module", "torch", "--exclude-module", "docling", "--exclude-module", "numpy", "--exclude-module", "pypdfium2",
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
    if ($mb -gt 60) { throw "$Name.exe is $mb MB - something heavy (torch/Docling?) was bundled by mistake." }
}

function Test-Exe([string]$Name) {
    # Starts the packaged EXE with --self-test: checks pdfium, the bundled worker script
    # and Docling detection from the frozen executable. Exit 0 = ready, 3 = problems.
    $exe = Join-Path $Dist "$Name.exe"
    Write-Host "== Self-test: $Name.exe --self-test" -ForegroundColor Cyan
    $p = Start-Process -FilePath $exe -ArgumentList "--self-test" -Wait -PassThru -NoNewWindow
    if ($p.ExitCode -eq 0) { Write-Host "   self-test OK" -ForegroundColor Green }
    elseif ($p.ExitCode -eq 3) { Write-Warning "$Name.exe starts, but Docling/pdfium is not ready on this PC (exit 3)." }
    else { throw "$Name.exe --self-test failed with exit code $($p.ExitCode)" }
}

if ($Mode -in @("Release", "Both")) { Invoke-Build "Datasheet2MD" @("--noconsole") }
if ($Mode -in @("Debug", "Both"))   { Invoke-Build "Datasheet2MD-Debug" @("--console") }
if (-not $SkipSelfTest) {
    if ($Mode -in @("Debug", "Both"))   { Test-Exe "Datasheet2MD-Debug" }
    if ($Mode -in @("Release", "Both")) { Test-Exe "Datasheet2MD" }
}

Write-Host "== Done" -ForegroundColor Green
