#!/bin/bash
# Comprehensive health check script for all HA services
# This script runs all individual health checks and provides a summary

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Function to print colored output
print_status() {
    local status=$1
    local message=$2
    
    case $status in
        "OK")
            echo -e "${GREEN}[OK]${NC} $message"
            ;;
        "WARNING")
            echo -e "${YELLOW}[WARNING]${NC} $message"
            ;;
        "ERROR")
            echo -e "${RED}[ERROR]${NC} $message"
            ;;
        *)
            echo -e "[UNKNOWN] $message"
            ;;
    esac
}

# Function to run health check and return result
run_health_check() {
    local service=$1
    local script=$2
    
    echo "Running $service health check..."
    
    if bash "$script"; then
        print_status "OK" "$service health check passed"
        return 0
    else
        print_status "ERROR" "$service health check failed"
        return 1
    fi
}

# Main health check
echo "Starting comprehensive HA infrastructure health check..."
echo "=================================================="

# Initialize counters
total_checks=0
passed_checks=0
failed_checks=0

# Run individual health checks
echo ""
echo "Checking individual services..."
echo "------------------------------"

# Check ClickHouse
total_checks=$((total_checks + 1))
if run_health_check "ClickHouse" "check-clickhouse.sh"; then
    passed_checks=$((passed_checks + 1))
else
    failed_checks=$((failed_checks + 1))
fi

# Check PostgreSQL
total_checks=$((total_checks + 1))
if run_health_check "PostgreSQL" "check-postgres.sh"; then
    passed_checks=$((passed_checks + 1))
else
    failed_checks=$((failed_checks + 1))
fi

# Check Redis
total_checks=$((total_checks + 1))
if run_health_check "Redis" "check-redis.sh"; then
    passed_checks=$((passed_checks + 1))
else
    failed_checks=$((failed_checks + 1))
fi

# Check Vault
total_checks=$((total_checks + 1))
if run_health_check "Vault" "check-vault.sh"; then
    passed_checks=$((passed_checks + 1))
else
    failed_checks=$((failed_checks + 1))
fi

# Check OTel Collector
total_checks=$((total_checks + 1))
if run_health_check "OTel Collector" "check-otel.sh"; then
    passed_checks=$((passed_checks + 1))
else
    failed_checks=$((failed_checks + 1))
fi

# Check MinIO
total_checks=$((total_checks + 1))
if run_health_check "MinIO" "check-minio.sh"; then
    passed_checks=$((passed_checks + 1))
else
    failed_checks=$((failed_checks + 1))
fi

# Check nginx load balancers
echo ""
echo "Checking nginx load balancers..."
echo "------------------------------"

# Check nginx for Langfuse
if curl -f -s "http://nginx-langfuse:3000/nginx_status" > /dev/null 2>&1; then
    print_status "OK" "nginx Langfuse load balancer is accessible"
    passed_checks=$((passed_checks + 1))
else
    print_status "ERROR" "nginx Langfuse load balancer is not accessible"
    failed_checks=$((failed_checks + 1))
fi

# Check nginx for OTel
if curl -f -s "http://nginx-otel:4317/nginx_status" > /dev/null 2>&1; then
    print_status "OK" "nginx OTel load balancer is accessible"
    passed_checks=$((passed_checks + 1))
else
    print_status "ERROR" "nginx OTel load balancer is not accessible"
    failed_checks=$((failed_checks + 1))
fi

# Summary
echo ""
echo "=================================================="
echo "Health Check Summary"
echo "=================================================="
echo "Total checks: $total_checks"
echo "Passed: $passed_checks"
echo "Failed: $failed_checks"
echo "Success rate: $((passed_checks * 100 / total_checks))%"

# Check if all checks passed
if [ "$failed_checks" -eq 0 ]; then
    echo ""
    print_status "OK" "All health checks passed! HA infrastructure is healthy."
    exit 0
else
    echo ""
    print_status "ERROR" "$failed_checks health checks failed! HA infrastructure has issues."
    exit 1
fi