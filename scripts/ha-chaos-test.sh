#!/bin/bash
# Chaos test script for HA infrastructure
# This script simulates various failure scenarios to test failover capabilities

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Configuration
CHAOS_DURATION=${CHAOS_DURATION:-300}
CHAOS_FAILURE_RATE=${CHAOS_FAILURE_RATE:-0.1}
CHAOS_NETWORK_LATENCY=${CHAOS_NETWORK_LATENCY:-1000}
CHAOS_CPU_STRESS=${CHAOS_CPU_STRESS:-false}
CHAOS_MEMORY_STRESS=${CHAOS_MEMORY_STRESS:-false}

# Service arrays
CLICKHOUSE_NODES=("clickhouse1" "clickhouse2" "clickhouse3" "clickhouse4")
POSTGRES_NODES=("pg-primary" "pg-standby1" "pg-standby2")
REDIS_NODES=("redis-master" "redis-replica1" "redis-replica2")
VAULT_NODES=("vault-1" "vault-2")
MINIO_NODES=("minio1" "minio2" "minio3" "minio4")
OTEL_NODES=("otel-collector-1" "otel-collector-2" "otel-collector-3")
LANGFUZE_NODES=("langfuse-1" "langfuse-2" "langfuse-3")

# Function to print colored output
print_message() {
    local type=$1
    local message=$2
    
    case $type in
        "INFO")
            echo -e "${BLUE}[INFO]${NC} $message"
            ;;
        "SUCCESS")
            echo -e "${GREEN}[SUCCESS]${NC} $message"
            ;;
        "WARNING")
            echo -e "${YELLOW}[WARNING]${NC} $message"
            ;;
        "ERROR")
            echo -e "${RED}[ERROR]${NC} $message"
            ;;
        *)
            echo -e "[UNKNOWN] $message"
            ;;
    esac
}

# Function to stop a service
stop_service() {
    local service=$1
    local node=$2
    
    print_message "INFO" "Stopping $service on $node..."
    docker compose -f infra/docker-compose.ha.yml stop "$node"
    
    if [ $? -eq 0 ]; then
        print_message "SUCCESS" "$service on $node stopped successfully"
        return 0
    else
        print_message "ERROR" "Failed to stop $service on $node"
        return 1
    fi
}

# Function to start a service
start_service() {
    local service=$1
    local node=$2
    
    print_message "INFO" "Starting $service on $node..."
    docker compose -f infra/docker-compose.ha.yml start "$node"
    
    if [ $? -eq 0 ]; then
        print_message "SUCCESS" "$service on $node started successfully"
        return 0
    else
        print_message "ERROR" "Failed to start $service on $node"
        return 1
    fi
}

# Function to check service health
check_service_health() {
    local service=$1
    local health_script=$2
    
    print_message "INFO" "Checking $service health..."
    
    if bash "$health_script"; then
        print_message "SUCCESS" "$service health check passed"
        return 0
    else
        print_message "ERROR" "$service health check failed"
        return 1
    fi
}

# Function to simulate network latency
simulate_network_latency() {
    local duration=$1
    local target=$2
    
    print_message "INFO" "Simulating network latency to $target for $duration seconds..."
    
    # Use tc to add network latency
    docker exec -it "$target" tc qdisc add root netem delay "${CHAOS_NETWORK_LATENCY}ms"
    
    sleep "$duration"
    
    # Remove network latency
    docker exec -it "$target" tc qdisc del root netem
    
    print_message "SUCCESS" "Network latency simulation completed"
}

# Function to simulate CPU stress
simulate_cpu_stress() {
    local duration=$1
    local target=$2
    
    print_message "INFO" "Simulating CPU stress on $target for $duration seconds..."
    
    # Run CPU stress test
    docker exec -d "$target" stress --cpu 2 --timeout "$duration"
    
    print_message "SUCCESS" "CPU stress simulation started"
}

# Function to simulate memory stress
simulate_memory_stress() {
    local duration=$1
    local target=$2
    
    print_message "INFO" "Simulating memory stress on $target for $duration seconds..."
    
    # Run memory stress test
    docker exec -d "$target" stress --vm 1 --vm-bytes 512M --timeout "$duration"
    
    print_message "SUCCESS" "Memory stress simulation started"
}

# Function to run chaos test scenario
run_chaos_scenario() {
    local scenario=$1
    local duration=$2
    
    print_message "INFO" "Starting chaos scenario: $scenario"
    print_message "INFO" "Duration: $duration seconds"
    
    case $scenario in
        "clickhouse-failure")
            # Stop a random ClickHouse replica
            local node=${CLICKHOUSE_NODES[$RANDOM % ${#CLICKHOUSE_NODES[@]}]}
            stop_service "ClickHouse" "$node"
            sleep "$duration"
            start_service "ClickHouse" "$node"
            ;;
        "postgres-failure")
            # Stop a random PostgreSQL replica
            local node=${POSTGRES_NODES[$RANDOM % ${#POSTGRES_NODES[@]}]}
            stop_service "PostgreSQL" "$node"
            sleep "$duration"
            start_service "PostgreSQL" "$node"
            ;;
        "redis-failure")
            # Stop a random Redis replica
            local node=${REDIS_NODES[$RANDOM % ${#REDIS_NODES[@]}]}
            stop_service "Redis" "$node"
            sleep "$duration"
            start_service "Redis" "$node"
            ;;
        "vault-failure")
            # Stop a random Vault node
            local node=${VAULT_NODES[$RANDOM % ${#VAULT_NODES[@]}]}
            stop_service "Vault" "$node"
            sleep "$duration"
            start_service "Vault" "$node"
            ;;
        "minio-failure")
            # Stop a random MinIO node
            local node=${MINIO_NODES[$RANDOM % ${#MINIO_NODES[@]}]}
            stop_service "MinIO" "$node"
            sleep "$duration"
            start_service "MinIO" "$node"
            ;;
        "otel-failure")
            # Stop a random OTel Collector
            local node=${OTEL_NODES[$RANDOM % ${#OTEL_NODES[@]}]}
            stop_service "OTel Collector" "$node"
            sleep "$duration"
            start_service "OTel Collector" "$node"
            ;;
        "langfuse-failure")
            # Stop a random Langfuse replica
            local node=${LANGFUZE_NODES[$RANDOM % ${#LANGFUZE_NODES[@]}]}
            stop_service "Langfuse" "$node"
            sleep "$duration"
            start_service "Langfuse" "$node"
            ;;
        "network-latency")
            # Simulate network latency to a random node
            local all_nodes=("${CLICKHOUSE_NODES[@]}" "${POSTGRES_NODES[@]}" "${REDIS_NODES[@]}" "${VAULT_NODES[@]}" "${MINIO_NODES[@]}" "${OTEL_NODES[@]}" "${LANGFUZE_NODES[@]}")
            local node=${all_nodes[$RANDOM % ${#all_nodes[@]}]}
            simulate_network_latency "$duration" "$node"
            ;;
        "cpu-stress")
            # Simulate CPU stress on a random node
            local all_nodes=("${CLICKHOUSE_NODES[@]}" "${POSTGRES_NODES[@]}" "${REDIS_NODES[@]}" "${VAULT_NODES[@]}" "${MINIO_NODES[@]}" "${OTEL_NODES[@]}" "${LANGFUZE_NODES[@]}")
            local node=${all_nodes[$RANDOM % ${#all_nodes[@]}]}
            simulate_cpu_stress "$duration" "$node"
            ;;
        "memory-stress")
            # Simulate memory stress on a random node
            local all_nodes=("${CLICKHOUSE_NODES[@]}" "${POSTGRES_NODES[@]}" "${REDIS_NODES[@]}" "${VAULT_NODES[@]}" "${MINIO_NODES[@]}" "${OTEL_NODES[@]}" "${LANGFUZE_NODES[@]}")
            local node=${all_nodes[$RANDOM % ${#all_nodes[@]}]}
            simulate_memory_stress "$duration" "$node"
            ;;
        *)
            print_message "ERROR" "Unknown chaos scenario: $scenario"
            return 1
            ;;
    esac
    
    print_message "SUCCESS" "Chaos scenario $scenario completed"
}

# Function to run comprehensive chaos test
run_comprehensive_chaos_test() {
    print_message "INFO" "Starting comprehensive chaos test..."
    
    # Test individual service failures
    for service in "clickhouse" "postgres" "redis" "vault" "minio" "otel" "langfuse"; do
        run_chaos_scenario "${service}-failure" 30
        check_service_health "ClickHouse" "infra/ha/healthchecks/check-clickhouse.sh"
        check_service_health "PostgreSQL" "infra/ha/healthchecks/check-postgres.sh"
        check_service_health "Redis" "infra/ha/healthchecks/check-redis.sh"
        check_service_health "Vault" "infra/ha/healthchecks/check-vault.sh"
        check_service_health "MinIO" "infra/ha/healthchecks/check-minio.sh"
        check_service_health "OTel Collector" "infra/ha/healthchecks/check-otel.sh"
    done
    
    # Test network issues
    run_chaos_scenario "network-latency" 60
    check_service_health "ClickHouse" "infra/ha/healthchecks/check-clickhouse.sh"
    check_service_health "PostgreSQL" "infra/ha/healthchecks/check-postgres.sh"
    check_service_health "Redis" "infra/ha/healthchecks/check-redis.sh"
    check_service_health "Vault" "infra/ha/healthchecks/check-vault.sh"
    check_service_health "MinIO" "infra/ha/healthchecks/check-minio.sh"
    check_service_health "OTel Collector" "infra/ha/healthchecks/check-otel.sh"
    
    # Test resource stress
    if [ "$CHAOS_CPU_STRESS" = "true" ]; then
        run_chaos_scenario "cpu-stress" 60
    fi
    
    if [ "$CHAOS_MEMORY_STRESS" = "true" ]; then
        run_chaos_scenario "memory-stress" 60
    fi
    
    # Final health check
    check_service_health "All Services" "infra/ha/healthchecks/check-all.sh"
    
    print_message "SUCCESS" "Comprehensive chaos test completed"
}

# Function to run random chaos test
run_random_chaos_test() {
    print_message "INFO" "Starting random chaos test..."
    
    local scenarios=("clickhouse-failure" "postgres-failure" "redis-failure" "vault-failure" "minio-failure" "otel-failure" "langfuse-failure" "network-latency" "cpu-stress" "memory-stress")
    
    for i in {1..10}; do
        local scenario=${scenarios[$RANDOM % ${#scenarios[@]}]}
        local duration=$((RANDOM % 60 + 30))  # Random duration between 30-90 seconds
        
        run_chaos_scenario "$scenario" "$duration"
        
        # Health check after each scenario
        check_service_health "All Services" "infra/ha/healthchecks/check-all.sh"
    done
    
    print_message "SUCCESS" "Random chaos test completed"
}

# Function to run specific chaos test
run_specific_chaos_test() {
    local scenario=$1
    local duration=$2
    
    if [ -z "$scenario" ]; then
        print_message "ERROR" "No scenario specified"
        return 1
    fi
    
    if [ -z "$duration" ]; then
        duration=60
    fi
    
    run_chaos_scenario "$scenario" "$duration"
    check_service_health "All Services" "infra/ha/healthchecks/check-all.sh"
}

# Function to show help
show_help() {
    echo "Chaos Test Script for HA Infrastructure"
    echo "======================================"
    echo ""
    echo "Usage:"
    echo "  $0 [command] [options]"
    echo ""
    echo "Commands:"
    echo "  comprehensive    Run comprehensive chaos test"
    echo "  random          Run random chaos test"
    echo "  specific <scenario> <duration>  Run specific chaos scenario"
    echo "  help            Show this help message"
    echo ""
    echo "Scenarios:"
    echo "  clickhouse-failure   Stop a ClickHouse replica"
    echo "  postgres-failure     Stop a PostgreSQL replica"
    echo "  redis-failure         Stop a Redis replica"
    echo "  vault-failure         Stop a Vault node"
    echo "  minio-failure         Stop a MinIO node"
    echo "  otel-failure          Stop an OTel Collector"
    echo "  langfuse-failure      Stop a Langfuse replica"
    echo "  network-latency       Simulate network latency"
    echo "  cpu-stress            Simulate CPU stress"
    echo "  memory-stress         Simulate memory stress"
    echo ""
    echo "Options:"
    echo "  --duration <seconds>    Chaos duration (default: 300)"
    echo "  --failure-rate <rate>   Failure rate (default: 0.1)"
    echo "  --network-latency <ms>  Network latency in ms (default: 1000)"
    echo "  --cpu-stress            Enable CPU stress"
    echo "  --memory-stress         Enable memory stress"
    echo ""
    echo "Examples:"
    echo "  $0 comprehensive"
    echo "  $0 random"
    echo "  $0 specific clickhouse-failure 60"
    echo "  $0 specific network-latency 120 --network-latency 2000"
    echo ""
}

# Main function
main() {
    case "$1" in
        "comprehensive")
            run_comprehensive_chaos_test
            ;;
        "random")
            run_random_chaos_test
            ;;
        "specific")
            run_specific_chaos_test "$2" "$3"
            ;;
        "help"|"-h"|"--help")
            show_help
            ;;
        *)
            print_message "ERROR" "Unknown command: $1"
            show_help
            exit 1
            ;;
    esac
}

# Parse command line arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --duration)
            CHAOS_DURATION="$2"
            shift 2
            ;;
        --failure-rate)
            CHAOS_FAILURE_RATE="$2"
            shift 2
            ;;
        --network-latency)
            CHAOS_NETWORK_LATENCY="$2"
            shift 2
            ;;
        --cpu-stress)
            CHAOS_CPU_STRESS=true
            shift
            ;;
        --memory-stress)
            CHAOS_MEMORY_STRESS=true
            shift
            ;;
        *)
            break
            ;;
    esac
done

# Run main function
main "$@"