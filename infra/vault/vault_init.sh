#!/bin/bash

set -e

echo "Waiting for Vault to be ready..."
while ! curl -s http://localhost:8200/v1/sys/health > /dev/null 2>&1; do
    echo "Waiting for Vault..."
    sleep 2
done

echo "Vault is ready. Configuring..."

# Enable audit backend
echo "Enabling audit backend..."
vault audit enable file file_path=/vault/audit/audit.log

# Apply policies
echo "Applying pii-recovery policy..."
vault policy write pii-recovery /vault/policies/pii-recovery.hcl

echo "Applying audit policy..."
vault policy write audit /vault/policies/audit.hcl

# Create SDK token with pii-recovery policy
echo "Creating SDK token..."
VAULT_TOKEN=$(vault token create -policy=pii-recovery -format=json | jq -r '.auth.client_token')

# Output the token for the user
echo "SDK Token created:"
echo "VAULT_TOKEN=$VAULT_TOKEN"

echo "Vault configuration completed successfully!"