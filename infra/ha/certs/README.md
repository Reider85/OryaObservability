# Internal mTLS PKI (PP02 / T3.0.2)

Certificate authority and service certificates for the mutual-TLS mesh between
all internal observability services: SDK ↔ nginx-LB ↔ OTel Collector ↔
Langfuse ↔ Postgres ↔ ClickHouse ↔ Redis ↔ Vault ↔ pgBouncer.

## Layout

```
infra/ha/certs/
├── internal-ca/
│   ├── ca.pem          # internal CA certificate (public, may be shared)
│   └── ca-key.pem      # CA private key — Vault only, NEVER committed
├── <service>/
│   ├── <service>.pem   # service certificate (serverAuth + clientAuth)
│   ├── <service>-key.pem
│   └── openssl.cnf     # CSR config with the service SANs
└── .gitignore          # ignores *.pem / *.key / *.csr / *.srl
```

Issued services: `langfuse`, `clickhouse`, `postgres`, `repmgr`, `vault`,
`vault-unseal`, `redis`, `proxy`, `otel-collector`, `nginx-langfuse`,
`nginx-otel`, `pgbouncer`, `sdk`.

Every certificate carries **both** `serverAuth` and `clientAuth` extended key
usages, so any mesh member can act as TLS server and/or client. SANs always
include `localhost`, `<service>`, `<service>.local` and `127.0.0.1`, plus the
per-service replica hostnames (see `extra_sans_for()` in
`scripts/certs/issue-certs.sh`).

## Issuing certificates

Requires `openssl` (Git Bash / WSL on Windows, any Linux/macOS otherwise).

```bash
# 1. Create the internal CA (once per environment)
scripts/certs/generate-ca.sh

# 2. Issue all service certificates (365-day validity)
scripts/certs/issue-certs.sh

# Or only specific services:
scripts/certs/issue-certs.sh redis vault
```

Immediately back up `internal-ca/ca-key.pem` into Vault and delete the local
copy if the environment does not need to re-issue certificates:

```bash
vault kv put secret/orya/mtls-ca ca_key=@infra/ha/certs/internal-ca/ca-key.pem
```

Losing the CA key means minting a new CA and re-issuing **every** service
certificate.

## Bootstrapping the HA stack

`docker-compose.ha.yml` contains a one-shot `certs-init` service that copies
the CA + service certs into per-service named volumes with the ownership each
image expects (private keys are `0600`). Every TLS-enabled service waits for
`certs-init` to finish.

```bash
docker compose -f infra/docker-compose.ha.yml up -d
```

## Rotation (every 90 days)

```bash
scripts/rotate-certs.sh           # rotate + rolling reload + handshake checks
scripts/rotate-certs.sh --dry-run # print planned actions only
```

The script re-issues service certificates (the CA is untouched), refreshes the
cert volumes, hot-reloads what can be reloaded (nginx, ClickHouse, Redis) and
rolling-restarts the rest (Vault, Postgres, pgBouncer, OTel Collector), then
validates the mTLS handshake of every public listener.

## SDK configuration

Point the agent SDK at the internal CA and its client certificate:

```bash
AGENT_OBS_TLS_CA=/path/to/ca.pem
AGENT_OBS_TLS_CERT=/path/to/sdk.pem
AGENT_OBS_TLS_KEY=/path/to/sdk-key.pem   # optional if the PEM bundles the key
```

`LangfuseExporter` reads these in `from_env()` and passes them to httpx as
`verify=<CA context>` + `cert=(cert, key)`. TLS verification is always on —
there is no `verify=False` path.

## Manual handshake verification

```bash
# OK: with client certificate
openssl s_client -connect localhost:3000 -CAfile infra/ha/certs/internal-ca/ca.pem \
  -cert infra/ha/certs/sdk/sdk.pem -key infra/ha/certs/sdk/sdk-key.pem

# Must be rejected: without client certificate
openssl s_client -connect localhost:3000 -CAfile infra/ha/certs/internal-ca/ca.pem
```

## Known limitations (accepted for PP02)

- **Langfuse upstream hop**: the Langfuse Node app cannot terminate mTLS, so
  client→nginx-Langfuse is mTLS while nginx→langfuse-N stays plain HTTP inside
  the compose network. Langfuse→Postgres *is* mTLS (Prisma `sslcert`/`sslkey`
  params in `DATABASE_URL`).
- **Redis / ClickHouse plaintext ports**: port 6379 (Redis) and 8123
  (ClickHouse HTTP) stay available for in-cluster application clients that
  cannot present client certificates. All infrastructure clients use the mTLS
  endpoints (Redis 6380, ClickHouse 8443/9440).
- **MinIO** is not part of the certificate list in the PP02 ticket and remains
  plaintext.
- **Vault SDK client**: `agent_obs.guardrail.vault_client` accepts
  `verify_tls` but not yet a client certificate; connecting it to the mTLS
  Vault requires a follow-up SDK change.
- pgBouncer private key ownership may need adjustment
  (`chown <pgbouncer-user>`) depending on the image user — verify with
  `docker compose -f infra/docker-compose.ha.yml logs pgbouncer` after boot.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `certs-init` fails with "CA not found" | Certs were never issued | Run `generate-ca.sh` + `issue-certs.sh` |
| Postgres refuses to start: "private key file permissions too open" | Cert volume chown failed | Re-run `docker compose run --rm certs-init` |
| `SSL certificate verify failed` from SDK | Wrong/expired CA | Point `AGENT_OBS_TLS_CA` at `internal-ca/ca.pem` |
| Vault sealed after restart | `vault-unseal` transit unreachable over https | Check `vault-unseal` logs; certs must be present in `certs_vault_unseal` |
| Langfuse cannot connect to Postgres | Missing `sslcert` in `DATABASE_URL` | Use the compose default URL with `sslmode=verify-full&sslcert=...&sslkey=...` |
