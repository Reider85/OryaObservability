<#
.SYNOPSIS
    Stops the Orya Observability Docker Compose stack.
.DESCRIPTION
    Gracefully stops all containers and prints final status.
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

$InfraDir    = Split-Path -Parent $MyInvocation.MyCommand.Definition | Split-Path -Parent
$ComposeFile = Join-Path $InfraDir 'infra' 'docker-compose.yml'
$EnvFile     = Join-Path $InfraDir 'infra' '.env'

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

# Down
$downArgs = @('-f', $ComposeFile, '--env-file', $EnvFile, 'down')
if ($RemoveVolumes) { $downArgs += '-v' }
if ($RemoveImages)  { $downArgs += '--rmi', 'all' }

Write-Host "  docker compose $($downArgs -join ' ')" -ForegroundColor DarkGray
& docker compose @downArgs
if ($LASTEXITCODE -ne 0) {
    Write-Error "docker compose down exited with code $LASTEXITCODE"
    exit $LASTEXITCODE
}

Write-Header 'Done'
Write-Host "  All services stopped." -ForegroundColor Green
if ($RemoveVolumes) {
    Write-Host "  Volumes removed." -ForegroundColor Yellow
}
Write-Host ""
