"""PP36: Tests for warm-tier HA with read/write split and failover."""

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Add repo root to Python path for imports
REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from agent_obs.storage.warm import WarmStore, _pgbouncer_opts, setup_connection


class TestWarmStoreHA:
    """Test warm-tier HA functionality including read/write split and failover."""

    @pytest.mark.asyncio
    async def test_pgbouncer_opts_detection(self):
        """Test pgbouncer options are applied for pgbouncer DSNs."""
        # Test regular DSN
        opts = _pgbouncer_opts("postgresql://user:pass@host:5432/db")
        assert opts == {}
        
        # Test pgbouncer port 6432
        opts = _pgbouncer_opts("postgresql://user:pass@host:6432/db")
        assert opts["statement_cache_size"] == 0
        
        # Test pgbouncer port 6433
        opts = _pgbouncer_opts("postgresql://user:pass@host:6433/db")
        assert opts["statement_cache_size"] == 0
        
        # Test pgbouncer in hostname
        opts = _pgbouncer_opts("postgresql://user:pass@pgbouncer:5432/db")
        assert opts["statement_cache_size"] == 0

    @pytest.mark.asyncio
    async def test_warm_store_initialization_with_read_dsn(self):
        """Test WarmStore initialization with separate read DSN."""
        warm_store = WarmStore(
            dsn="postgresql://warm:warm@localhost:5433/warm_store",
            read_dsn="postgresql://warm:warm@pgbouncer-warm:6433/warm_store"
        )
        
        assert warm_store._dsn == "postgresql://warm:warm@localhost:5433/warm_store"
        assert warm_store._read_dsn == "postgresql://warm:warm@pgbouncer-warm:6433/warm_store"

    @pytest.mark.asyncio
    async def test_warm_store_initialization_fallback_to_dsn(self):
        """Test WarmStore falls back to main DSN for read if not provided."""
        warm_store = WarmStore(
            dsn="postgresql://warm:warm@localhost:5433/warm_store"
        )
        
        assert warm_store._dsn == "postgresql://warm:warm@localhost:5433/warm_store"
        assert warm_store._read_dsn == "postgresql://warm:warm@localhost:5433/warm_store"

    @pytest.mark.asyncio
    async def test_connect_read_separate_pool(self):
        """Test that read connections use a separate pool."""
        warm_store = WarmStore(
            dsn="postgresql://warm:warm@localhost:5433/warm_store",
            read_dsn="postgresql://warm:warm@pgbouncer-warm:6433/warm_store"
        )
        
        # Mock asyncpg and create pools
        with patch('agent_obs.storage.warm.asyncpg') as mock_asyncpg:
            mock_pool1 = AsyncMock()
            mock_pool2 = AsyncMock()
            mock_asyncpg.create_pool.side_effect = [mock_pool1, mock_pool2]
            
            # Connect both pools
            await warm_store.connect()
            await warm_store.connect_read()
            
            # Verify both pools were created with different DSNs
            calls = mock_asyncpg.create_pool.call_args_list
            assert len(calls) == 2
            assert calls[0][1]['dsn'] == "postgresql://warm:warm@localhost:5433/warm_store"
            assert calls[1][1]['dsn'] == "postgresql://warm:warm@pgbouncer-warm:6433/warm_store"
            
            # Verify statement_cache_size is 0 for pgbouncer
            assert calls[1][1]['statement_cache_size'] == 0

    @pytest.mark.asyncio
    async def test_get_trace_uses_read_pool(self):
        """Test that get_trace uses the read pool."""
        warm_store = WarmStore(
            dsn="postgresql://warm:warm@localhost:5433/warm_store",
            read_dsn="postgresql://warm:warm@pgbouncer-warm:6433/warm_store"
        )
        
        # Mock read pool and connection
        mock_read_pool = AsyncMock()
        mock_conn = AsyncMock()
        mock_read_pool.acquire.return_value.__aenter__.return_value = mock_conn
        mock_read_pool.acquire.return_value.__aexit__.return_value = None
        
        with patch('agent_obs.storage.warm.asyncpg') as mock_asyncpg:
            mock_asyncpg.create_pool.return_value = mock_read_pool
            
            # Mock fetchrow to return a trace
            mock_conn.fetchrow.return_value = {
                'trace_id': 'test-trace-id',
                'tenant_id': 'test-tenant',
                'agent_id': 'test-agent',
                'start_time': '2023-01-01T00:00:00Z',
                'end_time': '2023-01-01T00:01:00Z',
                'status': 'success',
                'cost_usd_total': 0.05,
                'span_count': 3,
                'error_count': 0,
                'eval_avg': None,
                'input_chars': 100,
                'input_sha256': 'abc123',
                'output_chars': 200,
                'output_sha256': 'def456',
                'user_hash': 'user123'
            }
            
            # Call get_trace
            result = await warm_store.get_trace('test-trace-id')
            
            # Verify read pool was used
            mock_read_pool.acquire.assert_called_once()
            mock_conn.fetchrow.assert_called_once()
            
            # Verify result
            assert result['trace_id'] == 'test-trace-id'
            assert result['tenant_id'] == 'test-tenant'

    @pytest.mark.asyncio
    async def test_get_trace_with_tenant_uses_transaction(self):
        """Test that get_trace with tenant uses transaction-scoped GUC."""
        warm_store = WarmStore(
            dsn="postgresql://warm:warm@localhost:5433/warm_store",
            read_dsn="postgresql://warm:warm@pgbouncer-warm:6433/warm_store"
        )
        
        # Mock read pool and connection
        mock_read_pool = AsyncMock()
        mock_conn = AsyncMock()
        mock_read_pool.acquire.return_value.__aenter__.return_value = mock_conn
        mock_read_pool.acquire.return_value.__aexit__.return_value = None
        
        with patch('agent_obs.storage.warm.asyncpg') as mock_asyncpg:
            mock_asyncpg.create_pool.return_value = mock_read_pool
            
            # Mock fetchrow to return a trace
            mock_conn.fetchrow.return_value = {
                'trace_id': 'test-trace-id',
                'tenant_id': 'test-tenant',
                'agent_id': 'test-agent',
                'start_time': '2023-01-01T00:00:00Z',
                'end_time': '2023-01-01T00:01:00Z',
                'status': 'success',
                'cost_usd_total': 0.05,
                'span_count': 3,
                'error_count': 0,
                'eval_avg': None,
                'input_chars': 100,
                'input_sha256': 'abc123',
                'output_chars': 200,
                'output_sha256': 'def456',
                'user_hash': 'user123'
            }
            
            # Call get_trace with tenant
            result = await warm_store.get_trace('test-trace-id', 'test-tenant')
            
            # Verify transaction and GUC were set
            mock_conn.transaction.__aenter__.assert_called_once()
            mock_conn.transaction.__aexit__.assert_called_once()
            mock_conn.execute.assert_called_with("SELECT set_config('app.tenant_id', $1, true)", 'test-tenant')
            
            # Verify result
            assert result['trace_id'] == 'test-trace-id'

    @pytest.mark.asyncio
    async def test_setup_connection_transaction_local(self):
        """Test that setup_connection sets GUC transaction-locally."""
        mock_conn = AsyncMock()
        
        # Call setup_connection
        await setup_connection(mock_conn, 'test-tenant')
        
        # Verify GUC was set with transaction-local scope
        mock_conn.execute.assert_called_with("SELECT set_config('app.tenant_id', $1, true)", 'test-tenant')

    @pytest.mark.asyncio
    async def test_warm_store_close_both_pools(self):
        """Test that close() closes both read and write pools."""
        warm_store = WarmStore(
            dsn="postgresql://warm:warm@localhost:5433/warm_store",
            read_dsn="postgresql://warm:warm@pgbouncer-warm:6433/warm_store"
        )
        
        # Mock both pools
        mock_write_pool = AsyncMock()
        mock_read_pool = AsyncMock()
        warm_store._pool = mock_write_pool
        warm_store._read_pool = mock_read_pool
        
        # Call close
        await warm_store.close()
        
        # Verify both pools were closed
        mock_write_pool.close.assert_called_once()
        mock_read_pool.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_warm_store_context_manager(self):
        """Test that WarmStore works as an async context manager."""
        warm_store = WarmStore()
        
        # Mock connect to avoid actual database connection
        with patch.object(warm_store, 'connect', return_value=AsyncMock()):
            async with warm_store as store:
                assert store is warm_store


class TestWarmStoreFailover:
    """Test warm-tier failover scenarios."""

    @pytest.mark.asyncio
    async def test_failover_with_dsn_list(self):
        """Test failover behavior with DSN list."""
        warm_store = WarmStore(
            dsn="postgresql://warm:warm@primary:5433/warm_store",
            read_dsn="postgresql://warm:warm@replica1:6433/warm_store,postgresql://warm:warm@replica2:6433/warm_store"
        )
        
        # Verify DSN parsing
        assert warm_store._read_dsn == "postgresql://warm:warm@replica1:6433/warm_store,postgresql://warm:warm@replica2:6433/warm_store"
        
        # This would need more complex mocking to test actual failover logic
        # For now, just verify the DSN is stored correctly

    @pytest.mark.asyncio
    async def test_failover_fallback_to_dsn(self):
        """Test that missing read_dsn falls back to main DSN."""
        warm_store = WarmStore(
            dsn="postgresql://warm:warm@primary:5433/warm_store"
        )
        
        # Should fall back to main DSN
        assert warm_store._read_dsn == "postgresql://warm:warm@primary:5433/warm_store"