#!/bin/bash

# PP56: Build OPA bundle for observability policies
# This script creates an atomic bundle from the Rego policies in ./policies
# and places it in the ./bundle directory for the bundle server to serve.

set -e

BUNDLE_NAME="observability"
BUNDLE_DIR="./bundle"
POLICIES_DIR="./policies"
BUNDLE_FILE="${BUNDLE_DIR}/${BUNDLE_NAME}.tar.gz"

# Clean previous bundle
rm -rf "${BUNDLE_DIR}"
mkdir -p "${BUNDLE_DIR}"

# Create bundle manifest
cat > "${BUNDLE_DIR}/manifest.json" << EOF
{
  "revision": "$(date -u +"%Y%m%dT%H%M%SZ")",
  "description": "OryaObservability security policies",
  "name": "${BUNDLE_NAME}"
}
EOF

# Create bundle with policies
cd "${POLICIES_DIR}"
opa build -t "${BUNDLE_DIR}" -o "${BUNDLE_FILE}" .

# Verify bundle
opa bundle verify "${BUNDLE_FILE}"

echo "Bundle created successfully: ${BUNDLE_FILE}"
echo "Bundle revision: $(cat "${BUNDLE_DIR}/manifest.json" | grep -o '"revision": "[^"]*"' | cut -d'"' -f4)"