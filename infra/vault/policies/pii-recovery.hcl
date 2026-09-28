# Vault policy for the PII mask -> original mappings (PC02) and the TTL
# cleanup cron job (PC21).
#
# The SDK token is created with this policy in infra/vault/vault_init.sh, so
# both the recovery path and cleanup_vault_expired() share it.
path "secret/data/pii/*" { capabilities = ["read"] }

# list/read/delete on the metadata tree: the cleanup job must enumerate
# pii/{hex8}/{timestamp} to compute created_at + ttl_seconds (KV v2 registers
# no Vault lease, so sys/leases alone finds nothing). delete is what the
# optional --delete-expired mode uses.
path "secret/metadata/pii"   { capabilities = ["list"] }
path "secret/metadata/pii/*" { capabilities = ["read", "list", "delete"] }

# Genuine Vault leases, if any leased engine is introduced later.
path "sys/leases/pii/*" { capabilities = ["list", "read"] }
