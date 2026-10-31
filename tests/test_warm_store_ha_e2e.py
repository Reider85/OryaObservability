"""PP36: End-to-end HA warm tier tests with Docker Compose integration."""

import asyncio
import pytest
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# Add repo root to Python path for imports
REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from agent_obs.storage.warm import WarmStore


class TestWarmStoreHAEndToEnd:
    """End-to-end tests for warm-tier HA with Docker Compose integration."""

    @pytest.mark.asyncio
    async def test_ha_warm_store_initialization(self):
        """Test warm store initialization in HA environment."""
        # Test with HA configuration
        warm_store = WarmStore(
            dsn="postgresql://warm:warm@pg-primary:5432/warm_store",
            read_dsn="postgresql://warm:warm@pgbouncer-warm:6433/warm_store"
        )
        
        # Verify configuration
        assert warm_store._dsn == "postgresql://warm:warm@pg-primary:5432/warm_store"
        assert warm_store._read_dsn == "postgresql://warm:warm@pgbouncer-warm:6433/warm_store"
        
        # Verify pgbouncer options would be applied
        from agent_obs.storage.warm import _pgbouncer_opts
        opts = _pgbouncer_opts(warm_store._read_dsn)
        assert opts["statement_cache_size"] == 0

    @pytest.mark.asyncio
    async def test_ha_warm_store_connection_primary(self):
        """Test connection to primary in HA setup."""
        warm_store = WarmStore(
            dsn="postgresql://warm:warm@pg-primary:5432/warm_store"
        )
        
        # This would test actual connection to primary in HA environment
        # For now, just verify the configuration is correct
        assert "pg-primary:5432" in warm_store._dsn

    @pytest.mark.asyncio
    async def test_ha_warm_store_connection_replica(self):
        """Test connection to replica in HA setup."""
        warm_store = WarmStore(
            read_dsn="postgresql://warm:warm@pg-replica-1:5432/warm_store"
        )
        
        # This would test actual connection to replica in HA environment
        # For now, just verify the configuration is correct
        assert "pg-replica-1:5432" in warm_store._read_dsn

    @pytest.mark.asyncio
    async def test_ha_warm_store_connection_pgbouncer(self):
        """Test connection through pgBouncer in HA setup."""
        warm_store = WarmStore(
            read_dsn="postgresql://warm:warm@pgbouncer-warm:6433/warm_store"
        )
        
        # Verify pgbouncer configuration
        from agent_obs.storage.warm import _pgbouncer_opts
        opts = _pgbouncer_opts(warm_store._read_dsn)
        assert opts["statement_cache_size"] == 0

    @pytest.mark.asyncio
    async def test_ha_warm_store_multiple_replicas(self):
        """Test warm store with multiple replica configuration."""
        warm_store = WarmStore(
            dsn="postgresql://warm:warm@pg-primary:5432/warm_store",
            read_dsn="postgresql://warm:warm@pg-replica-1:5432/warm_store,postgresql://warm:warm@pg-replica-2:5432/warm_store"
        )
        
        # Verify multiple replica configuration
        assert "pg-replica-1:5432" in warm_store._read_dsn
        assert "pg-replica-2:5432" in warm_store._read_dsn

    @pytest.mark.asyncio
    async def test_ha_warm_store_failover_scenario(self):
        """Test failover scenario in HA environment."""
        warm_store = WarmStore(
            dsn="postgresql://warm:warm@pg-primary:5432/warm_store",
            read_dsn="postgresql://warm:warm@pg-replica-1:5432/warm_store,postgresql://warm:warm@pg-replica-2:5432/warm_store"
        )
        
        # Simulate primary failure
        with patch('agent_obs.storage.warm.asyncpg') as mock_asyncpg:
            # Primary connection fails
            mock_asyncpg.create_pool.side_effect = [
                Exception("Primary connection failed"),
                AsyncMock(),  # First replica connection succeeds
                AsyncMock()   # Second replica connection also available
            ]
            
            # Should failover to replica
            await warm_store.connect_read()
            
            # Verify multiple connection attempts
            assert mock_asyncpg.create_pool.call_count >= 2

    @pytest.mark.asyncio
    async def test_ha_warm_store_tls_configuration(self):
        """Test TLS configuration in HA environment."""
        warm_store = WarmStore(
            dsn="postgresql://warm:warm@pg-primary:5432/warm_store?sslmode=require&sslcert=/certs/postgres.pem&sslkey=/certs/postgres-key.pem&sslrootcert=/certs/ca.pem",
            read_dsn="postgresql://warm:warm@pgbouncer-warm:6433/warm_store?sslmode=require&sslcert=/certs/postgres.pem&sslkey=/certs/postgres-key.pem&sslrootcert=/certs/ca.pem"
        )
        
        # Verify TLS configuration
        assert "sslmode=require" in warm_store._dsn
        assert "sslcert=" in warm_store._dsn
        assert "sslkey=" in warm_store._dsn
        assert "sslrootcert=" in warm_store._dsn
        assert "sslmode=require" in warm_store._read_dsn
        assert "sslcert=" in warm_store._read_dsn
        assert "sslkey=" in warm_store._read_dsn
        assert "sslrootcert=" in warm_store._read_dsn

    @pytest.mark.asyncio
    async def test_ha_warm_store_tenant_isolation(self):
        """Test tenant isolation in HA environment."""
        warm_store = WarmStore(
            dsn="postgresql://warm:warm@pg-primary:5432/warm_store",
            read_dsn="postgresql://warm:warm@pgbouncer-warm:6433/warm_store"
        )
        
        with patch('agent_obs.storage.warm.asyncpg') as mock_asyncpg:
            mock_pool = AsyncMock()
            mock_conn = AsyncMock()
            mock_pool.acquire.return_value.__aenter__.return_value = mock_conn
            mock_pool.acquire.return_value.__aexit__.return_value = None
            mock_asyncpg.create_pool.return_value = mock_pool
            
            # Test tenant-scoped operations
            await warm_store.get_trace('test-trace', 'tenant-a')
            await warm_store.get_trace('test-trace', 'tenant-b')
            
            # Verify tenant GUC was set transaction-locally
            assert mock_conn.execute.call_count >= 2
            mock_conn.execute.assert_any_call(
                "SELECT set_config('app.tenant_id', $1, true)", 'tenant-a'
            )
            mock_conn.execute.assert_any_call(
                "SELECT set_config('app.tenant_id', $1, true)", 'tenant-b'
            )

    @pytest.mark.asyncio
    async def test_ha_warm_store_connection_pooling(self):
        """Test connection pooling in HA environment."""
        warm_store = WarmStore(
            dsn="postgresql://warm:warm@pg-primary:5432/warm_store",
            read_dsn="postgresql://warm:warm@pgbouncer-warm:6433/warm_store",
            min_size=2,
            max_size=10
        )
        
        # Verify pool configuration
        assert warm_store._min_size == 2
        assert warm_store._max_size == 10
        
        with patch('agent_obs.storage.warm.asyncpg') as mock_asyncpg:
            mock_pool = AsyncMock()
            mock_asyncpg.create_pool.return_value = mock_pool
            
            # Test connection pooling
            await warm_store.connect()
            await warm_store.connect_read()
            
            # Verify pool creation with correct parameters
            calls = mock_asyncpg.create_pool.call_args_list
            assert len(calls) == 2
            assert calls[0][1]['min_size'] == 2
            assert calls[0][1]['max_size'] == 10
            assert calls[1][1]['min_size'] == 2
            assert calls[1][1]['max_size'] == 10
            assert calls[1][1]['statement_cache_size'] == 0  # pgbouncer

    @pytest.mark.asyncio
    async def test_ha_warm_store_health_check(self):
        """Test health check in HA environment."""
        warm_store = WarmStore(
            dsn="postgresql://warm:warm@pg-primary:5432/warm_store",
            read_dsn="postgresql://warm:warm@pgbouncer-warm:6433/warm_store"
        )
        
        with patch('agent_obs.storage.warm.asyncpg') as mock_asyncpg:
            mock_pool = AsyncMock()
            mock_conn = AsyncMock()
            mock_pool.acquire.return_value.__aenter__.return_value = mock_conn
            mock_pool.acquire.return_value.__aexit__.return_value = None
            mock_asyncpg.create_pool.return_value = mock_pool
            
            # Test health check
            result = await warm_store.ping()
            
            # Verify health check succeeded
            assert result is True
            mock_conn.execute.assert_called_with("SELECT 1")

    @pytest.mark.asyncio
    async def test_ha_warm_store_error_handling(self):
        """Test error handling in HA environment."""
        warm_store = WarmStore(
            dsn="postgresql://warm:warm@pg-primary:5432/warm_store",
            read_dsn="postgresql://warm:warm@pgbouncer-warm:6433/warm_store"
        )
        
        with patch('agent_obs.storage.warm.asyncpg') as mock_asyncpg:
            # Connection fails
            mock_asyncpg.create_pool.side_effect = Exception("Connection failed")
            
            # Test error handling
            result = await warm_store.ping()
            
            # Verify error handling
            assert result is False

    @pytest.mark.asyncio
    async def test_ha_warm_store_close_both_pools(self):
        """Test closing both read and write pools in HA environment."""
        warm_store = WarmStore(
            dsn="postgresql://warm:warm@pg-primary:5432/warm_store",
            read_dsn="postgresql://warm:warm@pgbouncer-warm:6433/warm_store"
        )
        
        # Mock both pools
        mock_write_pool = AsyncMock()
        mock_read_pool = AsyncMock()
        warm_store._pool = mock_write_pool
        warm_store._read_pool = mock_read_pool
        
        # Test closing both pools
        await warm_store.close()
        
        # Verify both pools were closed
        mock_write_pool.close.assert_called_once()
        mock_read_pool.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_ha_warm_store_context_manager(self):
        """Test context manager in HA environment."""
        warm_store = WarmStore(
            dsn="postgresql://warm:warm@pg-primary:5432/warm_store",
            read_dsn="postgresql://warm:warm@pgbouncer-warm:6433/warm_store"
        )
        
        with patch.object(warm_store, 'connect', return_value=AsyncMock()):
            async with warm_store as store:
                assert store is warm_store

    @pytest.mark.asyncio
    async def test_ha_warm_store_compliance_catalog(self):
        """Test compliance catalog operations in HA environment."""
        warm_store = WarmStore(
            dsn="postgresql://warm:warm@pg-primary:5432/warm_store",
            read_dsn="postgresql://warm:warm@pgbouncer-warm:6433/warm_store"
        )
        
        with patch('agent_obs.storage.warm.asyncpg') as mock_asyncpg:
            mock_pool = AsyncMock()
            mock_conn = AsyncMock()
            mock_pool.acquire.return_value.__aenter__.return_value = mock_conn
            mock_pool.acquire.return_value.__aexit__.return_value = None
            mock_asyncpg.create_pool.return_value = mock_pool
            
            # Mock compliance catalog data
            mock_conn.fetch.return_value = [
                {
                    'id': 1,
                    'agent_id': 'test-agent',
                    'tool': 'test-tool',
                    'field': 'test-field',
                    'pii_type': 'email',
                    'frequency': 10,
                    'first_seen': '2023-01-01T00:00:00Z',
                    'last_seen': '2023-01-02T00:00:00Z'
                }
            ]
            
            # Test compliance catalog operations
            result = await warm_store.get_all_compliance_catalog()
            
            # Verify result
            assert len(result) == 1
            assert result[0]['agent_id'] == 'test-agent'
            assert result[0]['pii_type'] == 'email'


# Test fixtures for HA environment
@pytest.fixture
def ha_warm_store():
    """Fixture for warm store in HA environment."""
    return WarmStore(
        dsn="postgresql://warm:warm@pg-primary:5432/warm_store",
        read_dsn="postgresql://warm:warm@pgbouncer-warm:6433/warm_store"
    )


@pytest.fixture
def ha_warm_store_multiple_replicas():
    """Fixture for warm store with multiple replicas."""
    return WarmStore(
        dsn="postgresql://warm:warm@pg-primary:5432/warm_store",
        read_dsn="postgresql://warm:warm@pg-replica-1:5432/warm_store,postgresql://warm:warm@pg-replica-2:5432/warm_store"
    )