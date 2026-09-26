#!/bin/bash

# ClickHouse initialization script for observability database
# This script creates the observability database and runs the DDL
# It waits for ClickHouse to be ready before executing

set -e

# Wait for ClickHouse to be ready
echo "Waiting for ClickHouse to be ready..."
until clickhouse-client --host localhost --port 8123 --query "SELECT 1" >/dev/null 2>&1; do
    echo "ClickHouse is unavailable - sleeping"
    sleep 2
done

echo "ClickHouse is ready"

# Create the observability database
echo "Creating observability database..."
clickhouse-client --host localhost --port 8123 --query "CREATE DATABASE IF NOT EXISTS observability"

# Create observability user with limited privileges
echo "Creating observability user..."
clickhouse-client --host localhost --port 8123 --query "
CREATE USER IF NOT EXISTS observability_user 
IDENTIFIED BY 'observability_password'
"

# Grant privileges to observability_user
echo "Granting privileges to observability_user..."
clickhouse-client --host localhost --port 8123 --query "
GRANT SELECT, INSERT, UPDATE, DELETE ON observability.* TO observability_user
"

# Set default database for observability_user
echo "Setting default database for observability_user..."
clickhouse-client --host localhost --port 8123 --query "
ALTER USER observability_user SET default_database = observability
"

# Run the DDL script
echo "Running DDL script..."
clickhouse-client --host localhost --port 8123 --database=observability --multiquery < /docker-entrypoint-initdb.d/clickhouse_ddl.sql

# Verify tables were created
echo "Verifying tables..."
clickhouse-client --host localhost --port 8123 --database=observability --query "
SELECT 
    name,
    engine,
    total_rows
FROM system.tables 
WHERE database = 'observability'
ORDER BY name
"

echo "ClickHouse initialization completed successfully"