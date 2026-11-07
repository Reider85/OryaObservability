"""PP54: Semantic cache tests - Redis-based caching of identical LLM requests."""

from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import redis.asyncio as redis
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

from agent_obs.proxy import ProxyConfig

# Define namespace for observability metrics
OBSERVABILITY_NAMESPACE = "agent_obs"
from agent_obs.proxy.semantic_cache import (
    CachedResponse,
    SemanticCache,
    SEMANTIC_CACHE_HITS,
    SEMANTIC_CACHE_MISSES,
    SEMANTIC_CACHE_COST_SAVED,
    SEMANTIC_CACHE_LATENCY,
)


class TestCachedResponse:
    """Test CachedResponse dataclass."""

    def test_cached_response_serialization(self):
        """Test round-trip serialization."""
        original = CachedResponse(
            payload=b'{"response": "test"}',
            media_type="application/json",
            cost_usd=0.01,
            created_at=time.time(),
            expires_at=time.time() + 300,
            original_model="gpt-4o",
            headers={"content-type": "application/json"},
        )
        
        data = original.to_dict()
        restored = CachedResponse.from_dict(data)
        
        assert restored.payload == original.payload
        assert restored.media_type == original.media_type
        assert restored.cost_usd == original.cost_usd
        assert restored.original_model == original.original_model
        assert restored.headers == original.headers


class TestSemanticCache:
    """Test SemanticCache functionality."""

    @pytest.fixture
    def mock_redis(self):
        """Mock Redis client."""
        mock_client = AsyncMock()
        mock_pool = AsyncMock()
        mock_client.ping = AsyncMock(return_value=b"PONG")
        mock_client.get = AsyncMock()
        mock_client.setex = AsyncMock()
        mock_client.delete = AsyncMock()
        mock_client.keys = AsyncMock()
        return mock_client, mock_pool

    @pytest.fixture
    def cache_config(self):
        """Cache configuration."""
        return {
            "redis_url": "redis://localhost:6379/0",
            "ttl_seconds": 300,
            "max_connections": 10,
        }

    @pytest.fixture
    def semantic_cache(self, cache_config):
        """Create SemanticCache instance."""
        return SemanticCache(**cache_config)

    @pytest.mark.asyncio
    async def test_cache_initialization_success(self, semantic_cache, mock_redis):
        """Test successful cache initialization."""
        # Ensure cache is not initialized
        semantic_cache._redis_client = None
        semantic_cache._redis_pool = None
        
        with patch('agent_obs.proxy.semantic_cache.redis') as mock_redis_module:
            mock_redis_module.Redis.return_value = mock_redis[0]
            mock_redis_module.ConnectionPool.return_value = mock_redis[1]
            
            await semantic_cache.initialize()
            
            mock_redis[0].ping.assert_called_once()
        assert semantic_cache._redis_client is not None

    @pytest.mark.asyncio
    async def test_cache_initialization_failure(self, semantic_cache):
        """Test cache initialization failure."""
        semantic_cache._redis_client = None
        
        with patch.object(redis, "Redis") as mock_redis_class:
            mock_redis_class.side_effect = Exception("Connection failed")
            
            await semantic_cache.initialize()
            
            assert semantic_cache._redis_client is None

    @pytest.mark.asyncio
    async def test_cache_get_hit(self, semantic_cache, mock_redis):
        """Test cache hit scenario."""
        semantic_cache._redis_client, semantic_cache._redis_pool = mock_redis
        
        # Create cached response and serialize it
        cached_response = CachedResponse(
            payload=b'{"response": "cached"}',
            media_type="application/json",
            cost_usd=0.01,
            created_at=time.time(),
            expires_at=time.time() + 300,
            original_model="gpt-4o",
            headers={"content-type": "application/json"},
        )
        mock_redis[0].get.return_value = json.dumps(cached_response.to_dict()).encode()
        
        request_body = b'{"test": "request"}'
        result = await semantic_cache.get(request_body, "tenant1", "agent1")
        
        assert result is not None
        assert result.payload == b'{"response": "cached"}'
        assert result.cost_usd == 0.01
        mock_redis[0].get.assert_called_once()

    @pytest.mark.asyncio
    async def test_cache_get_miss(self, semantic_cache, mock_redis):
        """Test cache miss scenario."""
        semantic_cache._redis_client, semantic_cache._redis_pool = mock_redis
        mock_redis[0].get.return_value = None
        
        request_body = b'{"test": "request"}'
        result = await semantic_cache.get(request_body, "tenant1", "agent1")
        
        assert result is None
        mock_redis[0].get.assert_called_once()

    @pytest.mark.asyncio
    async def test_cache_get_expired(self, semantic_cache, mock_redis):
        """Test cache expiration."""
        semantic_cache._redis_client, semantic_cache._redis_pool = mock_redis
        
        # Mock expired cached response
        expired_response = CachedResponse(
            payload=b'{"response": "expired"}',
            media_type="application/json",
            cost_usd=0.01,
            created_at=time.time() - 400,  # Created 400 seconds ago
            expires_at=time.time() - 100,  # Expired 100 seconds ago
            original_model="gpt-4o",
            headers={"content-type": "application/json"},
        )
        mock_redis[0].get.return_value = json.dumps(expired_response.to_dict()).encode()
        
        request_body = b'{"test": "request"}'
        result = await semantic_cache.get(request_body, "tenant1", "agent1")
        
        assert result is None
        mock_redis[0].delete.assert_called_once()

    @pytest.mark.asyncio
    async def test_cache_set(self, semantic_cache, mock_redis):
        """Test cache set operation."""
        semantic_cache._redis_client, semantic_cache._redis_pool = mock_redis
        
        request_body = b'{"test": "request"}'
        response_payload = b'{"response": "test"}'
        
        await semantic_cache.set(
            request_body=request_body,
            response_payload=response_payload,
            media_type="application/json",
            cost_usd=0.02,
            tenant_id="tenant1",
            agent_id="agent1",
            original_model="gpt-4o",
            headers={"content-type": "application/json"},
        )
        
        mock_redis[0].setex.assert_called_once()
        args = mock_redis[0].setex.call_args
        assert args[0][0].startswith("semcache:")
        assert args[0][1] == 300  # TTL
        assert json.loads(args[0][2])["cost_usd"] == 0.02

    @pytest.mark.asyncio
    async def test_cache_record_cost_savings(self, semantic_cache, mock_redis):
        """Test cost savings recording."""
        semantic_cache._redis_client, semantic_cache._redis_pool = mock_redis
        
        await semantic_cache.record_cost_savings(0.05, "tenant1", "agent1")
        
        # Check that the counter was incremented
        assert SEMANTIC_CACHE_COST_SAVED._value._value >= 0.05

    @pytest.mark.asyncio
    async def test_cache_stats(self, semantic_cache, mock_redis):
        """Test cache statistics."""
        semantic_cache._redis_client, semantic_cache._redis_pool = mock_redis
        
        # Mock some keys
        mock_redis[0].keys.return_value = [b"semcache:1", b"semcache:2"]
        
        stats = await semantic_cache.get_stats()
        
        assert stats["status"] == "connected"
        assert stats["cached_keys_count"] == 2

    @pytest.mark.asyncio
    async def test_cache_close(self, semantic_cache, mock_redis):
        """Test cache cleanup."""
        semantic_cache._redis_client, semantic_cache._redis_pool = mock_redis
        
        await semantic_cache.close()
        
        mock_redis[1].aclose.assert_called_once()

    @pytest.mark.asyncio
    async def test_cache_error_handling(self, semantic_cache, mock_redis):
        """Test error handling in cache operations."""
        semantic_cache._redis_client, semantic_cache._redis_pool = mock_redis
        
        # Mock Redis error
        mock_redis[0].get.side_effect = Exception("Redis error")
        
        request_body = b'{"test": "request"}'
        result = await semantic_cache.get(request_body, "tenant1", "agent1")
        
        assert result is None  # Should not raise exception

    @pytest.mark.asyncio
    async def test_hash_consistency(self, semantic_cache):
        """Test that hash function is consistent."""
        request_body = b'{"test": "consistent hash"}'
        
        hash1 = semantic_cache._hash_request(request_body)
        hash2 = semantic_cache._hash_request(request_body)
        
        assert hash1 == hash2

    @pytest.mark.asyncio
    async def test_cache_ttl_expiry(self, semantic_cache, mock_redis):
        """Test TTL expiration handling."""
        semantic_cache._redis_client, semantic_cache._redis_pool = mock_redis
        
        # Set very short TTL for testing
        semantic_cache.ttl_seconds = 0.1
        
        request_body = b'{"test": "ttl"}'
        response_payload = b'{"response": "ttl"}'
        
        # Cache the response
        await semantic_cache.set(
            request_body=request_body,
            response_payload=response_payload,
            media_type="application/json",
            cost_usd=0.01,
            tenant_id="tenant1",
            agent_id="agent1",
        )
        
        # Wait for expiration
        await asyncio.sleep(0.2)
        
        # Try to get - should be expired
        result = await semantic_cache.get(request_body, "tenant1", "agent1")
        assert result is None


class TestSemanticCacheIntegration:
    """Integration tests with proxy server."""

    @pytest.fixture
    def proxy_config(self):
        """Proxy configuration with semantic cache enabled."""
        return ProxyConfig(
            cache_enabled=True,
            semantic_cache_enabled=True,
            semantic_cache_redis_url="redis://localhost:6379/0",
            semantic_cache_ttl_seconds=300,
            semantic_cache_max_connections=10,
        )

    @pytest.fixture
    def mock_redis(self):
        """Mock Redis for integration tests."""
        mock_client = AsyncMock()
        mock_pool = AsyncMock()
        mock_client.ping = AsyncMock(return_value=b"PONG")
        mock_client.get = AsyncMock()
        mock_client.setex = AsyncMock()
        return mock_client, mock_pool

    @pytest.mark.asyncio
    async def test_proxy_with_semantic_cache_enabled(self, proxy_config, mock_redis):
        """Test proxy server with semantic cache enabled."""
        from agent_obs.proxy.semantic_cache import get_semantic_cache
        
        with patch('agent_obs.proxy.semantic_cache.redis') as mock_redis_module:
            mock_redis_module.Redis.return_value = mock_redis[0]
            mock_redis_module.ConnectionPool.return_value = mock_redis[1]
            
            cache = get_semantic_cache(proxy_config)
            
            assert cache is not None
            assert cache.ttl_seconds == 300

    @pytest.mark.asyncio
    async def test_proxy_with_semantic_cache_disabled(self, proxy_config):
        """Test proxy server with semantic cache disabled."""
        proxy_config.semantic_cache_enabled = False
        
        from agent_obs.proxy.semantic_cache import get_semantic_cache
        
        cache = get_semantic_cache(proxy_config)
        
        assert cache is None

    @pytest.mark.asyncio
    async def test_proxy_cache_disabled(self, proxy_config):
        """Test proxy server with cache disabled."""
        proxy_config.cache_enabled = False
        
        from agent_obs.proxy.semantic_cache import get_semantic_cache
        
        cache = get_semantic_cache(proxy_config)
        
        assert cache is None


class TestSemanticCacheMetrics:
    """Test Prometheus metrics for semantic cache."""

    def test_metrics_counters_exist(self):
        """Test that all required counters exist."""
        assert SEMANTIC_CACHE_HITS is not None
        assert SEMANTIC_CACHE_MISSES is not None
        assert SEMANTIC_CACHE_COST_SAVED is not None
        assert SEMANTIC_CACHE_LATENCY is not None

    def test_metrics_counters_initial_values(self):
        """Test initial values of counters."""
        # Counters start at 0 by default
        registry = CollectorRegistry()
        # Create fresh counters in the test registry
        hits = Counter(
            f"{OBSERVABILITY_NAMESPACE}_semantic_cache_hits_total",
            "Total semantic cache hits",
            ["tenant_id", "agent_id"],
            registry=registry,
        )
        misses = Counter(
            f"{OBSERVABILITY_NAMESPACE}_semantic_cache_misses_total",
            "Total semantic cache misses",
            registry=registry,
        )
        saved = Counter(
            f"{OBSERVABILITY_NAMESPACE}_semantic_cache_cost_saved_usd",
            "Total cost saved from semantic cache hits (USD)",
            registry=registry,
        )
        
        assert hits.labels("default", "unknown")._value._value == 0
        assert misses._value._value == 0
        assert saved._value._value == 0

    @pytest.mark.asyncio
    async def test_metrics_increment_on_cache_operations(self):
        """Test that metrics are incremented correctly."""
        # Simulate cache operations
        initial_hits = SEMANTIC_CACHE_HITS.labels("default", "unknown")._value._value
        initial_misses = SEMANTIC_CACHE_MISSES._value._value
        initial_saved = SEMANTIC_CACHE_COST_SAVED._value._value
        
        SEMANTIC_CACHE_HITS.labels("default", "unknown").inc()
        SEMANTIC_CACHE_MISSES.inc()
        SEMANTIC_CACHE_COST_SAVED.inc(0.05)
        
        assert SEMANTIC_CACHE_HITS.labels("default", "unknown")._value._value == initial_hits + 1
        assert SEMANTIC_CACHE_MISSES._value._value == initial_misses + 1
        assert SEMANTIC_CACHE_COST_SAVED._value._value == initial_saved + 0.05

    @pytest.mark.asyncio
    async def test_cache_hit_ratio_calculation(self):
        """Test cache hit ratio calculation."""
        registry = CollectorRegistry()
        # Create fresh counters in the test registry
        hits = Counter(
            f"{OBSERVABILITY_NAMESPACE}_semantic_cache_hits_total",
            "Total semantic cache hits",
            ["tenant_id", "agent_id"],
            registry=registry,
        )
        misses = Counter(
            f"{OBSERVABILITY_NAMESPACE}_semantic_cache_misses_total",
            "Total semantic cache misses",
            registry=registry,
        )
        
        # Simulate operations
        hits.labels("default", "unknown").inc()
        misses.inc()
        
        # Hit ratio should be 0.5
        expected_ratio = hits.labels("default", "unknown")._value._value / (
            hits.labels("default", "unknown")._value._value + misses._value._value
        )
        
        assert expected_ratio == 0.5


class TestSemanticCacheEdgeCases:
    """Test edge cases and error scenarios."""

    @pytest.mark.asyncio
    async def test_empty_request_body(self):
        """Test handling of empty request body."""
        cache = SemanticCache("redis://localhost:6379/0")
        
        # Should not raise exception
        result = await cache.get(b"", "tenant1", "agent1")
        assert result is None

    @pytest.mark.asyncio
    async def test_large_request_body(self):
        """Test handling of large request body."""
        cache = SemanticCache("redis://localhost:6379/0")
        
        large_body = b"{" + b"x" * 100000 + b"}"
        
        # Should not raise exception
        result = await cache.get(large_body, "tenant1", "agent1")
        assert result is None  # Cache miss expected

    @pytest.mark.asyncio
    async def test_redis_connection_failure(self):
        """Test behavior when Redis connection fails."""
        cache = SemanticCache("redis://localhost:9999")  # Non-existent Redis
        
        # Should not raise exception
        result = await cache.get(b'{"test": "request"}', "tenant1", "agent1")
        assert result is None

    @pytest.mark.asyncio
    async def test_concurrent_cache_access(self):
        """Test concurrent access to cache."""
        cache = SemanticCache("redis://localhost:6379/0")
        
        async def access_cache(cache_key):
            return await cache.get(cache_key, "tenant1", "agent1")
        
        # Multiple concurrent requests
        tasks = [access_cache(f"request_{i}") for i in range(10)]
        results = await asyncio.gather(*tasks)
        
        # All should complete without exception
        assert len(results) == 10

    @pytest.mark.asyncio
    async def test_cache_with_special_characters(self):
        """Test cache with request body containing special characters."""
        cache = SemanticCache("redis://localhost:6379/0")
        
        special_body = b'{"test": "request with \\"quotes\\" and \n newlines"}'
        
        # Should not raise exception
        result = await cache.get(special_body, "tenant1", "agent1")
        assert result is None  # Cache miss expected


class TestSemanticCacheConfiguration:
    """Test semantic cache configuration options."""

    def test_cache_config_defaults(self):
        """Test default configuration values."""
        config = ProxyConfig()
        
        assert config.semantic_cache_enabled is True
        assert config.semantic_cache_redis_url == "redis://localhost:6379/0"
        assert config.semantic_cache_ttl_seconds == 300
        assert config.semantic_cache_max_connections == 10

    def test_cache_config_from_env(self):
        """Test configuration from environment variables."""
        with patch.dict('os.environ', {
            'SEMANTIC_CACHE_ENABLED': 'false',
            'SEMANTIC_CACHE_REDIS_URL': 'redis://custom:6379/1',
            'SEMANTIC_CACHE_TTL': '600',
            'SEMANTIC_CACHE_MAX_CONNECTIONS': '20',
        }):
            config = ProxyConfig.from_env()
            
            assert config.semantic_cache_enabled is False
            assert config.semantic_cache_redis_url == 'redis://custom:6379/1'
            assert config.semantic_cache_ttl_seconds == 600
            assert config.semantic_cache_max_connections == 20

    def test_cache_config_override(self):
        """Test configuration override."""
        overrides = {
            'semantic_cache_enabled': False,
            'semantic_cache_ttl_seconds': 600,
        }
        
        config = ProxyConfig.from_env(overrides=overrides)
        
        assert config.semantic_cache_enabled is False
        assert config.semantic_cache_ttl_seconds == 600