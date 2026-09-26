# HashiCorp Vault for PII Recovery

This directory contains the HashiCorp Vault configuration for storing PII recovery mappings with TTL 24 hours.

## Services

| Service | Image | Port | Purpose |
|---|---|---|---|
| `vault` | `hashicorp/vault:1.18` | `8200` | Vault server for PII recovery storage |

## Quick Start

### 1. Start Vault

```bash
cd infra
docker compose up -d vault
docker compose logs -f vault
```

Wait for the Vault container to start. You should see "Vault server configuration loaded" in the logs.

### 2. Initialize Vault

The first time you start Vault, you need to initialize it:

```bash
docker compose exec vault vault operator init
```

Save the **Unseal Keys** and **Root Token** somewhere secure. You'll need the Unseal Keys to unseal Vault after restarts.

### 3. Unseal Vault

Run this command 5 times (once for each unseal key):

```bash
docker compose exec vault vault operator unseal <UNSEAL_KEY_1>
docker compose exec vault vault operator unseal <UNSEAL_KEY_2>
docker compose exec vault vault operator unseal <UNSEAL_KEY_3>
docker compose exec vault vault operator unseal <UNSEAL_KEY_4>
docker compose exec vault vault operator unseal <UNSEAL_KEY_5>
```

After the 5th unseal command, Vault should be unsealed and ready to use.

### 4. Configure Vault

The init script will automatically:
- Enable the audit backend
- Apply the `pii-recovery` and `audit` policies
- Create an SDK token for PII recovery

```bash
docker compose exec vault /vault/vault_init.sh
```

Copy the output `VAULT_TOKEN` value to your `.env` file.

### 5. Verify Vault is working

```bash
# Check Vault status
curl http://localhost:8200/v1/sys/health

# Check if audit backend is enabled
docker compose exec vault vault audit list

# Check if policies are applied
docker compose exec vault vault policy list
```

## Configuration Details

### Storage
- **Mode**: File storage (not dev mode)
- **Path**: `/vault/data`
- **Persistence**: Named volume `vault_data`

### Security
- **TLS**: Disabled for local dev (use mTLS in production)
- **Lease TTL**: 24 hours (both default and max)
- **Auto-unseal**: Manual 5-key-share (documented for prod migration)

### Policies

#### pii-recovery.hcl
- Read access to `secret/data/pii/*` (PII recovery mappings)
- Delete access to `secret/metadata/pii/*` (for cron cleanup)

#### audit.hcl
- Update access to `sys/audit-hash/*` (audit log management)

### Audit Backend
- **Type**: File
- **Path**: `/vault/audit/audit.log`
- **Enabled**: Automatically by init script

### SDK Token
- **Policy**: `pii-recovery`
- **Purpose**: Store and recover PII mappings with TTL
- **Usage**: The SDK uses this token to store mask → original PII mappings

## Environment Variables

| Variable | Description |
|---|---|
| `VAULT_ADDR` | Vault API address (default: http://localhost:8200) |
| `VAULT_TOKEN` | SDK token for PII recovery (created by init script) |
| `VAULT_NAMESPACE` | Vault Cloud namespace (empty for self-hosted) |
| `VAULT_TLS_CA` | Path to CA bundle for TLS (production only) |

## Production Migration

For production deployment:
1. Replace file storage with a production backend (Consul, AWS KMS, etc.)
2. Enable TLS with proper certificates
3. Configure auto-unseal with KMS/AWS CloudHSM
4. Set up proper authentication (LDAP, OIDC, etc.)
5. Implement proper monitoring and alerting

## Troubleshooting

### Vault is sealed
Run the unseal commands with your 5 unseal keys.

### Healthcheck fails
Vault responds to health checks even when sealed. This is normal. The healthcheck ensures the service is running.

### Init script fails
Check the logs: `docker compose logs -f vault`
Common issues:
- Vault not fully started
- Permission issues with volume mounts
- Policy files not found

### Audit backend not enabled
Check: `docker compose exec vault vault audit list`
If not enabled, run the init script again.

## API Usage

Once configured, the SDK will:
1. Store PII mappings: `PUT /v1/secret/data/pii/{mask}`
2. Recover PII: `GET /v1/secret/data/pii/{mask}`
3. Auto-delete after 24 hours (lease TTL)