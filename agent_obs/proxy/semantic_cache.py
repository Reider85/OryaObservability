"""Minimal exact-match cache for PP43 preview.

PP54 will replace this with semantic cache (Redis, embedding similarity).
"""
import asyncio
import hashlib
import time
from dataclasses import dataclass
from typing import Optional

from agent_obs.proxy import ProxyConfig


@dataclass
class CachedResponse:
    payload: bytes
    media_type: str
    expires_at: float


class ExactMatchCache:
    """In-memory exact-match cache for HTTP responses."""

    def __init__(self, ttl_seconds: float, max_entries: int = 1024):
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self._cache: dict[str, CachedResponse] = {}
        self._keys: list[str] = []  # For FIFO eviction

    def get(self, body: bytes) -> Optional[CachedResponse]:
        """Get cached response if exists and not expired."""
        key = self._hash(body)
        cached = self._cache.get(key)
        if cached is None:
            return None
        
        if cached.expires_at <= time.time():
            # Remove expired
            self._cache.pop(key, None)
            self._keys.remove(key)
            return None
            
        return cached

    def set(self, body: bytes, payload: bytes, media_type: str) -> None:
        """Cache response with TTL."""
        key = self._hash(body)
        
        # Evict if at capacity
        if len(self._cache) >= self.max_entries:
            oldest_key = self._keys.pop(0)
            self._cache.pop(oldest_key, None)
        
        self._cache[key] = CachedResponse(
            payload=payload,
            media_type=media_type,
            expires_at=time.time() + self.ttl_seconds
        )
        self._keys.append(key)

    def _hash(self, body: bytes) -> str:
        """SHA256 hash of request body."""
        return hashlib.sha256(body).hexdigest()


def get_cache(config: ProxyConfig) -> ExactMatchCache | None:
    """Create cache if enabled, else None."""
    if not config.cache_enabled:
        return None
    return ExactMatchCache(config.cache_ttl_seconds)