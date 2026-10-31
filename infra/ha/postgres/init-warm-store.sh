#!/bin/bash
# PostgreSQL warm-tier initialization script
# Creates warm_store database, role, and applies schema
set -euo pipefail

# Wait for primary to be ready
echo "Waiting for PostgreSQL primary to be ready..."
until pg_isready -U "${POSTGRES_USER:-langfuse}" -d "${POSTGRES_DB:-langfuse}" -h pg-primary; do
    echo "Waiting for PostgreSQL primary..."
    sleep 2
done

# Wait for replicas to be ready (optional, but good for consistency)
echo "Waiting for PostgreSQL replicas to be ready..."
for REPLICA in pg-replica-1 pg-replica-2; do
    until pg_isready -U "${POSTGRES_USER:-langfuse}" -d "${POSTGRES_DB:-langfuse}" -h "${REPLICA}"; do
        echo "Waiting for ${REPLICA}..."
        sleep 2
    done
done

echo "PostgreSQL cluster is ready, initializing warm tier..."

# Create warm role if it doesn't exist
echo "Creating warm role..."
psql -U "${POSTGRES_USER:-langfuse}" -d "${POSTGRES_DB:-langfuse}" -h pg-primary -c "
DO \$\$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'warm') THEN
        CREATE ROLE warm WITH LOGIN PASSWORD 'warm' NOSUPERUSER NOCREATEDB NOCREATEROLE;
    END IF;
END \$\$;
"

# Create warm_store database if it doesn't exist
echo "Creating warm_store database..."
psql -U "${POSTGRES_USER:-langfuse}" -d "${POSTGRES_DB:-langfuse}" -h pg-primary -c "
DO \$\$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'warm_store') THEN
        CREATE DATABASE warm_store OWNER warm;
    END IF;
END \$\$;
"

# Apply warm tier schema
echo "Applying warm tier schema..."
psql -U warm -d warm_store -h pg-primary -f /docker-entrypoint-initdb.d/postgres_warm.sql

echo "Warm tier initialization completed successfully"