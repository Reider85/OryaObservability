#!/bin/bash
# Health check script for ClickHouse HA cluster
# This script checks the health of all ClickHouse nodes and replicas

set -e

CLICKHOUSE_NODES=("clickhouse1" "clickhouse2" "clickhouse3" "clickhouse4")
CLICKHOUSE_KEEPER_NODES=("clickhouse-keeper1" "clickhouse-keeper2" "clickhouse-keeper3")
CLICKHOUSE_PORT=9000
KEEPER_PORT=9181

# Function to check ClickHouse node health
check_clickhouse_node() {
    local node=$1
    local port=$2
    
    if curl -f -s "http://${node}:${port}/ping" > /dev/null 2>&1; then
        echo "OK: ${node}:${port} is reachable"
        return 0
    else
        echo "ERROR: ${node}:${port} is not reachable"
        return 1
    fi
}

# Function to check ClickHouse cluster status
check_clickhouse_cluster() {
    # Check if cluster is accessible
    if curl -f -s "http://clickhouse1:${CLICKHOUSE_PORT}/cluster?query=SELECT+cluster%2C+shard_num%2C+replica_num%2C+host_name+FROM+system.clusters+WHERE+cluster+%3D+%27observability_cluster%27" > /dev/null 2>&1; then
        echo "OK: ClickHouse cluster is accessible"
        return 0
    else
        echo "ERROR: ClickHouse cluster is not accessible"
        return 1
    fi
}

# Function to check ClickHouseKeeper health
check_clickhouse_keeper() {
    local node=$1
    local port=$2
    
    if curl -f -s "http://${node}:${port}/health" > /dev/null 2>&1; then
        echo "OK: ${node}:${port} is reachable"
        return 0
    else
        echo "ERROR: ${node}:${port} is not reachable"
        return 1
    fi
}

# Function to check database connectivity
check_database() {
    local node=$1
    local port=$2
    
    if curl -f -s "http://${node}:${port}/?query=SELECT+1+AS+health" > /dev/null 2>&1; then
        echo "OK: Database on ${node}:${port} is accessible"
        return 0
    else
        echo "ERROR: Database on ${node}:${port} is not accessible"
        return 1
    fi
}

# Main health check
echo "Starting ClickHouse HA health check..."

# Check individual ClickHouse nodes
all_nodes_ok=true
for node in "${CLICKHOUSE_NODES[@]}"; do
    if ! check_clickhouse_node "$node" "$CLICKHOUSE_PORT"; then
        all_nodes_ok=false
    fi
done

# Check ClickHouseKeeper nodes
all_keeper_ok=true
for node in "${CLICKHOUSE_KEEPER_NODES[@]}"; do
    if ! check_clickhouse_keeper "$node" "$KEEPER_PORT"; then
        all_keeper_ok=false
    fi
done

# Check cluster status
if ! check_clickhouse_cluster; then
    all_nodes_ok=false
fi

# Check database connectivity
for node in "${CLICKHOUSE_NODES[@]}"; do
    if ! check_database "$node" "$CLICKHOUSE_PORT"; then
        all_nodes_ok=false
    fi
done

# Summary
echo "Health check summary:"
echo "- ClickHouse nodes: $([ "$all_nodes_ok" = true ] && echo "OK" || echo "FAILED")"
echo "- ClickHouseKeeper nodes: $([ "$all_keeper_ok" = true ] && echo "OK" || echo "FAILED")"
echo "- Cluster status: $([ "$all_nodes_ok" = true ] && echo "OK" || echo "FAILED")"

# Exit with appropriate code
if [ "$all_nodes_ok" = true ] && [ "$all_keeper_ok" = true ]; then
    echo "All health checks passed!"
    exit 0
else
    echo "Some health checks failed!"
    exit 1
fi