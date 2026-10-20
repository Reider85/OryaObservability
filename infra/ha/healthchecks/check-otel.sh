#!/bin/bash
# Health check script for OTel Collector HA cluster
# This script checks the health of OTel Collector nodes

set -e

OTEL_NODES=("otel-collector-1" "otel-collector-2" "otel-collector-3")
OTEL_PORT=4317
OTEL_HTTP_PORT=8888
OTEL_HEALTH_PORT=13133

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CERT_DIR="${MTLS_CERTS_DIR:-${SCRIPT_DIR}/../certs}"
CA_PEM="${CERT_DIR}/internal-ca/ca.pem"
OTEL_CERT="${CERT_DIR}/otel-collector/otel-collector.pem"
OTEL_KEY="${CERT_DIR}/otel-collector/otel-collector-key.pem"

# Function to verify the mTLS handshake on the OTLP receiver
check_otel_mtls() {
    local host=$1
    local port=$2

    if echo | openssl s_client -connect "${host}:${port}" \
        -CAfile "${CA_PEM}" -cert "${OTEL_CERT}" -key "${OTEL_KEY}" \
        -verify_return_error > /dev/null 2>&1; then
        echo "OK: OTel Collector ${host}:${port} mTLS handshake succeeded"
        return 0
    else
        echo "ERROR: OTel Collector ${host}:${port} mTLS handshake failed"
        return 1
    fi
}

# Function to check OTel Collector connectivity (health extension, plain HTTP)
check_otel_collector() {
    local host=$1
    local port=$2

    if curl -f -s "http://${host}:${port}/" > /dev/null 2>&1; then
        echo "OK: OTel Collector ${host}:${port} is accessible"
        return 0
    else
        echo "ERROR: OTel Collector ${host}:${port} is not accessible"
        return 1
    fi
}

# Function to check OTel Collector metrics
check_otel_metrics() {
    local host=$1
    local port=$2
    
    if curl -f -s "http://${host}:${port}/metrics" > /dev/null 2>&1; then
        echo "OK: OTel Collector ${host}:${port} metrics are accessible"
        return 0
    else
        echo "ERROR: OTel Collector ${host}:${port} metrics are not accessible"
        return 1
    fi
}

# Function to check OTel Collector info
check_otel_info() {
    local host=$1
    local port=$2
    
    if curl -f -s "http://${host}:${port}/info" > /dev/null 2>&1; then
        echo "OK: OTel Collector ${host}:${port} info is accessible"
        return 0
    else
        echo "ERROR: OTel Collector ${host}:${port} info is not accessible"
        return 1
    fi
}

# Function to check OTel Collector debug
check_otel_debug() {
    local host=$1
    local port=$2
    
    if curl -f -s "http://${host}:${port}/debug" > /dev/null 2>&1; then
        echo "OK: OTel Collector ${host}:${port} debug is accessible"
        return 0
    else
        echo "WARNING: OTel Collector ${host}:${port} debug is not accessible"
        return 0  # Not an error, just a warning
    fi
}

# Function to check OTel Collector performance
check_otel_performance() {
    local host=$1
    local port=$2
    
    # Check specific metrics
    local metrics=$(curl -s "http://${host}:${port}/metrics")
    
    # Check if metrics are being received
    local received_metrics=$(echo "$metrics" | grep -c "otel_collector_received_records_total")
    if [ "$received_metrics" -gt 0 ]; then
        echo "OK: OTel Collector ${host}:${port} is receiving metrics"
        return 0
    else
        echo "WARNING: OTel Collector ${host}:${port} is not receiving metrics"
        return 0  # Not an error, just a warning
    fi
}

# Function to check OTel Collector memory usage
check_otel_memory() {
    local host=$1
    local port=$2
    
    # Check memory usage from metrics
    local memory_usage=$(curl -s "http://${host}:${port}/metrics" | grep "process_resident_memory_bytes" | awk '{print $2}')
    if [ -n "$memory_usage" ]; then
        local memory_mb=$((memory_usage / 1024 / 1024))
        echo "OK: OTel Collector ${host}:${port} memory usage: ${memory_mb}MB"
        return 0
    else
        echo "WARNING: Could not get memory metrics from ${host}:${port}"
        return 0  # Not an error, just a warning
    fi
}

# Function to check OTel Collector CPU usage
check_otel_cpu() {
    local host=$1
    local port=$2
    
    # Check CPU usage from metrics
    local cpu_usage=$(curl -s "http://${host}:${port}/metrics" | grep "process_cpu_seconds_total" | tail -1 | awk '{print $2}')
    if [ -n "$cpu_usage" ]; then
        echo "OK: OTel Collector ${host}:${port} CPU usage: ${cpu_usage}s"
        return 0
    else
        echo "WARNING: Could not get CPU metrics from ${host}:${port}"
        return 0  # Not an error, just a warning
    fi
}

# Main health check
echo "Starting OTel Collector HA health check (mTLS)..."

# Check individual OTel Collector nodes
all_nodes_ok=true
all_metrics_ok=true
all_info_ok=true

for node in "${OTEL_NODES[@]}"; do
    if ! check_otel_mtls "$node" "$OTEL_PORT"; then
        all_nodes_ok=false
    fi

    if ! check_otel_collector "$node" "$OTEL_HEALTH_PORT"; then
        all_nodes_ok=false
    fi
    
    if ! check_otel_metrics "$node" "$OTEL_HTTP_PORT"; then
        all_metrics_ok=false
    fi
    
    if ! check_otel_info "$node" "$OTEL_HTTP_PORT"; then
        all_info_ok=false
    fi
    
    if ! check_otel_debug "$node" "$OTEL_HTTP_PORT"; then
        # Not an error, just a warning
        :
    fi
    
    if ! check_otel_performance "$node" "$OTEL_HTTP_PORT"; then
        # Not an error, just a warning
        :
    fi
    
    if ! check_otel_memory "$node" "$OTEL_HTTP_PORT"; then
        # Not an error, just a warning
        :
    fi
    
    if ! check_otel_cpu "$node" "$OTEL_HTTP_PORT"; then
        # Not an error, just a warning
        :
    fi
done

# Summary
echo "Health check summary:"
echo "- OTel Collector nodes: $([ "$all_nodes_ok" = true ] && echo "OK" || echo "FAILED")"
echo "- Metrics endpoints: $([ "$all_metrics_ok" = true ] && echo "OK" || echo "FAILED")"
echo "- Info endpoints: $([ "$all_info_ok" = true ] && echo "OK" || echo "FAILED")"

# Exit with appropriate code
if [ "$all_nodes_ok" = true ] && [ "$all_metrics_ok" = true ] && [ "$all_info_ok" = true ]; then
    echo "All OTel Collector health checks passed!"
    exit 0
else
    echo "Some OTel Collector health checks failed!"
    exit 1
fi