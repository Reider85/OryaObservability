import pytest
import subprocess
import time
import json
import requests
from typing import Dict, List, Optional
import docker
from docker.models.containers import Container


class TestHAConfig:
    """Test suite for HA infrastructure configuration validation"""
    
    @pytest.fixture(scope="class")
    def docker_client(self):
        """Docker client fixture"""
        return docker.from_env()
    
    @pytest.fixture(scope="class")
    def compose_services(self):
        """Get list of services from docker-compose.ha.yml"""
        try:
            result = subprocess.run(
                ["docker", "compose", "-f", "infra/docker-compose.ha.yml", "config", "--services"],
                capture_output=True,
                text=True,
                check=True
            )
            return result.stdout.strip().split('\n')
        except subprocess.CalledProcessError as e:
            pytest.fail(f"Failed to get services from docker-compose.ha.yml: {e}")
    
    def test_compose_file_syntax(self, compose_services):
        """Test that docker-compose.ha.yml has valid syntax and expected services"""
        # Check that compose file is valid
        try:
            subprocess.run(
                ["docker", "compose", "-f", "infra/docker-compose.ha.yml", "config"],
                capture_output=True,
                text=True,
                check=True
            )
        except subprocess.CalledProcessError as e:
            pytest.fail(f"docker-compose.ha.yml has invalid syntax: {e}")
        
        # Check that expected services are present
        expected_services = [
            "clickhouse1", "clickhouse2", "clickhouse3", "clickhouse4",
            "clickhouse-keeper1", "clickhouse-keeper2", "clickhouse-keeper3",
            "pg-primary", "pg-standby1", "pg-standby2",
            "pgbouncer",
            "redis-master", "redis-replica1", "redis-replica2",
            "redis-sentinel1", "redis-sentinel2", "redis-sentinel3",
            "vault-1", "vault-2",
            "minio1", "minio2", "minio3", "minio4",
            "otel-collector-1", "otel-collector-2", "otel-collector-3",
            "langfuse-1", "langfuse-2", "langfuse-3",
            "nginx-langfuse", "nginx-otel"
        ]
        
        for service in expected_services:
            assert service in compose_services, f"Service {service} not found in docker-compose.ha.yml"
    
    def test_clickhouse_ha_configuration(self):
        """Test ClickHouse HA configuration"""
        # Test cluster configuration
        try:
            result = subprocess.run([
                "docker", "compose", "-f", "infra/docker-compose.ha.yml", "exec", "clickhouse1",
                "clickhouse", "--version"
            ], capture_output=True, text=True, check=True)
            assert "ClickHouse" in result.stdout
        except subprocess.CalledProcessError:
            pytest.fail("ClickHouse version check failed")
        
        # Test cluster connectivity
        try:
            result = subprocess.run([
                "docker", "compose", "-f", "infra/docker-compose.ha.yml", "exec", "clickhouse1",
                "clickhouse-client", "--query", "SELECT cluster FROM system.clusters LIMIT 1"
            ], capture_output=True, text=True, check=True)
            assert "observability_cluster" in result.stdout
        except subprocess.CalledProcessError:
            pytest.fail("ClickHouse cluster connectivity test failed")
    
    def test_postgres_ha_configuration(self):
        """Test PostgreSQL HA configuration"""
        # Test repmgr configuration
        try:
            result = subprocess.run([
                "docker", "compose", "-f", "infra/docker-compose.ha.yml", "exec", "pg-primary",
                "repmgr", "--version"
            ], capture_output=True, text=True, check=True)
            assert "repmgr" in result.stdout
        except subprocess.CalledProcessError:
            pytest.fail("repmgr version check failed")
        
        # Test replication status
        try:
            result = subprocess.run([
                "docker", "compose", "-f", "infra/docker-compose.ha.yml", "exec", "pg-primary",
                "psql", "-U", "repmgr", "-d", "repmgr", "-c", "SELECT node_id, type, state FROM repmgr.nodes;"
            ], capture_output=True, text=True, check=True)
            assert "primary" in result.stdout
        except subprocess.CalledProcessError:
            pytest.fail("PostgreSQL replication status check failed")
        
        # Test pgBouncer configuration
        try:
            result = subprocess.run([
                "docker", "compose", "-f", "infra/docker-compose.ha.yml", "exec", "pgbouncer",
                "pgbouncer", "--version"
            ], capture_output=True, text=True, check=True)
            assert "pgbouncer" in result.stdout
        except subprocess.CalledProcessError:
            pytest.fail("pgBouncer version check failed")
    
    def test_redis_ha_configuration(self):
        """Test Redis HA configuration"""
        # Test Redis master
        try:
            result = subprocess.run([
                "docker", "compose", "-f", "infra/docker-compose.ha.yml", "exec", "redis-master",
                "redis-cli", "ping"
            ], capture_output=True, text=True, check=True)
            assert "PONG" in result.stdout
        except subprocess.CalledProcessError:
            pytest.fail("Redis master ping check failed")
        
        # Test Redis replica
        try:
            result = subprocess.run([
                "docker", "compose", "-f", "infra/docker-compose.ha.yml", "exec", "redis-replica1",
                "redis-cli", "ping"
            ], capture_output=True, text=True, check=True)
            assert "PONG" in result.stdout
        except subprocess.CalledProcessError:
            pytest.fail("Redis replica ping check failed")
        
        # Test Redis Sentinel
        try:
            result = subprocess.run([
                "docker", "compose", "-f", "infra/docker-compose.ha.yml", "exec", "redis-sentinel1",
                "redis-cli", "-p", "26379", "ping"
            ], capture_output=True, text=True, check=True)
            assert "PONG" in result.stdout
        except subprocess.CalledProcessError:
            pytest.fail("Redis Sentinel ping check failed")
    
    def test_vault_ha_configuration(self):
        """Test Vault HA configuration"""
        # Test Vault unseal status
        try:
            result = subprocess.run([
                "docker", "compose", "-f", "infra/docker-compose.ha.yml", "exec", "vault-1",
                "vault", "status", "-format=json"
            ], capture_output=True, text=True, check=True)
            status = json.loads(result.stdout)
            assert status["sealed"] is False
        except subprocess.CalledProcessError:
            pytest.fail("Vault status check failed")
        
        # Test Vault cluster status
        try:
            result = subprocess.run([
                "docker", "compose", "-f", "infra/docker-compose.ha.yml", "exec", "vault-1",
                "vault", "operator", "raft", "list-peers", "-format=json"
            ], capture_output=True, text=True, check=True)
            peers = json.loads(result.stdout)
            assert len(peers) >= 1
        except subprocess.CalledProcessError:
            pytest.fail("Vault cluster status check failed")
    
    def test_minio_ha_configuration(self):
        """Test MinIO distributed configuration"""
        # Test MinIO cluster status
        try:
            result = subprocess.run([
                "docker", "compose", "-f", "infra/docker-compose.ha.yml", "exec", "minio1",
                "mc", "admin", "info", "--no-color", "--json", "http://minio1:9000"
            ], capture_output=True, text=True, check=True)
            status = json.loads(result.stdout)
            assert status["status"] == "online"
        except subprocess.CalledProcessError:
            pytest.fail("MinIO cluster status check failed")
        
        # Test MinIO drive status
        try:
            result = subprocess.run([
                "docker", "compose", "-f", "infra/docker-compose.ha.yml", "exec", "minio1",
                "mc", "admin", "info", "--no-color", "--json", "http://minio1:9000"
            ], capture_output=True, text=True, check=True)
            info = json.loads(result.stdout)
            servers = info["servers"]
            assert len(servers) == 4
        except subprocess.CalledProcessError:
            pytest.fail("MinIO drive status check failed")
    
    def test_otel_ha_configuration(self):
        """Test OTel Collector HA configuration"""
        # Test OTel Collector connectivity
        try:
            result = subprocess.run([
                "docker", "compose", "-f", "infra/docker-compose.ha.yml", "exec", "otel-collector-1",
                "otelcol", "--version"
            ], capture_output=True, text=True, check=True)
            assert "otelcol" in result.stdout
        except subprocess.CalledProcessError:
            pytest.fail("OTel Collector version check failed")
        
        # Test OTel Collector metrics
        try:
            result = subprocess.run([
                "docker", "compose", "-f", "infra/docker-compose.ha.yml", "exec", "otel-collector-1",
                "curl", "-f", "http://localhost:8888/metrics"
            ], capture_output=True, text=True, check=True)
            assert "otelcol" in result.stdout
        except subprocess.CalledProcessError:
            pytest.fail("OTel Collector metrics check failed")
    
    def test_langfuse_ha_configuration(self):
        """Test Langfuse HA configuration"""
        # Test Langfuse connectivity
        try:
            result = subprocess.run([
                "docker", "compose", "-f", "infra/docker-compose.ha.yml", "exec", "langfuse-1",
                "npm", "--version"
            ], capture_output=True, text=True, check=True)
            assert "npm" in result.stdout
        except subprocess.CalledProcessError:
            pytest.fail("Langfuse npm version check failed")
        
        # Test Langfuse health
        try:
            result = subprocess.run([
                "docker", "compose", "-f", "infra/docker-compose.ha.yml", "exec", "langfuse-1",
                "curl", "-f", "http://localhost:3000/health"
            ], capture_output=True, text=True, check=True)
            assert "200" in result.stdout
        except subprocess.CalledProcessError:
            pytest.fail("Langfuse health check failed")
    
    def test_nginx_ha_configuration(self):
        """Test nginx load balancer configuration"""
        # Test nginx for Langfuse
        try:
            result = subprocess.run([
                "docker", "compose", "-f", "infra/docker-compose.ha.yml", "exec", "nginx-langfuse",
                "nginx", "-t"
            ], capture_output=True, text=True, check=True)
            assert "syntax is ok" in result.stdout
        except subprocess.CalledProcessError:
            pytest.fail("nginx Langfuse configuration test failed")
        
        # Test nginx for OTel
        try:
            result = subprocess.run([
                "docker", "compose", "-f", "infra/docker-compose.ha.yml", "exec", "nginx-otel",
                "nginx", "-t"
            ], capture_output=True, text=True, check=True)
            assert "syntax is ok" in result.stdout
        except subprocess.CalledProcessError:
            pytest.fail("nginx OTel configuration test failed")
    
    def test_health_check_scripts(self):
        """Test that all health check scripts exist and are executable"""
        health_scripts = [
            "infra/ha/healthchecks/check-clickhouse.sh",
            "infra/ha/healthchecks/check-postgres.sh",
            "infra/ha/healthchecks/check-redis.sh",
            "infra/ha/healthchecks/check-vault.sh",
            "infra/ha/healthchecks/check-otel.sh",
            "infra/ha/healthchecks/check-minio.sh",
            "infra/ha/healthchecks/check-all.sh"
        ]
        
        for script in health_scripts:
            assert os.path.exists(script), f"Health check script {script} does not exist"
            assert os.access(script, os.X_OK), f"Health check script {script} is not executable"
    
    def test_chaos_test_script(self):
        """Test that chaos test script exists and is executable"""
        chaos_script = "scripts/ha-chaos-test.sh"
        assert os.path.exists(chaos_script), f"Chaos test script {chaos_script} does not exist"
        assert os.access(chaos_script, os.X_OK), f"Chaos test script {chaos_script} is not executable"
    
    def test_environment_variables(self):
        """Test that environment variables are properly configured"""
        # Check that .env.ha exists
        assert os.path.exists("infra/.env.ha"), "Environment file infra/.env.ha does not exist"
        
        # Check that required environment variables are set
        required_vars = [
            "CLICKHOUSE_REPLICAS",
            "CLICKHOUSE_SHARDS",
            "POSTGRES_PRIMARY",
            "POSTGRES_STANDBYS",
            "REDIS_MASTER",
            "REDIS_REPLICAS",
            "VAULT_NODES",
            "MINIO_NODES",
            "OTEL_NODES",
            "LANGFUZE_NODES"
        ]
        
        with open("infra/.env.ha", "r") as f:
            env_content = f.read()
        
        for var in required_vars:
            assert var in env_content, f"Required environment variable {var} not found in .env.ha"
    
    def test_data_persistence(self):
        """Test that data persistence is properly configured"""
        # Check that volume declarations exist in docker-compose.ha.yml
        with open("infra/docker-compose.ha.yml", "r") as f:
            compose_content = f.read()
        
        expected_volumes = [
            "clickhouse_data",
            "postgres_data",
            "redis_data",
            "vault_data",
            "minio_data",
            "otel_data",
            "langfuse_data"
        ]
        
        for volume in expected_volumes:
            assert volume in compose_content, f"Volume {volume} not found in docker-compose.ha.yml"
    
    def test_network_configuration(self):
        """Test that network configuration is properly set up"""
        # Check that network declarations exist in docker-compose.ha.yml
        with open("infra/docker-compose.ha.yml", "r") as f:
            compose_content = f.read()
        
        assert "observability-ha" in compose_content, "Network observability-ha not found"
        assert "observability-ha-external" in compose_content, "Network observability-ha-external not found"
    
    def test_security_configuration(self):
        """Test that security configuration is properly set up"""
        # Check that security configurations exist
        with open("infra/docker-compose.ha.yml", "r") as f:
            compose_content = f.read()
        
        # Check for health checks
        assert "healthcheck" in compose_content, "Health checks not configured"
        
        # Check for resource limits
        assert "deploy.resources" in compose_content, "Resource limits not configured"
        
        # Check for restart policies
        assert "restart" in compose_content, "Restart policies not configured"


class TestHAFailover:
    """Test suite for HA failover scenarios"""
    
    @pytest.fixture(scope="class")
    def setup_cluster(self):
        """Set up the HA cluster for testing"""
        # Start the cluster
        subprocess.run(["docker", "compose", "-f", "infra/docker-compose.ha.yml", "up", "-d"], check=True)
        
        # Wait for cluster to be ready
        time.sleep(60)
        
        yield
        
        # Clean up
        subprocess.run(["docker", "compose", "-f", "infra/docker-compose.ha.yml", "down"], check=True)
    
    def test_clickhouse_failover(self, setup_cluster):
        """Test ClickHouse failover scenario"""
        # Get initial cluster status
        initial_status = subprocess.run([
            "docker", "compose", "-f", "infra/docker-compose.ha.yml", "exec", "clickhouse1",
            "clickhouse-client", "--query", "SELECT cluster, shard_num, replica_num, host_name FROM system.clusters WHERE cluster = 'observability_cluster';"
        ], capture_output=True, text=True, check=True)
        
        # Stop a replica
        subprocess.run([
            "docker", "compose", "-f", "infra/docker-compose.ha.yml", "stop", "clickhouse2"
        ], check=True)
        
        # Wait for failover
        time.sleep(30)
        
        # Check cluster status after failover
        final_status = subprocess.run([
            "docker", "compose", "-f", "infra/docker-compose.ha.yml", "exec", "clickhouse1",
            "clickhouse-client", "--query", "SELECT cluster, shard_num, replica_num, host_name FROM system.clusters WHERE cluster = 'observability_cluster';"
        ], capture_output=True, text=True, check=True)
        
        # Verify that cluster is still functional
        assert "observability_cluster" in final_status.stdout
    
    def test_postgres_failover(self, setup_cluster):
        """Test PostgreSQL failover scenario"""
        # Get initial replication status
        initial_status = subprocess.run([
            "docker", "compose", "-f", "infra/docker-compose.ha.yml", "exec", "pg-primary",
            "psql", "-U", "repmgr", "-d", "repmgr", "-c", "SELECT node_id, type, state FROM repmgr.nodes;"
        ], capture_output=True, text=True, check=True)
        
        # Stop a standby
        subprocess.run([
            "docker", "compose", "-f", "infra/docker-compose.ha.yml", "stop", "pg-standby1"
        ], check=True)
        
        # Wait for failover
        time.sleep(30)
        
        # Check replication status after failover
        final_status = subprocess.run([
            "docker", "compose", "-f", "infra/docker-compose.ha.yml", "exec", "pg-primary",
            "psql", "-U", "repmgr", "-d", "repmgr", "-c", "SELECT node_id, type, state FROM repmgr.nodes;"
        ], capture_output=True, text=True, check=True)
        
        # Verify that cluster is still functional
        assert "primary" in final_status.stdout
    
    def test_redis_failover(self, setup_cluster):
        """Test Redis failover scenario"""
        # Get initial master status
        initial_status = subprocess.run([
            "docker", "compose", "-f", "infra/docker-compose.ha.yml", "exec", "redis-master",
            "redis-cli", "info", "replication"
        ], capture_output=True, text=True, check=True)
        
        # Stop the master
        subprocess.run([
            "docker", "compose", "-f", "infra/docker-compose.ha.yml", "stop", "redis-master"
        ], check=True)
        
        # Wait for failover
        time.sleep(30)
        
        # Check new master status
        new_master = subprocess.run([
            "docker", "compose", "-f", "infra/docker-compose.ha.yml", "exec", "redis-replica1",
            "redis-cli", "info", "replication"
        ], capture_output=True, text=True, check=True)
        
        # Verify that failover occurred
        assert "role:master" in new_master.stdout
    
    def test_vault_failover(self, setup_cluster):
        """Test Vault failover scenario"""
        # Get initial cluster status
        initial_status = subprocess.run([
            "docker", "compose", "-f", "infra/docker-compose.ha.yml", "exec", "vault-1",
            "vault", "operator", "raft", "list-peers", "-format=json"
        ], capture_output=True, text=True, check=True)
        
        # Stop a node
        subprocess.run([
            "docker", "compose", "-f", "infra/docker-compose.ha.yml", "stop", "vault-1"
        ], check=True)
        
        # Wait for failover
        time.sleep(30)
        
        # Check cluster status after failover
        final_status = subprocess.run([
            "docker", "compose", "-f", "infra/docker-compose.ha.yml", "exec", "vault-2",
            "vault", "operator", "raft", "list-peers", "-format=json"
        ], capture_output=True, text=True, check=True)
        
        # Verify that cluster is still functional
        peers = json.loads(final_status.stdout)
        assert len(peers) >= 1


if __name__ == "__main__":
    pytest.main([__file__])