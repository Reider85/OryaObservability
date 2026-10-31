#!/bin/bash
# Vault transit unseal bootstrap (PP37).
#
# Runs inside the dedicated vault-unseal container (file storage, Shamir).
# Responsibilities:
#   1. Init the transit Vault if it is not yet initialized (keys persisted to
#      the vault_unseal_data volume, NOT /tmp — /tmp dies with the container).
#   2. Unseal it with the generated keys (self-unseal: the script holds them).
#   3. Enable the transit engine and create the "autounseal" key.
#   4. Mint a periodic orphan token with the FIXED id "vault-transit-token"
#      (the raft HCL seal blocks expect exactly this id via VAULT_TOKEN).
#
# mTLS (PP02): every call presents the vault-unseal mesh certificate and
# trusts the internal CA (no -k / insecure flags anywhere).

set -e

VAULT_ADDR="https://localhost:8200"
VAULT_CACERT="${VAULT_CACERT:-/vault/certs/ca.pem}"
VAULT_CLIENT_CERT="${VAULT_CLIENT_CERT:-/vault/certs/vault-unseal.pem}"
VAULT_CLIENT_KEY="${VAULT_CLIENT_KEY:-/vault/certs/vault-unseal-key.pem}"

# Persisted on the vault_unseal_data volume so a container restart can re-unseal.
KEYS_FILE="/vault/data/unseal_keys.txt"
TOKEN_FILE="/vault/data/root_token.txt"
TRANSIT_TOKEN_ID="vault-transit-token"

vtls() {
    curl --cacert "${VAULT_CACERT}" \
         --cert "${VAULT_CLIENT_CERT}" \
         --key "${VAULT_CLIENT_KEY}" \
         "$@"
}

echo "Starting Vault transit bootstrap..."

# Wait for the listener to answer (sealed-but-initialized returns 501/503).
until curl --cacert "${VAULT_CACERT}" \
           --cert "${VAULT_CLIENT_CERT}" \
           --key "${VAULT_CLIENT_KEY}" \
           -s -o /dev/null -w '%{http_code}' "${VAULT_ADDR}/v1/sys/health" \
       | grep -qE '200|429|501|503'; do
    echo "Waiting for Vault listener..."
    sleep 2
done

INITIALIZED=$(vtls -s "${VAULT_ADDR}/v1/sys/init" | jq -r '.initialized')

if [ "$INITIALIZED" != "true" ]; then
    echo "Initializing transit Vault..."
    INIT_RESPONSE=$(vtls -s -X PUT "${VAULT_ADDR}/v1/sys/init" \
        -H "Content-Type: application/json" \
        -d '{"secret_shares": 5, "secret_threshold": 3}')

    ROOT_TOKEN=$(echo "$INIT_RESPONSE" | jq -r '.root_token')
    echo "$INIT_RESPONSE" | jq -r '.unseal_keys_b64[]' > "$KEYS_FILE"
    echo "$ROOT_TOKEN" > "$TOKEN_FILE"
    chmod 600 "$KEYS_FILE" "$TOKEN_FILE"
    echo "Transit Vault initialized; keys persisted to ${KEYS_FILE}."
else
    echo "Transit Vault already initialized."
    ROOT_TOKEN=$(cat "$TOKEN_FILE" 2>/dev/null || true)
fi

# Self-unseal: submit keys until unsealed (persisted keys survive restarts).
if [ -f "$KEYS_FILE" ]; then
    while [ "$(vtls -s "${VAULT_ADDR}/v1/sys/health" | jq -r '.sealed')" = "true" ]; do
        UNSEALED=false
        while IFS= read -r key; do
            [ -z "$key" ] && continue
            vtls -s -X PUT "${VAULT_ADDR}/v1/sys/unseal" \
                -H "Content-Type: application/json" \
                -d "{\"key\": \"${key}\"}" > /dev/null || true
            if [ "$(vtls -s "${VAULT_ADDR}/v1/sys/health" | jq -r '.sealed')" = "false" ]; then
                UNSEALED=true
                break
            fi
        done < "$KEYS_FILE"
        if [ "$UNSEALED" = "true" ]; then
            echo "Transit Vault unsealed."
            break
        fi
        echo "Still sealed, retrying key submission..."
        sleep 2
    done
else
    echo "ERROR: no ${KEYS_FILE} — cannot unseal a previously initialized Vault."
    exit 1
fi

if [ -z "$ROOT_TOKEN" ]; then
    echo "ERROR: no root token in ${TOKEN_FILE}; cannot configure transit."
    exit 1
fi

auth() { vtls -s -H "X-Vault-Token: ${ROOT_TOKEN}" "$@"; }

echo "Enabling transit engine..."
auth -X PUT "${VAULT_ADDR}/v1/sys/mounts/transit" \
    -H "Content-Type: application/json" \
    -d '{"type": "transit"}' > /dev/null || echo "Transit engine already enabled."

echo "Creating autounseal key..."
auth -X POST "${VAULT_ADDR}/v1/transit/keys/autounseal" \
    -H "Content-Type: application/json" \
    -d '{"type": "aes256-gcm96"}' > /dev/null || echo "Key autounseal already exists."

# Minimal policy: encrypt/decrypt only on the autounseal key.
echo "Writing transit policy..."
auth -X PUT "${VAULT_ADDR}/v1/sys/policy/transit-autounseal" \
    -H "Content-Type: application/json" \
    -d '{"policy":"path \"transit/encrypt/autounseal\" { capabilities = [\"update\"] }\npath \"transit/decrypt/autounseal\" { capabilities = [\"update\"] }\npath \"transit/keys/autounseal\" { capabilities = [\"read\"] }"}' > /dev/null

# Mint the transit token with a FIXED id so the seal HCL (VAULT_TOKEN env) is
# stable across reboots. If the id already exists (restart), skip creation.
if auth -s "${VAULT_ADDR}/v1/auth/token/lookup/${TRANSIT_TOKEN_ID}" | jq -e '.data.id' > /dev/null 2>&1; then
    echo "Transit token ${TRANSIT_TOKEN_ID} already exists; keeping it."
else
    echo "Minting periodic transit token ${TRANSIT_TOKEN_ID}..."
    auth -X POST "${VAULT_ADDR}/v1/auth/token/create" \
        -H "Content-Type: application/json" \
        -d "{\"policies\": [\"transit-autounseal\"], \"ttl\": \"768h\", \"period\": \"768h\", \"renewable\": true, \"id\": \"${TRANSIT_TOKEN_ID}\"}" > /dev/null
    echo "Transit token ${TRANSIT_TOKEN_ID} minted."
fi

echo "Transit bootstrap complete: unsealed, autounseal key + fixed-id token ready."
