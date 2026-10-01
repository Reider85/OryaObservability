<#
.SYNOPSIS
    Starts the full Orya Observability stack (infra + SDK components).
.DESCRIPTION
    1. Validates prerequisites (Docker, .env).
    2. Brings up the Docker Compose stack (Postgres, Redis, ClickHouse, MinIO,
       Langfuse v3, OTel Collector, Prometheus).
    3. Runs one-shot init services (clickhouse-init, minio-init).
    4. Waits for services health/status.
    5. Initializes Vault if possible.
    6. Performs smoke checks.
    7. Prints service status and connection info.
.PARAMETER Detach
    Run compose in detached mode (default: true).
.PARAMETER Build
    Force image rebuild before starting.
.PARAMETER NoWait
    Skip the health-check wait loop.
.PARAMETER InitVault
    Attempt to initialize Vault if unsealed and VAULT_ROOT_TOKEN is set.
.PARAMETER SkipInitServices
    Skip one-shot init services (clickhouse-init, minio-init).
#>
[CmdletBinding()]
param(
    [switch]$Detach    = $true,
    [switch]$Build     = $false,
    [switch]$NoWait    = $false,
    [switch]$InitVault = $false,
    [switch]$SkipInitServices = $false
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$RootDir   = Split-Path -Parent $MyInvocation.MyCommand.Definition
$InfraDir  = Join-Path $RootDir 'infra'
$ComposeFile = Join-Path $InfraDir 'docker-compose.yml'
$OverrideFile = Join-Path $InfraDir 'docker-compose.override.yml'
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
        [int]$IntervalSec = 5,
        [switch]$WaitForRunning = $false
    )
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    $lastStatus = ""
    
    while ((Get-Date) -lt $deadline) {
        # Parse docker compose ps output line by line for PS 5.1 compatibility
        $raw = docker compose -f $ComposeFile -f $OverrideFile ps --format json 2>$null
        $status = $raw | ForEach-Object { $_ | ConvertFrom-Json } | Where-Object { $_.Service -eq $Service }
        
        if ($status) {
            $lastStatus = $status.State
            if ($WaitForRunning -and $status.State -eq 'running') {
                Write-Host "  [OK] $Service is running" -ForegroundColor Green
                return $true
            }
            if (-not $WaitForRunning -and $status.Health -eq 'healthy') {
                Write-Host "  [OK] $Service is healthy" -ForegroundColor Green
                return $true
            }
        }
        
        $waitType = if ($WaitForRunning) { 'running' } else { 'healthy' }
        $waitMsg = "  [WAIT] " + $Service + ": " + $lastStatus + " (waiting for " + $waitType + ")..."
        Write-Host $waitMsg -ForegroundColor Yellow
        Start-Sleep -Seconds $IntervalSec
    }
    
    Write-Host "  [TIMEOUT] $Service did not become $(if ($WaitForRunning) { 'running' } else { 'healthy' }) in ${TimeoutSec}s (last status: $lastStatus)" -ForegroundColor Red
    return $false
}

function Test-SmokeEndpoint {
    param(
        [string]$Name,
        [string]$Url,
        [switch]$Critical = $false
    )
    
    try {
        $response = Invoke-RestMethod -Uri $Url -Method GET -TimeoutSec 10 -ErrorAction Stop
        $okMsg = "  [OK] " + $Name + ": " + $Url
        Write-Host $okMsg -ForegroundColor Green
        return $true
    }
    catch {
        $status = $_.Exception.Response.StatusCode.value__
        $color = if ($Critical) { 'Red' } else { 'Yellow' }
        $failMsg = "  [FAIL] " + $Name + ": " + $Url + " (" + $status + ")"
        Write-Host $failMsg -ForegroundColor $color
        return $false
    }
}

function Update-EnvFile {
    param(
        [string]$Key,
        [string]$Value
    )
    
    $content = Get-Content $EnvFile -Raw
    if ($content -match "^$Key=.*") {
        $content = $content -replace "^$Key=.*", "$Key=$Value"
    } else {
        $content += "`n$Key=$Value"
    }
    $content | Set-Content $EnvFile
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
        Copy-Item $EnvExample $EnvFile
        Write-Host "[INFO] .env created from .env.example. Edit with real values before production use." -ForegroundColor Yellow
    }
    else {
        Write-Error '.env.example not found — cannot bootstrap configuration.'
        exit 1
    }
}

# Check for placeholder secrets (warnings only)
$envContent = Get-Content $EnvFile
$placeholders = @(
    @{key='LANGFUSE_SALT'; pattern='change-me'; desc='Langfuse salt'},
    @{key='NEXTAUTH_SECRET'; pattern='change-me'; desc='NextAuth secret'},
    @{key='LANGFUSE_PUBLIC_KEY'; pattern='pk-lf-your'; desc='Langfuse public key'},
    @{key='OPENAI_API_KEY'; pattern=''; desc='OpenAI API key (for eval worker/judge)'},
    @{key='VAULT_TOKEN'; pattern=''; desc='Vault token (for PII recovery)'}
)

$placeholders = @(
    @{key='LANGFUSE_SALT'; pattern='change-me'; desc='Langfuse salt'},
    @{key='NEXTAUTH_SECRET'; pattern='change-me'; desc='NextAuth secret'},
    @{key='LANGFUSE_PUBLIC_KEY'; pattern='pk-lf-your'; desc='Langfuse public key'},
    @{key='OPENAI_API_KEY'; pattern=''; desc='OpenAI API key (for eval worker/judge)'},
    @{key='VAULT_TOKEN'; pattern=''; desc='Vault token (for PII recovery)'}
)

foreach ($placeholder in $placeholders) {
    $key = $placeholder.key
    $pattern = $placeholder.pattern
    $desc = $placeholder.desc
    
    $regex = "^" + $key + "=" + $pattern
    if ($envContent -match $regex) {
        $msg = "[WARN] " + $key + " has placeholder value - " + $desc
        Write-Host $msg -ForegroundColor Yellow
    }
}

# ---------------------------------------------------------------------------
# Docker Compose
# ---------------------------------------------------------------------------

Write-Header 'Starting Docker Compose stack'

$composeArgs = @('-f', $ComposeFile, '-f', $OverrideFile, '--env-file', $EnvFile, 'up')
if ($Detach) { $composeArgs += '-d' }
if ($Build)  { $composeArgs += '--build' }

Write-Host "  docker compose $($composeArgs -join ' ')" -ForegroundColor DarkGray
& docker compose @composeArgs
if ($LASTEXITCODE -ne 0) {
    Write-Error "docker compose up exited with code $LASTEXITCODE"
    exit $LASTEXITCODE
}

# ---------------------------------------------------------------------------
# One-shot init services
# ---------------------------------------------------------------------------

if (-not $SkipInitServices) {
    Write-Header 'Running one-shot init services'
    
    # Wait for core services first
    $coreServices = @('clickhouse', 'minio')
    foreach ($svc in $coreServices) {
        Wait-ForServiceHealth -Service $svc -TimeoutSec 300
    }
    
    # Run clickhouse-init
    Write-Host "  Running clickhouse-init..."
    & docker compose -f $ComposeFile -f $OverrideFile --env-file $EnvFile run --rm clickhouse-init
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  [WARN] clickhouse-init failed" -ForegroundColor Yellow
    }
    
    # Run minio-init
    Write-Host "  Running minio-init..."
    & docker compose -f $ComposeFile -f $OverrideFile --env-file $EnvFile run --rm minio-init
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  [WARN] minio-init failed" -ForegroundColor Yellow
    }
}

# ---------------------------------------------------------------------------
# Health checks (optional)
# ---------------------------------------------------------------------------

if (-not $NoWait) {
    Write-Header 'Waiting for services to become healthy'
    
    # Services with healthcheck that we wait for 'healthy'
    $healthServices = @('postgres', 'postgres-warm', 'redis', 'eval-redis', 'clickhouse', 'minio', 
                        'langfuse', 'vault', 'cron', 'sampler-proxy', 'phoenix', 'grafana', 'compliance-ui')
    
    # Services without healthcheck that we wait for 'running'
    $runningServices = @('langfuse-worker', 'otel-collector', 'alertmanager', 'prometheus', 'eval-worker')
    
    $allHealthy = $true
    
    foreach ($svc in $healthServices) {
        $ok = Wait-ForServiceHealth -Service $svc -TimeoutSec 300
        if (-not $ok) { $allHealthy = $false }
    }
    
    foreach ($svc in $runningServices) {
        $ok = Wait-ForServiceHealth -Service $svc -TimeoutSec 300 -WaitForRunning
        if (-not $ok) { $allHealthy = $false }
    }
    
    if ($allHealthy) {
        Write-Host "`n  All core services are ready." -ForegroundColor Green
    } else {
        Write-Host "`n  Some services did not become ready. Check logs with:" -ForegroundColor Yellow
        Write-Host "    docker compose -f $ComposeFile -f $OverrideFile logs`n" -ForegroundColor Yellow
    }
}

# ---------------------------------------------------------------------------
# Vault initialization (optional)
# ---------------------------------------------------------------------------

if ($InitVault -or $env:VAULT_ROOT_TOKEN) {
    Write-Header 'Vault initialization check'
    
    # Check if vault is ready
    $vaultReady = $false
    try {
        $status = Invoke-RestMethod -Uri "http://localhost:8201/v1/sys/health" -Method GET -TimeoutSec 10
        $vaultReady = $true
    }
    catch {
        Write-Host "  [WARN] Vault health check failed: $($_.Exception.Message)" -ForegroundColor Yellow
    }
    
    if ($vaultReady) {
        $sealed = $status.Sealed
        $initialized = $status.Initialized
        
        if ($sealed) {
            Write-Host "  [INFO] Vault is sealed. Manual unsealing required." -ForegroundColor Yellow
            Write-Host '    Run: docker compose exec vault vault operator unseal <KEY> (repeat 5 times)' -ForegroundColor Yellow
        }
        elseif ($env:VAULT_ROOT_TOKEN) {
            Write-Host "  [INFO] Vault is unsealed. Initializing with root token..." -ForegroundColor Cyan
            
            # Temporarily set VAULT_ROOT_TOKEN for exec
            $env:VAULT_TOKEN = $env:VAULT_ROOT_TOKEN
            
            try {
                $output = & docker compose -f $ComposeFile -f $OverrideFile --env-file $EnvFile exec -T vault /vault/vault_init.sh
                $output | Write-Host -ForegroundColor Cyan
                
# Extract VAULT_TOKEN from output
        if ($output -match 'VAULT_TOKEN=(\S+)') {
            $vaultToken = $matches[1]
                    Update-EnvFile -Key 'VAULT_TOKEN' -Value $vaultToken
                    Write-Host "  [OK] Vault token written to .env" -ForegroundColor Green
                }
            }
            catch {
                Write-Host "  [WARN] Vault init script failed: $($_.Exception.Message)" -ForegroundColor Yellow
            }
            finally {
                $env:VAULT_TOKEN = $null
            }
        }
        else {
            Write-Host "  [INFO] Vault is initialized and unsealed. No root token provided." -ForegroundColor Cyan
        }
    }
}

# ---------------------------------------------------------------------------
# Smoke checks
# ---------------------------------------------------------------------------

if (-not $NoWait) {
    Write-Header 'Smoke checks'
    
    $smokeChecks = @(
        @{name='Langfuse UI'; url='http://localhost:3000/api/public/health'; critical=$true},
        @{name='OTel HTTP'; url='http://localhost:4318/v1/traces'; critical=$true},
        @{name='Cron Metrics'; url='http://localhost:9777/metrics'; critical=$true},
        @{name='Sampler Status'; url='http://localhost:4321/sampler/status'; critical=$false},
        @{name='Grafana'; url='http://localhost:3001/api/health'; critical=$false},
        @{name='Phoenix'; url='http://localhost:6006/health'; critical=$false},
        @{name='Compliance UI'; url='http://localhost:8088/compliance/api/health'; critical=$false},
        @{name='Vault'; url='http://localhost:8201/v1/sys/health'; critical=$false}
    )
    
    $criticalFailures = 0
    foreach ($check in $smokeChecks) {
        if (-not (Test-SmokeEndpoint -Name $check.name -Url $check.url -Critical:$check.critical)) {
            if ($check.critical) { $criticalFailures++ }
        }
    }
    
    if ($criticalFailures -gt 0) {
        Write-Host "`n  [ERROR] $criticalFailures critical smoke checks failed!" -ForegroundColor Red
        exit 1
    }
    elseif ($criticalFailures -eq 0 -and ($smokeChecks.Count -gt ($smokeChecks | Where-Object { $_.critical }).Count)) {
        Write-Host "`n  All critical smoke checks passed. Some optional checks may have failed." -ForegroundColor Green
    }
}

# ---------------------------------------------------------------------------
# Final status
# ---------------------------------------------------------------------------

Write-Header 'Service Status'
& docker compose -f $ComposeFile -f $OverrideFile ps

Write-Host "`n  Endpoints:" -ForegroundColor White
Write-Host "    Langfuse UI        : http://localhost:3000"  -ForegroundColor White
Write-Host "    Grafana            : http://localhost:3001"  -ForegroundColor White
Write-Host "    Phoenix            : http://localhost:6006"  -ForegroundColor White
Write-Host "    Compliance UI      : http://localhost:8088"  -ForegroundColor White
Write-Host "    Vault              : http://localhost:8201"  -ForegroundColor White
Write-Host "    Prometheus         : http://localhost:9091"  -ForegroundColor White
Write-Host "    Alertmanager       : http://localhost:9093"  -ForegroundColor White
Write-Host "    ClickHouse HTTP    : http://localhost:8123"  -ForegroundColor White
Write-Host "    ClickHouse native  : localhost:9004"          -ForegroundColor White
Write-Host "    OTel gRPC          : localhost:4317"          -ForegroundColor White
Write-Host "    OTel HTTP          : localhost:4318"          -ForegroundColor White
Write-Host "    Sampler OTLP/HTTP  : http://localhost:4321"  -ForegroundColor White
Write-Host "    Sampler metrics    : http://localhost:9095"  -ForegroundColor White
Write-Host "    Cron Metrics       : http://localhost:9777"  -ForegroundColor White
Write-Host "    MinIO              : in-network minio:9000 (not published to host)" -ForegroundColor White
Write-Host "    Postgres/Redis     : in-network only"          -ForegroundColor White
Write-Host ""

Write-Host "  Logs:    docker compose -f " + $ComposeFile + " -f " + $OverrideFile + " logs -f" -ForegroundColor DarkGray
Write-Host "  Stop:    docker compose -f " + $ComposeFile + " -f " + $OverrideFile + " down" -ForegroundColor Yellow