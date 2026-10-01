<#
.SYNOPSIS
    Stops the Orya Observability Docker Compose stack.
.DESCRIPTION
    Gracefully stops all containers and prints final status.
    Also stops external drift-detector if present.
.PARAMETER RemoveVolumes
    Also delete named volumes (postgres_data, redis_data, etc.).
.PARAMETER RemoveImages
    Also remove project images.
#>
[CmdletBinding()]
param(
    [switch]$RemoveVolumes = $false,
    [switch]$RemoveImages  = $false
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$RootDir   = Split-Path -Parent $MyInvocation.MyCommand.Definition | Split-Path -Parent
$InfraDir  = Join-Path $RootDir 'infra'
$ComposeFile = Join-Path $InfraDir 'docker-compose.yml'
$OverrideFile = Join-Path $InfraDir 'docker-compose.override.yml'
$EnvFile     = Join-Path $InfraDir '.env'

function Write-Header($msg) {
    Write-Host "`n$('=' * 70)" -ForegroundColor Cyan
    Write-Host "  $msg" -ForegroundColor Cyan
    Write-Host "$('=' * 70)`n" -ForegroundColor Cyan
}

Write-Header 'Orya Observability — Stack Shutdown'

# Pre-flight
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Write-Error 'Docker is not installed or not in PATH.'
    exit 1
}
if (-not (docker info 2>$null)) {
    Write-Error 'Docker daemon is not running.'
    exit 1
}

# Down main stack
$downArgs = @('-f', $ComposeFile, '-f', $OverrideFile, '--env-file', $EnvFile, 'down')
if ($RemoveVolumes) { $downArgs += '-v' }
if ($RemoveImages)  { $downArgs += '--rmi', 'all' }

Write-Host "  docker compose $($downArgs -join ' ')" -ForegroundColor DarkGray
& docker compose @downArgs
if ($LASTEXITCODE -ne 0) {
    Write-Error "docker compose down exited with code $LASTEXITCODE"
    exit $LASTEXITCODE
}

# Stop drift-detector if present
$driftCompose = Join-Path $RootDir 'scripts\cron\docker-compose.drift.yml'
if (Test-Path $driftCompose) {
    Write-Host "  Checking for drift-detector..."
    try {
        & docker compose -f $driftCompose down
        Write-Host "  [OK] Drift-detector stopped." -ForegroundColor Green
    }
    catch {
        Write-Host "  [WARN] Failed to stop drift-detector: $($_.Exception.Message)" -ForegroundColor Yellow
    }
}

# Check remaining containers
Write-Header 'Post-down Status'
$remaining = & docker compose -f $ComposeFile -f $OverrideFile ps --format json 2>$null | ConvertFrom-Json
$remainingCount = ($remaining | Where-Object { $_.Service -match 'orya-observability' }).Count

if ($remainingCount -gt 0) {
    Write-Host "  [WARN] $remainingCount containers still running:" -ForegroundColor Yellow
    $remaining | Where-Object { $_.Service -match 'orya-observability' } | ForEach-Object {
        Write-Host "    $($_.Service) - $($_.State)" -ForegroundColor Yellow
    }
}
else {
    Write-Host "  [OK] All Orya Observability containers stopped." -ForegroundColor Green
}

Write-Header 'Done'
Write-Host "  All services stopped." -ForegroundColor Green
if ($RemoveVolumes) {
    Write-Host "  Volumes removed." -ForegroundColor Yellow
}
if ($RemoveImages) {
    Write-Host "  Project images removed." -ForegroundColor Yellow
}
Write-Host ""