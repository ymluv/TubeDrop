$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

$Python = Join-Path $Root ".venv\Scripts\python.exe"

if (-not (Test-Path $Python)) {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        py -3 -m venv .venv
    } elseif (Get-Command python -ErrorAction SilentlyContinue) {
        python -m venv .venv
    } else {
        throw "Python 3.10+ is required only for building the exe."
    }
}

& $Python -m pip install --upgrade pip
& $Python -m pip install -r requirements.txt pyinstaller

$BuildDir = Join-Path $Root "build"
$DistDir = Join-Path $Root "dist"

foreach ($Path in @($BuildDir, $DistDir)) {
    if (Test-Path $Path) {
        Remove-Item -LiteralPath $Path -Recurse -Force
    }
}

& $Python -m PyInstaller --clean --noconfirm TubeDrop.spec

$ExePath = Join-Path $DistDir "TubeDrop.exe"
if (-not (Test-Path $ExePath)) {
    throw "TubeDrop.exe was not created."
}

$ZipPath = Join-Path $DistDir "TubeDrop-windows-x64.zip"
Compress-Archive -LiteralPath $ExePath -DestinationPath $ZipPath -Force

Write-Host "Created:"
Write-Host $ExePath
Write-Host $ZipPath
