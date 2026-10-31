# Vault Raft node 2 (PP37). Static config — no env interpolation:
# Vault HCL does NOT expand ${VAR:-default} (PP36/PP37 bug this replaces).
# Token for the transit seal comes from the VAULT_TOKEN env var.

storage "raft" {
  path    = "/vault/data"
  node_id = "vault-2"

  retry_join {
    leader_api_addr         = "https://vault-1:8200"
    leader_ca_cert          = file("/vault/certs/ca.pem")
    leader_client_cert_file = "/vault/certs/vault.pem"
    leader_client_key_file  = "/vault/certs/vault-key.pem"
  }
  retry_join {
    leader_api_addr         = "https://vault-2:8200"
    leader_ca_cert          = file("/vault/certs/ca.pem")
    leader_client_cert_file = "/vault/certs/vault.pem"
    leader_client_key_file  = "/vault/certs/vault-key.pem"
  }
  retry_join {
    leader_api_addr         = "https://vault-3:8200"
    leader_ca_cert          = file("/vault/certs/ca.pem")
    leader_client_cert_file = "/vault/certs/vault.pem"
    leader_client_key_file  = "/vault/certs/vault-key.pem"
  }
}

# Auto-unseal via the dedicated transit vault (vault-unseal). The token is
# read from VAULT_TOKEN (set by compose to the fixed-id transit token) —
# never hardcode tokens in a mounted config.
seal "transit" {
  address             = "https://vault-unseal:8200"
  key_name            = "autounseal"
  mount_path          = "transit/"
  disable_renewal     = "false"
  tls_ca_cert         = "/vault/certs/ca.pem"
  tls_client_cert_file = "/vault/certs/vault.pem"
  tls_client_key_file  = "/vault/certs/vault-key.pem"
}

# mTLS listener: server cert + mandatory, verified client certs (PP02)
listener "tcp" {
  address                    = "0.0.0.0:8200"
  tls_cert_file              = "/vault/certs/vault.pem"
  tls_key_file               = "/vault/certs/vault-key.pem"
  tls_ca_cert_file           = "/vault/certs/ca.pem"
  tls_disable_client_certs   = false
  require_request_client_cert = true
}

api_addr     = "https://vault-2:8200"
cluster_addr = "https://vault-2:8200"

cluster_name = "observability-ha"

disable_clustering = "false"
disable_cache      = "false"

log_level  = "info"
log_format = "standard"

telemetry {
  disable_hostname          = true
  enable_hostname_label     = false
  enable_service_label      = false
  prometheus_retention_time = "24h"
}
