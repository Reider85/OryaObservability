#!/bin/bash

set -e

echo "Waiting for Vault to be ready..."
while ! curl -s http://localhost:8200/v1/sys/health > /dev/null 2>&1; do
    echo "Waiting for Vault..."
    sleep 2
done

echo "Vault is ready. Configuring..."

# Create audit directory if it doesn't exist
mkdir -p /vault/audit

# Enable audit backend
echo "Enabling audit backend..."
vault audit enable file file_path=/vault/audit/audit.log

# Apply policies
echo "Applying pii-recovery policy..."
vault policy write pii-recovery /vault/policies/pii-recovery.hcl

echo "Applying audit policy..."
vault policy write audit /vault/policies/audit.hcl

# PP33: Enable transit engine for per-tenant PII encryption
echo "Enabling transit engine..."
vault secrets enable transit 2>/dev/null || echo "Transit engine already enabled"

# PP33: Enable JWT authentication if configured
if [ -n "$VAULT_JWT_OIDC_DISCOVERY_URL" ]; then
    echo "Enabling JWT authentication..."
    vault auth enable jwt 2>/dev/null || echo "JWT auth already enabled"
    
    echo "Configuring JWT authentication..."
    vault write auth/jwt/config \
        oidc_discovery_url="$VAULT_JWT_OIDC_DISCOVERY_URL" \
        bound_issuer="$VAULT_JWT_OIDC_ISSUER" \
        bound_audiences="$VAULT_JWT_OIDC_AUDIENCE" \
        default_role="default" \
        2>/dev/null || echo "JWT config already exists"
fi

# PP33: Create per-tenant policies and transit keys
if [ -n "$VAULT_TENANTS" ]; then
    echo "Configuring per-tenant Vault setup..."
    for tenant in $(echo "$VAULT_TENANTS" | tr ',' ' '); do
        echo "Processing tenant: $tenant"
        
        # Render and apply per-tenant policy
        echo "Creating policy for tenant: $tenant"
        sed "s/{{tenant_id}}/$tenant/g" /vault/policies/per-tenant.hcl | vault policy write pii-$tenant -
        
        # Create per-tenant transit key
        echo "Creating transit key for tenant: $tenant"
        if ! vault read transit/keys/$tenant_pii_key >/dev/null 2>&1; then
            vault write transit/keys/$tenant_pii_key type=aes256-gcm96
        else
            echo "Transit key $tenant_pii_key already exists"
        fi
        
        # Create JWT role for this tenant if JWT is enabled
        if [ -n "$VAULT_JWT_OIDC_DISCOVERY_URL" ]; then
            echo "Creating JWT role for tenant: $tenant"
            vault write auth/jwt/roles/$tenant \
                bound_claims='{"tenant_id":["'$tenant'"]}' \
                user_claim="$VAULT_JWT_USER_CLAIM" \
                policies=pii-$tenant \
                ttl=24h \
                2>/dev/null || echo "JWT role for $tenant already exists"
        fi
    done
else
    echo "VAULT_TENANTS not set, skipping per-tenant configuration"
fi

# Create SDK token with pii-recovery policy
echo "Creating SDK token..."
VAULT_TOKEN=$(vault token create -policy=pii-recovery -format=json | jq -r '.auth.client_token')

# Output the token for the user
echo "SDK Token created:"
echo "VAULT_TOKEN=$VAULT_TOKEN"

echo "Vault configuration completed successfully!"