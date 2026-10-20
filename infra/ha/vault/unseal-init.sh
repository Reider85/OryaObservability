#!/bin/bash
# Vault unseal bootstrap script
# This script initializes the transit vault and unseals the raft nodes

set -e

VAULT_ADDR="http://localhost:8200"
VAULT_TRANSIT_TOKEN="vault-transit-token"
VAULT_UNSEAL_TOKEN="vault-unseal-token"

echo "Starting Vault unseal initialization..."

# Wait for Vault to be ready
until curl -f -s "${VAULT_ADDR}/v1/sys/health"; do
    echo "Waiting for Vault to be ready..."
    sleep 2
done

# Initialize Vault if not already initialized
if ! curl -f -s "${VAULT_ADDR}/v1/sys/init" | grep -q "initialized.*true"; then
    echo "Initializing Vault..."
    INIT_RESPONSE=$(curl -s -X PUT "${VAULT_ADDR}/v1/sys/init" \
        -H "Content-Type: application/json" \
        -d '{"secret_shares": 5, "secret_threshold": 3, "recovery_shares": 5, "recovery_threshold": 3}')
    
    ROOT_TOKEN=$(echo "$INIT_RESPONSE" | jq -r '.root_token')
    UNSEAL_KEYS=$(echo "$INIT_RESPONSE" | jq -r '.unseal_keys_b64[]')
    
    echo "Vault initialized!"
    echo "Root token: $ROOT_TOKEN"
    echo "Unseal keys:"
    echo "$UNSEAL_KEYS"
    
    # Store unseal keys in a temporary file for this session
    echo "$UNSEAL_KEYS" > /tmp/unseal_keys.txt
    echo "$ROOT_TOKEN" > /tmp/root_token.txt
    
    # Enable transit backend
    echo "Enabling transit backend..."
    curl -s -X PUT "${VAULT_ADDR}/v1/sys/mounts/transit" \
        -H "X-Vault-Token: $ROOT_TOKEN" \
        -H "Content-Type: application/json" \
        -d '{"type": "transit"}'
    
    # Create a token for auto-unseal
    echo "Creating transit token for auto-unseal..."
    TRANSIT_TOKEN_RESPONSE=$(curl -s -X POST "${VAULT_ADDR}/v1/auth/token/create" \
        -H "X-Vault-Token: $ROOT_TOKEN" \
        -H "Content-Type: application/json" \
        -d '{"policies": ["root"], "ttl": "24h", "renewable": true}')
    
    VAULT_TRANSIT_TOKEN=$(echo "$TRANSIT_TOKEN_RESPONSE" | jq -r '.auth.client_token')
    echo "Transit token: $VAULT_TRANSIT_TOKEN"
    
    # Configure auto-unseal keys in transit backend
    echo "Configuring auto-unseal keys in transit backend..."
    KEY_NAME="vault-unseal-key"
    
    # Create the key
    curl -s -X POST "${VAULT_ADDR}/v1/transit/keys/${KEY_NAME}" \
        -H "X-Vault-Token: $ROOT_TOKEN" \
        -H "Content-Type: application/json" \
        -d '{"type": "aes256-gcm96", "exportable": true}'
    
    # Store unseal keys in the transit backend
    while IFS= read -r unseal_key; do
        if [ -n "$unseal_key" ]; then
            curl -s -X POST "${VAULT_ADDR}/v1/transit/encrypt/${KEY_NAME}" \
                -H "X-Vault-Token: $ROOT_TOKEN" \
                -H "Content-Type: application/json" \
                -d '{"plaintext": "'"$(echo -n "$unseal_key" | base64 -w 0)"'"}' | \
                jq -r '.data.ciphertext' > /tmp/encrypted_key.txt
            
            # Store encrypted key in a variable for later use
            ENCRYPTED_KEY=$(cat /tmp/encrypted_key.txt)
            echo "Encrypted unseal key stored in transit backend"
        fi
    done <<< "$UNSEAL_KEYS"
    
    # Configure auto-unseal for the raft nodes
    echo "Configuring auto-unseal for raft nodes..."
    
    # Create a configuration file for the raft nodes
    cat > /tmp/vault-raft-config.hcl << EOF
storage "raft" {
  path = "/vault/data"
  node_id = "vault-1"
}

seal "transit" {
  address = "http://vault-unseal:8200"
  token = "$VAULT_TRANSIT_TOKEN"
  disable_renewal = "false"
  mount_path = "transit/"
  key_name = "$KEY_NAME"
}

listener "tcp" {
  address = "0.0.0.0:8200"
  tls_disable = "true"
}

api_addr = "http://localhost:8200"
cluster_addr = "http://vault-1:8200"
EOF
    
    # Copy the configuration to the raft nodes
    echo "Vault unseal initialization complete!"
    echo "Transit token: $VAULT_TRANSIT_TOKEN"
    echo "Key name: $KEY_NAME"
    echo "Configuration file created at /tmp/vault-raft-config.hcl"
    
    # Output the configuration for debugging
    echo "Vault configuration:"
    cat /tmp/vault-raft-config.hcl
    
else
    echo "Vault already initialized. Proceeding with unseal..."
    
    # Try to unseal using existing keys
    if [ -f /tmp/unseal_keys.txt ]; then
        echo "Using existing unseal keys..."
        while IFS= read -r unseal_key; do
            if [ -n "$unseal_key" ]; then
                curl -s -X PUT "${VAULT_ADDR}/v1/sys/unseal" \
                    -H "Content-Type: application/json" \
                    -d '{"key": "'"$unseal_key"'"}' | \
                    jq -r '.sealed'
            fi
        done < /tmp/unseal_keys.txt
    else
        echo "No existing unseal keys found. Please provide them manually."
        exit 1
    fi
fi

# Check if Vault is unsealed
SEALED_STATUS=$(curl -s "${VAULT_ADDR}/v1/sys/health" | jq -r '.sealed')
if [ "$SEALED_STATUS" = "false" ]; then
    echo "Vault successfully unsealed!"
else
    echo "Vault is still sealed. Please check the logs."
    exit 1
fi