# Test script for PC01 tiered storage infrastructure
# This script verifies that all configuration files are properly set up

Write-Host "=== PC01 Tiered Storage Infrastructure Test ===" -ForegroundColor Green
Write-Host ""

# Check if we're in the correct directory
if (-not (Test-Path "docker-compose.yml")) {
    Write-Host "ERROR: docker-compose.yml not found in current directory" -ForegroundColor Red
    exit 1
}

Write-Host "✓ docker-compose.yml found" -ForegroundColor Green

# Check if storage directory exists
if (-not (Test-Path "storage")) {
    Write-Host "ERROR: storage directory not found" -ForegroundColor Red
    exit 1
}

Write-Host "✓ storage directory exists" -ForegroundColor Green

# Check required files in storage directory
$required_files = @(
    "clickhouse_ddl.sql",
    "postgres_warm.sql",
    "s3_lifecycle.json",
    "clickhouse_init.sh",
    "postgres_warm_init.sh",
    "minio_init.sh",
    "README.md"
)

foreach ($file in $required_files) {
    if (-not (Test-Path "storage\$file")) {
        Write-Host "ERROR: storage\$file not found" -ForegroundColor Red
        exit 1
    }
    Write-Host "✓ storage\$file exists" -ForegroundColor Green
}

# Check if .env.example exists and has new variables
if (-not (Test-Path ".env.example")) {
    Write-Host "ERROR: .env.example not found" -ForegroundColor Red
    exit 1
}

Write-Host "✓ .env.example exists" -ForegroundColor Green

# Check for required environment variables in .env.example
$required_env_vars = @(
    "CLICKHOUSE_DB",
    "CLICKHOUSE_USER",
    "CLICKHOUSE_PASSWORD",
    "CLICKHOUSE_HOT_URL",
    "WARM_PG_USER",
    "WARM_PG_PASSWORD",
    "WARM_PG_DSN",
    "WARM_PG_HOST_URL",
    "S3_ENDPOINT",
    "S3_ACCESS_KEY",
    "S3_SECRET_KEY",
    "S3_COLD_BUCKET",
    "S3_AUDIT_BUCKET"
)

foreach ($var in $required_env_vars) {
    $content = Get-Content ".env.example" -Raw
    if ($content -notmatch "^$var=") {
        Write-Host "ERROR: $var not found in .env.example" -ForegroundColor Red
        exit 1
    }
    Write-Host "✓ $var found in .env.example" -ForegroundColor Green
}

Write-Host ""
Write-Host "=== Configuration Validation Complete ===" -ForegroundColor Green
Write-Host ""
Write-Host "All configuration files are properly set up."
Write-Host ""
Write-Host "To test the infrastructure:"
Write-Host "1. Start Docker Desktop"
Write-Host "2. Run: docker compose up -d"
Write-Host "3. Verify services with: docker compose ps"
Write-Host "4. Run verification commands from storage/README.md"
Write-Host ""
Write-Host "Verification commands:"
Write-Host "- ClickHouse: docker compose exec clickhouse clickhouse-client --query 'SHOW DATABASES'"
Write-Host "- PostgreSQL Warm: docker compose exec postgres-warm psql -U warm -d warm_store -c '\dt'"
Write-Host "- MinIO: docker compose exec minio mc ls local/"