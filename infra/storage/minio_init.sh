#!/bin/bash

# MinIO initialization script for tiered storage
# This script creates the cold-traces and audit-events buckets and applies lifecycle rules
# It waits for MinIO to be ready before executing

set -e

# Wait for MinIO to be ready
echo "Waiting for MinIO to be ready..."
until mc alias set local http://localhost:9000 minio miniosecret >/dev/null 2>&1; do
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
echo "Applying lifecycle rules to cold-traces bucket..."
mc ilm add local/cold-traces \
    --expiry "365" \
    --status "enabled"

# Apply lifecycle rules to audit-events bucket
echo "Applying lifecycle rules to audit-events bucket..."
mc ilm add local/audit-events \
    --expiry "90" \
    --status "enabled"

# Verify buckets were created
echo "Verifying buckets..."
mc ls local/

# Show lifecycle rules
echo "Showing lifecycle rules..."
mc ilm ls local/cold-traces
mc ilm ls local/audit-events

echo "MinIO tiered storage initialization completed successfully"