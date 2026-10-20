#!/bin/bash
# Health check script for Redis HA cluster
# This script checks the health of Redis master, replicas, and sentinels

set -e

REDIS_MASTER="redis-master"
REDIS_REPLICAS=("redis-replica1" "redis-replica2")
REDIS_SENTINELS=("redis-sentinel1" "redis-sentinel2" "redis-sentinel3")
REDIS_PORT=6379
SENTINEL_PORT=26379
REDIS_AUTH="${REDIS_AUTH:-orya-local-redis-secret}"

# Function to check Redis connectivity
check_redis() {
    local host=$1
    local port=$2
    
    if redis-cli -h "$host" -p "$port" -a "$REDIS_AUTH" ping > /dev/null 2>&1; then
        echo "OK: Redis ${host}:${port} is accessible"
        return 0
    else
        echo "ERROR: Redis ${host}:${port} is not accessible"
        return 1
    fi
}

# Function to check Redis master role
check_redis_master() {
    local host=$1
    local port=$2
    
    local role=$(redis-cli -h "$host" -p "$port" -a "$REDIS_AUTH" info replication | grep role | cut -d: -f2 | xargs)
    if [ "$role" = "master" ]; then
        echo "OK: Redis ${host}:${port} is master"
        return 0
    else
        echo "ERROR: Redis ${host}:${port} is not master (role: $role)"
        return 1
    fi
}

# Function to check Redis replica role
check_redis_replica() {
    local host=$1
    local port=$2
    
    local role=$(redis-cli -h "$host" -p "$port" -a "$REDIS_AUTH" info replication | grep role | cut -d: -f2 | xargs)
    if [ "$role" = "slave" ]; then
        # Check replication lag
        local lag=$(redis-cli -h "$host" -p "$port" -a "$REDIS_AUTH" info replication | grep master_link_last_io_seconds_ago | cut -d: -f2 | xargs)
        if [ "$lag" -lt 30 ]; then
            echo "OK: Redis ${host}:${port} is replica with lag ${lag}s"
            return 0
        else
            echo "ERROR: Redis ${host}:${port} is replica with high lag ${lag}s"
            return 1
        fi
    else
        echo "ERROR: Redis ${host}:${port} is not replica (role: $role)"
        return 1
    fi
}

# Function to check Redis Sentinel
check_redis_sentinel() {
    local host=$1
    local port=$2
    
    if redis-cli -h "$host" -p "$port" -a "$REDIS_AUTH" ping > /dev/null 2>&1; then
        # Check if sentinel is monitoring the master
        local master=$(redis-cli -h "$host" -p "$port" -a "$REDIS_AUTH" sentinel get-master-addr-by-name mymaster | head -1 | xargs)
        if [ -n "$master" ]; then
            echo "OK: Redis Sentinel ${host}:${port} is monitoring master $master"
            return 0
        else
            echo "ERROR: Redis Sentinel ${host}:${port} is not monitoring master"
            return 1
        fi
    else
        echo "ERROR: Redis Sentinel ${host}:${port} is not accessible"
        return 1
    fi
}

# Function to check Redis Sentinel quorum
check_sentinel_quorum() {
    local quorum_count=0
    
    for sentinel in "${REDIS_SENTINELS[@]}"; do
        if redis-cli -h "$sentinel" -p "$SENTINEL_PORT" -a "$REDIS_AUTH" ping > /dev/null 2>&1; then
            local master=$(redis-cli -h "$sentinel" -p "$SENTINEL_PORT" -a "$REDIS_AUTH" sentinel get-master-addr-by-name mymaster | head -1 | xargs)
            if [ -n "$master" ]; then
                quorum_count=$((quorum_count + 1))
            fi
        fi
    done
    
    if [ "$quorum_count" -ge 2 ]; then
        echo "OK: Redis Sentinel quorum achieved (${quorum_count}/3)"
        return 0
    else
        echo "ERROR: Redis Sentinel quorum not achieved (${quorum_count}/3)"
        return 1
    fi
}

# Main health check
echo "Starting Redis HA health check..."

# Check Redis master
master_ok=true
if ! check_redis "$REDIS_MASTER" "$REDIS_PORT"; then
    master_ok=false
fi

if ! check_redis_master "$REDIS_MASTER" "$REDIS_PORT"; then
    master_ok=false
fi

# Check Redis replicas
all_replica_ok=true
for replica in "${REDIS_REPLICAS[@]}"; do
    if ! check_redis "$replica" "$REDIS_PORT"; then
        all_replica_ok=false
    fi
    
    if ! check_redis_replica "$replica" "$REDIS_PORT"; then
        all_replica_ok=false
    fi
done

# Check Redis Sentinels
all_sentinel_ok=true
for sentinel in "${REDIS_SENTINELS[@]}"; do
    if ! check_redis_sentinel "$sentinel" "$SENTINEL_PORT"; then
        all_sentinel_ok=false
    fi
done

# Check Sentinel quorum
quorum_ok=true
if ! check_sentinel_quorum; then
    quorum_ok=false
fi

# Summary
echo "Health check summary:"
echo "- Redis master: $([ "$master_ok" = true ] && echo "OK" || echo "FAILED")"
echo "- Redis replicas: $([ "$all_replica_ok" = true ] && echo "OK" || echo "FAILED")"
echo "- Redis Sentinels: $([ "$all_sentinel_ok" = true ] && echo "OK" || echo "FAILED")"
echo "- Sentinel quorum: $([ "$quorum_ok" = true ] && echo "OK" || echo "FAILED")"

# Exit with appropriate code
if [ "$master_ok" = true ] && [ "$all_replica_ok" = true ] && [ "$all_sentinel_ok" = true ] && [ "$quorum_ok" = true ]; then
    echo "All Redis health checks passed!"
    exit 0
else
    echo "Some Redis health checks failed!"
    exit 1
fi