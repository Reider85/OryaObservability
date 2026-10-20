#!/bin/sh
# Populate per-service mTLS certificate volumes with correct ownership (PP02).
#
# Runs once as root inside the `certs-init` service of docker-compose.ha.yml.
# Copies the CA + service certs from the read-only host mount into named
# volumes, setting uid/gid and permissions each consumer expects
# (private keys must be 0600 and owned by the process user).
set -eu

SRC="${CERTS_SRC:-/src/certs}"
DST_ROOT="${CERTS_DST:-/volumes}"
CA="${SRC}/internal-ca/ca.pem"

if [ ! -f "${CA}" ]; then
    echo "ERROR: ${CA} not found. Run scripts/certs/generate-ca.sh + issue-certs.sh first." >&2
    exit 1
fi

install_set() {
    dest="$1"
    owner="$2"
    shift 2
    mkdir -p "${dest}"
    cp "${CA}" "${dest}/ca.pem"
    chmod 644 "${dest}/ca.pem"
    for svc in "$@"; do
        cp "${SRC}/${svc}/${svc}.pem" "${dest}/${svc}.pem"
        cp "${SRC}/${svc}/${svc}-key.pem" "${dest}/${svc}-key.pem"
        chmod 644 "${dest}/${svc}.pem"
        chmod 600 "${dest}/${svc}-key.pem"
    done
    chown -R "${owner}" "${dest}"
}

# uid:gid per consumer image (postgres:999, clickhouse:101, vault:100, redis:999)
install_set "${DST_ROOT}/certs_postgres"     999:999 postgres repmgr langfuse
install_set "${DST_ROOT}/certs_pgbouncer"    0:0     pgbouncer langfuse
install_set "${DST_ROOT}/certs_redis"        999:999 redis
install_set "${DST_ROOT}/certs_vault"        100:100 vault
install_set "${DST_ROOT}/certs_vault_unseal" 100:100 vault-unseal
install_set "${DST_ROOT}/certs_clickhouse"   101:101 clickhouse
install_set "${DST_ROOT}/certs_nginx"        0:0     nginx-langfuse nginx-otel
install_set "${DST_ROOT}/certs_otel"         0:0     otel-collector
install_set "${DST_ROOT}/certs_langfuse"     0:0     langfuse
install_set "${DST_ROOT}/certs_sdk"          0:0     sdk

echo "mTLS cert volumes populated under ${DST_ROOT}"
