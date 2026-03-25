Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Test-Command {
    param([string]$Name)
    return [bool](Get-Command $Name -ErrorAction SilentlyContinue)
}

function Add-UserPathIfNeeded {
    param([string]$Dir)

    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $parts = @()
    if ($userPath) {
        $parts = $userPath.Split(';') | Where-Object { $_ -ne "" }
    }

    if ($parts -notcontains $Dir) {
        $newPath = if ($userPath) { "$userPath;$Dir" } else { $Dir }
        [Environment]::SetEnvironmentVariable("Path", $newPath, "User")
        $env:Path = "$env:Path;$Dir"
    }
}

if (-not (Test-Command "git")) {
    throw "Git is not installed or not on PATH."
}

if (-not (Test-Command "uv")) {
    Write-Host "uv not found. Installing uv..."
    powershell -ExecutionPolicy Bypass -c "irm https://astral.sh/uv/install.ps1 | iex"
}

$uvBin = Join-Path $env:USERPROFILE ".local\bin"
if (Test-Path $uvBin) {
    Add-UserPathIfNeeded -Dir $uvBin
}

if (-not (Test-Command "uv")) {
    throw "uv is still not available after installation."
}

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Push-Location $scriptDir
try {
    uv run .\setup_auto_commit.py --write-default-config
}
finally {
    Pop-Location
}