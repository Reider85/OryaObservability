import pytest
import subprocess
import time
import json
import os
import tempfile
import shutil
from typing import Dict, List, Optional
import docker
from docker.models.containers import Container


class TestChaosScenarios:
    """Test suite for chaos scenarios in HA infrastructure"""
    
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
    
    def test_chaos_script_syntax(self):
        """Test that chaos test script has valid syntax"""
        # Test script syntax
        try:
            subprocess.run(["bash", "scripts/ha-chaos-test.sh", "help"], check=True)
        except subprocess.CalledProcessError:
            pytest.fail("Chaos test script has syntax errors")
    
    def test_chaos_script_comprehensive(self):
        """Test comprehensive chaos scenario"""
        # Run comprehensive chaos test with short duration
        try:
            result = subprocess.run([
                "bash", "scripts/ha-chaos-test.sh", "comprehensive"
            ], timeout=300, capture_output=True, text=True)
            
            # Check that script completed successfully
            assert result.returncode == 0, f"Chaos test failed: {result.stderr}"
            
            # Check that expected scenarios were tested
            assert "clickhouse-failure" in result.stdout
            assert "postgres-failure" in result.stdout
            assert "redis-failure" in result.stdout
            assert "vault-failure" in result.stdout
            assert "minio-failure" in result.stdout
            assert "otel-failure" in result.stdout
            assert "langfuse-failure" in result.stdout
            assert "network-latency" in result.stdout
            
        except subprocess.TimeoutExpired:
            pytest.fail("Chaos test timed out")
        except subprocess.CalledProcessError as e:
            pytest.fail(f"Chaos test failed: {e}")
    
    def test_chaos_script_random(self):
        """Test random chaos scenario"""
        # Run random chaos test with short duration
        try:
            result = subprocess.run([
                "bash", "scripts/ha-chaos-test.sh", "random"
            ], timeout=180, capture_output=True, text=True)
            
            # Check that script completed successfully
            assert result.returncode == 0, f"Chaos test failed: {result.stderr}"
            
            # Check that scenarios were tested
            assert "Chaos scenario" in result.stdout
            assert "completed" in result.stdout
            
        except subprocess.TimeoutExpired:
            pytest.fail("Chaos test timed out")
        except subprocess.CalledProcessError as e:
            pytest.fail(f"Chaos test failed: {e}")
    
    def test_chaos_script_specific(self):
        """Test specific chaos scenarios"""
        scenarios = [
            "clickhouse-failure",
            "postgres-failure",
            "redis-failure",
            "vault-failure",
            "minio-failure",
            "otel-failure",
            "langfuse-failure",
            "network-latency"
        ]
        
        for scenario in scenarios:
            try:
                result = subprocess.run([
                    "bash", "scripts/ha-chaos-test.sh", "specific", scenario, "10"
                ], timeout=60, capture_output=True, text=True)
                
                # Check that script completed successfully
                assert result.returncode == 0, f"Chaos test for {scenario} failed: {result.stderr}"
                
                # Check that scenario was tested
                assert f"Chaos scenario {scenario} completed" in result.stdout
                
            except subprocess.TimeoutExpired:
                pytest.fail(f"Chaos test for {scenario} timed out")
            except subprocess.CalledProcessError as e:
                pytest.fail(f"Chaos test for {scenario} failed: {e}")
    
    def test_chaos_script_with_options(self):
        """Test chaos script with various options"""
        try:
            # Test with duration option
            result = subprocess.run([
                "bash", "scripts/ha-chaos-test.sh", "specific", "clickhouse-failure", "30",
                "--duration", "30"
            ], timeout=60, capture_output=True, text=True)
            
            assert result.returncode == 0, f"Chaos test with duration option failed: {result.stderr}"
            
            # Test with network latency option
            result = subprocess.run([
                "bash", "scripts/ha-chaos-test.sh", "specific", "network-latency", "30",
                "--network-latency", "2000"
            ], timeout=60, capture_output=True, text=True)
            
            assert result.returncode == 0, f"Chaos test with network latency option failed: {result.stderr}"
            
            # Test with CPU stress option
            result = subprocess.run([
                "bash", "scripts/ha-chaos-test.sh", "specific", "cpu-stress", "30",
                "--cpu-stress"
            ], timeout=60, capture_output=True, text=True)
            
            assert result.returncode == 0, f"Chaos test with CPU stress option failed: {result.stderr}"
            
            # Test with memory stress option
            result = subprocess.run([
                "bash", "scripts/ha-chaos-test.sh", "specific", "memory-stress", "30",
                "--memory-stress"
            ], timeout=60, capture_output=True, text=True)
            
            assert result.returncode == 0, f"Chaos test with memory stress option failed: {result.stderr}"
            
        except subprocess.TimeoutExpired:
            pytest.fail("Chaos test with options timed out")
        except subprocess.CalledProcessError as e:
            pytest.fail(f"Chaos test with options failed: {e}")
    
    def test_chaos_script_help(self):
        """Test chaos script help functionality"""
        try:
            result = subprocess.run([
                "bash", "scripts/ha-chaos-test.sh", "help"
            ], capture_output=True, text=True)
            
            assert result.returncode == 0, f"Chaos test help failed: {result.stderr}"
            
            # Check that help contains expected information
            assert "Chaos Test Script for HA Infrastructure" in result.stdout
            assert "Commands:" in result.stdout
            assert "Scenarios:" in result.stdout
            assert "Options:" in result.stdout
            assert "Examples:" in result.stdout
            
        except subprocess.CalledProcessError as e:
            pytest.fail(f"Chaos test help failed: {e}")
    
    def test_chaos_script_error_handling(self):
        """Test chaos script error handling"""
        # Test unknown command
        result = subprocess.run([
            "bash", "scripts/ha-chaos-test.sh", "unknown"
        ], capture_output=True, text=True)
        
        assert result.returncode != 0, "Chaos test should fail for unknown command"
        assert "Unknown command" in result.stderr
        
        # Test unknown scenario
        result = subprocess.run([
            "bash", "scripts/ha-chaos-test.sh", "specific", "unknown-scenario", "10"
        ], capture_output=True, text=True)
        
        assert result.returncode != 0, "Chaos test should fail for unknown scenario"
        assert "Unknown chaos scenario" in result.stderr
        
        # Test missing scenario
        result = subprocess.run([
            "bash", "scripts/ha-chaos-test.sh", "specific"
        ], capture_output=True, text=True)
        
        assert result.returncode != 0, "Chaos test should fail for missing scenario"
        assert "No scenario specified" in result.stderr


class TestChaosIntegration:
    """Integration tests for chaos scenarios"""
    
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
    
    def test_simultaneous_failures(self, setup_cluster):
        """Test handling of simultaneous service failures"""
        # Stop multiple services simultaneously
        subprocess.run([
            "docker", "compose", "-f", "infra/docker-compose.ha.yml", "stop", "clickhouse2", "pg-standby1", "redis-replica1"
        ], check=True)
        
        # Wait for failover
        time.sleep(60)
        
        # Check that cluster is still functional
        try:
            # Check ClickHouse
            result = subprocess.run([
                "docker", "compose", "-f", "infra/docker-compose.ha.yml", "exec", "clickhouse1",
                "clickhouse-client", "--query", "SELECT 1"
            ], capture_output=True, text=True, check=True)
            
            # Check PostgreSQL
            result = subprocess.run([
                "docker", "compose", "-f", "infra/docker-compose.ha.yml", "exec", "pg-primary",
                "psql", "-U", "langfuse", "-d", "langfuse", "-c", "SELECT 1"
            ], capture_output=True, text=True, check=True)
            
            # Check Redis
            result = subprocess.run([
                "docker", "compose", "-f", "infra/docker-compose.ha.yml", "exec", "redis-master",
                "redis-cli", "ping"
            ], capture_output=True, text=True, check=True)
            
            assert "PONG" in result.stdout
            
        except subprocess.CalledProcessError as e:
            pytest.fail(f"Cluster not functional after simultaneous failures: {e}")
        
        # Restart stopped services
        subprocess.run([
            "docker", "compose", "-f", "infra/docker-compose.ha.yml", "start", "clickhouse2", "pg-standby1", "redis-replica1"
        ], check=True)
        
        # Wait for recovery
        time.sleep(60)
    
    def test_network_partition(self, setup_cluster):
        """Test handling of network partition"""
        # Create network partition between clickhouse1 and other nodes
        subprocess.run([
            "docker", "compose", "-f", "infra/docker-compose.ha.yml", "exec", "clickhouse1",
            "iptables", "-A", "FORWARD", "-j", "DROP", "-s", "172.20.0.0/16", "-d", "172.20.0.0/16"
        ], check=True)
        
        # Wait for partition to take effect
        time.sleep(30)
        
        # Check that services are still running locally
        try:
            result = subprocess.run([
                "docker", "compose", "-f", "infra/docker-compose.ha.yml", "exec", "clickhouse1",
                "clickhouse-client", "--query", "SELECT 1"
            ], capture_output=True, text=True, check=True)
            
        except subprocess.CalledProcessError as e:
            pytest.fail(f"ClickHouse not functional after network partition: {e}")
        
        # Remove network partition
        subprocess.run([
            "docker", "compose", "-f", "infra/docker-compose.ha.yml", "exec", "clickhouse1",
            "iptables", "-D", "FORWARD", "-j", "DROP", "-s", "172.20.0.0/16", "-d", "172.20.0.0/16"
        ], check=True)
        
        # Wait for recovery
        time.sleep(30)
    
    def test_resource_stress(self, setup_cluster):
        """Test handling of resource stress"""
        # Simulate high CPU usage
        subprocess.run([
            "docker", "compose", "-f", "infra/docker-compose.ha.yml", "exec", "clickhouse1",
            "stress", "--cpu", "4", "--timeout", "60"
        ], check=True)
        
        # Simulate high memory usage
        subprocess.run([
            "docker", "compose", "-f", "infra/docker-compose.ha.yml", "exec", "pg-primary",
            "stress", "--vm", "2", "--vm-bytes", "1G", "--timeout", "60"
        ], check=True)
        
        # Wait for stress test to complete
        time.sleep(120)
        
        # Check that services are still functional
        try:
            result = subprocess.run([
                "docker", "compose", "-f", "infra/docker-compose.ha.yml", "exec", "clickhouse1",
                "clickhouse-client", "--query", "SELECT 1"
            ], capture_output=True, text=True, check=True)
            
            result = subprocess.run([
                "docker", "compose", "-f", "infra/docker-compose.ha.yml", "exec", "pg-primary",
                "psql", "-U", "langfuse", "-d", "langfuse", "-c", "SELECT 1"
            ], capture_output=True, text=True, check=True)
            
        except subprocess.CalledProcessError as e:
            pytest.fail(f"Services not functional after resource stress: {e}")
    
    def test_data_consistency(self, setup_cluster):
        """Test data consistency after failover"""
        # Insert test data into ClickHouse
        subprocess.run([
            "docker", "compose", "-f", "infra/docker-compose.ha.yml", "exec", "clickhouse1",
            "clickhouse-client", "--query", "INSERT INTO observability.spans_hot_local VALUES ('test-trace', 'test-span', '', 'test-agent', 'test-tenant', 'test-span', 'agent.loop', now64(), now64(), 'success', '{}', '[]', 0.0, [], now64());"
        ], check=True)
        
        # Insert test data into PostgreSQL
        subprocess.run([
            "docker", "compose", "-f", "infra/docker-compose.ha.yml", "exec", "pg-primary",
            "psql", "-U", "langfuse", "-d", "langfuse", "-c", "INSERT INTO traces (id, name, start_time, end_time, status) VALUES ('test-trace', 'test-trace', now(), now(), 'success');"
        ], check=True)
        
        # Insert test data into Redis
        subprocess.run([
            "docker", "compose", "-f", "infra/docker-compose.ha.yml", "exec", "redis-master",
            "redis-cli", "SET", "test-key", "test-value"
        ], check=True)
        
        # Stop and restart a service to trigger failover
        subprocess.run([
            "docker", "compose", "-f", "infra/docker-compose.ha.yml", "stop", "clickhouse2"
        ], check=True)
        
        time.sleep(30)
        
        subprocess.run([
            "docker", "compose", "-f", "infra/docker-compose.ha.yml", "start", "clickhouse2"
        ], check=True)
        
        time.sleep(30)
        
        # Verify data consistency
        try:
            # Check ClickHouse data
            result = subprocess.run([
                "docker", "compose", "-f", "infra/docker-compose.ha.yml", "exec", "clickhouse1",
                "clickhouse-client", "--query", "SELECT COUNT(*) FROM observability.spans_hot_local WHERE trace_id = 'test-trace';"
            ], capture_output=True, text=True, check=True)
            
            assert "1" in result.stdout
            
            # Check PostgreSQL data
            result = subprocess.run([
                "docker", "compose", "-f", "infra/docker-compose.ha.yml", "exec", "pg-primary",
                "psql", "-U", "langfuse", "-d", "langfuse", "-c", "SELECT COUNT(*) FROM traces WHERE id = 'test-trace';"
            ], capture_output=True, text=True, check=True)
            
            assert "1" in result.stdout
            
            # Check Redis data
            result = subprocess.run([
                "docker", "compose", "-f", "infra/docker-compose.ha.yml", "exec", "redis-master",
                "redis-cli", "GET", "test-key"
            ], capture_output=True, text=True, check=True)
            
            assert "test-value" in result.stdout
            
        except subprocess.CalledProcessError as e:
            pytest.fail(f"Data consistency check failed: {e}")


if __name__ == "__main__":
    pytest.main([__file__])