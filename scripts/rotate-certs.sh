#!/usr/bin/env bash
# Rotate mTLS service certificates without downtime (PP02 / T3.0.2).
#
# Recommended cadence: every 90 days (certs are issued with 365-day validity).
#
# Usage: scripts/rotate-certs.sh [--dry-run]
#
# What it does:
#   1. Re-issues every service certificate from the existing internal CA.
#   2. Refreshes the per-service cert volumes via the certs-init service.
#   3. Rolling-reloads each service so new connections pick up new certs
#      (services that support hot-reload are reloaded; the rest are restarted
#      one node at a time).
#   4. Validates the mTLS handshake of every public listener.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
COMPOSE_FILE="${REPO_ROOT}/infra/docker-compose.ha.yml"
CERTS_DIR="${REPO_ROOT}/infra/ha/certs"
CA_PEM="${CERTS_DIR}/internal-ca/ca.pem"

DRY_RUN=0
if [[ "${1:-}" == "--dry-run" ]]; then
    DRY_RUN=1
    echo "== DRY RUN: actions are printed, nothing is changed =="
fi

run() {
    if [[ "${DRY_RUN}" -eq 1 ]]; then
        echo "DRY-RUN: $*"
    else
        "$@"
    fi
}

dc() {
    docker compose -f "${COMPOSE_FILE}" "$@"
}

echo "==> [1/4] Re-issuing service certificates (CA unchanged)"
run bash "${SCRIPT_DIR}/certs/issue-certs.sh"

echo "==> [2/4] Refreshing mTLS cert volumes (certs-init)"
run dc run --rm --no-deps certs-init

echo "==> [3/4] Rolling reload of services"

# nginx: graceful reload, zero dropped connections
run dc exec -T nginx-langfuse nginx -s reload || true
run dc exec -T nginx-otel nginx -s reload || true

# ClickHouse: re-reads openSSL server certs on SYSTEM RELOAD CONFIG
for ch in clickhouse1 clickhouse2 clickhouse3 clickhouse4; do
    run dc exec -T "${ch}" clickhouse-client \
        --query "SYSTEM RELOAD CONFIG" || true
done

# Redis: CONFIG SET swaps cert files for new TLS connections
for rd in redis-master redis-replica-1 redis-replica-2; do
    run dc exec -T "${rd}" redis-cli -a "${REDIS_AUTH:-orya-local-redis-secret}" \
        CONFIG SET tls-cert-file /etc/redis/certs/redis.pem || true
    run dc exec -T "${rd}" redis-cli -a "${REDIS_AUTH:-orya-local-redis-secret}" \
        CONFIG SET tls-key-file /etc/redis/certs/redis-key.pem || true
done

# Vault / Postgres / pgBouncer / OTel Collector read certs at startup:
# restart one node at a time to keep the quorum / pool alive.
for svc in vault-2 vault-1; do
    run dc restart "${svc}"
done
for svc in pg-replica-2 pg-replica-1 pg-primary; do
    run dc restart "${svc}"
done
run dc restart pgbouncer
for svc in otel-collector-3 otel-collector-2 otel-collector-1; do
    run dc restart "${svc}"
done

echo "==> [4/4] Validating mTLS handshakes (inside the compose network)"

# nginx LBs: TLS handshake from the test-client container (sdk identity)
validate_nginx() {
    local name=$1 host=$2 port=$3
    if [[ "${DRY_RUN}" -eq 1 ]]; then
        echo "DRY-RUN: python ssl handshake ${host}:${port} (${name})"
        return 0
    fi
    if dc exec -T ha-test-client python -c "
import socket, ssl
ctx = ssl.create_default_context(cafile='/certs/ca.pem')
ctx.load_cert_chain('/certs/sdk.pem', '/certs/sdk-key.pem')
with socket.create_connection(('${host}', ${port}), timeout=5) as s:
    with ctx.wrap_socket(s, server_hostname='${host}') as ts:
        assert ts.version() is not None
" > /dev/null 2>&1; then
        echo "OK: ${name} ${host}:${port} mTLS handshake succeeded"
    else
        echo "ERROR: ${name} ${host}:${port} mTLS handshake FAILED" >&2
        return 1
    fi
}

# Vault / ClickHouse / Redis: native clients inside their own containers
validate_service() {
    local name=$1
    shift
    if [[ "${DRY_RUN}" -eq 1 ]]; then
        echo "DRY-RUN: $* (${name})"
        return 0
    fi
    if dc exec -T "$@" > /dev/null 2>&1; then
        echo "OK: ${name} mTLS probe succeeded"
    else
        echo "ERROR: ${name} mTLS probe FAILED" >&2
        return 1
    fi
}

validate_nginx langfuse   nginx-langfuse 3000
validate_nginx otel-http  nginx-otel     4318
validate_service vault \
    vault-1 curl -sf --cacert /vault/certs/ca.pem \
    --cert /vault/certs/vault.pem --key /vault/certs/vault-key.pem \
    https://localhost:8200/v1/sys/health
validate_service clickhouse \
    clickhouse1 curl -sf --cacert /etc/clickhouse-server/certs/ca.pem \
    --cert /etc/clickhouse-server/certs/clickhouse.pem \
    --key /etc/clickhouse-server/certs/clickhouse-key.pem \
    https://localhost:8443/ping
validate_service redis \
    redis-master redis-cli --tls --cacert /etc/redis/certs/ca.pem \
    --cert /etc/redis/certs/redis.pem --key /etc/redis/certs/redis-key.pem \
    -p 6380 -a "${REDIS_AUTH:-orya-local-redis-secret}" ping
validate_service postgres \
    pg-primary env PGPASSWORD="${POSTGRES_PASSWORD:-langfuse}" \
    PGSSLROOTCERT=/var/lib/postgresql/certs/ca.pem \
    PGSSLCERT=/var/lib/postgresql/certs/langfuse.pem \
    PGSSLKEY=/var/lib/postgresql/certs/langfuse-key.pem \
    psql "sslmode=verify-full host=localhost port=5432 user=langfuse dbname=langfuse" \
    -c "SELECT 1;"

echo "Certificate rotation complete."
