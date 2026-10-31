#!/bin/bash
# Vault Raft cluster bootstrap (PP37).
#
# Runs as a one-shot container (vault-init-ha) after vault-1/2/3 are started.
# Responsibilities:
#   1. `vault operator init` on vault-1 (auto-unseal: we get recovery keys,
#      not unseal keys — the seal block handles unsealing).
#   2. Wait for auto-unseal (sealed=false within 60s) — this proves the
#      transit seal works.
#   3. Apply PC02/PP33 config: audit, policies, transit engine, per-tenant
#      keys, JWT auth (optional), SDK token.
#   4. Persist root token + recovery keys to the vault_raft_bootstrap volume.
#   5. Wait for raft list-peers == 3 (all nodes joined).
#
# mTLS (PP02): every call presents the vault mesh certificate.

set -e

BOOTSTRAP_ADDR="${BOOTSTRAP_ADDR:-https://vault-1:8200}"
VAULT_CACERT="${VAULT_CACERT:-/vault/certs/ca.pem}"
VAULT_CLIENT_CERT="${VAULT_CLIENT_CERT:-/vault/certs/vault.pem}"
VAULT_CLIENT_KEY="${VAULT_CLIENT_KEY:-/vault/certs/vault-key.pem}"
BOOTSTRAP_DIR="${BOOTSTRAP_DIR:-/vault/bootstrap}"
POLICY_DIR="${POLICY_DIR:-/vault/policies}"

vtls() {
    curl --cacert "${VAULT_CACERT}" \
         --cert "${VAULT_CLIENT_CERT}" \
         --key "${VAULT_CLIENT_KEY}" \
         "$@"
}

# Wait for the raft node to answer (sealed = 503, standby = 429).
echo "Waiting for ${BOOTSTRAP_ADDR}..."
until vtls -s -o /dev/null -w '%{http_code}' "${BOOTSTRAP_ADDR}/v1/sys/health" \
       | grep -qE '200|429|503'; do
    sleep 2
done

INITIALIZED=$(vtls -s "${BOOTSTRAP_ADDR}/v1/sys/init" | jq -r '.initialized')

if [ "$INITIALIZED" != "true" ]; then
    echo "Initializing raft cluster (recovery-only, auto-unseal)..."
    INIT_RESPONSE=$(vtls -s -X PUT "${BOOTSTRAP_ADDR}/v1/sys/init" \
        -H "Content-Type: application/json" \
        -d '{"recovery_shares": 5, "recovery_threshold": 3}')

    ROOT_TOKEN=$(echo "$INIT_RESPONSE" | jq -r '.root_token')
    mkdir -p "$BOOTSTRAP_DIR"
    echo "$INIT_RESPONSE" | jq -r '.recovery_keys_b64[]' > "$BOOTSTRAP_DIR/recovery_keys.txt"
    echo "$ROOT_TOKEN" > "$BOOTSTRAP_DIR/root_token.txt"
    chmod 600 "$BOOTSTRAP_DIR/recovery_keys.txt" "$BOOTSTRAP_DIR/root_token.txt"
    echo "Raft cluster initialized; recovery keys + root token persisted."
else
    echo "Raft cluster already initialized."
    ROOT_TOKEN=$(cat "$BOOTSTRAP_DIR/root_token.txt" 2>/dev/null || true)
fi

# Wait for auto-unseal (transit seal must decrypt within 60s).
echo "Waiting for auto-unseal..."
DEADLINE=$(( $(date +%s) + 60 ))
while [ "$(vtls -s "${BOOTSTRAP_ADDR}/v1/sys/health" | jq -r '.sealed')" = "true" ]; do
    if [ "$(date +%s)" -gt "$DEADLINE" ]; then
        echo "ERROR: Vault still sealed after 60s — auto-unseal failed."
        exit 1
    fi
    sleep 3
done
echo "Auto-unseal confirmed."

if [ -z "$ROOT_TOKEN" ]; then
    echo "ERROR: no root token; cannot apply configuration."
    exit 1
fi

auth() { vtls -s -H "X-Vault-Token: ${ROOT_TOKEN}" "$@"; }

# --- PC02 / PP33 configuration (mirrors infra/vault/vault_init.sh) ---

echo "Enabling audit device..."
auth -X PUT "${BOOTSTRAP_ADDR}/v1/sys/audit/file" \
    -H "Content-Type: application/json" \
    -d '{"type":"file","options":{"file_path":"/vault/audit/audit.log"}}' \
    > /dev/null 2>&1 || echo "Audit device already enabled."

echo "Writing policies..."
for policy in pii-recovery audit; do
    if [ -f "${POLICY_DIR}/${policy}.hcl" ]; then
        auth -X PUT "${BOOTSTRAP_ADDR}/v1/sys/policy/${policy}" \
            -H "Content-Type: application/json" \
            -d "$(jq -n --rawfile p "${POLICY_DIR}/${policy}.hcl" '{policy: $p}')" \
            > /dev/null
        echo "Policy ${policy} written."
    else
        echo "WARNING: ${POLICY_DIR}/${policy}.hcl not found, skipping."
    fi
done

echo "Enabling transit engine..."
auth -X PUT "${BOOTSTRAP_ADDR}/v1/sys/mounts/transit" \
    -H "Content-Type: application/json" \
    -d '{"type":"transit"}' > /dev/null 2>&1 || echo "Transit engine already enabled."

# JWT auth (optional, PP33)
if [ -n "$VAULT_JWT_OIDC_DISCOVERY_URL" ]; then
    echo "Enabling JWT auth..."
    auth -X PUT "${BOOTSTRAP_ADDR}/v1/sys/auth/jwt" \
        -H "Content-Type: application/json" \
        -d '{"type":"jwt"}' > /dev/null 2>&1 || echo "JWT auth already enabled."
    auth -X PUT "${BOOTSTRAP_ADDR}/v1/auth/jwt/config" \
        -H "Content-Type: application/json" \
        -d "$(jq -n \
            --arg url "$VAULT_JWT_OIDC_DISCOVERY_URL" \
            --arg issuer "$VAULT_JWT_OIDC_ISSUER" \
            --arg audience "$VAULT_JWT_OIDC_AUDIENCE" \
            '{oidc_discovery_url: $url, bound_issuer: $issuer, bound_audiences: [$audience], default_role: "default"}')" \
        > /dev/null 2>&1 || echo "JWT config already exists."
fi

# Per-tenant policies + transit keys (PP33)
if [ -n "$VAULT_TENANTS" ]; then
    echo "Configuring per-tenant setup..."
    for tenant in $(echo "$VAULT_TENANTS" | tr ',' ' '); do
        echo "Tenant: ${tenant}"
        if [ -f "${POLICY_DIR}/per-tenant.hcl" ]; then
            sed "s/{{tenant_id}}/${tenant}/g" "${POLICY_DIR}/per-tenant.hcl" \
                | auth -X PUT "${BOOTSTRAP_ADDR}/v1/sys/policy/pii-${tenant}" \
                    -H "Content-Type: application/json" \
                    --data-binary @- > /dev/null
        fi
        # Fix: use ${tenant}_pii_key (was $tenant_pii_key = empty in old script)
        auth -X POST "${BOOTSTRAP_ADDR}/v1/transit/keys/${tenant}_pii_key" \
            -H "Content-Type: application/json" \
            -d '{"type":"aes256-gcm96"}' > /dev/null 2>&1 \
            || echo "Transit key ${tenant}_pii_key already exists."
        if [ -n "$VAULT_JWT_OIDC_DISCOVERY_URL" ]; then
            auth -X PUT "${BOOTSTRAP_ADDR}/v1/auth/jwt/roles/${tenant}" \
                -H "Content-Type: application/json" \
                -d "$(jq -n \
                    --arg tenant "$tenant" \
                    --arg claim "$VAULT_JWT_USER_CLAIM" \
                    '{bound_claims: {tenant_id: [$tenant]}, user_claim: $claim, policies: ["pii-" + $tenant], ttl: "24h"}')" \
                > /dev/null 2>&1 || echo "JWT role ${tenant} already exists."
        fi
    done
else
    echo "VAULT_TENANTS not set, skipping per-tenant configuration."
fi

# SDK token with pii-recovery policy
echo "Creating SDK token..."
SDK_TOKEN_RESPONSE=$(auth -X POST "${BOOTSTRAP_ADDR}/v1/auth/token/create" \
    -H "Content-Type: application/json" \
    -d '{"policies":["pii-recovery"],"ttl":"768h","renewable":true}')
SDK_TOKEN=$(echo "$SDK_TOKEN_RESPONSE" | jq -r '.auth.client_token')
echo "$SDK_TOKEN" > "$BOOTSTRAP_DIR/sdk_token.txt"
chmod 600 "$BOOTSTRAP_DIR/sdk_token.txt"
echo "SDK token persisted to ${BOOTSTRAP_DIR}/sdk_token.txt."

# Wait for all 3 raft peers to join.
echo "Waiting for raft quorum (3 peers)..."
DEADLINE=$(( $(date +%s) + 90 ))
while true; do
    PEERS=$(auth -s "${BOOTSTRAP_ADDR}/v1/sys/storage/raft/configuration" \
        | jq -r '.data.config.servers | length' 2>/dev/null || echo 0)
    if [ "$PEERS" -ge 3 ]; then
        echo "Raft quorum achieved: ${PEERS} peers."
        break
    fi
    if [ "$(date +%s)" -gt "$DEADLINE" ]; then
        echo "WARNING: only ${PEERS} peers after 90s (expected 3)."
        break
    fi
    sleep 5
done

echo "Raft bootstrap complete."
