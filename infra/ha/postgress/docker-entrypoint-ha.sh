#!/bin/bash
# PostgreSQL HA entrypoint script with repmgr
# This script initializes PostgreSQL nodes for streaming replication and repmgr

set -e

# Wait for PostgreSQL to be ready
until pg_isready -U "${POSTGRES_USER:-langfuse}" -d "${POSTGRES_DB:-langfuse}"; do
    echo "Waiting for PostgreSQL to be ready..."
    sleep 2
done

# Initialize repmgr if this is the primary node
if [ "${REPMGR_PRIMARY}" = "true" ]; then
    echo "Initializing repmgr primary node..."
    
    # Create repmgr user and database
    psql -U "${POSTGRES_USER:-langfuse}" -d "${POSTGRES_DB:-langfuse}" -c "CREATE USER repmgr WITH SUPERUSER PASSWORD '${REPMGR_PASSWORD:-repmgr}';"
    psql -U "${POSTGRES_USER:-langfuse}" -d "${POSTGRES_DB:-langfuse}" -c "CREATE DATABASE repmgr;"
    
    # Initialize repmgr
    repmgr -f /etc/repmgr.conf primary register
    
    # Enable streaming replication
    echo "host replication repmgr ${REPMGR_PASSWORD:-repmgr} 0.0.0.0/0 md5" >> /var/lib/postgresql/data/pg_hba.conf
    
    # Configure wal_level for streaming replication
    echo "wal_level = replica" >> /var/lib/postgresql/data/postgresql.conf
    echo "max_wal_senders = 3" >> /var/lib/postgresql/data/postgresql.conf
    echo "max_replication_slots = 3" >> /var/lib/postgresql/data/postgresql.conf
    echo "hot_standby = on" >> /var/lib/postgresql/data/postgresql.conf
    
    # Restart PostgreSQL to apply changes
    pg_ctl restart -D /var/lib/postgresql/data
    
    echo "Primary node initialized successfully"
else
    # This is a standby node
    echo "Initializing repmgr standby node..."
    
    # Wait for primary to be ready
    until repmgr -f /etc/repmgr.conf primary check; do
        echo "Waiting for primary node to be ready..."
        sleep 5
    done
    
    # Clone from primary
    repmgr -f /etc/repmgr.conf standby clone "${POSTGRES_PRIMARY_CONNINFO}" --fast-checkpoint
    
    # Start standby
    pg_ctl start -D /var/lib/postgresql/data
    
    # Register standby
    repmgr -f /etc/repmgr.conf standby register
    
    # Enable streaming replication
    echo "host replication repmgr ${REPMGR_PASSWORD:-repmgr} 0.0.0.0/0 md5" >> /var/lib/postgresql/data/pg_hba.conf
    
    # Configure wal_level for streaming replication
    echo "wal_level = replica" >> /var/lib/postgresql/data/postgresql.conf
    echo "max_wal_senders = 3" >> /var/lib/postgresql/data/postgresql.conf
    echo "max_replication_slots = 3" >> /var/lib/postgresql/data/postgresql.conf
    echo "hot_standby = on" >> /var/lib/postgresql/data/postgresql.conf
    
    # Restart PostgreSQL to apply changes
    pg_ctl restart -D /var/lib/postgresql/data
    
    echo "Standby node initialized successfully"
fi

# Start repmgr daemon for monitoring
repmgrd -f /etc/repmgr.conf --daemonize --pid-file=/var/run/postgresql/repmgrd.pid

# Execute the original entrypoint script
exec /usr/local/bin/docker-entrypoint.sh postgres