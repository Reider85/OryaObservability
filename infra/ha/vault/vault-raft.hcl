# Vault configuration for HA with Raft storage and auto-unseal
# This configuration is used by both vault-1 and vault-2 nodes

storage "raft" {
  path = "/vault/data"
  node_id = "${VAULT_NODE_ID:-vault-1}"
  retry_join {
    leader_api_addr = "http://vault-1:8200"
    leader_addr = "vault-1:8200"
  }
  retry_join {
    leader_api_addr = "http://vault-2:8200"
    leader_addr = "vault-2:8200"
  }
}

# Auto-unseal configuration using transit backend
seal "transit" {
  address = "http://vault-unseal:8200"
  token = "${VAULT_TRANSIT_TOKEN:-vault-transit-token}"
  disable_renewal = "false"
  # Additional transit configuration for auto-unseal
  mount_path = "transit/"
}

# Listener configuration
listener "tcp" {
  address = "0.0.0.0:8200"
  tls_disable = "true"
}

# API configuration
api_addr = "http://localhost:8200"
cluster_addr = "http://${VAULT_NODE_ID:-vault-1}:8200"

# Performance tuning
disable_clustering = "false"
disable_cache = "false"

# Log configuration
log_level = "info"
log_format = "standard"

# Telemetry configuration
telemetry {
  disable_hostname = true
  enable_hostname_label = false
  enable_service_label = false
  prometheus_retention_time = "24h"
}

# Additional configuration for HA
ha_storage "raft" {
  performance_multiplier = 1
  performance_standby_multiplier = 1
}

# Auto-unseal settings
seal "transit" {
  address = "http://vault-unseal:8200"
  token = "${VAULT_TRANSIT_TOKEN:-vault-transit-token}"
  disable_renewal = "false"
  # Transit backend configuration
  mount_path = "transit/"
}

# Leader election settings
cluster_name = "observability-ha"
leader_address = "${VAULT_NODE_ID:-vault-1}:8200"

# Storage performance settings
storage "raft" {
  path = "/vault/data"
  node_id = "${VAULT_NODE_ID:-vault-1}"
  retry_join {
    leader_api_addr = "http://vault-1:8200"
    leader_addr = "vault-1:8200"
  }
  retry_join {
    leader_api_addr = "http://vault-2:8200"
    leader_addr = "vault-2:8200"
  }
  performance_multiplier = 1
  performance_standby_multiplier = 1
}

# Auto-unseal configuration
seal "transit" {
  address = "http://vault-unseal:8200"
  token = "${VAULT_TRANSIT_TOKEN:-vault-transit-token}"
  disable_renewal = "false"
  mount_path = "transit/"
}

# Listener configuration
listener "tcp" {
  address = "0.0.0.0:8200"
  tls_disable = "true"
}

# API configuration
api_addr = "http://localhost:8200"
cluster_addr = "http://${VAULT_NODE_ID:-vault-1}:8200"

# Performance tuning
disable_clustering = "false"
disable_cache = "false"

# Log configuration
log_level = "info"
log_format = "standard"

# Telemetry configuration
telemetry {
  disable_hostname = true
  enable_hostname_label = false
  enable_service_label = false
  prometheus_retention_time = "24h"
}

# Additional configuration for HA
ha_storage "raft" {
  performance_multiplier = 1
  performance_standby_multiplier = 1
}