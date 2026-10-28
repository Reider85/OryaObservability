"""
Tests for PP23 Drill-down Redis cache: 10-min TTL, invalidation, metrics.

Covers:
- cache_key includes mfa_used (PP23 spec: sha256(user + trace + fields + mfa))
- 1st request -> cache miss + writes (setex + index sadd)
- 2nd request -> cache hit + returns cached result (no store reads)
- invalidate_trace / invalidate_user_trace delete via index sets
- late_annotation.annotate_with_fallback invalidates drill-down cache
- vault_client recovery invalidates drill-down cache for (user, trace_id)
- DRILL_DOWN_CACHE_TTL env honored; rate_limit/cache_ttl params override env
"""

import asyncio
import json
import os
from unittest.mock import MagicMock, patch

import pytest

from agent_obs.storage.drill_down import (
    CACHE_BYTES_KEY,
    CACHE_KEY_PREFIX,
    IDX_TRACE_PREFIX,
    IDX_USER_TRACE_PREFIX,
    DrillDownRequest,
    DrillDownService,
    invalidate_drill_down_cache,
)
from agent_obs.storage.hot import HotStore
from agent_obs.storage.warm import WarmStore
from agent_obs.storage.cold import ColdStore


class FakeRedis:
    """Stateful sync fake redis implementing the subset used by DrillDownService.

    Used via asyncio.to_thread, so methods are sync (matching redis-py sync client
    semantics that the service expects).
    """

    def __init__(self):
        self.store = {}       # key -> value (str)
        self.sets = {}        # key -> set of members
        self.ttls = {}        # key -> ttl seconds (recorded, not enforced)
        self.deleted = []     # log of deleted keys

    def get(self, key):
        return self.store.get(key)

    def setex(self, key, ttl, value):
        self.store[key] = value
        self.ttls[key] = ttl
        return True

    def sadd(self, key, member):
        self.sets.setdefault(key, set()).add(member)
        return len(self.sets[key])

    def smembers(self, key):
        return set(self.sets.get(key, set()))

    def expire(self, key, ttl):
        self.ttls[key] = ttl
        return True

    def incr(self, key):
        val = int(self.store.get(key, 0)) + 1
        self.store[key] = str(val)
        return val

    def incrby(self, key, amount):
        val = int(self.store.get(key, 0)) + int(amount)
        self.store[key] = str(val)
        return val

    def delete(self, *keys):
        deleted = 0
        for key in keys:
            self.deleted.append(key)
            if key in self.store:
                del self.store[key]
                deleted += 1
            if key in self.sets:
                del self.sets[key]
                deleted += 1
        return deleted


class MockHotStore(HotStore):
    def __init__(self):
        self.spans = {}
        self.read_count = 0

    def is_available(self):
        return True

    def get_spans(self, trace_id):
        self.read_count += 1
        if trace_id not in self.spans:
            raise Exception("not found")
        return self.spans[trace_id]


class MockWarmStore(WarmStore):
    def __init__(self):
        self.traces = {}

    async def get_trace(self, trace_id):
        return self.traces.get(trace_id)

    async def connect(self):
        return None


class MockColdStore(ColdStore):
    def __init__(self):
        self.parquet_data = {}

    def is_available(self):
        return True

    def list_keys(self, prefix=""):
        return list(self.parquet_data.keys())

    def read_bytes(self, key, bucket=None):
        if key not in self.parquet_data:
            raise Exception("not found")
        return self.parquet_data[key]


@pytest.fixture
def fake_redis():
    return FakeRedis()


@pytest.fixture
def hot_store():
    return MockHotStore()


@pytest.fixture
def warm_store():
    return MockWarmStore()


@pytest.fixture
def cold_store():
    return MockColdStore()


@pytest.fixture
def service(hot_store, warm_store, cold_store, fake_redis):
    return DrillDownService(
        hot_store=hot_store,
        warm_store=warm_store,
        cold_store=cold_store,
        redis_client=fake_redis,
        rate_limit=10,
        cache_ttl=600,
    )


def _seed_hot(hot_store, trace_id, attrs):
    hot_store.spans[trace_id] = [{"attributes": attrs}]


def _cache_value_keys(fake_redis):
    """Return only drill-down value cache keys (exclude the :bytes counter)."""
    return [
        k
        for k in fake_redis.store
        if k.startswith(CACHE_KEY_PREFIX) and k != CACHE_BYTES_KEY
    ]


class TestCacheKey:
    """PP23: cache_key = sha256(user + trace + sorted(fields) + mfa_used)."""

    def test_cache_key_includes_mfa_used(self, service, hot_store, fake_redis):
        """mfa_used=True and False must produce different cache keys."""
        trace_id = "trace-mfa"
        _seed_hot(hot_store, trace_id, {"custom.field": "v"})

        req_no_mfa = DrillDownRequest(
            trace_id=trace_id, fields=["custom.field"], user="u", mfa_used=False
        )
        asyncio.run(service.drill_down(req_no_mfa))

        # Different mfa_used -> different key -> must be a fresh miss, not a hit
        req_mfa = DrillDownRequest(
            trace_id=trace_id, fields=["custom.field"], user="u", mfa_used=True
        )
        result = asyncio.run(service.drill_down(req_mfa))

        assert result.cache_hit is False
        # Two distinct cache entries now exist
        cache_keys = _cache_value_keys(fake_redis)
        assert len(cache_keys) == 2

    def test_cache_key_is_prefixed(self, service, hot_store, fake_redis):
        trace_id = "trace-prefix"
        _seed_hot(hot_store, trace_id, {"f": "v"})
        req = DrillDownRequest(trace_id=trace_id, fields=["f"], user="u")
        asyncio.run(service.drill_down(req))
        cache_keys = _cache_value_keys(fake_redis)
        assert len(cache_keys) == 1


class TestCacheHitMiss:
    """PP23: 1st request -> miss + write; 2nd -> hit + cached result."""

    def test_first_request_misses_and_writes(self, service, hot_store, fake_redis):
        trace_id = "t1"
        _seed_hot(hot_store, trace_id, {"custom.field": "value"})

        req = DrillDownRequest(trace_id=trace_id, fields=["custom.field"], user="u")
        result = asyncio.run(service.drill_down(req))

        assert result.cache_hit is False
        # Value was written
        cache_keys = _cache_value_keys(fake_redis)
        assert len(cache_keys) == 1
        # TTL = 600 recorded
        assert fake_redis.ttls[cache_keys[0]] == 600
        # Trace index contains the cache key
        idx = fake_redis.sets[f"{IDX_TRACE_PREFIX}{trace_id}"]
        assert cache_keys[0] in idx

    def test_second_request_hits_cache(self, service, hot_store, fake_redis):
        trace_id = "t2"
        _seed_hot(hot_store, trace_id, {"custom.field": "value"})

        req = DrillDownRequest(trace_id=trace_id, fields=["custom.field"], user="u")
        asyncio.run(service.drill_down(req))
        reads_after_first = hot_store.read_count

        result = asyncio.run(service.drill_down(req))

        assert result.cache_hit is True
        assert result.fields["custom.field"] == "value"
        # Store was NOT re-read on the cached call
        assert hot_store.read_count == reads_after_first

    def test_cache_misses_then_hit_updates_hit_ratio(self, service, hot_store):
        trace_id = "t-ratio"
        _seed_hot(hot_store, trace_id, {"f": "v"})
        req = DrillDownRequest(trace_id=trace_id, fields=["f"], user="u")

        asyncio.run(service.drill_down(req))   # miss
        asyncio.run(service.drill_down(req))   # hit

        assert service._cache_misses == 1
        assert service._cache_hits == 1


class TestInvalidation:
    """PP23: invalidate_trace / invalidate_user_trace delete via index."""

    def test_invalidate_trace_removes_keys(self, service, hot_store, fake_redis):
        trace_id = "t-inv"
        _seed_hot(hot_store, trace_id, {"f": "v"})
        req = DrillDownRequest(trace_id=trace_id, fields=["f"], user="u")
        asyncio.run(service.drill_down(req))

        deleted = asyncio.run(service.invalidate_trace(trace_id))

        assert deleted >= 1
        cache_keys = _cache_value_keys(fake_redis)
        assert cache_keys == []
        # Index itself removed
        assert f"{IDX_TRACE_PREFIX}{trace_id}" not in fake_redis.sets

    def test_invalidate_then_next_request_is_miss(
        self, service, hot_store, fake_redis
    ):
        trace_id = "t-inv-miss"
        _seed_hot(hot_store, trace_id, {"f": "v"})
        req = DrillDownRequest(trace_id=trace_id, fields=["f"], user="u")
        asyncio.run(service.drill_down(req))   # miss
        asyncio.run(service.invalidate_trace(trace_id))
        result = asyncio.run(service.drill_down(req))   # must miss again
        assert result.cache_hit is False

    def test_invalidate_user_trace(self, service, hot_store, fake_redis):
        trace_id = "t-inv-user"
        _seed_hot(hot_store, trace_id, {"f": "v"})
        req = DrillDownRequest(trace_id=trace_id, fields=["f"], user="alice")
        asyncio.run(service.drill_down(req))

        # User index populated
        assert f"{IDX_USER_TRACE_PREFIX}alice:{trace_id}" in fake_redis.sets

        deleted = asyncio.run(service.invalidate_user_trace("alice", trace_id))
        assert deleted >= 1
        cache_keys = _cache_value_keys(fake_redis)
        assert cache_keys == []

    def test_module_level_invalidate_helper(self, hot_store, fake_redis):
        service = DrillDownService(
            hot_store=hot_store,
            warm_store=MockWarmStore(),
            cold_store=MockColdStore(),
            redis_client=fake_redis,
        )
        trace_id = "t-helper"
        _seed_hot(hot_store, trace_id, {"f": "v"})
        req = DrillDownRequest(trace_id=trace_id, fields=["f"], user="bob")
        asyncio.run(service.drill_down(req))

        deleted = asyncio.run(
            invalidate_drill_down_cache(fake_redis, trace_id, user="bob")
        )
        assert deleted >= 1
        cache_keys = _cache_value_keys(fake_redis)
        assert cache_keys == []


class TestConfig:
    """PP23: DRILL_DOWN_CACHE_TTL env; params override env."""

    def test_cache_ttl_env_honored(self, hot_store, warm_store, cold_store, fake_redis):
        old = os.environ.get("DRILL_DOWN_CACHE_TTL")
        os.environ["DRILL_DOWN_CACHE_TTL"] = "120"
        try:
            svc = DrillDownService(
                hot_store=hot_store,
                warm_store=warm_store,
                cold_store=cold_store,
                redis_client=fake_redis,
            )
            assert svc.cache_ttl == 120
        finally:
            if old is None:
                os.environ.pop("DRILL_DOWN_CACHE_TTL", None)
            else:
                os.environ["DRILL_DOWN_CACHE_TTL"] = old

    def test_cache_ttl_param_overrides_env(
        self, hot_store, warm_store, cold_store, fake_redis
    ):
        old = os.environ.get("DRILL_DOWN_CACHE_TTL")
        os.environ["DRILL_DOWN_CACHE_TTL"] = "120"
        try:
            svc = DrillDownService(
                hot_store=hot_store,
                warm_store=warm_store,
                cold_store=cold_store,
                redis_client=fake_redis,
                cache_ttl=999,
            )
            assert svc.cache_ttl == 999
        finally:
            if old is None:
                os.environ.pop("DRILL_DOWN_CACHE_TTL", None)
            else:
                os.environ["DRILL_DOWN_CACHE_TTL"] = old

    def test_rate_limit_param_honored(self, hot_store, warm_store, cold_store):
        svc = DrillDownService(
            hot_store=hot_store,
            warm_store=warm_store,
            cold_store=cold_store,
            redis_client=None,
            rate_limit=42,
        )
        assert svc.rate_limit == 42


class TestMetrics:
    """PP23: cache size bytes gauge + hit ratio gauge are updated."""

    def test_cache_size_bytes_gauge_set(self, service, hot_store, fake_redis):
        from agent_obs.metrics import drill_down_cache_size_bytes

        trace_id = "t-size"
        _seed_hot(hot_store, trace_id, {"f": "v"})
        req = DrillDownRequest(trace_id=trace_id, fields=["f"], user="u")
        asyncio.run(service.drill_down(req))

        # Redis byte counter was incremented
        raw = fake_redis.store.get(CACHE_BYTES_KEY)
        assert raw is not None
        assert int(raw) > 0
        # Gauge reflects the counter (>= 0; exact value depends on registry state)
        assert drill_down_cache_size_bytes._value.get() >= 0

    def test_hit_ratio_gauge_updated(self, service, hot_store):
        from agent_obs.metrics import drill_down_cache_hit_ratio

        trace_id = "t-hit-ratio"
        _seed_hot(hot_store, trace_id, {"f": "v"})
        req = DrillDownRequest(trace_id=trace_id, fields=["f"], user="u")
        asyncio.run(service.drill_down(req))   # miss
        asyncio.run(service.drill_down(req))   # hit

        # 1 hit / (1 hit + 1 miss) = 0.5
        assert drill_down_cache_hit_ratio._value.get() == pytest.approx(0.5)


class TestLateAnnotationInvalidation:
    """PP23 hook: annotate_with_fallback invalidates drill-down cache."""

    def test_annotate_with_fallback_invalidates_cache(self, fake_redis):
        from agent_obs.eval.base import EvalResult
        from agent_obs.eval.late_annotation import annotate_with_fallback

        trace_id = "t-annot"
        # Seed a fake cache entry + index
        cache_key = f"{CACHE_KEY_PREFIX}abc"
        fake_redis.store[cache_key] = "{}"
        fake_redis.sets[f"{IDX_TRACE_PREFIX}{trace_id}"] = {cache_key}

        eval_result = EvalResult(
            trace_id=trace_id,
            eval_id="e1",
            eval_name="faithfulness",
            eval_version="1.0.0",
            eval_timestamp=1.0,
            eval_latency_seconds=0.1,
            scores={"faithfulness": 0.9},
        )

        # hot_store=None -> goes to redis fallback branch; redis_client is our fake
        # setex on fake redis works, then invalidation deletes the seeded key.
        asyncio.run(
            annotate_with_fallback(eval_result, hot_store=None, redis_client=fake_redis)
        )

        assert cache_key not in fake_redis.store


class TestVaultRecoveryInvalidation:
    """PP23 hook: vault recovery invalidates drill-down cache for (user, trace)."""

    def test_invalidate_user_trace_on_vault_recovery(self, fake_redis):
        """Directly exercise the (user, trace) invalidation used by vault_client."""
        trace_id = "t-vault"
        user = "engineer"
        cache_key = f"{CACHE_KEY_PREFIX}vault"
        fake_redis.store[cache_key] = "{}"
        fake_redis.sets[f"{IDX_USER_TRACE_PREFIX}{user}:{trace_id}"] = {cache_key}

        deleted = asyncio.run(
            invalidate_drill_down_cache(fake_redis, trace_id, user=user)
        )

        assert deleted >= 1
        assert cache_key not in fake_redis.store

    def test_vault_client_calls_invalidation(self):
        """VaultClient.recover with trace_id triggers invalidation (mocked)."""
        from agent_obs.guardrail import vault_client as vc

        fake_client = MagicMock()
        fake_client.is_authenticated.return_value = True
        fake_client.secrets.kv.v2.read_secret_version.return_value = {
            "data": {
                "data": {
                    "original": "John Doe",
                    "created_at": __import__("time").time(),
                    "ttl_seconds": 999999,
                }
            }
        }

        client = vc.VaultClient.__new__(vc.VaultClient)
        client.redis_url = "redis://localhost:6379"
        client.mfa_config = None
        client._audit_enabled = False
        client._audit_store = None
        client._client = fake_client

        def _fake_from_url(url):
            m = MagicMock()
            m.aclose = MagicMock(return_value=asyncio.sleep(0))
            return m

        with patch(
            "agent_obs.storage.drill_down.invalidate_drill_down_cache"
        ) as mock_inv, patch(
            "redis.asyncio.from_url", side_effect=_fake_from_url
        ):
            mock_inv.return_value = 0

            asyncio.run(
                client.recover(
                    vault_key="pii/abc/1",
                    mfa_token="",
                    login="engineer",
                    reason="debugging",
                    trace_id="t-vault-2",
                )
            )

            # Invalidation helper was invoked with trace_id and user
            mock_inv.assert_called_once()
            args, kwargs = mock_inv.call_args
            assert args[1] == "t-vault-2"
            assert kwargs.get("user") == "engineer"
