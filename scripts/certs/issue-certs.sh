#!/usr/bin/env bash
# Issue service certificates for the internal mTLS mesh (PP02 / T3.0.2).
#
# Usage:
#   scripts/certs/issue-certs.sh              # issue certs for all services
#   scripts/certs/issue-certs.sh redis vault  # issue only for named services
#
# Prerequisite: scripts/certs/generate-ca.sh has been run (internal CA exists).
#
# Env:
#   CERT_DAYS  validity of service certs in days (default 365; rotate every 90)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
CERTS_DIR="${REPO_ROOT}/infra/ha/certs"
CA_DIR="${CERTS_DIR}/internal-ca"
CERT_DAYS="${CERT_DAYS:-365}"

if [[ ! -f "${CA_DIR}/ca.pem" || ! -f "${CA_DIR}/ca-key.pem" ]]; then
    echo "ERROR: internal CA not found in ${CA_DIR}. Run scripts/certs/generate-ca.sh first." >&2
    exit 1
fi

# All mesh members. Every cert carries both serverAuth and clientAuth EKUs so
# any service can act as TLS server and/or client (mTLS mesh).
DEFAULT_SERVICES=(
    langfuse
    clickhouse
    postgres
    repmgr
    vault
    vault-unseal
    redis
    proxy
    otel-collector
    nginx-langfuse
    nginx-otel
    pgbouncer
    sdk
)

# Extra SAN entries per service (comma separated, appended to the defaults
# localhost / <service> / <service>.local / 127.0.0.1).
extra_sans_for() {
    case "$1" in
        clickhouse)
            echo "DNS:clickhouse1,DNS:clickhouse2,DNS:clickhouse3,DNS:clickhouse4" ;;
        otel-collector)
            echo "DNS:otel-collector-1,DNS:otel-collector-2,DNS:otel-collector-3" ;;
        postgres)
            echo "DNS:pg-primary,DNS:pg-replica-1,DNS:pg-replica-2" ;;
        vault)
            echo "DNS:vault-1,DNS:vault-2" ;;
        redis)
            echo "DNS:redis-master,DNS:redis-replica-1,DNS:redis-replica-2" ;;
        langfuse)
            echo "DNS:langfuse-1,DNS:langfuse-2,DNS:langfuse-3" ;;
        *)
            echo "" ;;
    esac
}

issue_service() {
    local svc="$1"
    local dir="${CERTS_DIR}/${svc}"
    local cnf="${dir}/openssl.cnf"
    local san="DNS:localhost,DNS:${svc},DNS:${svc}.local,IP:127.0.0.1"
    local extra
    extra="$(extra_sans_for "${svc}")"
    if [[ -n "${extra}" ]]; then
        san="${san},${extra}"
    fi

    mkdir -p "${dir}"
    cat > "${cnf}" <<EOF
[req]
distinguished_name = dn
req_extensions = ext
prompt = no
[dn]
C = US
O = OryaObservability
OU = internal-mTLS
CN = ${svc}
[ext]
basicConstraints = critical, CA:FALSE
keyUsage = critical, digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth, clientAuth
subjectKeyIdentifier = hash
subjectAltName = ${san}
# Same as [ext] plus the AKI that only makes sense when signing with -CA
[extsign]
basicConstraints = critical, CA:FALSE
keyUsage = critical, digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth, clientAuth
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid:issuer
subjectAltName = ${san}
EOF

    openssl genrsa -out "${dir}/${svc}-key.pem" 2048
    openssl req -new -key "${dir}/${svc}-key.pem" -config "${cnf}" \
        -out "${dir}/${svc}.csr"
    openssl x509 -req -in "${dir}/${svc}.csr" \
        -CA "${CA_DIR}/ca.pem" -CAkey "${CA_DIR}/ca-key.pem" -CAcreateserial \
        -days "${CERT_DAYS}" -sha256 \
        -extfile "${cnf}" -extensions extsign \
        -out "${dir}/${svc}.pem"
    rm -f "${dir}/${svc}.csr"
    chmod 600 "${dir}/${svc}-key.pem"
    chmod 644 "${dir}/${svc}.pem"
    echo "issued: ${svc} (${dir})"
}

if [[ $# -gt 0 ]]; then
    SERVICES=("$@")
else
    SERVICES=("${DEFAULT_SERVICES[@]}")
fi

for svc in "${SERVICES[@]}"; do
    issue_service "${svc}"
done

echo "Done. ${#SERVICES[@]} service certificate(s) issued under ${CERTS_DIR}."
