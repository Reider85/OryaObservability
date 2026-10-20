#!/bin/bash
# Health check script for PostgreSQL HA cluster
# This script checks the health of all PostgreSQL nodes and replication status

set -e

PG_PRIMARY="pg-primary"
PG_STANDBYS=("pg-standby1" "pg-standby2")
PG_PORT=5432
PGBOUNCER_PORT=6432
REPMGR_USER="repmgr"
REPMGR_PASSWORD="repmgr"

# Function to check PostgreSQL connectivity
check_postgres() {
    local host=$1
    local port=$2
    local user=${3:-${POSTGRES_USER:-langfuse}}
    local database=${4:-${POSTGRES_DB:-langfuse}}
    
    if PGPASSWORD="${REPMGR_PASSWORD}" psql -h "$host" -p "$port" -U "$user" -d "$database" -c "SELECT 1;" > /dev/null 2>&1; then
        echo "OK: PostgreSQL ${host}:${port} is accessible"
        return 0
    else
        echo "ERROR: PostgreSQL ${host}:${port} is not accessible"
        return 1
    fi
}

# Function to check pgBouncer connectivity
check_pgbouncer() {
    local host=$1
    local port=$2
    
    if PGPASSWORD="${REPMGR_PASSWORD}" psql -h "$host" -p "$port" -U "langfuse" -d "langfuse" -c "SELECT 1;" > /dev/null 2>&1; then
        echo "OK: pgBouncer ${host}:${port} is accessible"
        return 0
    else
        echo "ERROR: pgBouncer ${host}:${port} is not accessible"
        return 1
    fi
}

# Function to check replication status
check_replication() {
    local host=$1
    local port=$2
    
    # Check if this is a primary or standby
    local role=$(PGPASSWORD="${REPMGR_PASSWORD}" psql -h "$host" -p "$port" -U "$REPMGR_USER" -d "repmgr" -t -c "SELECT CASE WHEN pg_is_in_recovery() THEN 'standby' ELSE 'primary' END;" | xargs)
    
    if [ "$role" = "primary" ]; then
        # Check replication lag on primary
        local lag=$(PGPASSWORD="${REPMGR_PASSWORD}" psql -h "$host" -p "$port" -U "$REPMGR_USER" -d "repmgr" -t -c "SELECT EXTRACT(EPOCH FROM (now() - pg_last_xact_replay_timestamp()))::integer AS lag;" | xargs)
        if [ "$lag" -lt 30 ]; then
            echo "OK: Primary ${host}:${port} has replication lag of ${lag}s"
            return 0
        else
            echo "ERROR: Primary ${host}:${port} has high replication lag of ${lag}s"
            return 1
        fi
    else
        # Check if standby is connected to primary
        local upstream=$(PGPASSWORD="${REPMGR_PASSWORD}" psql -h "$host" -p "$port" -U "$REPMGR_USER" -d "repmgr" -t -c "SELECT upstream_node_id FROM repmgr.nodes WHERE node_id = (SELECT node_id FROM repgr.nodes WHERE conninfo ~ '${PG_PRIMARY}');" | xargs)
        if [ -n "$upstream" ]; then
            echo "OK: Standby ${host}:${port} is connected to primary"
            return 0
        else
            echo "ERROR: Standby ${host}:${port} is not connected to primary"
            return 1
        fi
    fi
}

# Function to check repmgr status
check_repmgr() {
    local host=$1
    local port=$2
    
    if PGPASSWORD="${REPMGR_PASSWORD}" repmgr -h "$host" -p "$port" -U "$REPMGR_USER" -d "repmgr" cluster show > /dev/null 2>&1; then
        echo "OK: repmgr cluster on ${host}:${port} is accessible"
        return 0
    else
        echo "ERROR: repmgr cluster on ${host}:${port} is not accessible"
        return 1
    fi
}

# Main health check
echo "Starting PostgreSQL HA health check..."

# Check primary node
primary_ok=true
if ! check_postgres "$PG_PRIMARY" "$PG_PORT"; then
    primary_ok=false
fi

# Check replication status on primary
if ! check_replication "$PG_PRIMARY" "$PG_PORT"; then
    primary_ok=false
fi

# Check repmgr status on primary
if ! check_repmgr "$PG_PRIMARY" "$PG_PORT"; then
    primary_ok=false
fi

# Check standby nodes
all_standby_ok=true
for standby in "${PG_STANDBYS[@]}"; do
    if ! check_postgres "$standby" "$PG_PORT"; then
        all_standby_ok=false
    fi
    
    if ! check_replication "$standby" "$PG_PORT"; then
        all_standby_ok=false
    fi
    
    if ! check_repmgr "$standby" "$PG_PORT"; then
        all_standby_ok=false
    fi
done

# Check pgBouncer
pgbouncer_ok=true
if ! check_pgbouncer "pgbouncer" "$PGBOUNCER_PORT"; then
    pgbouncer_ok=false
fi

# Summary
echo "Health check summary:"
echo "- Primary node: $([ "$primary_ok" = true ] && echo "OK" || echo "FAILED")"
echo "- Standby nodes: $([ "$all_standby_ok" = true ] && echo "OK" || echo "FAILED")"
echo "- pgBouncer: $([ "$pgbouncer_ok" = true ] && echo "OK" || echo "FAILED")"
echo "- repmgr cluster: $([ "$primary_ok" = true ] && echo "OK" || echo "FAILED")"

# Exit with appropriate code
if [ "$primary_ok" = true ] && [ "$all_standby_ok" = true ] && [ "$pgbouncer_ok" = true ]; then
    echo "All PostgreSQL health checks passed!"
    exit 0
else
    echo "Some PostgreSQL health checks failed!"
    exit 1
fi