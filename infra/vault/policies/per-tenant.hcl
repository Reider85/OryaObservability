# PP33: Multi-tenant Vault policy template
# 
# This template is rendered per-tenant by vault_init.sh using sed substitution:
# sed "s/{{tenant_id}}/<actual_tenant>/g" per-tenant.hcl | vault policy write pii-<tenant> -
#
# Path templating uses {{tenant_id}} placeholder to ensure tenant isolation.
# The Vault policy restricts access to only the tenant's own paths.
#
# JWT Authentication:
# - Each tenant has a JWT role with bound_claims={"tenant_id":["<tenant>"]}
# - Role is configured with user_claim (e.g., "sub") and policy=pii-<tenant>
# - Vault checks JWT tenant_id claim at login and enforces policy path restrictions
# - Defense-in-depth: client-side tenant guard in VaultClient.recover()
#
# Transit Encryption:
# - Per-tenant transit key "{tenant}_pii_key" for encrypting KV originals
# - Policy allows transit encrypt/decrypt operations on tenant-specific keys
#
# Note: allowed_parameters omitted to avoid breaking KV writes.
# Tenant isolation is enforced via path scoping and JWT role claims.

# Read KV data for tenant's PII originals
path "secret/data/pii/{{tenant_id}}/*" {
  capabilities = ["read"]
}

# List and manage metadata for tenant's PII keys
path "secret/metadata/pii/{{tenant_id}}" {
  capabilities = ["list", "delete"]
}

path "secret/metadata/pii/{{tenant_id}}/*" {
  capabilities = ["read", "list", "delete"]
}

# Transit encryption/decryption for tenant's PII originals
path "transit/encrypt/{{tenant_id}}_pii_key" {
  capabilities = ["update"]
}

path "transit/decrypt/{{tenant_id}}_pii_key" {
  capabilities = ["update"]
}

# Read transit key metadata (key exists, rotation status, etc.)
path "transit/keys/{{tenant_id}}_pii_key" {
  capabilities = ["read"]
}