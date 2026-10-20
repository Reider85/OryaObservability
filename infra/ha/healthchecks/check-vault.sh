#!/bin/bash
# Health check script for Vault HA cluster (mTLS-aware, PP02)
# Probes present the vault mesh certificate and trust the internal CA.

set -e

VAULT_NODES=("vault-1" "vault-2")
VAULT_PORT=8200
VAULT_UNSEAL_PORT=8200
VAULT_ROOT_TOKEN="${VAULT_ROOT_TOKEN:-s.root-token}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CERT_DIR="${MTLS_CERTS_DIR:-${SCRIPT_DIR}/../certs}"
CA_PEM="${CERT_DIR}/internal-ca/ca.pem"
VAULT_CERT="${CERT_DIR}/vault/vault.pem"
VAULT_KEY="${CERT_DIR}/vault/vault-key.pem"

vtls_curl() {
    curl --cacert "${CA_PEM}" --cert "${VAULT_CERT}" --key "${VAULT_KEY}" "$@"
}

# Function to check Vault connectivity
check_vault() {
    local host=$1
    local port=$2

    if vtls_curl -f -s "https://${host}:${port}/v1/sys/health" > /dev/null 2>&1; then
        echo "OK: Vault ${host}:${port} is accessible (mTLS)"
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

    local sealed=$(vtls_curl -s "https://${host}:${port}/v1/sys/health" | jq -r '.sealed')
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

    local cluster=$(vtls_curl -s "https://${host}:${port}/v1/sys/health" | jq -r '.cluster_name')
    if [ "$cluster" != "null" ] && [ "$cluster" != "" ]; then
        echo "OK: Vault ${host}:${port} is part of cluster: $cluster"
        return 0
    else
        echo "WARNING: Vault ${host}:${port} is not part of a cluster"
        return 0  # Not an error, just a warning
    fi
}

# Function to check Vault storage
check_vault_storage() {
    local host=$1
    local port=$2

    local storage=$(vtls_curl -s "https://${host}:${port}/v1/sys/storage/raft/configuration" | jq -r '.data.configuration')
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

    local is_leader=$(vtls_curl -s "https://${host}:${port}/v1/sys/leader" | jq -r '.is_self')
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

# Check that a connection WITHOUT a client certificate is rejected (mTLS DoD)
check_vault_mtls_enforced() {
    local host=$1
    local port=$2

    if vtls_curl_no_cert -f -s "https://${host}:${port}/v1/sys/health" > /dev/null 2>&1; then
        echo "ERROR: Vault ${host}:${port} accepted a request without a client certificate (mTLS not enforced)"
        return 1
    else
        echo "OK: Vault ${host}:${port} rejects clients without a certificate"
        return 0
    fi
}

vtls_curl_no_cert() {
    curl --cacert "${CA_PEM}" "$@"
}

# Main health check
echo "Starting Vault HA health check (mTLS)..."

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
        :
    fi

    if ! check_vault_mtls_enforced "$node" "$VAULT_PORT"; then
        all_nodes_ok=false
    fi
done

# Check if there's exactly one leader
leader_count=0
for node in "${VAULT_NODES[@]}"; do
    is_leader=$(vtls_curl -s "https://${node}:${VAULT_PORT}/v1/sys/leader" | jq -r '.is_self')
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

if [ "$all_nodes_ok" = true ] && [ "$all_unsealed" = true ] && [ "$all_cluster_ok" = true ] && [ "$all_storage_ok" = true ] && [ "$leader_count" -eq 1 ]; then
    echo "All Vault health checks passed!"
    exit 0
else
    echo "Some Vault health checks failed!"
    exit 1
fi
