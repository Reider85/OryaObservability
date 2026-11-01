"""PP38 — HealthChecker active probes and dependency_health gauges.

All probes are mocked (httpx client / HotStore / asyncpg / redis / boto3
fakes) — no real network, so this file runs in the fast suite.
"""

from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_obs.ha import Health, HealthChecker, reset_circuit_breakers
from agent_obs.ha.health_checks import (
    DEFAULT_FAILURE_THRESHOLD,
    DEFAULT_RECOVERY_SECONDS,
)


@pytest.fixture(autouse=True)
def clean_state():
    reset_circuit_breakers()
    yield
    reset_circuit_breakers()


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Remove every env var that makes a dependency look configured."""
    for var in (
        "LANGFUSE_HOST",
        "CLICKHOUSE_HOSTS",
        "CLICKHOUSE_HOST",
        "CLICKHOUSE_PORT",
        "COMPLIANCE_PG_DSN",
        "VAULT_ADDR",
        "VAULT_ADDRS",
        "AGENT_OBS_REDIS_URL",
        "REDIS_URL",
        "EVAL_REDIS_URL",
        "S3_ENDPOINT",
        "AGENT_OBS_HEALTH_PROBE_INTERVAL",
        "AGENT_OBS_HEALTH_PROBE_TIMEOUT",
    ):
        monkeypatch.delenv(var, raising=False)


def _http_client(status_code: int = 200, exc: Exception | None = None) -> MagicMock:
    client = MagicMock()
    if exc is not None:
        client.get = AsyncMock(side_effect=exc)
    else:
        client.get = AsyncMock(return_value=SimpleNamespace(status_code=status_code))
    client.aclose = AsyncMock()
    return client


class TestHealth:
    def test_healthy_property(self):
        assert Health("healthy", 1.0, 0.0).healthy is True
        assert Health("unhealthy", 1.0, 0.0).healthy is False

    def test_fields(self):
        h = Health("healthy", 12.5, 123.0)
        assert h.status == "healthy"
        assert h.latency_ms == 12.5
        assert h.last_checked == 123.0


class TestTargets:
    def test_nothing_configured(self):
        assert HealthChecker().targets() == []

    def test_all_six_configured_via_env(self, monkeypatch):
        monkeypatch.setenv("LANGFUSE_HOST", "http://langfuse:3000")
        monkeypatch.setenv("CLICKHOUSE_HOSTS", "ch-1:9000")
        monkeypatch.setenv("COMPLIANCE_PG_DSN", "postgresql://pg/warm")
        monkeypatch.setenv("VAULT_ADDR", "http://vault:8200")
        monkeypatch.setenv("AGENT_OBS_REDIS_URL", "redis://redis:6379")
        monkeypatch.setenv("S3_ENDPOINT", "http://minio:9000")
        assert set(HealthChecker().targets()) == {
            "langfuse",
            "ch",
            "pg",
            "vault",
            "redis",
            "minio",
        }

    def test_explicit_args_beat_env(self):
        checker = HealthChecker(
            langfuse_url="http://explicit:3000",
            vault_addrs=["http://v1:8200", "http://v2:8200"],
        )
        assert checker.targets() == ["langfuse", "vault"]
        assert checker._vault_addr == "http://v1:8200"

    def test_vault_addrs_takes_first_entry(self):
        checker = HealthChecker(vault_addrs="http://v1:8200, http://v2:8200")
        assert checker._vault_addr == "http://v1:8200"


class TestLangfuseProbe:
    async def test_200_healthy(self):
        checker = HealthChecker(langfuse_url="http://lf:3000", http_client=_http_client(200))
        health = await checker.check_langfuse()
        assert health.healthy
        assert health.latency_ms >= 0

    async def test_500_unhealthy(self):
        checker = HealthChecker(langfuse_url="http://lf:3000", http_client=_http_client(500))
        assert (await checker.check_langfuse()).healthy is False

    async def test_connect_error_unhealthy(self):
        checker = HealthChecker(
            langfuse_url="http://lf:3000",
            http_client=_http_client(exc=ConnectionError("refused")),
        )
        assert (await checker.check_langfuse()).healthy is False


class TestVaultProbe:
    @pytest.mark.parametrize("code,healthy", [(200, True), (429, True), (501, False), (503, False)])
    async def test_status_codes(self, code, healthy):
        checker = HealthChecker(vault_addrs="http://v:8200", http_client=_http_client(code))
        health = await checker.check_vault()
        assert health.healthy is healthy

    async def test_connection_error_unhealthy(self):
        checker = HealthChecker(
            vault_addrs="http://v:8200",
            http_client=_http_client(exc=ConnectionError("refused")),
        )
        assert (await checker.check_vault()).healthy is False


class TestClickhouseProbe:
    async def test_available_healthy(self):
        hotstore = MagicMock()
        hotstore.is_available.return_value = True
        checker = HealthChecker(hotstore=hotstore)
        assert (await checker.check_clickhouse()).healthy is True
        hotstore.is_available.assert_called_once()

    async def test_unavailable_unhealthy(self):
        hotstore = MagicMock()
        hotstore.is_available.return_value = False
        checker = HealthChecker(hotstore=hotstore)
        assert (await checker.check_clickhouse()).healthy is False


class TestPostgresProbe:
    async def test_select_one_healthy(self):
        conn = MagicMock()
        conn.fetchval = AsyncMock(return_value=1)
        conn.close = AsyncMock()
        fake_asyncpg = MagicMock()
        fake_asyncpg.connect = AsyncMock(return_value=conn)
        checker = HealthChecker(pg_dsn="postgresql://pg/warm")
        with patch.dict(sys.modules, {"asyncpg": fake_asyncpg}):
            health = await checker.check_postgres()
        assert health.healthy is True
        conn.close.assert_awaited()

    async def test_connect_failure_unhealthy(self):
        fake_asyncpg = MagicMock()
        fake_asyncpg.connect = AsyncMock(side_effect=ConnectionError("refused"))
        checker = HealthChecker(pg_dsn="postgresql://pg/warm")
        with patch.dict(sys.modules, {"asyncpg": fake_asyncpg}):
            assert (await checker.check_postgres()).healthy is False

    async def test_missing_asyncpg_unhealthy(self):
        checker = HealthChecker(pg_dsn="postgresql://pg/warm")
        # asyncpg is optional (cron extra); hide it for this probe.
        with patch.dict(sys.modules, {"asyncpg": None}):
            assert (await checker.check_postgres()).healthy is False


class TestRedisProbe:
    async def test_ping_healthy(self):
        client = MagicMock()
        client.ping.return_value = True
        client.close = MagicMock()
        checker = HealthChecker(redis_url="redis://r:6379")
        with patch("redis.from_url", return_value=client) as from_url:
            health = await checker.check_redis()
        assert health.healthy is True
        from_url.assert_called_once()

    async def test_ping_failure_unhealthy(self):
        client = MagicMock()
        client.ping.side_effect = ConnectionError("refused")
        client.close = MagicMock()
        checker = HealthChecker(redis_url="redis://r:6379")
        with patch("redis.from_url", return_value=client):
            assert (await checker.check_redis()).healthy is False


class TestMinioProbe:
    async def test_list_buckets_healthy(self):
        s3 = MagicMock()
        fake_boto3 = MagicMock()
        fake_boto3.client.return_value = s3
        fake_config = MagicMock()
        fake_botocore = MagicMock()
        fake_botocore.config.Config = fake_config
        checker = HealthChecker(s3_endpoint="http://minio:9000")
        with patch.dict(
            sys.modules, {"boto3": fake_boto3, "botocore": fake_botocore, "botocore.config": fake_botocore.config}
        ):
            health = await checker.check_minio()
        assert health.healthy is True
        s3.list_buckets.assert_called_once()

    async def test_list_buckets_failure_unhealthy(self):
        s3 = MagicMock()
        s3.list_buckets.side_effect = ConnectionError("refused")
        fake_boto3 = MagicMock()
        fake_boto3.client.return_value = s3
        fake_botocore = MagicMock()
        checker = HealthChecker(s3_endpoint="http://minio:9000")
        with patch.dict(
            sys.modules, {"boto3": fake_boto3, "botocore": fake_botocore, "botocore.config": fake_botocore.config}
        ):
            assert (await checker.check_minio()).healthy is False


class TestCheckAllAndGauges:
    async def test_no_targets_returns_empty(self):
        assert await HealthChecker().check_all() == {}

    async def test_check_all_probes_every_target(self):
        hotstore = MagicMock()
        hotstore.is_available.return_value = True
        checker = HealthChecker(
            hotstore=hotstore,
            vault_addrs="http://v:8200",
            http_client=_http_client(200),
        )
        results = await checker.check_all()
        assert set(results) == {"ch", "vault"}
        assert all(h.healthy for h in results.values())

    def test_apply_gauges_sets_healthy_and_unhealthy(self):
        from prometheus_client import REGISTRY

        checker = HealthChecker()
        checker.apply_gauges(
            {
                "langfuse": Health("healthy", 1.0, 0.0),
                "ch": Health("unhealthy", 1.0, 0.0),
            }
        )
        assert (
            REGISTRY.get_sample_value(
                "agent_obs_dependency_health", {"dependency": "langfuse"}
            )
            == 1.0
        )
        assert (
            REGISTRY.get_sample_value(
                "agent_obs_dependency_health", {"dependency": "ch"}
            )
            == 0.0
        )

    async def test_run_forever_runs_one_cycle_then_stops(self):
        from prometheus_client import REGISTRY

        hotstore = MagicMock()
        hotstore.is_available.return_value = True
        checker = HealthChecker(
            hotstore=hotstore, interval_seconds=0.1, timeout_seconds=1.0
        )
        stop = asyncio.Event()

        task = asyncio.create_task(checker.run_forever(stop))
        await asyncio.sleep(0.3)
        stop.set()
        await asyncio.wait_for(task, timeout=2.0)
        assert (
            REGISTRY.get_sample_value(
                "agent_obs_dependency_health", {"dependency": "ch"}
            )
            == 1.0
        )

    async def test_run_forever_survives_probe_exception(self):
        # check_all itself never raises, but a buggy targets()/probe should
        # not kill the loop.
        checker = HealthChecker(interval_seconds=0.05, timeout_seconds=0.5)
        checker.targets = MagicMock(side_effect=[["ch"], []])  # type: ignore[method-assign]
        checker.check_all = AsyncMock(side_effect=[RuntimeError("boom"), {}])  # type: ignore[method-assign]
        stop = asyncio.Event()
        task = asyncio.create_task(checker.run_forever(stop))
        await asyncio.sleep(0.2)
        stop.set()
        await asyncio.wait_for(task, timeout=2.0)
        assert checker.check_all.await_count >= 1


class TestEnvConfig:
    def test_interval_and_timeout_from_env(self, monkeypatch):
        monkeypatch.setenv("AGENT_OBS_HEALTH_PROBE_INTERVAL", "15")
        monkeypatch.setenv("AGENT_OBS_HEALTH_PROBE_TIMEOUT", "3")
        checker = HealthChecker()
        assert checker.interval_seconds == 15.0
        assert checker.timeout_seconds == 3.0

    def test_defaults(self):
        from agent_obs.ha.health_checks import (
            DEFAULT_PROBE_INTERVAL_SECONDS,
            DEFAULT_PROBE_TIMEOUT_SECONDS,
        )

        checker = HealthChecker()
        assert checker.interval_seconds == DEFAULT_PROBE_INTERVAL_SECONDS
        assert checker.timeout_seconds == DEFAULT_PROBE_TIMEOUT_SECONDS


def test_default_breaker_constants_match_spec():
    assert DEFAULT_FAILURE_THRESHOLD == 5
    assert DEFAULT_RECOVERY_SECONDS == 60.0
