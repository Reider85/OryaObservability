#!/bin/bash
# PostgreSQL HA entrypoint with repmgr (proper entrypoint override)
set -euo pipefail

# Wait for PostgreSQL to be ready
until pg_isready -U "${POSTGRES_USER:-langfuse}" -d "${POSTGRES_DB:-langfuse}"; do
    echo "Waiting for PostgreSQL to be ready..."
    sleep 2
done

# Initialize repmgr if this is the primary node
if [ "${REPMGR_PRIMARY}" = "true" ]; then
    echo "Initializing repmgr primary node..."
    
    # Create repmgr user and database (idempotent)
    psql -U "${POSTGRES_USER:-langfuse}" -d "${POSTGRES_DB:-langfuse}" -c "
    DO \$\$
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'repmgr') THEN
            CREATE ROLE repmgr WITH SUPERUSER LOGIN PASSWORD '${REPMGR_PASSWORD:-repmgr}';
        END IF;
    END \$\$;
    "
    
    psql -U "${POSTGRES_USER:-langfuse}" -d "${POSTGRES_DB:-langfuse}" -c "
    DO \$\$
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'repmgr') THEN
            CREATE DATABASE repgr OWNER repmgr;
        END IF;
    END \$\$
    "
    
    # Initialize repmgr
    repmgr -f /etc/repmgr.conf primary register
    
    # Enable streaming replication (already in command-line -c flags, but add to pg_hba)
    echo "host replication repmgr ${REPMGR_PASSWORD:-repmgr} 0.0.0.0/0 md5" >> /var/lib/postgresql/data/pg_hba.conf
    
    echo "Primary node initialized successfully"
else
    # This is a standby node
    echo "Initializing repmgr standby node..."
    
    # Wait for primary to be ready
    until repmgr -f /etc/repmgr.conf primary check; do
        echo "Waiting for primary node to be ready..."
        sleep 5
    done
    
    # Clone from primary if data directory is empty
    if [ ! -s "/var/lib/postgresql/data/PG_VERSION" ]; then
        echo "Cloning primary database..."
        repmgr -f /etc/repmgr.conf standby clone "${POSTGRES_PRIMARY_CONNINFO}" --fast-checkpoint
    else
        # Data directory exists, rejoin if needed
        echo "Rejoining existing standby database..."
        repmgr -f /etc/repmgr.conf standby rejoin "${POSTGRES_PRIMARY_CONNINFO}" --log-level=INFO
    fi
    
    # Enable streaming replication (already in command-line -c flags, but add to pg_hba)
    echo "host replication repmgr ${REPMGR_PASSWORD:-repmgr} 0.0.0.0/0 md5" >> /var/lib/postgresql/data/pg_hba.conf
    
    echo "Standby node initialized successfully"
fi

# Start repmgr daemon for monitoring
repmgrd -f /etc/repmgr.conf --daemonize --pid-file=/var/run/postgresql/repmgrd.pid

# Execute the original entrypoint script
exec /usr/local/bin/docker-entrypoint.sh postgres