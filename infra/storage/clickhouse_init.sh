#!/bin/bash
# ClickHouse initialization for the observability database.
#
# Runs in two contexts:
#   1. /docker-entrypoint-initdb.d/init.sh — stock CH entrypoint; only on a
#      FRESH data dir. Existing volumes never re-run it.
#   2. one-shot `clickhouse-init` compose service — always runnable against a
#      live server, including existing volumes:
#        docker compose run --rm clickhouse-init
#
# Connects with the compose-provisioned admin account (CLICKHOUSE_USER /
# CLICKHOUSE_PASSWORD, default clickhouse/clickhouse). That account can
# CREATE DATABASE/TABLE but has no CREATE USER grant — observability_user is
# therefore defined via users.d XML (observability_user.xml), not SQL.
# This script: RELOAD CONFIG → CREATE DATABASE → apply DDL → verify.

set -e

DDL_PATH="${DDL_PATH:-/docker-entrypoint-initdb.d/clickhouse_ddl.sql}"
CH_HOST="${CLICKHOUSE_HOST:-localhost}"
CH_PORT="${CLICKHOUSE_PORT:-9000}"
CH_USER="${CLICKHOUSE_USER:-clickhouse}"
CH_PASSWORD="${CLICKHOUSE_PASSWORD:-clickhouse}"

client() {
    clickhouse-client \
        --host "${CH_HOST}" \
        --port "${CH_PORT}" \
        --user "${CH_USER}" \
        --password "${CH_PASSWORD}" \
        "$@"
}

echo "Waiting for ClickHouse at ${CH_HOST}:${CH_PORT} as ${CH_USER}..."
until client --query "SELECT 1" >/dev/null 2>&1; do
    echo "ClickHouse is unavailable - sleeping"
    sleep 2
done
echo "ClickHouse is ready"

# Pick up observability_user.xml from users.d on a running server.
echo "Reloading ClickHouse config (users.d)..."
client --query "SYSTEM RELOAD CONFIG" || true

echo "Creating observability database..."
client --query "CREATE DATABASE IF NOT EXISTS observability"

echo "Running DDL from ${DDL_PATH}..."
client --database=observability --multiquery < "${DDL_PATH}"

echo "Verifying tables..."
client --database=observability --query "
SELECT
    name,
    engine,
    total_rows
FROM system.tables
WHERE database = 'observability'
ORDER BY name
"

echo "Verifying users..."
client --query "
SELECT name
FROM system.users
WHERE name IN ('clickhouse', 'observability_user')
ORDER BY name
"

echo "ClickHouse initialization completed successfully"
