#!/bin/bash
# Health check script for Vault HA cluster
# This script checks the health of Vault nodes and unseal status

set -e

VAULT_NODES=("vault-1" "vault-2")
VAULT_PORT=8200
VAULT_UNSEAL_PORT=8200
VAULT_ROOT_TOKEN="${VAULT_ROOT_TOKEN:-s.root-token}"

# Function to check Vault connectivity
check_vault() {
    local host=$1
    local port=$2
    
    if curl -f -s "http://${host}:${port}/v1/sys/health" > /dev/null 2>&1; then
        echo "OK: Vault ${host}:${port} is accessible"
        return 0
    else
        echo "ERROR: Vault ${host}:${port} is not accessible"
        return 1
    fi
}

# Function to check Vault unseal status
check_vault_unseal() {
    local host=$1
    local port=$2
    
    local sealed=$(curl -s "http://${host}:${port}/v1/sys/health" | jq -r '.sealed')
    if [ "$sealed" = "false" ]; then
        echo "OK: Vault ${host}:${port} is unsealed"
        return 0
    else
        echo "ERROR: Vault ${host}:${port} is sealed"
        return 1
    fi
}

# Function to check Vault cluster status
check_vault_cluster() {
    local host=$1
    local port=$2
    
    # Check if this node is part of a cluster
    local cluster=$(curl -s "http://${host}:${port}/v1/sys/health" | jq -r '.cluster_name')
    if [ "$cluster" != "null" ] && [ "$cluster" != "" ]; then
        echo "OK: Vault ${host}:${port} is part of cluster: $cluster"
        return 0
    else
        echo "WARNING: Vault ${host}:${port} is not part of a cluster"
        return 0  # Not an error, just a warning
    fi
}

# Function to check Vault performance
check_vault_performance() {
    local host=$1
    local port=$2
    
    # Check performance metrics
    local latency=$(curl -s "http://${host}:${port}/v1/sys/metrics" | jq -r '.data[] | select(.name == "vault.request.latency") | .value')
    if [ -n "$latency" ]; then
        echo "OK: Vault ${host}:${port} request latency: ${latency}ms"
        return 0
    else
        echo "WARNING: Could not get performance metrics from ${host}:${port}"
        return 0  # Not an error, just a warning
    fi
}

# Function to check Vault storage
check_vault_storage() {
    local host=$1
    local port=$2
    
    # Check storage backend
    local storage=$(curl -s "http://${host}:${port}/v1/sys/storage/raft/configuration" | jq -r '.data.configuration')
    if [ "$storage" = "raft" ]; then
        echo "OK: Vault ${host}:${port} using Raft storage"
        return 0
    else
        echo "ERROR: Vault ${host}:${port} not using Raft storage"
        return 1
    fi
}

# Function to check Vault leader election
check_vault_leader() {
    local host=$1
    local port=$2
    
    # Check if this node is the leader
    local is_leader=$(curl -s "http://${host}:${port}/v1/sys/leader" | jq -r '.is_self')
    if [ "$is_leader" = "true" ]; then
        echo "OK: Vault ${host}:${port} is the leader"
        return 0
    elif [ "$is_leader" = "false" ]; then
        echo "OK: Vault ${host}:${port} is a follower"
        return 0
    else
        echo "WARNING: Could not determine leader status for ${host}:${port}"
        return 0  # Not an error, just a warning
    fi
}

# Main health check
echo "Starting Vault HA health check..."

# Check individual Vault nodes
all_nodes_ok=true
all_unsealed=true
all_cluster_ok=true
all_storage_ok=true

for node in "${VAULT_NODES[@]}"; do
    if ! check_vault "$node" "$VAULT_PORT"; then
        all_nodes_ok=false
    fi
    
    if ! check_vault_unseal "$node" "$VAULT_PORT"; then
        all_unsealed=false
    fi
    
    if ! check_vault_cluster "$node" "$VAULT_PORT"; then
        all_cluster_ok=false
    fi
    
    if ! check_vault_storage "$node" "$VAULT_PORT"; then
        all_storage_ok=false
    fi
    
    if ! check_vault_leader "$node" "$VAULT_PORT"; then
        # Not an error, just a warning
        :
    fi
    
    if ! check_vault_performance "$node" "$VAULT_PORT"; then
        # Not an error, just a warning
        :
    fi
done

# Check if there's exactly one leader
leader_count=0
for node in "${VAULT_NODES[@]}"; do
    local is_leader=$(curl -s "http://${node}:${VAULT_PORT}/v1/sys/leader" | jq -r '.is_self')
    if [ "$is_leader" = "true" ]; then
        leader_count=$((leader_count + 1))
    fi
done

if [ "$leader_count" -eq 1 ]; then
    echo "OK: Exactly one leader found"
else
    echo "ERROR: Expected exactly one leader, found $leader_count"
    all_nodes_ok=false
fi

# Summary
echo "Health check summary:"
echo "- Vault nodes: $([ "$all_nodes_ok" = true ] && echo "OK" || echo "FAILED")"
echo "- Vault unsealed: $([ "$all_unsealed" = true ] && echo "OK" || echo "FAILED")"
echo "- Cluster status: $([ "$all_cluster_ok" = true ] && echo "OK" || echo "FAILED")"
echo "- Storage: $([ "$all_storage_ok" = true ] && echo "OK" || echo "FAILED")"
echo "- Leader election: $([ "$leader_count" -eq 1 ] && echo "OK" || echo "FAILED")"

# Exit with appropriate code
if [ "$all_nodes_ok" = true ] && [ "$all_unsealed" = true ] && [ "$all_cluster_ok" = true ] && [ "$all_storage_ok" = true ] && [ "$leader_count" -eq 1 ]; then
    echo "All Vault health checks passed!"
    exit 0
else
    echo "Some Vault health checks failed!"
    exit 1
fi