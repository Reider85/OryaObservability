#!/usr/bin/env bash
# Generate the internal mTLS certificate authority (PP02 / T3.0.2).
#
# Usage: scripts/certs/generate-ca.sh [output-dir]
#   output-dir defaults to <repo>/infra/ha/certs
#
# Env:
#   CA_DAYS  validity of the CA certificate in days (default 3650)
#   FORCE=1  regenerate even if a CA already exists
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
CERTS_DIR="${1:-${REPO_ROOT}/infra/ha/certs}"
CA_DIR="${CERTS_DIR}/internal-ca"
CA_DAYS="${CA_DAYS:-3650}"

mkdir -p "${CA_DIR}"
cd "${CA_DIR}"

if [[ -f ca.pem && -f ca-key.pem && "${FORCE:-0}" != "1" ]]; then
    echo "CA already exists in ${CA_DIR} (set FORCE=1 to regenerate)"
    exit 0
fi

openssl genrsa -out ca-key.pem 4096
chmod 600 ca-key.pem

openssl req -x509 -new -key ca-key.pem -sha256 -days "${CA_DAYS}" \
    -subj "/C=US/O=OryaObservability/OU=internal-mTLS/CN=orya-observability-internal-ca" \
    -addext "keyUsage=critical,digitalSignature,keyCertSign,cRLSign" \
    -addext "subjectKeyIdentifier=hash" \
    -addext "authorityKeyIdentifier=keyid:issuer" \
    -out ca.pem
chmod 644 ca.pem

echo "Internal CA written to ${CA_DIR} (ca.pem, ca-key.pem)"
echo "IMPORTANT: back up ca-key.pem into Vault and never commit it to git"
echo "           (see infra/ha/certs/README.md)."
