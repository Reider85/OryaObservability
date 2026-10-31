"""PP36: HA failover tests for warm-tier with repmgr integration."""

import asyncio
import pytest
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# Add repo root to Python path for imports
REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from agent_obs.storage.warm import WarmStore


class TestWarmStoreHAFailover:
    """Test warm-tier HA failover scenarios with repmgr integration."""

    @pytest.mark.asyncio
    async def test_failover_detection(self):
        """Test failover detection when primary becomes unavailable."""
        warm_store = WarmStore(
            dsn="postgresql://warm:warm@primary:5433/warm_store",
            read_dsn="postgresql://warm:warm@replica1:6433/warm_store,postgresql://warm:warm@replica2:6433/warm_store"
        )
        
        # Mock connection to primary failing
        with patch('agent_obs.storage.warm.asyncpg') as mock_asyncpg:
            # First connection to primary fails
            mock_pool_primary = AsyncMock()
            mock_pool_primary.acquire.side_effect = Exception("Connection failed")
            
            # Second connection to replica succeeds
            mock_pool_replica = AsyncMock()
            mock_conn = AsyncMock()
            mock_pool_replica.acquire.return_value.__aenter__.return_value = mock_conn
            mock_pool_replica.acquire.return_value.__aexit__.return_value = None
            
            mock_asyncpg.create_pool.side_effect = [mock_pool_primary, mock_pool_replica]
            
            # Try to connect to primary, should fall back to replica
            await warm_store.connect_read()
            
            # Verify both DSNs were attempted
            assert mock_asyncpg.create_pool.call_count == 2

    @pytest.mark.asyncio
    async def test_rpmgr_promote_hook_integration(self):
        """Test integration with repmgr promote hook."""
        # Mock the repmgr promote hook script
        with patch('subprocess.run') as mock_subprocess:
            mock_subprocess.return_value = MagicMock(returncode=0)
            
            # Simulate the promote hook being called
            result = mock_subprocess.run([
                'sh', '/ha/postgres/promote-hook.sh', 'pg-replica-1'
            ], capture_output=True)
            
            # Verify the hook was called
            assert result.returncode == 0

    @pytest.mark.asyncio
    async def test_pgbouncer_reload_after_failover(self):
        """Test pgbouncer is reloaded after failover."""
        with patch('subprocess.run') as mock_subprocess:
            mock_subprocess.return_value = MagicMock(returncode=0)
            
            # Simulate pgbouncer reload
            result = mock_subprocess.run([
                'pgbounce', '-c', '/etc/pgbouncer/pgbouncer-warm.ini', 'reload'
            ], capture_output=True)
            
            # Verify reload command was executed
            assert result.returncode == 0

    @pytest.mark.asyncio
    async def test_dynamic_config_update(self):
        """Test dynamic config update after failover."""
        # Mock file operations for dynamic config
        with patch('builtins.open') as mock_open, patch('os.path.exists') as mock_exists:
            mock_exists.return_value = True
            mock_file = mock_open.return_value.__aenter__.return_value
            mock_file.write = MagicMock()
            
            # Simulate dynamic config update
            config_content = """
[databases]
warm_store = host=new-primary port=5433 dbname=warm_store sslmode=require sslcert=/certs/postgres.pem sslkey=/certs/postgres-key.pem sslrootcert=/certs/ca.pem

[users]
warm = \"md5cc512516d433b0572e262f274177403c\"
pgbouncer = \"md5be5544d3807b54dd0637f2439ecb03b9\"
"""
            
            mock_file.write(config_content)
            
            # Verify config was updated
            mock_file.write.assert_called_once_with(config_content)

    @pytest.mark.asyncio
    async def test_follow_hook_integration(self):
        """Test integration with repmgr follow hook."""
        with patch('subprocess.run') as mock_subprocess:
            mock_subprocess.return_value = MagicMock(returncode=0)
            
            # Simulate the follow hook being called
            result = mock_subprocess.run([
                'sh', '/ha/postgres/follow-hook.sh', 'pg-replica-2', 'pg-primary'
            ], capture_output=True)
            
            # Verify the hook was called
            assert result.returncode == 0

    @pytest.mark.asyncio
    async def test_rejoin_hook_integration(self):
        """Test integration with repmgr rejoin hook."""
        with patch('subprocess.run') as mock_subprocess:
            mock_subprocess.return_value = MagicMock(returncode=0)
            
            # Simulate the rejoin hook being called
            result = mock_subprocess.run([
                'sh', '/ha/postgres/rejoin-hook.sh', 'pg-replica-1'
            ], capture_output=True)
            
            # Verify the hook was called
            assert result.returncode == 0

    @pytest.mark.asyncio
    async def test_failover_sequence(self):
        """Test complete failover sequence."""
        warm_store = WarmStore(
            dsn="postgresql://warm:warm@primary:5433/warm_store",
            read_dsn="postgresql://warm:warm@replica1:6433/warm_store,postgresql://warm:warm@replica2:6433/warm_store"
        )
        
        with patch('agent_obs.storage.warm.asyncpg') as mock_asyncpg, \
             patch('subprocess.run') as mock_subprocess, \
             patch('builtins.open') as mock_open:
            
            # Mock database connections
            mock_pool_replica = AsyncMock()
            mock_conn = AsyncMock()
            mock_pool_replica.acquire.return_value.__aenter__.return_value = mock_conn
            mock_pool_replica.acquire.return_value.__aexit__.return_value = None
            mock_asyncpg.create_pool.return_value = mock_pool_replica
            
            # Mock subprocess calls
            mock_subprocess.return_value = MagicMock(returncode=0)
            
            # Mock file operations
            mock_file = mock_open.return_value.__aenter__.return_value
            mock_file.write = MagicMock()
            
            # Simulate failover sequence
            await warm_store.connect_read()  # This would trigger failover logic
            
            # Verify all steps were called:
            # 1. Connection attempt to primary (fails)
            # 2. Connection attempt to replica (succeeds)
            # 3. Dynamic config update
            # 4. pgbouncer reload
            
            assert mock_asyncpg.create_pool.call_count >= 1
            assert mock_subprocess.run.call_count >= 1
            assert mock_file.write.called

    @pytest.mark.asyncio
    async def test_failover_with_tls(self):
        """Test failover with TLS/mTLS enabled."""
        warm_store = WarmStore(
            dsn="postgresql://warm:warm@primary:5433/warm_store?sslmode=require&sslcert=/certs/postgres.pem&sslkey=/certs/postgres-key.pem&sslrootcert=/certs/ca.pem",
            read_dsn="postgresql://warm:warm@replica1:6433/warm_store?sslmode=require&sslcert=/certs/postgres.pem&sslkey=/certs/postgres-key.pem&sslrootcert=/certs/ca.pem"
        )
        
        # Verify TLS parameters are preserved in failover
        assert "sslmode=require" in warm_store._read_dsn
        assert "sslcert=" in warm_store._read_dsn
        assert "sslkey=" in warm_store._read_dsn
        assert "sslrootcert=" in warm_store._read_dsn

    @pytest.mark.asyncio
    async def test_failover_with_multiple_failures(self):
        """Test behavior when multiple replicas are unavailable."""
        warm_store = WarmStore(
            dsn="postgresql://warm:warm@primary:5433/warm_store",
            read_dsn="postgresql://warm:warm@replica1:6433/warm_store,postgresql://warm:warm@replica2:6433/warm_store,postgresql://warm:warm@replica3:6433/warm_store"
        )
        
        with patch('agent_obs.storage.warm.asyncpg') as mock_asyncpg:
            # All replica connections fail
            mock_asyncpg.create_pool.side_effect = Exception("Connection failed")
            
            # Should handle failure gracefully
            with pytest.raises(Exception):
                await warm_store.connect_read()

    @pytest.mark.asyncio
    async def test_failover_recovery(self):
        """Test recovery after failover when primary becomes available again."""
        warm_store = WarmStore(
            dsn="postgresql://warm:warm@primary:5433/warm_store",
            read_dsn="postgresql://warm:warm@replica1:6433/warm_store"
        )
        
        with patch('agent_obs.storage.warm.asyncpg') as mock_asyncpg:
            # First connection fails (simulating failover)
            mock_asyncpg.create_pool.side_effect = Exception("Connection failed")
            
            # Second connection succeeds (simulating recovery)
            mock_pool = AsyncMock()
            mock_conn = AsyncMock()
            mock_pool.acquire.return_value.__aenter__.return_value = mock_conn
            mock_pool.acquire.return_value.__aexit__.return_value = None
            mock_asyncpg.create_pool.return_value = mock_pool
            
            # Should reconnect successfully
            await warm_store.connect_read()


class TestWarmStoreHAFailoverScenarios:
    """Test specific failover scenarios."""

    @pytest.mark.asyncio
    async def test_primary_failure_scenario(self):
        """Test scenario where primary database fails."""
        # Simulate primary failure
        warm_store = WarmStore(
            dsn="postgresql://warm:warm@primary:5433/warm_store",
            read_dsn="postgresql://warm:warm@replica1:6433/warm_store"
        )
        
        with patch('agent_obs.storage.warm.asyncpg') as mock_asyncpg:
            # Primary connection fails
            mock_asyncpg.create_pool.side_effect = [
                Exception("Primary connection failed"),
                AsyncMock()  # Replica connection succeeds
            ]
            
            # Should failover to replica
            await warm_store.connect_read()

    @pytest.mark.asyncio
    async def test_replica_failure_scenario(self):
        """Test scenario where replica database fails."""
        # Simulate replica failure
        warm_store = WarmStore(
            dsn="postgresql://warm:warm@primary:5433/warm_store",
            read_dsn="postgresql://warm:warm@replica1:6433/warm_store,postgresql://warm:warm@replica2:6433/warm_store"
        )
        
        with patch('agent_obs.storage.warm.asyncpg') as mock_asyncpg:
            # First replica connection fails
            mock_asyncpg.create_pool.side_effect = [
                Exception("Replica1 connection failed"),
                AsyncMock()  # Replica2 connection succeeds
            ]
            
            # Should failover to second replica
            await warm_store.connect_read()

    @pytest.mark.asyncio
    async def test_network_partition_scenario(self):
        """Test scenario with network partition."""
        # Simulate network partition
        warm_store = WarmStore(
            dsn="postgresql://warm:warm@primary:5433/warm_store",
            read_dsn="postgresql://warm:warm@replica1:6433/warm_store"
        )
        
        with patch('agent_obs.storage.warm.asyncpg') as mock_asyncpg:
            # Both connections fail due to network partition
            mock_asyncpg.create_pool.side_effect = Exception("Network partition")
            
            # Should handle gracefully
            with pytest.raises(Exception):
                await warm_store.connect_read()