#!/bin/sh

# MinIO initialization script for tiered storage
# Creates the cold-traces and audit-events buckets and applies lifecycle rules.
#
# Runs as the one-shot `minio-init` compose service (minio/mc image), NOT as a
# /docker-entrypoint-initdb.d hook: the MinIO server image never executes that
# path, so a script mounted there silently does nothing.
#
# Credentials and endpoint come from the environment (compose passes
# MINIO_ROOT_USER / MINIO_ROOT_PASSWORD).

set -e

MINIO_ENDPOINT="${MINIO_ENDPOINT:-http://minio:9000}"
MINIO_USER="${MINIO_ROOT_USER:-minio}"
MINIO_PASSWORD="${MINIO_ROOT_PASSWORD:-miniosecret}"

# Wait for MinIO to be ready
echo "Waiting for MinIO to be ready at ${MINIO_ENDPOINT}..."
until mc alias set local "${MINIO_ENDPOINT}" "${MINIO_USER}" "${MINIO_PASSWORD}" >/dev/null 2>&1; do
    echo "MinIO is unavailable - sleeping"
    sleep 2
done

echo "MinIO is ready"

# Create the langfuse bucket (already exists in original setup, but ensure it's there)
echo "Ensuring langfuse bucket exists..."
mc mb -p local/langfuse || true

# Create cold-traces bucket for cold-tier storage
echo "Creating cold-traces bucket..."
mc mb -p local/cold-traces || true

# Create audit-events bucket for audit events
echo "Creating audit-events bucket..."
mc mb -p local/audit-events || true

# Apply lifecycle rules to cold-traces bucket
# Flags: --expire-days is the only one needed. `mc ilm add` has neither
# --expiry nor --status, and it aborts on an unknown flag, which would leave
# the bucket with no lifecycle at all. Rules are enabled by default.
echo "Applying lifecycle rules to cold-traces bucket..."
mc ilm add local/cold-traces --expire-days "365"

# Apply lifecycle rules to audit-events bucket
# 365 days, NOT 90: §3.4 ARCHITECT.md requires audit retention of at least one
# year, and PC21 archives audit events into this bucket.
echo "Applying lifecycle rules to audit-events bucket..."
mc ilm add local/audit-events --expire-days "365"

# Verify buckets were created
echo "Verifying buckets..."
mc ls local/

# Show lifecycle rules
echo "Showing lifecycle rules..."
mc ilm ls local/cold-traces
mc ilm ls local/audit-events

echo "MinIO tiered storage initialization completed successfully"