<#
.SYNOPSIS
    Builds the ArchX3D desktop installer.

.DESCRIPTION
    Three stages, in order, because each consumes the previous one's output:

      1. web/out                     static frontend      (npm run build)
      2. desktop/dist/archx3d-backend frozen backend      (PyInstaller)
      3. the NSIS installer           both, wrapped       (cargo tauri build)

    Run from anywhere; paths are resolved relative to this script.

.PARAMETER SkipFrontend
    Reuse the existing web/out. Saves ~30 s when only Rust or Python changed.

.PARAMETER SkipBackend
    Reuse the existing desktop/dist. Saves ~2 min when only the shell or the
    frontend changed. The backend is the slowest stage by far.

.EXAMPLE
    .\desktop\build.ps1
    .\desktop\build.ps1 -SkipBackend
#>
[CmdletBinding()]
param(
    [switch]$SkipFrontend,
    [switch]$SkipBackend
)

$ErrorActionPreference = "Stop"

$DesktopDir = $PSScriptRoot
$RepoRoot = Split-Path -Parent $DesktopDir
$WebDir = Join-Path $RepoRoot "web"
$Venv = Join-Path $RepoRoot ".venv-build\Scripts\python.exe"

function Step($message) {
    Write-Host ""
    Write-Host "==> $message" -ForegroundColor Cyan
}

# --- MSVC + cargo on PATH ---------------------------------------------------
# Rust links with link.exe from the Visual Studio toolchain, which is not on
# PATH by default. Importing the developer shell also brings in the Windows SDK
# library paths, without which the link fails on kernel32.lib.
$vsInstaller = "C:\Program Files (x86)\Microsoft Visual Studio\Installer"
if (Test-Path $vsInstaller) { $env:Path = "$vsInstaller;$env:Path" }

# VsDevCmd.bat, not Launch-VsDevShell.ps1: the PowerShell wrapper only grew
# -Arch/-HostArch in VS 2022. On VS 2019 Build Tools — a documented prerequisite
# — those parameters do not exist, and because this script runs with
# $ErrorActionPreference = "Stop" the binding failure aborted the whole build
# before it reached stage 1. The batch file has taken -arch/-host_arch since
# VS 2017, so one call covers every supported version.
$vsDevCmd = Get-ChildItem "C:\Program Files*\Microsoft Visual Studio\*\*\Common7\Tools\VsDevCmd.bat" `
    -ErrorAction SilentlyContinue | Select-Object -First 1
if ($vsDevCmd) {
    # Best effort: rustc can often locate link.exe unaided, so a failure here
    # should let the link step speak for itself rather than stop the build.
    try {
        cmd /c "`"$($vsDevCmd.FullName)`" -arch=x64 -host_arch=x64 >nul 2>&1 && set" |
            ForEach-Object {
                if ($_ -match '^([^=]+)=(.*)$') {
                    Set-Item -Path "env:$($matches[1])" -Value $matches[2] -ErrorAction SilentlyContinue
                }
            }
    } catch {
        Write-Warning "Could not import the Visual Studio build environment: $_"
    }
} else {
    Write-Warning "Visual Studio build environment not found; the Rust link step may fail."
}
$env:Path = "$env:USERPROFILE\.cargo\bin;$env:Path"

# --- 1. Frontend ------------------------------------------------------------
if (-not $SkipFrontend) {
    Step "Building the static frontend"
    Push-Location $WebDir
    try {
        if (-not (Test-Path "node_modules")) { npm install }
        npm run build
        if ($LASTEXITCODE -ne 0) { throw "frontend build failed" }
    } finally { Pop-Location }
} else {
    Step "Skipping the frontend (-SkipFrontend)"
}

if (-not (Test-Path (Join-Path $WebDir "out\index.html"))) {
    throw "web/out is missing or empty. Run without -SkipFrontend."
}

# --- 2. Backend -------------------------------------------------------------
if (-not $SkipBackend) {
    Step "Freezing the Python backend"
    if (-not (Test-Path $Venv)) {
        Write-Host "Creating the build virtualenv..."
        python -m venv (Join-Path $RepoRoot ".venv-build")
        & $Venv -m pip install --upgrade pip
        & $Venv -m pip install -r (Join-Path $RepoRoot "requirements.txt") pyinstaller
    }
    Push-Location $RepoRoot
    try {
        & $Venv -m PyInstaller (Join-Path $DesktopDir "archx3d-backend.spec") `
            --noconfirm `
            --distpath (Join-Path $DesktopDir "dist") `
            --workpath (Join-Path $DesktopDir "build")
        if ($LASTEXITCODE -ne 0) { throw "backend freeze failed" }
    } finally { Pop-Location }
} else {
    Step "Skipping the backend (-SkipBackend)"
}

$backendExe = Join-Path $DesktopDir "dist\archx3d-backend\archx3d-backend.exe"
if (-not (Test-Path $backendExe)) {
    throw "The frozen backend is missing. Run without -SkipBackend."
}

# --- 3. Installer -----------------------------------------------------------
Step "Bundling the installer"
Push-Location (Join-Path $DesktopDir "src-tauri")
try {
    cargo tauri build
    if ($LASTEXITCODE -ne 0) { throw "tauri build failed" }
} finally { Pop-Location }

$installer = Get-ChildItem (Join-Path $DesktopDir "src-tauri\target\release\bundle\nsis\*.exe") `
    -ErrorAction SilentlyContinue | Select-Object -First 1

Write-Host ""
if ($installer) {
    $mb = [math]::Round($installer.Length / 1MB, 1)
    Write-Host "Installer ready: $($installer.FullName) ($mb MB)" -ForegroundColor Green
} else {
    throw "The build reported success but no installer was produced."
}
