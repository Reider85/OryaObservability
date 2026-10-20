# Vault configuration for HA with Raft storage, auto-unseal and mTLS (PP02).
# Mounted by both vault-1 and vault-2; node-specific values come from env
# (VAULT_NODE_ID). All inter-node and client traffic requires client certs.

storage "raft" {
  path = "/vault/data"
  node_id = "${VAULT_NODE_ID:-vault-1}"

  retry_join {
    leader_api_addr = "https://vault-1:8200"
    leader_ca_cert  = file("/vault/certs/ca.pem")
  }
  retry_join {
    leader_api_addr = "https://vault-2:8200"
    leader_ca_cert  = file("/vault/certs/ca.pem")
  }
}

# Auto-unseal via the dedicated transit vault (vault-unseal)
seal "transit" {
  address       = "https://vault-unseal:8200"
  token         = "${VAULT_TRANSIT_TOKEN:-vault-transit-token}"
  tls_ca_cert   = "/vault/certs/ca.pem"
  disable_renewal = "false"
  mount_path    = "transit/"
}

# mTLS listener: server cert + mandatory, verified client certs (PP02)
listener "tcp" {
  address                   = "0.0.0.0:8200"
  tls_cert_file             = "/vault/certs/vault.pem"
  tls_key_file              = "/vault/certs/vault-key.pem"
  tls_ca_cert_file          = "/vault/certs/ca.pem"
  tls_disable_client_certs  = false
}

api_addr     = "https://${VAULT_NODE_ID:-vault-1}:8200"
cluster_addr = "https://${VAULT_NODE_ID:-vault-1}:8200"

cluster_name = "observability-ha"

disable_clustering = "false"
disable_cache      = "false"

log_level = "info"
log_format = "standard"

telemetry {
  disable_hostname            = true
  enable_hostname_label       = false
  enable_service_label        = false
  prometheus_retention_time   = "24h"
}
