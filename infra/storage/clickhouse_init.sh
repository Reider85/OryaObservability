#!/bin/bash
# ClickHouse initialization for the observability database.
#
# Runs in two contexts:
#   1. /docker-entrypoint-initdb.d/init.sh — stock CH entrypoint; only on a
#      FRESH data dir. Existing volumes never re-run it.
#   2. one-shot `clickhouse-init` compose service — always runnable against a
#      live server, including existing volumes:
#        docker compose run --rm clickhouse-init
#      (HA stack: docker compose run --rm clickhouse-init-ha)
#
# Connects with the compose-provisioned admin account (CLICKHOUSE_USER /
# CLICKHOUSE_PASSWORD, default clickhouse/clickhouse). That account can
# CREATE DATABASE/TABLE but has no CREATE USER grant — observability_user is
# therefore defined via users.d XML (observability_user.xml), not SQL.
# This script: wait for nodes → RELOAD CONFIG → CREATE DATABASE → apply DDL →
# verify.
#
# PP35: waits for ALL configured cluster nodes (CLICKHOUSE_HOST..HOST4) before
# applying the ON CLUSTER DDL, so a single issued DDL reaches a full quorum.

set -e

DDL_PATH="${DDL_PATH:-/docker-entrypoint-initdb.d/clickhouse_ddl.sql}"
CLUSTER_CONFIG="${CLUSTER_CONFIG:-/init/cluster.xml}"
CH_USER="${CLICKHOUSE_USER:-clickhouse}"
CH_PASSWORD="${CLICKHOUSE_PASSWORD:-clickhouse}"

# Hosts are CLICKHOUSE_HOST..CLICKHOUSE_HOST4 (hostN/portN); unset hostN
# slots are skipped so 2-shard and single-node stacks keep working.
HOSTS=()
for n in "" 2 3 4; do
    h_var="CLICKHOUSE_HOST${n}"
    p_var="CLICKHOUSE_PORT${n}"
    h_val="${!h_var:-}"
    if [ -n "${h_val}" ]; then
        HOSTS+=("${h_val}:${!p_var:-9000}")
    fi
done

if [ "${#HOSTS[@]}" -eq 0 ]; then
    echo "No CLICKHOUSE_HOST* configured" >&2
    exit 1
fi

client_to() {
    local target="$1"; shift
    local host="${target%%:*}"
    local port="${target##*:}"
    clickhouse-client \
        --host "${host}" \
        --port "${port}" \
        --user "${CH_USER}" \
        --password "${CH_PASSWORD}" \
        "$@"
}

for target in "${HOSTS[@]}"; do
    echo "Waiting for ClickHouse at ${target} as ${CH_USER}..."
    until client_to "${target}" --query "SELECT 1" >/dev/null 2>&1; do
        echo "ClickHouse at ${target} is unavailable - sleeping"
        sleep 2
    done
    echo "ClickHouse at ${target} is ready"
done

PRIMARY="${HOSTS[0]}"
PRIMARY_HOST="${PRIMARY%%:*}"

# Pick up observability_user.xml from users.d on a running server.
echo "Reloading ClickHouse config (users.d) on all nodes..."
for target in "${HOSTS[@]}"; do
    client_to "${target}" --query "SYSTEM RELOAD CONFIG" || true
done

echo "Creating observability database on all nodes..."
for target in "${HOSTS[@]}"; do
    client_to "${target}" --query "CREATE DATABASE IF NOT EXISTS observability"
done

# The cluster config is bind-mounted read-only into every server by
# docker-compose.ha.yml (config.d/cluster.xml), so no copy step is needed for
# the HA stack. For the legacy non-compose path the file is already at
# CLUSTER_CONFIG inside the init image; reload it on the primary and let
# SYSTEM RELOAD CONFIG on each node pick up its own mounted copy.
echo "Cluster configuration expected from mounted config.d/cluster.xml"
client_to "${PRIMARY}" --query "SYSTEM RELOAD CONFIG" || true

echo "Running DDL from ${DDL_PATH} on ${PRIMARY_HOST} (ON CLUSTER propagates)..."
client_to "${PRIMARY}" --database=observability --multiquery < "${DDL_PATH}"

# PC24: verify embedding storage schema. Fails the init loudly if
# span_embeddings is missing or spans_hot never got the response_embedding
# healing ALTER (existing volumes do not re-run CREATE TABLE IF NOT EXISTS).
echo "Verifying PC24 embedding schema on ${PRIMARY_HOST}..."
client_to "${PRIMARY}" --database=observability --query "
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

echo "Verifying tables on ${PRIMARY_HOST}..."
client_to "${PRIMARY}" --database=observability --query "
SELECT
    name,
    engine,
    total_rows
FROM system.tables
WHERE database = 'observability'
ORDER BY name
"

# PP35: verify the replication DDL landed on EVERY node, not just the
# primary — ON CLUSTER can miss a node that was down at DDL time.
echo "Verifying replicated tables on all nodes..."
for target in "${HOSTS[@]}"; do
    host="${target%%:*}"
    echo "--- ${host} ---"
    client_to "${target}" --database=observability --query "
    SELECT
        name,
        engine
    FROM system.tables
    WHERE database = 'observability'
      AND name LIKE '%_local'
    ORDER BY name
    "
done

echo "Verifying users..."
client_to "${PRIMARY}" --query "
SELECT name
FROM system.users
WHERE name IN ('clickhouse', 'observability_user')
ORDER BY name
"

echo "ClickHouse cluster initialization completed successfully"
