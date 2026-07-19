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

function Remove-BuildPathWithRetry {
    param([Parameter(Mandatory = $true)][string]$Path)

    $ResolvedRoot = [System.IO.Path]::GetFullPath($Root).TrimEnd('\') + '\'
    $ResolvedPath = [System.IO.Path]::GetFullPath($Path)
    if (-not $ResolvedPath.StartsWith($ResolvedRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to remove a build path outside the project: $ResolvedPath"
    }

    for ($Attempt = 1; $Attempt -le 10; $Attempt++) {
        if (-not (Test-Path -LiteralPath $ResolvedPath)) {
            return
        }
        try {
            Remove-Item -LiteralPath $ResolvedPath -Recurse -Force -ErrorAction Stop
            return
        } catch {
            if ($Attempt -eq 10) {
                throw
            }
            Start-Sleep -Milliseconds 800
        }
    }
}

foreach ($Path in @($BuildDir, $DistDir)) {
    Remove-BuildPathWithRetry -Path $Path
}

& $Python -m PyInstaller --clean --noconfirm TubeDrop.spec

$AppDir = Join-Path $DistDir "TubeDrop"
$ExePath = Join-Path $AppDir "TubeDrop.exe"
if (-not (Test-Path $ExePath)) {
    throw "TubeDrop.exe was not created."
}

$ZipPath = Join-Path $DistDir "TubeDrop-windows-x64.zip"
Compress-Archive -Path (Join-Path $AppDir "*") -DestinationPath $ZipPath -Force

Write-Host "Created:"
Write-Host $AppDir
Write-Host $ExePath
Write-Host $ZipPath
