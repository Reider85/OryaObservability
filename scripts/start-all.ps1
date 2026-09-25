<#
.SYNOPSIS
    Starts the full Orya Observability stack (infra + SDK components).
.DESCRIPTION
    1. Validates prerequisites (Docker, .env).
    2. Brings up the Docker Compose stack (Postgres, Redis, ClickHouse, MinIO,
       Langfuse v3, OTel Collector, Prometheus).
    3. Waits until all services are healthy.
    4. Prints service status and connection info.
.PARAMETER Detach
    Run compose in detached mode (default: true).
.PARAMETER Build
    Force image rebuild before starting.
.PARAMETER NoWait
    Skip the health-check wait loop.
#>
[CmdletBinding()]
param(
    [switch]$Detach    = $true,
    [switch]$Build     = $false,
    [switch]$NoWait    = $false
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$RootDir   = Split-Path -Parent $MyInvocation.MyCommand.Definition
$InfraDir  = Join-Path $RootDir 'infra'
$ComposeFile = Join-Path $InfraDir 'docker-compose.yml'
$EnvFile     = Join-Path $InfraDir '.env'
$EnvExample  = Join-Path $InfraDir '.env.example'

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

function Write-Header($msg) {
    Write-Host "`n$('=' * 70)" -ForegroundColor Cyan
    Write-Host "  $msg" -ForegroundColor Cyan
    Write-Host "$('=' * 70)`n" -ForegroundColor Cyan
}

function Wait-ForServiceHealth {
    param(
        [string]$Service,
        [int]$TimeoutSec = 300,
        [int]$IntervalSec = 5
    )
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        $status = docker compose -f $ComposeFile ps --format json 2>$null |
            ConvertFrom-Json | Where-Object { $_.Service -eq $Service } |
            Select-Object -ExpandProperty Health -ErrorAction SilentlyContinue
        if ($status -eq 'healthy') {
            Write-Host "  [OK] $Service is healthy" -ForegroundColor Green
            return $true
        }
        Start-Sleep -Seconds $IntervalSec
    }
    Write-Host "  [TIMEOUT] $Service did not become healthy in ${TimeoutSec}s" -ForegroundColor Yellow
    return $false
}

# ---------------------------------------------------------------------------
# Pre-flight checks
# ---------------------------------------------------------------------------

Write-Header 'Orya Observability — Stack Startup'

# Docker availability
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Write-Error 'Docker is not installed or not in PATH.'
    exit 1
}
if (-not (docker info 2>$null)) {
    Write-Error 'Docker daemon is not running. Start Docker Desktop first.'
    exit 1
}

# .env file
if (-not (Test-Path $EnvFile)) {
    if (Test-Path $EnvExample) {
        Write-Host "[INFO] .env not found — copying from .env.example" -ForegroundColor Yellow
        Copy-Item $EnvExample $EnvFile
        Write-Host "[INFO] Edit $EnvFile with real values before production use.`n" -ForegroundColor Yellow
    } else {
        Write-Error '.env.example not found — cannot bootstrap configuration.'
        exit 1
    }
}

# ---------------------------------------------------------------------------
# Docker Compose
# ---------------------------------------------------------------------------

Write-Header 'Starting Docker Compose stack'

$composeArgs = @('-f', $ComposeFile, '--env-file', $EnvFile, 'up')
if ($Detach) { $composeArgs += '-d' }
if ($Build)  { $composeArgs += '--build' }

Write-Host "  docker compose $($composeArgs -join ' ')" -ForegroundColor DarkGray
& docker compose @composeArgs
if ($LASTEXITCODE -ne 0) {
    Write-Error "docker compose up exited with code $LASTEXITCODE"
    exit $LASTEXITCODE
}

# ---------------------------------------------------------------------------
# Health checks (optional)
# ---------------------------------------------------------------------------

if (-not $NoWait) {
    Write-Header 'Waiting for services to become healthy'

    $services = @('postgres', 'redis', 'clickhouse', 'minio', 'langfuse')
    $allHealthy = $true

    foreach ($svc in $services) {
        $ok = Wait-ForServiceHealth -Service $svc -TimeoutSec 300
        if (-not $ok) { $allHealthy = $false }
    }

    if ($allHealthy) {
        Write-Host "`n  All core services are healthy." -ForegroundColor Green
    } else {
        Write-Host "`n  Some services did not become healthy. Check logs with:" -ForegroundColor Yellow
        Write-Host "    docker compose -f $ComposeFile logs`n" -ForegroundColor Yellow
    }
}

# ---------------------------------------------------------------------------
# Final status
# ---------------------------------------------------------------------------

Write-Header 'Service Status'
& docker compose -f $ComposeFile ps

Write-Host "`n  Endpoints:" -ForegroundColor White
Write-Host "    Langfuse UI        : http://localhost:3000"  -ForegroundColor White
Write-Host "    OTel gRPC          : localhost:4317"          -ForegroundColor White
Write-Host "    OTel HTTP          : localhost:4318"          -ForegroundColor White
Write-Host "    Prometheus         : http://localhost:9091"  -ForegroundColor White
Write-Host ""

Write-Host "  Logs:    docker compose -f $ComposeFile logs -f"   -ForegroundColor DarkGray
Write-Host "  Stop:    docker compose -f $ComposeFile down`n"   -ForegroundColor DarkGray
