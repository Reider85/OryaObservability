"""PP42: tenant-aware cost attribution + budget enforcement for the LLM proxy.

Covers the four spec deliverables: header attribution (with defaults), the
background aggregation flush (in-memory + ClickHouse recompute + warm upsert
+ gauge + audit), tenant-level budget enforcement (80%/100% bands, env +
tenant_config override), and the proxy's 429/403 gate end-to-end.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime

import pytest

httpx = pytest.importorskip("httpx")
pytest.importorskip("fastapi")

import respx
from httpx import ASGITransport

from agent_obs.cost.attribution import (
    BUDGET_APPROACHING,
    BUDGET_EXHAUSTED,
    BUDGET_OK,
    BudgetEnforcer,
    CostAttributionTracker,
    attribution_from_headers,
)
from agent_obs.guardrail.audit import CostAttributionAuditEvent
from agent_obs.metrics import cost_per_tenant_usd
from agent_obs.observability import ObservabilitySDK, Span, SpanContext, SpanType
from agent_obs.proxy import ProxyConfig
from agent_obs.proxy.obs_pipeline import ObsPipeline, Timing
from agent_obs.proxy.server import LLMProxyServer

UPSTREAM = "https://api.openai.com/v1/chat/completions"

MESSAGES = [{"role": "user", "content": "hello"}]
REQUEST = json.dumps(
    {"model": "gpt-4o", "messages": MESSAGES, "temperature": 0.0, "stream": False}
).encode("utf-8")
RESPONSE = json.dumps(
    {
        "choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }
).encode("utf-8")

TIMING = Timing(
    t_request_received=1000.0,
    t_upstream_started=1000.125,
    t_upstream_completed=1000.5,
)

_PROXY_ENV_VARS = [
    "AGENT_OBS_PROXY_MODE",
    "AGENT_OBS_PROXY_HOST",
    "AGENT_OBS_PROXY_PORT",
    "AGENT_OBS_PROXY_UPSTREAM_PROVIDER",
    "AGENT_OBS_PROXY_UPSTREAM_BASE_URL",
    "AGENT_OBS_PROXY_UPSTREAM_API_KEY_ENV",
    "AGENT_OBS_PROXY_REQUEST_TIMEOUT_SECONDS",
    "AGENT_OBS_PROXY_STREAMING_ENABLED",
    "AGENT_OBS_PROXY_LOG_BODIES",
    "AGENT_OBS_PROXY_LOG_BODIES_MAX_BYTES",
    "SHARED_PROXY_ENABLED",
    "AGENT_OBS_TENANT_ID",
    "TENANT_BUDGET_USD_DEFAULT",
    "AGENT_OBS_COST_FLUSH_INTERVAL",
    "AGENT_OBS_PRICE_BOOK_PATH",
]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for var in _PROXY_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AGENT_OBS_ENABLED", "true")
    monkeypatch.setenv("AGENT_OBS_CONTENT_RATE", "100")


@pytest.fixture
def proxy_config() -> ProxyConfig:
    return ProxyConfig(
        mode="on",
        enabled=True,
        upstream_base_url="https://api.openai.com/v1",
        upstream_api_key_env="OPENAI_API_KEY",
    )


@pytest.fixture
def sdk() -> ObservabilitySDK:
    return ObservabilitySDK([])


def make_span(trace_id: str = "trace-pp42") -> Span:
    return Span(
        name="llm.proxy.call:gpt-4o",
        span_type=SpanType.LLM_PROXY_CALL,
        context=SpanContext(trace_id=trace_id, span_id="span-1", agent_id="proxy"),
        start_time=1000.0,
    )


def chat_body(**overrides) -> dict:
    body = {"model": "gpt-4o", "messages": MESSAGES}
    body.update(overrides)
    return body


def proxy_spans(sdk: ObservabilitySDK) -> list[Span]:
    return [s for s in sdk.last_spans if s.span_type == SpanType.LLM_PROXY_CALL]


def client_for(proxy: LLMProxyServer) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=ASGITransport(app=proxy.app), base_url="http://proxy"
    )


class FakeHotStore:
    """ClickHouse double: configurable recompute rows + audit sink."""

    def __init__(self, rows=None, raise_exc=None):
        self.rows = rows or []
        self.raise_exc = raise_exc
        self.audit_events = []
        self.queries = []

    def get_tenant_cost_daily(self, start_ts: float):
        self.queries.append(start_ts)
        if self.raise_exc is not None:
            raise self.raise_exc
        return list(self.rows)

    def write_audit_event(self, event):
        if self.raise_exc is not None:
            raise self.raise_exc
        self.audit_events.append(event)


class FakeWarmStore:
    """Postgres double: captures upserts and serves budget overrides."""

    def __init__(self, overrides=None, raise_exc=None):
        self.upserts = []
        self.overrides = dict(overrides or {})
        self.raise_exc = raise_exc

    async def upsert_tenant_cost_daily(self, rows):
        if self.raise_exc is not None:
            raise self.raise_exc
        self.upserts.append(list(rows))
        return len(rows)

    async def get_tenant_budget_overrides(self):
        if self.raise_exc is not None:
            raise self.raise_exc
        return dict(self.overrides)


# ---------------------------------------------------------------------------
# Header attribution (PP42 §1)
# ---------------------------------------------------------------------------


class TestAttributionHeaders:
    def test_full_headers(self):
        headers = {
            "X-OryaObs-Tenant-Id": "ten-a",
            "X-OryaObs-Agent-Id": "agent-9",
            "X-OryaObs-User-Id": "alice",
        }
        assert attribution_from_headers(headers) == ("ten-a", "agent-9", "alice")

    def test_defaults_when_missing(self):
        assert attribution_from_headers({}) == ("default", "unknown", "anonymous")

    def test_blank_values_fall_back(self):
        headers = {
            "X-OryaObs-Tenant-Id": "  ",
            "X-OryaObs-Agent-Id": "",
        }
        assert attribution_from_headers(headers) == ("default", "unknown", "anonymous")

    def test_case_insensitive_lookup(self):
        headers = {
            "x-oryaobs-tenant-id": "ten-low",
            "X-ORYAOBS-AGENT-ID": "agent-up",
        }
        tenant, agent, user = attribution_from_headers(headers)
        assert tenant == "ten-low"
        assert agent == "agent-up"
        assert user == "anonymous"


# ---------------------------------------------------------------------------
# ObsPipeline enrichment (PP42 §1)
# ---------------------------------------------------------------------------


class TestPipelineAttribution:
    async def test_headers_stamped_on_span_and_context(self, proxy_config):
        tracker = CostAttributionTracker()
        pipeline = ObsPipeline(proxy_config, cost_tracker=tracker)
        span = make_span()
        headers = {
            "X-OryaObs-Tenant-Id": "ten-a",
            "X-OryaObs-Agent-Id": "agent-9",
            "X-OryaObs-User-Id": "alice",
        }
        await pipeline.enrich_span(
            span,
            request_body=REQUEST,
            response_body=RESPONSE,
            stream_chunks=None,
            headers=headers,
            timing=TIMING,
        )
        assert span.context.tenant_id == "ten-a"
        assert span.attributes["cost.attribution.tenant_id"] == "ten-a"
        assert span.attributes["cost.attribution.agent_id"] == "agent-9"
        assert span.attributes["cost.attribution.user_id"] == "alice"

    async def test_missing_headers_get_defaults(self, proxy_config):
        tracker = CostAttributionTracker()
        pipeline = ObsPipeline(proxy_config, cost_tracker=tracker)
        span = make_span()
        await pipeline.enrich_span(
            span,
            request_body=REQUEST,
            response_body=RESPONSE,
            stream_chunks=None,
            headers={},
            timing=TIMING,
        )
        assert span.context.tenant_id == "default"
        assert span.attributes["cost.attribution.tenant_id"] == "default"
        assert span.attributes["cost.attribution.agent_id"] == "unknown"
        assert span.attributes["cost.attribution.user_id"] == "anonymous"

    async def test_cost_recorded_into_tracker(self, proxy_config):
        tracker = CostAttributionTracker()
        pipeline = ObsPipeline(proxy_config, cost_tracker=tracker)
        span = make_span()
        headers = {"X-OryaObs-Tenant-Id": "ten-a", "X-OryaObs-Agent-Id": "agent-9"}
        await pipeline.enrich_span(
            span,
            request_body=REQUEST,
            response_body=RESPONSE,
            stream_chunks=None,
            headers=headers,
            timing=TIMING,
        )
        cost_usd = span.attributes.get("cost.usd")
        if cost_usd:  # price book may be absent in minimal environments
            assert tracker.spend_today("ten-a") == pytest.approx(float(cost_usd))
        else:
            # No price book -> cost attribute skipped -> tracker untouched.
            assert tracker.spend_today("ten-a") == 0.0

    async def test_no_tracker_still_enriches(self, proxy_config):
        pipeline = ObsPipeline(proxy_config)  # cost_tracker=None
        span = make_span()
        await pipeline.enrich_span(
            span,
            request_body=REQUEST,
            response_body=RESPONSE,
            stream_chunks=None,
            headers={"X-OryaObs-Tenant-Id": "ten-a"},
            timing=TIMING,
        )
        assert span.attributes["cost.attribution.tenant_id"] == "ten-a"


# ---------------------------------------------------------------------------
# CostAttributionTracker (PP42 §2)
# ---------------------------------------------------------------------------


class TestTracker:
    def test_record_and_spend_today(self):
        tracker = CostAttributionTracker()
        tracker.record("ten-a", "agent-1", 0.25)
        tracker.record("ten-a", "agent-2", 0.50)
        tracker.record("ten-b", "agent-1", 5.0)
        assert tracker.spend_today("ten-a") == pytest.approx(0.75)
        assert tracker.spend_today("ten-b") == pytest.approx(5.0)
        assert tracker.spend_today("ten-missing") == 0.0

    def test_zero_cost_is_noop(self):
        tracker = CostAttributionTracker()
        tracker.record("ten-a", "agent-1", 0.0)
        assert tracker.spend_today("ten-a") == 0.0
        assert tracker._spend == {}

    async def test_flush_hermetic_noop_without_spend_or_stores(self):
        """stop()/flush must not dial the network when there is nothing to do."""
        tracker = CostAttributionTracker()
        await tracker.flush_once()  # must not raise / connect

    async def test_flush_memory_fallback_when_ch_unavailable(self):
        hot = FakeHotStore(raise_exc=RuntimeError("ch down"))
        warm = FakeWarmStore()
        tracker = CostAttributionTracker(hot_store=hot, warm_store=warm)
        tracker.record("ten-a", "agent-1", 0.4)
        await tracker.flush_once()
        assert len(warm.upserts) == 1
        row = warm.upserts[0][0]
        assert row["tenant_id"] == "ten-a"
        assert row["agent_id"] == "agent-1"
        assert row["cost_usd_total"] == pytest.approx(0.4)
        assert row["day"] == datetime.now(UTC).date()

    async def test_flush_ch_recompute_wins_on_max(self):
        today = datetime.now(UTC).date()
        hot = FakeHotStore(rows=[("ten-a", "agent-1", 0.7)])
        warm = FakeWarmStore()
        tracker = CostAttributionTracker(hot_store=hot, warm_store=warm)
        tracker.record("ten-a", "agent-1", 0.4)  # memory 0.4 < CH 0.7 -> 0.7
        await tracker.flush_once()
        assert hot.queries, "CH recompute must run when spend exists"
        row = warm.upserts[0][0]
        assert row["cost_usd_total"] == pytest.approx(0.7)
        assert row["day"] == today
        # Gauge reflects the authoritative total.
        value = cost_per_tenant_usd.labels(
            tenant_id="ten-a", agent_id="agent-1"
        )._value.get()
        assert value == pytest.approx(0.7)

    async def test_flush_memory_wins_when_higher(self):
        hot = FakeHotStore(rows=[("ten-a", "agent-1", 0.3)])
        warm = FakeWarmStore()
        tracker = CostAttributionTracker(hot_store=hot, warm_store=warm)
        tracker.record("ten-a", "agent-1", 0.9)  # recorded after CH snapshot
        await tracker.flush_once()
        row = warm.upserts[0][0]
        assert row["cost_usd_total"] == pytest.approx(0.9)

    async def test_flush_writes_audit_on_change_not_on_repeat(self):
        hot = FakeHotStore()
        warm = FakeWarmStore()
        tracker = CostAttributionTracker(hot_store=hot, warm_store=warm)
        tracker.record("ten-a", "agent-1", 0.5)
        await tracker.flush_once()
        assert len(hot.audit_events) == 1
        event = hot.audit_events[0]
        assert event.action == "cost_attribution"
        assert event.resource["tenant_id"] == "ten-a"
        assert event.resource["agent_id"] == "agent-1"
        # Unchanged second flush -> no duplicate audit.
        await tracker.flush_once()
        assert len(hot.audit_events) == 1

    async def test_flush_refreshes_budget_overrides(self):
        warm = FakeWarmStore(overrides={"ten-a": 42.0})
        tracker = CostAttributionTracker(warm_store=warm)
        tracker.record("ten-a", "agent-1", 0.1)
        await tracker.flush_once()
        assert tracker.overrides == {"ten-a": 42.0}

    async def test_flush_survives_warm_failure(self):
        hot = FakeHotStore()
        warm = FakeWarmStore(raise_exc=RuntimeError("pg down"))
        tracker = CostAttributionTracker(hot_store=hot, warm_store=warm)
        tracker.record("ten-a", "agent-1", 0.2)
        await tracker.flush_once()  # must not raise
        # Audit still written from the hot store.
        assert len(hot.audit_events) == 1


# ---------------------------------------------------------------------------
# BudgetEnforcer (PP42 §3)
# ---------------------------------------------------------------------------


class TestBudgetEnforcer:
    def test_default_budget_from_env(self, monkeypatch):
        monkeypatch.setenv("TENANT_BUDGET_USD_DEFAULT", "25.0")
        tracker = CostAttributionTracker()
        enforcer = BudgetEnforcer(tracker)
        assert enforcer.budget_for("any") == 25.0

    def test_default_budget_is_10_when_unset(self):
        tracker = CostAttributionTracker()
        enforcer = BudgetEnforcer(tracker)
        assert enforcer.budget_for("any") == pytest.approx(10.0)

    def test_ok_below_80_percent(self):
        tracker = CostAttributionTracker()
        tracker.record("ten-a", "agent-1", 7.99)
        enforcer = BudgetEnforcer(tracker, default_budget_usd=10.0)
        assert enforcer.check("ten-a") == BUDGET_OK

    def test_approaching_at_80_percent(self):
        tracker = CostAttributionTracker()
        tracker.record("ten-a", "agent-1", 8.0)
        enforcer = BudgetEnforcer(tracker, default_budget_usd=10.0)
        assert enforcer.check("ten-a") == BUDGET_APPROACHING

    def test_exhausted_at_100_percent(self):
        tracker = CostAttributionTracker()
        tracker.record("ten-a", "agent-1", 10.0)
        enforcer = BudgetEnforcer(tracker, default_budget_usd=10.0)
        assert enforcer.check("ten-a") == BUDGET_EXHAUSTED

    def test_zero_budget_disables_enforcement(self):
        tracker = CostAttributionTracker()
        tracker.record("ten-a", "agent-1", 9999.0)
        enforcer = BudgetEnforcer(tracker, default_budget_usd=0.0)
        assert enforcer.check("ten-a") == BUDGET_OK

    def test_negative_budget_disables_enforcement(self):
        tracker = CostAttributionTracker()
        tracker.record("ten-a", "agent-1", 9999.0)
        enforcer = BudgetEnforcer(tracker, default_budget_usd=-1.0)
        assert enforcer.check("ten-a") == BUDGET_OK

    def test_per_tenant_override_beats_default(self):
        tracker = CostAttributionTracker()
        tracker.overrides["ten-rich"] = 1000.0
        tracker.record("ten-rich", "agent-1", 50.0)
        enforcer = BudgetEnforcer(tracker, default_budget_usd=10.0)
        assert enforcer.budget_for("ten-rich") == 1000.0
        assert enforcer.check("ten-rich") == BUDGET_OK
        # Same spend against the default budget would be exhausted.
        tracker.record("ten-poor", "agent-1", 50.0)
        assert enforcer.check("ten-poor") == BUDGET_EXHAUSTED

    def test_tenant_isolation_of_spend(self):
        tracker = CostAttributionTracker()
        tracker.record("ten-a", "agent-1", 9.5)
        enforcer = BudgetEnforcer(tracker, default_budget_usd=10.0)
        assert enforcer.check("ten-a") == BUDGET_APPROACHING
        assert enforcer.check("ten-b") == BUDGET_OK


# ---------------------------------------------------------------------------
# CostAttributionAuditEvent (PP42 §4)
# ---------------------------------------------------------------------------


class TestAuditEvent:
    def test_action_and_resource(self):
        day = date(2026, 11, 1)
        event = CostAttributionAuditEvent.for_cost_update(
            tenant_id="ten-a",
            agent_id="agent-1",
            day=day,
            cost_usd_total=1.25,
            source="clickhouse",
        )
        assert event.action == "cost_attribution"
        assert event.resource["tenant_id"] == "ten-a"
        assert event.resource["agent_id"] == "agent-1"
        assert event.resource["day"] == "2026-11-01"
        assert event.resource["cost_usd_total"] == pytest.approx(1.25)
        assert event.resource["source"] == "clickhouse"
        assert "ten-a" in event.reason

    def test_clickhouse_row_is_10_columns(self):
        event = CostAttributionAuditEvent.for_cost_update(
            tenant_id="ten-a",
            agent_id="agent-1",
            day=date(2026, 11, 1),
            cost_usd_total=2.5,
        )
        row = event.to_clickhouse_row()
        assert len(row) == 10
        audit_id, _ts, trace_id, actor, action, decision, resource, _reason, ip, ua = row
        assert audit_id.startswith("cae-")
        assert trace_id == ""
        assert action == "cost_attribution"
        assert decision == "allow"
        assert json.loads(actor)["type"] == "system"
        assert json.loads(resource)["tenant_id"] == "ten-a"
        assert ip == "127.0.0.1"
        assert ua == "agent-obs/cost-attribution"

    def test_to_dict_roundtrip(self):
        event = CostAttributionAuditEvent.for_cost_update(
            tenant_id="ten-a",
            agent_id="agent-1",
            day=date(2026, 11, 1),
            cost_usd_total=3.0,
        )
        data = event.to_dict()
        assert data["action"] == "cost_attribution"
        assert data["resource"]["cost_usd_total"] == pytest.approx(3.0)


# ---------------------------------------------------------------------------
# Proxy budget gate end-to-end (PP42 §3 + §5)
# ---------------------------------------------------------------------------


class TestProxyBudgetGate:
    def _make_proxy(self, sdk, monkeypatch, budget: str | None = None) -> LLMProxyServer:
        monkeypatch.setenv("OPENAI_API_KEY", "sk-upstream-secret")
        if budget is not None:
            monkeypatch.setenv("TENANT_BUDGET_USD_DEFAULT", budget)
        cfg = ProxyConfig(
            mode="on",
            enabled=True,
            upstream_base_url="https://api.openai.com/v1",
            upstream_api_key_env="OPENAI_API_KEY",
        )
        return LLMProxyServer(cfg, sdk=sdk)

    async def test_request_with_tenant_header_passes_under_budget(
        self, sdk, monkeypatch
    ):
        proxy = self._make_proxy(sdk, monkeypatch)
        with respx.mock:
            respx.post(UPSTREAM).mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "choices": [{"message": {"content": "ok"}}],
                        "usage": {"prompt_tokens": 3, "completion_tokens": 2},
                    },
                )
            )
            async with client_for(proxy) as client:
                resp = await client.post(
                    "/llm/v1/chat/completions",
                    json=chat_body(),
                    headers={"X-OryaObs-Tenant-Id": "ten-a"},
                )
        assert resp.status_code == 200
        span = proxy_spans(sdk)[0]
        assert span.context.tenant_id == "ten-a"
        assert span.attributes["cost.attribution.tenant_id"] == "ten-a"

    async def test_429_when_budget_approaching(self, sdk, monkeypatch):
        proxy = self._make_proxy(sdk, monkeypatch)
        # 9.0 / 10.0 = 90% -> approaching (429).
        proxy.cost_tracker.record("ten-a", "agent-1", 9.0)
        with respx.mock:
            respx.post(UPSTREAM).mock(
                return_value=httpx.Response(200, json={"choices": []})
            )
            async with client_for(proxy) as client:
                resp = await client.post(
                    "/llm/v1/chat/completions",
                    json=chat_body(),
                    headers={"X-OryaObs-Tenant-Id": "ten-a"},
                )
            upstream_calls = len(respx.calls)
        assert resp.status_code == 429
        body = resp.json()
        assert body["error"] == "budget approaching"
        assert body["hint"] == "budget approaching"
        assert body["tenant_id"] == "ten-a"
        assert resp.headers.get("Retry-After") == "60"
        # Upstream was never called.
        assert upstream_calls == 0
        # The refusal is observable as a blocked span.
        span = proxy_spans(sdk)[0]
        assert span.attributes["status"] == "blocked"
        assert span.attributes["error.type"] == "budget_approaching"
        assert span.attributes["cost.budget.decision"] == BUDGET_APPROACHING
        assert span.attributes["cost.budget.tenant_id"] == "ten-a"
        assert span.context.tenant_id == "ten-a"

    async def test_403_when_budget_exhausted(self, sdk, monkeypatch):
        proxy = self._make_proxy(sdk, monkeypatch)
        proxy.cost_tracker.record("ten-a", "agent-1", 10.0)
        with respx.mock:
            respx.post(UPSTREAM).mock(
                return_value=httpx.Response(200, json={"choices": []})
            )
            async with client_for(proxy) as client:
                resp = await client.post(
                    "/llm/v1/chat/completions",
                    json=chat_body(),
                    headers={"X-OryaObs-Tenant-Id": "ten-a"},
                )
            upstream_calls = len(respx.calls)
        assert resp.status_code == 403
        body = resp.json()
        assert body["error"] == "budget exhausted"
        assert body["hint"] == "budget exhausted"
        assert body["tenant_id"] == "ten-a"
        assert "Retry-After" not in resp.headers
        assert upstream_calls == 0
        span = proxy_spans(sdk)[0]
        assert span.attributes["status"] == "blocked"
        assert span.attributes["error.type"] == "budget_exhausted"

    async def test_other_tenant_unaffected_by_blocked_tenant(self, sdk, monkeypatch):
        proxy = self._make_proxy(sdk, monkeypatch)
        proxy.cost_tracker.record("ten-a", "agent-1", 10.0)
        with respx.mock:
            respx.post(UPSTREAM).mock(
                return_value=httpx.Response(200, json={"choices": []})
            )
            async with client_for(proxy) as client:
                resp = await client.post(
                    "/llm/v1/chat/completions",
                    json=chat_body(),
                    headers={"X-OryaObs-Tenant-Id": "ten-b"},
                )
            upstream_calls = len(respx.calls)
        assert resp.status_code == 200
        assert upstream_calls == 1

    async def test_missing_tenant_header_uses_default_bucket(self, sdk, monkeypatch):
        proxy = self._make_proxy(sdk, monkeypatch)
        # Spend against "default" blocks requests without a tenant headers.
        proxy.cost_tracker.record("default", "unknown", 10.0)
        with respx.mock:
            respx.post(UPSTREAM).mock(
                return_value=httpx.Response(200, json={"choices": []})
            )
            async with client_for(proxy) as client:
                resp = await client.post(
                    "/llm/v1/chat/completions", json=chat_body()
                )
            upstream_calls = len(respx.calls)
        assert resp.status_code == 403
        assert resp.json()["tenant_id"] == "default"
        assert upstream_calls == 0

    async def test_zero_budget_disables_gate(self, sdk, monkeypatch):
        proxy = self._make_proxy(sdk, monkeypatch, budget="0")
        proxy.cost_tracker.record("ten-a", "agent-1", 9999.0)
        with respx.mock:
            respx.post(UPSTREAM).mock(
                return_value=httpx.Response(200, json={"choices": []})
            )
            async with client_for(proxy) as client:
                resp = await client.post(
                    "/llm/v1/chat/completions",
                    json=chat_body(),
                    headers={"X-OryaObs-Tenant-Id": "ten-a"},
                )
            upstream_calls = len(respx.calls)
        assert resp.status_code == 200
        assert upstream_calls == 1

    async def test_budget_gate_reads_env_default(self, sdk, monkeypatch):
        # Budget 1.0; spend 0.85 -> 85% -> approaching.
        proxy = self._make_proxy(sdk, monkeypatch, budget="1.0")
        proxy.cost_tracker.record("ten-a", "agent-1", 0.85)
        with respx.mock:
            respx.post(UPSTREAM).mock(
                return_value=httpx.Response(200, json={"choices": []})
            )
            async with client_for(proxy) as client:
                resp = await client.post(
                    "/llm/v1/chat/completions",
                    json=chat_body(),
                    headers={"X-OryaObs-Tenant-Id": "ten-a"},
                )
        assert resp.status_code == 429
