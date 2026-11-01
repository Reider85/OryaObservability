"""PP38 — graceful degradation: circuit breakers wired into every storage
client and exporter, plus the DependencyUnhealthy alert contract.

Every dependency is exercised with its breaker OPEN: the client must fail
fast without touching the underlying driver/HTTP stack, and the SDK-level
behaviour documented in the PP38 spec must hold (ring-buffer export keeps
draining, compliance buffer is preserved, guardrail masking still applies).

The fast suite runs these with fully mocked drivers — no live services.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml
from clickhouse_driver import errors as ch_errors

from agent_obs.compliance.catalog import (
    ComplianceCatalog,
    ComplianceRow,
    PostgresCatalogWriter,
)
from agent_obs.exporters.langfuse_exporter import LangfuseExporter, _PostOutcome
from agent_obs.guardrail.engine import GuardrailConfig, GuardrailEngine
from agent_obs.guardrail.injection_classifier import InjectionClassifier, InjectionScore
from agent_obs.guardrail.pii_detector import PIIDetector
from agent_obs.guardrail.pii_types import PIIMatch
from agent_obs.guardrail.vault_client import VaultClient
from agent_obs.ha import (
    CircuitBreaker,
    CircuitOpenError,
    CircuitState,
    circuit_breaker_for,
    reset_circuit_breakers,
)
from agent_obs.observability import Span, SpanContext, SpanType
from agent_obs.storage.hot import HotStore


@pytest.fixture(autouse=True)
def clean_state():
    reset_circuit_breakers()
    yield
    reset_circuit_breakers()


@pytest.fixture(autouse=True)
def clean_vault_env(monkeypatch):
    for var in ("VAULT_ADDR", "VAULT_ADDRS", "VAULT_TOKEN", "VAULT_JWT", "VAULT_NAMESPACE"):
        monkeypatch.delenv(var, raising=False)


def _open_breaker(dependency: str, threshold: int = 1, recovery: float = 60.0) -> CircuitBreaker:
    breaker = CircuitBreaker(dependency, failure_threshold=threshold, recovery_time=recovery)
    for _ in range(threshold):
        breaker.record_failure()
    assert breaker.state is CircuitState.OPEN
    return breaker


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _span() -> Span:
    return Span(
        name="agent-loop",
        span_type=SpanType.AGENT_LOOP,
        context=SpanContext.new(agent_id="pp38-test"),
    )


# ---------------------------------------------------------------------------
# ClickHouse (HotStore) — graceful degradation: fail fast, no driver touch
# ---------------------------------------------------------------------------


class TestHotStoreBreaker:
    def test_open_breaker_skips_driver(self):
        breaker = _open_breaker("ch")
        store = HotStore(hosts=["ch-1:9000"], circuit_breaker=breaker)
        with patch("clickhouse_driver.Client") as factory:
            with pytest.raises(CircuitOpenError):
                store._get_client().execute("SELECT 1")
            factory.assert_not_called()

    def test_open_breaker_makes_is_available_false(self):
        breaker = _open_breaker("ch")
        store = HotStore(hosts=["ch-1:9000"], circuit_breaker=breaker)
        with patch("clickhouse_driver.Client") as factory:
            assert store.is_available() is False
            factory.assert_not_called()

    def test_breaker_opens_after_threshold_all_replica_failures(self):
        breaker = CircuitBreaker("ch", failure_threshold=2, recovery_time=60)
        store = HotStore(
            hosts=["ch-1:9000", "ch-2:9000"], circuit_breaker=breaker
        )

        def factory(host, port, **kwargs):
            client = MagicMock()
            client.execute.side_effect = ch_errors.NetworkError("Connection refused")
            return client

        with patch("clickhouse_driver.Client", side_effect=factory):
            with pytest.raises(ch_errors.NetworkError):
                store._get_client().execute("SELECT 1")
            assert breaker.state is CircuitState.CLOSED
            with pytest.raises(ch_errors.NetworkError):
                store._get_client().execute("SELECT 1")
            assert breaker.state is CircuitState.OPEN
            # Third call: fail-fast, the driver is never reached again.
            with patch("clickhouse_driver.Client") as never:
                with pytest.raises(CircuitOpenError):
                    store._get_client().execute("SELECT 1")
                never.assert_not_called()

    def test_success_closes_breaker(self):
        clock = FakeClock()
        breaker = CircuitBreaker(
            "ch", failure_threshold=1, recovery_time=60, clock=clock
        )
        store = HotStore(hosts=["ch-1:9000"], circuit_breaker=breaker)
        client = MagicMock()
        client.execute.return_value = [(1,)]
        with patch("clickhouse_driver.Client", return_value=client):
            # Open the breaker, then travel past the recovery window so the
            # next execute() is admitted as a half-open probe (single-flight:
            # allow_request is called inside _with_failover, not by the test).
            breaker.record_failure()
            clock.advance(60.0)
            store._get_client().execute("SELECT 1")
        assert breaker.state is CircuitState.CLOSED
        assert breaker.failures == 0


# ---------------------------------------------------------------------------
# Vault — fail fast, guardrail keeps masking
# ---------------------------------------------------------------------------


class TestVaultBreaker:
    def test_open_breaker_skips_hvac(self):
        breaker = _open_breaker("vault")
        client = VaultClient(
            addr="http://vault:8200", circuit_breaker=breaker, audit_enabled=False
        )
        with patch("hvac.Client") as factory:
            with pytest.raises(CircuitOpenError):
                client._with_failover(lambda c: True)
            factory.assert_not_called()

    async def test_store_returns_none_fast_when_open(self):
        breaker = _open_breaker("vault")
        client = VaultClient(
            addr="http://vault:8200", circuit_breaker=breaker, audit_enabled=False
        )
        with patch("hvac.Client") as factory:
            result = await client.store(mask="[EMAIL:ab]", original="a@b.c")
            factory.assert_not_called()
        assert result is None

    def test_is_available_false_when_open(self):
        breaker = _open_breaker("vault")
        client = VaultClient(
            addr="http://vault:8200", circuit_breaker=breaker, audit_enabled=False
        )
        with patch("hvac.Client") as factory:
            assert client.is_available() is False
            factory.assert_not_called()


class TestGuardrailWithOpenVaultBreaker:
    async def test_masking_still_applied(self):
        """Vault down must not stop PII masking (fail-closed for recovery
        only): the text leaves the guardrail masked either way."""
        breaker = _open_breaker("vault")
        vault = VaultClient(
            addr="http://vault:8200", circuit_breaker=breaker, audit_enabled=False
        )
        detector = MagicMock(spec=PIIDetector)
        detector.detect.return_value = [
            PIIMatch(
                entity_type="email",
                value="alice@example.com",
                span_start=0,
                span_end=17,
                mask="[EMAIL:abcd]",
            )
        ]
        classifier = MagicMock(spec=InjectionClassifier)
        classifier.classify.return_value = InjectionScore(
            score=0.0, label="benign", confidence=0.99
        )
        engine = GuardrailEngine(
            pii_detector=detector,
            injection_classifier=classifier,
            vault_client=vault,
            config=GuardrailConfig(),
        )
        with patch("hvac.Client") as factory:
            verdict = await engine.check_input("contact alice@example.com now")
            factory.assert_not_called()
        assert "[EMAIL:abcd]" in verdict.masked_text


# ---------------------------------------------------------------------------
# Langfuse — stashing instead of retry storms
# ---------------------------------------------------------------------------


class TestLangfuseBreaker:
    async def test_open_breaker_stashes_without_http(self):
        breaker = _open_breaker("langfuse")
        exporter = LangfuseExporter(
            endpoint="http://langfuse:3000",
            public_key="pk",
            secret_key="sk",
            circuit_breaker=breaker,
        )
        exporter._post_with_retries = AsyncMock()  # type: ignore[method-assign]
        batch = [_span()]
        await exporter.export(batch)
        exporter._post_with_retries.assert_not_called()
        assert exporter._pending == [batch]
        await exporter.close()

    async def test_retryable_exhaustion_records_failure(self):
        breaker = CircuitBreaker("langfuse", failure_threshold=5, recovery_time=60)
        exporter = LangfuseExporter(
            endpoint="http://langfuse:3000",
            public_key="pk",
            secret_key="sk",
            circuit_breaker=breaker,
        )
        exporter._post_with_retries = AsyncMock(  # type: ignore[method-assign]
            return_value=_PostOutcome(retryable=True, error="HTTP 503")
        )
        await exporter.export([_span()])
        assert breaker.failures == 1
        assert len(exporter._pending) == 1
        await exporter.close()

    async def test_success_records_success(self):
        clock = FakeClock()
        breaker = CircuitBreaker(
            "langfuse", failure_threshold=1, recovery_time=60, clock=clock
        )
        breaker.record_failure()
        clock.advance(60.0)  # next export() is the half-open probe
        exporter = LangfuseExporter(
            endpoint="http://langfuse:3000",
            public_key="pk",
            secret_key="sk",
            circuit_breaker=breaker,
        )
        exporter._post_with_retries = AsyncMock(  # type: ignore[method-assign]
            return_value=_PostOutcome(success=True)
        )
        await exporter.export([_span()])
        assert breaker.state is CircuitState.CLOSED
        assert breaker.failures == 0
        assert exporter._pending == []
        await exporter.close()

    async def test_non_retryable_4xx_counts_as_reachable(self):
        breaker = CircuitBreaker("langfuse", failure_threshold=5, recovery_time=60)
        exporter = LangfuseExporter(
            endpoint="http://langfuse:3000",
            public_key="pk",
            secret_key="sk",
            circuit_breaker=breaker,
        )
        exporter._post_with_retries = AsyncMock(  # type: ignore[method-assign]
            return_value=_PostOutcome(retryable=False, error="HTTP 401")
        )
        await exporter.export([_span()])
        assert breaker.failures == 0
        await exporter.close()


# ---------------------------------------------------------------------------
# Postgres compliance catalog — buffer preserved, NullWriter-like degradation
# ---------------------------------------------------------------------------


class TestPostgresBreaker:
    async def test_open_breaker_raises_before_pool(self):
        breaker = _open_breaker("pg")
        writer = PostgresCatalogWriter(dsn="postgresql://pg/warm", circuit_breaker=breaker)
        row = ComplianceRow(
            agent_id="a", tool="t", field="f", pii_type="email", frequency=1
        )
        with (
            patch.object(
                writer,
                "_get_pool",
                new=AsyncMock(side_effect=AssertionError("must not connect")),
            ),
            pytest.raises(CircuitOpenError),
        ):
            await writer.upsert([row])

    async def test_catalog_flush_keeps_buffer_when_open(self):
        """The documented degradation: Postgres down → catalog behaves like
        NullCatalogWriter — rows stay buffered for the next tick."""
        breaker = _open_breaker("pg")
        writer = PostgresCatalogWriter(dsn="postgresql://pg/warm", circuit_breaker=breaker)
        catalog = ComplianceCatalog(writer)
        try:
            catalog.record(agent_id="a", tool="t", field="f", pii_type="email")
            catalog.record(agent_id="a", tool="t", field="p", pii_type="phone")
            await catalog.flush()
            assert catalog.buffer_size == 2
        finally:
            await catalog.close()

    async def test_pool_failure_records_breaker_failure(self):
        breaker = CircuitBreaker("pg", failure_threshold=2, recovery_time=60)
        writer = PostgresCatalogWriter(dsn="postgresql://pg/warm", circuit_breaker=breaker)
        row = ComplianceRow(
            agent_id="a", tool="t", field="f", pii_type="email", frequency=1
        )
        with patch.object(
            writer, "_get_pool", new=AsyncMock(side_effect=ConnectionError("refused"))
        ), pytest.raises(ConnectionError):
            await writer.upsert([row])
        assert breaker.failures == 1


# ---------------------------------------------------------------------------
# Redis embedding fallback — skip the attempt entirely
# ---------------------------------------------------------------------------


class TestRedisFallbackBreaker:
    async def test_open_breaker_skips_redis(self):
        from agent_obs.observability import ObservabilitySDK

        # The fallback pulls the shared breaker from the registry — open it.
        breaker = circuit_breaker_for("redis")
        for _ in range(breaker.failure_threshold):
            breaker.record_failure()
        assert breaker.state is CircuitState.OPEN

        sdk_stub = object.__new__(ObservabilitySDK)
        batch = [("t1", "s1", "a1", "", [0.1, 0.2])]
        with patch("redis.asyncio.from_url") as from_url:
            # Must not raise — degradation is silent drop with a log line.
            await ObservabilitySDK._embedding_redis_fallback(sdk_stub, batch)
            from_url.assert_not_called()


# ---------------------------------------------------------------------------
# Alert contract
# ---------------------------------------------------------------------------


class TestDependencyUnhealthyAlert:
    def _load_groups(self) -> list[dict]:
        with open("infra/prometheus-rules.yml", encoding="utf-8") as fh:
            doc = yaml.safe_load(fh)
        return doc["groups"]

    def test_alert_rule_present(self):
        rules = [
            rule
            for group in self._load_groups()
            for rule in group.get("rules", [])
        ]
        alerts = {rule["alert"]: rule for rule in rules if "alert" in rule}
        assert "DependencyUnhealthy" in alerts

    def test_alert_fires_after_5m_at_health_zero(self):
        rules = [
            rule
            for group in self._load_groups()
            for rule in group.get("rules", [])
        ]
        alert = next(r for r in rules if r.get("alert") == "DependencyUnhealthy")
        assert alert["expr"] == "agent_obs_dependency_health == 0"
        assert alert["for"] == "5m"
        assert alert["labels"]["severity"] == "warning"
        assert alert["labels"]["component"] == "ha"

    def test_alertmanager_routes_component_ha_to_slack(self):
        with open("infra/alertmanager.yml", encoding="utf-8") as fh:
            doc = yaml.safe_load(fh)
        routes = doc["route"]["routes"]
        ha_route = next(
            r for r in routes if any(m == 'component="ha"' for m in r.get("matchers", []))
        )
        assert ha_route["receiver"] == "slack"
        assert "dependency" in ha_route["group_by"]


# ---------------------------------------------------------------------------
# SDK-level continuity
# ---------------------------------------------------------------------------


class TestSdkContinuity:
    async def test_export_worker_path_survives_open_breaker(self):
        """The spec's Langfuse-down scenario: spans keep draining from the
        ring buffer into the pending stash; nothing raises."""
        breaker = _open_breaker("langfuse")
        exporter = LangfuseExporter(
            endpoint="http://langfuse:3000",
            public_key="pk",
            secret_key="sk",
            circuit_breaker=breaker,
        )
        exporter._post_with_retries = AsyncMock(  # type: ignore[method-assign]
            side_effect=AssertionError("must not be called while open")
        )
        for _ in range(3):
            await exporter.export([_span(), _span()])
        assert len(exporter._pending) == 3
        await exporter.close()
