"""Semantic cache for PP54: Redis-based caching of identical LLM requests.

Caches identical requests (by sha256 of masked request body) for 5 minutes.
Cost savings from cache hits are attributed to eval pipeline budget.
PII-safe: only masked request bodies are hashed and cached.
"""

import asyncio
import base64
import hashlib
import json
import logging
import time
from dataclasses import dataclass, asdict
from typing import Optional

import redis.asyncio as redis
from prometheus_client import Counter, Gauge, Histogram

from agent_obs.proxy import ProxyConfig
from agent_obs.metrics import spans_total

# Define namespace for observability metrics
OBSERVABILITY_NAMESPACE = "agent_obs"

logger = logging.getLogger(__name__)

# Prometheus metrics for semantic cache
SEMANTIC_CACHE_HITS = Counter(
    f"{OBSERVABILITY_NAMESPACE}_semantic_cache_hits_total",
    "Total semantic cache hits",
    ["tenant_id", "agent_id"],
)

SEMANTIC_CACHE_MISSES = Counter(
    f"{OBSERVABILITY_NAMESPACE}_semantic_cache_misses_total",
    "Total semantic cache misses",
)

SEMANTIC_CACHE_HIT_RATIO = Gauge(
    f"{OBSERVABILITY_NAMESPACE}_semantic_cache_hit_ratio",
    "Semantic cache hit ratio",
)

SEMANTIC_CACHE_COST_SAVED = Counter(
    f"{OBSERVABILITY_NAMESPACE}_semantic_cache_cost_saved_usd",
    "Total cost saved from semantic cache hits (USD)",
)

SEMANTIC_CACHE_LATENCY = Histogram(
    f"{OBSERVABILITY_NAMESPACE}_semantic_cache_latency_seconds",
    "Semantic cache operation latency",
    ["operation"],  # "get" or "set"
)


@dataclass
class CachedResponse:
    """Cached response with metadata."""
    payload: bytes
    media_type: str
    cost_usd: float
    created_at: float
    expires_at: float
    original_model: str
    headers: dict

    def to_dict(self) -> dict:
        """Convert to dictionary for Redis storage."""
        data = asdict(self)
        # Base64 encode bytes fields for JSON serialization
        if isinstance(data["payload"], bytes):
            data["payload"] = base64.b64encode(data["payload"]).decode("utf-8")
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "CachedResponse":
        """Create from dictionary stored in Redis."""
        # Base64 decode bytes fields
        if isinstance(data["payload"], str):
            data["payload"] = base64.b64decode(data["payload"])
        return cls(**data)


class SemanticCache:
    """Redis-based semantic cache for identical LLM requests."""

    def __init__(
        self,
        redis_url: str,
        ttl_seconds: int = 300,
        max_connections: int = 10,
        key_prefix: str = "semcache:",
    ):
        self.redis_url = redis_url
        self.ttl_seconds = ttl_seconds
        self.key_prefix = key_prefix
        self._redis_pool: Optional[redis.ConnectionPool] = None
        self._redis_client: Optional[redis.Redis] = None
        self._lock = asyncio.Lock()
        
    async def initialize(self) -> None:
        """Initialize Redis connection pool."""
        if self._redis_client is None:
            async with self._lock:
                if self._redis_client is None:
                    try:
                        self._redis_pool = redis.ConnectionPool.from_url(
                            self.redis_url,
                            max_connections=self.max_connections,
                            retry_on_timeout=True,
                            health_check_interval=30,
                        )
                        self._redis_client = redis.Redis(connection_pool=self._redis_pool)
                        # Test connection
                        await self._redis_client.ping()
                        logger.info("Semantic cache initialized with Redis: %s", self.redis_url)
                    except Exception as e:
                        logger.warning("Failed to initialize semantic cache Redis: %s", e)
                        self._redis_client = None
                        self._redis_pool = None

    async def close(self) -> None:
        """Close Redis connection pool."""
        if self._redis_pool:
            await self._redis_pool.aclose()
            self._redis_client = None
            self._redis_pool = None

    @property
    def max_connections(self) -> int:
        """Get max connections from environment or default."""
        import os
        return int(os.getenv("SEMANTIC_CACHE_MAX_CONNECTIONS", "10"))

    def _hash_request(self, request_body: bytes) -> str:
        """SHA256 hash of request body for cache key."""
        return hashlib.sha256(request_body).hexdigest()

    def _build_cache_key(self, request_hash: str) -> str:
        """Build Redis cache key."""
        return f"{self.key_prefix}{request_hash}"

    async def get(self, request_body: bytes, tenant_id: str = "default", agent_id: str = "unknown") -> Optional[CachedResponse]:
        """Get cached response if exists and not expired."""
        if not self._redis_client:
            return None

        request_hash = self._hash_request(request_body)
        cache_key = self._build_cache_key(request_hash)
        
        try:
            start_time = time.time()
            cached_data = await self._redis_client.get(cache_key)
            
            if cached_data is None:
                SEMANTIC_CACHE_MISSES.inc()
                return None

            cached_response = CachedResponse.from_dict(json.loads(cached_data))
            
            # Check expiration
            if cached_response.expires_at <= time.time():
                await self._redis_client.delete(cache_key)
                SEMANTIC_CACHE_MISSES.inc()
                return None

            # Update metrics
            SEMANTIC_CACHE_HITS.labels(tenant_id=tenant_id, agent_id=agent_id).inc()
            SEMANTIC_CACHE_LATENCY.labels(operation="get").observe(time.time() - start_time)
            
            # Update hit ratio gauge
            self._update_hit_ratio()
            
            logger.debug("Semantic cache hit for tenant=%s, agent=%s", tenant_id, agent_id)
            return cached_response
            
        except Exception as e:
            logger.warning("Semantic cache get error: %s", e)
            SEMANTIC_CACHE_MISSES.inc()
            return None

    async def set(
        self,
        request_body: bytes,
        response_payload: bytes,
        media_type: str,
        cost_usd: float,
        tenant_id: str = "default",
        agent_id: str = "unknown",
        original_model: str = "",
        headers: Optional[dict] = None,
    ) -> None:
        """Cache response with TTL."""
        if not self._redis_client:
            return

        request_hash = self._hash_request(request_body)
        cache_key = self._build_cache_key(request_hash)
        
        cached_response = CachedResponse(
            payload=response_payload,
            media_type=media_type,
            cost_usd=cost_usd,
            created_at=time.time(),
            expires_at=time.time() + self.ttl_seconds,
            original_model=original_model,
            headers=headers or {},
        )

        try:
            start_time = time.time()
            await self._redis_client.setex(
                cache_key,
                self.ttl_seconds,
                json.dumps(cached_response.to_dict()),
            )
            
            SEMANTIC_CACHE_LATENCY.labels(operation="set").observe(time.time() - start_time)
            logger.debug("Semantic cache set for tenant=%s, agent=%s", tenant_id, agent_id)
            
        except Exception as e:
            logger.warning("Semantic cache set error: %s", e)

    def _update_hit_ratio(self) -> None:
        """Update cache hit ratio gauge."""
        try:
            hits = SEMANTIC_CACHE_HITS._value._value
            misses = SEMANTIC_CACHE_MISSES._value._value
            total = hits + misses
            if total > 0:
                ratio = hits / total
                SEMANTIC_CACHE_HIT_RATIO.set(ratio)
        except Exception:
            # Ignore metric update errors
            pass

    async def record_cost_savings(self, cost_usd: float, tenant_id: str = "default", agent_id: str = "unknown") -> None:
        """Record cost savings from cache hit."""
        SEMANTIC_CACHE_COST_SAVED.inc(cost_usd)
        logger.debug("Semantic cache cost saved: %.6f USD for tenant=%s, agent=%s", cost_usd, tenant_id, agent_id)

    async def get_stats(self) -> dict:
        """Get cache statistics."""
        try:
            if not self._redis_client:
                return {"status": "disconnected"}
            
            # Get approximate number of keys
            pattern = f"{self.key_prefix}*"
            keys = await self._redis_client.keys(pattern)
            
            hits = SEMANTIC_CACHE_HITS._value._value if hasattr(SEMANTIC_CACHE_HITS, '_value') else 0
            misses = SEMANTIC_CACHE_MISSES._value._value if hasattr(SEMANTIC_CACHE_MISSES, '_value') else 0
            cost_saved = SEMANTIC_CACHE_COST_SAVED._value._value if hasattr(SEMANTIC_CACHE_COST_SAVED, '_value') else 0
            
            return {
                "status": "connected",
                "cached_keys_count": len(keys),
                "hits": int(hits),
                "misses": int(misses),
                "hit_ratio": hits / (hits + misses) if (hits + misses) > 0 else 0,
                "cost_saved_usd": float(cost_saved),
                "ttl_seconds": self.ttl_seconds,
            }
        except Exception as e:
            logger.warning("Failed to get cache stats: %s", e)
            return {"status": "error", "error": str(e)}


def get_semantic_cache(config: ProxyConfig) -> SemanticCache | None:
    """Create semantic cache if enabled, else None."""
    import os
    
    if not config.cache_enabled:
        return None
    
    # Check if semantic cache is explicitly enabled
    semantic_cache_enabled = config.semantic_cache_enabled
    if not semantic_cache_enabled:
        return None
    
    # Use configuration values with environment fallbacks
    redis_url = config.semantic_cache_redis_url
    ttl_seconds = config.semantic_cache_ttl_seconds
    max_connections = config.semantic_cache_max_connections
    
    cache = SemanticCache(
        redis_url=redis_url,
        ttl_seconds=ttl_seconds,
        max_connections=max_connections,
    )
    
    return cache