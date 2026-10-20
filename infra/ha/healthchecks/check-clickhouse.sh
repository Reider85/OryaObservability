#!/bin/bash
# Health check script for ClickHouse HA cluster (mTLS-aware, PP02)
# HTTPS port 8443 requires a client certificate; plaintext 8123 is used for
# the in-cluster ping probe only.

set -e

CLICKHOUSE_NODES=("clickhouse1" "clickhouse2" "clickhouse3" "clickhouse4")
CLICKHOUSE_KEEPER_NODES=("clickhouse-keeper1" "clickhouse-keeper2" "clickhouse-keeper3")
CLICKHOUSE_HTTP_PORT=8123
CLICKHOUSE_TLS_PORT=8443
KEEPER_PORT=9181

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CERT_DIR="${MTLS_CERTS_DIR:-${SCRIPT_DIR}/../certs}"
CA_PEM="${CERT_DIR}/internal-ca/ca.pem"
CH_CERT="${CERT_DIR}/clickhouse/clickhouse.pem"
CH_KEY="${CERT_DIR}/clickhouse/clickhouse-key.pem"

ch_curl() {
    curl --cacert "${CA_PEM}" --cert "${CH_CERT}" --key "${CH_KEY}" "$@"
}

# Function to check ClickHouse node health (plain HTTP ping, in-cluster)
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

# Function to check the mTLS HTTPS endpoint with a client certificate
check_clickhouse_mtls() {
    local node=$1
    local port=$2

    if ch_curl -f -s "https://${node}:${port}/ping" > /dev/null 2>&1; then
        echo "OK: ${node}:${port} mTLS endpoint is accessible"
        return 0
    else
        echo "ERROR: ${node}:${port} mTLS endpoint is not accessible"
        return 1
    fi
}

# Function to check that mTLS is enforced (no cert -> refused)
check_clickhouse_mtls_enforced() {
    local node=$1
    local port=$2

    if curl --cacert "${CA_PEM}" -f -s "https://${node}:${port}/ping" > /dev/null 2>&1; then
        echo "ERROR: ${node}:${port} accepted a request without a client certificate"
        return 1
    else
        echo "OK: ${node}:${port} rejects clients without a certificate"
        return 0
    fi
}

# Function to check ClickHouse cluster status
check_clickhouse_cluster() {
    if ch_curl -f -s "https://clickhouse1:${CLICKHOUSE_TLS_PORT}/cluster?query=SELECT+cluster%2C+shard_num%2C+replica_num%2C+host_name+FROM+system.clusters+WHERE+cluster+%3D+%27observability_cluster%27" > /dev/null 2>&1; then
        echo "OK: ClickHouse cluster is accessible (mTLS)"
        return 0
    else
        echo "ERROR: ClickHouse cluster is not accessible"
        return 1
    fi
}

# Function to check ClickHouseKeeper health (plain HTTP, coordination plane)
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

    if ch_curl -f -s "https://${node}:${port}/?query=SELECT+1+AS+health" > /dev/null 2>&1; then
        echo "OK: Database on ${node}:${port} is accessible (mTLS)"
        return 0
    else
        echo "ERROR: Database on ${node}:${port} is not accessible"
        return 1
    fi
}

# Main health check
echo "Starting ClickHouse HA health check (mTLS)..."

all_nodes_ok=true
for node in "${CLICKHOUSE_NODES[@]}"; do
    if ! check_clickhouse_node "$node" "$CLICKHOUSE_HTTP_PORT"; then
        all_nodes_ok=false
    fi
    if ! check_clickhouse_mtls "$node" "$CLICKHOUSE_TLS_PORT"; then
        all_nodes_ok=false
    fi
    if ! check_clickhouse_mtls_enforced "$node" "$CLICKHOUSE_TLS_PORT"; then
        all_nodes_ok=false
    fi
done

all_keeper_ok=true
for node in "${CLICKHOUSE_KEEPER_NODES[@]}"; do
    if ! check_clickhouse_keeper "$node" "$KEEPER_PORT"; then
        all_keeper_ok=false
    fi
done

if ! check_clickhouse_cluster; then
    all_nodes_ok=false
fi

for node in "${CLICKHOUSE_NODES[@]}"; do
    if ! check_database "$node" "$CLICKHOUSE_TLS_PORT"; then
        all_nodes_ok=false
    fi
done

# Summary
echo "Health check summary:"
echo "- ClickHouse nodes: $([ "$all_nodes_ok" = true ] && echo "OK" || echo "FAILED")"
echo "- ClickHouseKeeper nodes: $([ "$all_keeper_ok" = true ] && echo "OK" || echo "FAILED")"
echo "- Cluster status: $([ "$all_nodes_ok" = true ] && echo "OK" || echo "FAILED")"

if [ "$all_nodes_ok" = true ] && [ "$all_keeper_ok" = true ]; then
    echo "All health checks passed!"
    exit 0
else
    echo "Some health checks failed!"
    exit 1
fi
