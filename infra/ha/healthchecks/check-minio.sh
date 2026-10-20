#!/bin/bash
# Health check script for MinIO distributed cluster
# This script checks the health of MinIO nodes and cluster status

set -e

MINIO_NODES=("minio1" "minio2" "minio3" "minio4")
MINIO_PORT=9000
MINIO_CONSOLE_PORT=9001
MINIO_ROOT_USER="${MINIO_ROOT_USER:-minioadmin}"
MINIO_ROOT_PASSWORD="${MINIO_ROOT_PASSWORD:-minioadmin}"

# Function to check MinIO connectivity
check_minio() {
    local host=$1
    local port=$2
    
    if mc admin info --no-color --json "http://${host}:${port}" > /dev/null 2>&1; then
        echo "OK: MinIO ${host}:${port} is accessible"
        return 0
    else
        echo "ERROR: MinIO ${host}:${port} is not accessible"
        return 1
    fi
}

# Function to check MinIO cluster status
check_minio_cluster() {
    local host=$1
    local port=$2
    
    # Get cluster status
    local cluster_info=$(mc admin info --no-color --json "http://${host}:${port}" 2>/dev/null)
    if [ $? -eq 0 ]; then
        local status=$(echo "$cluster_info" | jq -r '.status')
        if [ "$status" = "online" ]; then
            echo "OK: MinIO cluster on ${host}:${port} is online"
            return 0
        else
            echo "ERROR: MinIO cluster on ${host}:${port} is not online (status: $status)"
            return 1
        fi
    else
        echo "ERROR: Could not get cluster status from ${host}:${port}"
        return 1
    fi
}

# Function to check MinIO drive status
check_minio_drives() {
    local host=$1
    local port=$2
    
    # Get drive information
    local drives=$(mc admin info --no-color --json "http://${host}:${port}" | jq -r '.servers[].drives[]')
    if [ -n "$drives" ]; then
        local drive_count=$(echo "$drives" | jq -r '.state' | grep -c "ok")
        if [ "$drive_count" -eq 4 ]; then
            echo "OK: MinIO ${host}:${port} has 4 healthy drives"
            return 0
        else
            echo "ERROR: MinIO ${host}:${port} has unhealthy drives (healthy: $drive_count/4)"
            return 1
        fi
    else
        echo "ERROR: Could not get drive information from ${host}:${port}"
        return 1
    fi
}

# Function to check MinIO bucket access
check_minio_bucket() {
    local host=$1
    local port=$2
    
    # Check if we can access the default bucket
    if mc ls "minio/admin@http://${host}:${port}" > /dev/null 2>&1; then
        echo "OK: MinIO ${host}:${port} bucket access is working"
        return 0
    else
        echo "ERROR: MinIO ${host}:${port} bucket access is not working"
        return 1
    fi
}

# Function to check MinIO console
check_minio_console() {
    local host=$1
    local port=$2
    
    if curl -f -s "http://${host}:${MINIO_CONSOLE_PORT}" > /dev/null 2>&1; then
        echo "OK: MinIO console ${host}:${MINIO_CONSOLE_PORT} is accessible"
        return 0
    else
        echo "ERROR: MinIO console ${host}:${MINIO_CONSOLE_PORT} is not accessible"
        return 1
    fi
}

# Function to check MinIO server health
check_minio_server_health() {
    local host=$1
    local port=$2
    
    # Get server health information
    local health=$(mc admin info --no-color --json "http://${host}:${port}" | jq -r '.servers[].status')
    if [ "$health" = "online" ]; then
        echo "OK: MinIO server ${host}:${port} is healthy"
        return 0
    else
        echo "ERROR: MinIO server ${host}:${port} is not healthy (status: $health)"
        return 1
    fi
}

# Function to check MinIO network connectivity
check_minio_network() {
    local host=$1
    local port=$2
    
    # Check network connectivity to all nodes
    local connectivity_ok=true
    for node in "${MINIO_NODES[@]}"; do
        if ! ping -c 1 -W 2 "$node" > /dev/null 2>&1; then
            echo "ERROR: Network connectivity to $node failed"
            connectivity_ok=false
        fi
    done
    
    if [ "$connectivity_ok" = true ]; then
        echo "OK: Network connectivity to all MinIO nodes"
        return 0
    else
        echo "ERROR: Network connectivity issues detected"
        return 1
    fi
}

# Main health check
echo "Starting MinIO distributed health check..."

# Check individual MinIO nodes
all_nodes_ok=true
all_cluster_ok=true
all_drives_ok=true
all_buckets_ok=true
all_console_ok=true
all_network_ok=true

for node in "${MINIO_NODES[@]}"; do
    if ! check_minio "$node" "$MINIO_PORT"; then
        all_nodes_ok=false
    fi
    
    if ! check_minio_cluster "$node" "$MINIO_PORT"; then
        all_cluster_ok=false
    fi
    
    if ! check_minio_drives "$node" "$MINIO_PORT"; then
        all_drives_ok=false
    fi
    
    if ! check_minio_bucket "$node" "$MINIO_PORT"; then
        all_buckets_ok=false
    fi
    
    if ! check_minio_console "$node" "$MINIO_CONSOLE_PORT"; then
        all_console_ok=false
    fi
    
    if ! check_minio_server_health "$node" "$MINIO_PORT"; then
        all_nodes_ok=false
    fi
done

# Check network connectivity
if ! check_minio_network "${MINIO_NODES[0]}" "$MINIO_PORT"; then
    all_network_ok=false
fi

# Summary
echo "Health check summary:"
echo "- MinIO nodes: $([ "$all_nodes_ok" = true ] && echo "OK" || echo "FAILED")"
echo "- MinIO cluster: $([ "$all_cluster_ok" = true ] && echo "OK" || echo "FAILED")"
echo "- MinIO drives: $([ "$all_drives_ok" = true ] && echo "OK" || echo "FAILED")"
echo "- MinIO buckets: $([ "$all_buckets_ok" = true ] && echo "OK" || echo "FAILED")"
echo "- MinIO console: $([ "$all_console_ok" = true ] && echo "OK" || echo "FAILED")"
echo "- Network connectivity: $([ "$all_network_ok" = true ] && echo "OK" || echo "FAILED")"

# Exit with appropriate code
if [ "$all_nodes_ok" = true ] && [ "$all_cluster_ok" = true ] && [ "$all_drives_ok" = true ] && [ "$all_buckets_ok" = true ] && [ "$all_console_ok" = true ] && [ "$all_network_ok" = true ]; then
    echo "All MinIO health checks passed!"
    exit 0
else
    echo "Some MinIO health checks failed!"
    exit 1
fi