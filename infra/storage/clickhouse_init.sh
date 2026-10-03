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
CLUSTER_CONFIG="${CLUSTER_CONFIG:-/init/cluster.xml}"
CH_HOST="${CLICKHOUSE_HOST:-localhost}"
CH_PORT="${CLICKHOUSE_PORT:-9000}"
CH_HOST2="${CLICKHOUSE_HOST2:-clickhouse2}"
CH_PORT2="${CLICKHOUSE_PORT2:-9000}"
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

client2() {
    clickhouse-client \
        --host "${CH_HOST2}" \
        --port "${CH_PORT2}" \
        --user "${CH_USER}" \
        --password "${CH_PASSWORD}" \
        "$@"
}

echo "Waiting for ClickHouse at ${CH_HOST}:${CH_PORT} as ${CH_USER}..."
until client --query "SELECT 1" >/dev/null 2>&1; do
    echo "ClickHouse shard 1 is unavailable - sleeping"
    sleep 2
done
echo "ClickHouse shard 1 is ready"

echo "Waiting for ClickHouse at ${CH_HOST2}:${CH_PORT2} as ${CH_USER}..."
until client2 --query "SELECT 1" >/dev/null 2>&1; do
    echo "ClickHouse shard 2 is unavailable - sleeping"
    sleep 2
done
echo "ClickHouse shard 2 is ready"

# Pick up observability_user.xml from users.d on a running server.
echo "Reloading ClickHouse config (users.d) on both shards..."
client --query "SYSTEM RELOAD CONFIG" || true
client2 --query "SYSTEM RELOAD CONFIG" || true

echo "Creating observability database on both shards..."
client --query "CREATE DATABASE IF NOT EXISTS observability"
client2 --query "CREATE DATABASE IF NOT EXISTS observability"

echo "Setting up cluster configuration..."
if [ -f "${CLUSTER_CONFIG}" ]; then
    cp "${CLUSTER_CONFIG}" /etc/clickhouse-server/config.d/cluster.xml
    client --query "SYSTEM RELOAD CONFIG"
    client2 --query "SYSTEM RELOAD CONFIG"
    echo "Cluster configuration loaded"
fi

echo "Running DDL from ${DDL_PATH} on both shards..."
client --database=observability --multiquery < "${DDL_PATH}"
client2 --database=observability --multiquery < "${DDL_PATH}"

# PC24: verify embedding storage schema on both shards. Fails the init loudly
# if span_embeddings is missing or spans_hot never got the response_embedding
# healing ALTER (existing volumes do not re-run CREATE TABLE IF NOT EXISTS).
echo "Verifying PC24 embedding schema on shard 1..."
client --database=observability --query "
SELECT
    table,
    name,
    type
FROM system.columns
WHERE database = 'observability'
  AND table IN ('spans_hot', 'span_embeddings')
  AND name IN ('response_embedding', 'embedding', 'agent_id', 'created_at')
ORDER BY table, name
"

echo "Verifying PC24 embedding schema on shard 2..."
client2 --database=observability --query "
SELECT
    table,
    name,
    type
FROM system.columns
WHERE database = 'observability'
  AND table IN ('spans_hot', 'span_embeddings')
  AND name IN ('response_embedding', 'embedding', 'agent_id', 'created_at')
ORDER BY table, name
"

echo "Verifying tables on shard 1..."
client --database=observability --query "
SELECT
    name,
    engine,
    total_rows
FROM system.tables
WHERE database = 'observability'
ORDER BY name
"

echo "Verifying tables on shard 2..."
client2 --database=observability --query "
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

echo "ClickHouse sharded initialization completed successfully"
