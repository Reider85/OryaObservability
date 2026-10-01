# Summary: Start/Stop Scripts Enhancement

## What was fixed

### `infra/docker-compose.yml`
- **compliance-ui**: Fixed volume mount (`../:/app`), added pip install for `[api]` extra, switched healthcheck to Python/urllib
- **eval-worker**: Fixed command to run both HTTP server and RQ worker, switched healthcheck to Python/urllib
- **langfuse-worker**: Added healthcheck for proper waiting

### `scripts/start-all.ps1`
- Always passes both `docker-compose.yml` and `docker-compose.override.yml` (fixes Vault port 8201)
- Added `-InitVault` and `-SkipInitServices` parameters
- One-shot init services after core healthy: `clickhouse-init`, `minio-init`
- Better wait logic: separate for `healthy` and `running` services, PS 5.1-safe NDJSON parsing
- Vault initialization: auto if unsealed + `VAULT_ROOT_TOKEN`, else instructions
- Smoke checks for all endpoints (critical/optional)
- Full endpoint list (sampler, phoenix, grafana, vault 8201)
- Placeholder secrets warnings
- Auto-build missing custom images if `-Build` or missing

### `scripts/stop-all.ps1`
- Fixed PS 5.1 Join-Path bug
- Always passes both compose files
- Stops external drift-detector if present
- Post-down verification

### `pyproject.toml`
- Added `pandas>=2.0` to `[api]` extra for compliance-ui

### `infra/.env.example`
- Added `VAULT_ROOT_TOKEN=` field
- Fixed `VAULT_ADDR=http://localhost:8201`

## Usage

### Start
```powershell
# Basic start
.\scripts\start-all.ps1

# With auto-vault init if root token available
.\scripts\start-all.ps1 -InitVault

# Force rebuild images
.\scripts\start-all.ps1 -Build

# Skip one-shot init services (for existing volumes)
.\scripts\start-all.ps1 -SkipInitServices

# No wait + no smoke (for CI/testing)
.\scripts\start-all.ps1 -NoWait -Detach
```

### Stop
```powershell
# Basic stop
.\scripts\stop-all.ps1

# Remove volumes and images
.\scripts\stop-all.ps1 -RemoveVolumes -RemoveImages
```

## Benefits
1. **Consistent Vault port**: Override always loaded, no collisions
2. **Full initialization**: One-shot services run for existing volumes
3. **Better reliability**: Health checks, smoke tests, auto-build
4. **Vault automation**: Semi-auto init with clear instructions
5. **Complete endpoint visibility**: All services accessible
6. **Graceful drift-detector stop**: External project cleaned up
7. **PS 5.1 compatibility**: Fixed all syntax issues

## Files changed
- `infra/docker-compose.yml`
- `scripts/start-all.ps1`
- `scripts/stop-all.ps1`
- `pyproject.toml`
- `infra/.env.example`

The stack now starts completely and stops cleanly.