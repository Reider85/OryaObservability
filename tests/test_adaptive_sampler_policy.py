"""Tests for PC30 (T2.7.2) — the adaptive tail-sampling policy engine.

Covers the three rule branches, Prometheus scraping (including failure
degradation), per-trace sampling decisions in the proxy, and the audit event
emitted on every rate change.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from agent_obs.guardrail.audit import (
    CPU_HIGH_THRESHOLD,
    ERROR_RATE_THRESHOLD,
    SamplerRateChangeAuditEvent,
)
from scripts.sampler.policy_engine import (
    CPU_QUERY,
    ERROR_RATE_QUERY,
    PolicyDecision,
    PolicyEngine,
    build_policy_engine_from_env,
    evaluate_rate,
)
from scripts.sampler.sampler_proxy import (
    AdaptiveSampler,
    RateFileWriter,
    SamplerProxy,
    extract_spans,
    group_spans_by_trace,
    is_interesting_span,
    start_grpc_receiver,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _otlp_span(
    trace_id: str = "t1",
    span_id: str = "s1",
    attributes: dict | None = None,
    status_code: int = 0,
) -> dict:
    """Build one OTLP span with a proper attributes array."""
    attrs = [
        {"key": key, "value": {"stringValue": value}}
        for key, value in (attributes or {}).items()
    ]
    return {
        "traceId": trace_id,
        "spanId": span_id,
        "name": "span",
        "attributes": attrs,
        "status": {"code": status_code},
    }


def _otlp_payload(*spans: dict) -> dict:
    return {
        "resourceSpans": [
            {
                "resource": {"attributes": []},
                "scopeSpans": [
                    {
                        "scope": {"name": "agent_obs"},
                        "spans": list(spans),
                    }
                ],
            }
        ]
    }


def _prom_client(cpu=None, error=None, status_code: int = 200):
    """Build an httpx.AsyncClient that answers Prometheus instant queries.

    ``cpu``/``error`` are the values to return; a list returns several series so
    the per-agent aggregation can be exercised.
    """

    def _handler(request: httpx.Request) -> httpx.Response:
        query = request.url.params.get("query", "")
        if query == CPU_QUERY:
            value = cpu
        elif query == ERROR_RATE_QUERY:
            value = error
        else:
            return httpx.Response(400, json={"status": "error", "error": "bad query"})
        if value is None:
            return httpx.Response(
                200, json={"status": "success", "data": {"result": []}}
            )
        values = value if isinstance(value, list) else [value]
        result = [
            {"metric": {}, "value": [time_now(), str(v)]} for v in values
        ]
        return httpx.Response(
            200, json={"status": "success", "data": {"resultType": "vector", "result": result}}
        )

    return httpx.AsyncClient(transport=httpx.MockTransport(_handler), base_url="http://prom")


def time_now() -> int:
    import time

    return int(time.time())


# ---------------------------------------------------------------------------
# rule evaluation
# ---------------------------------------------------------------------------


class TestPolicyRules:
    """The three branches fixed by the PC30 spec."""

    def test_cpu_high_selects_five_percent(self):
        rate, reason = evaluate_rate(cpu_ratio=0.9, error_rate_5m=0.0)
        assert rate == pytest.approx(0.05)
        assert reason == "cpu_high"

    def test_error_high_selects_thirty_percent(self):
        rate, reason = evaluate_rate(cpu_ratio=0.1, error_rate_5m=0.07)
        assert rate == pytest.approx(0.30)
        assert reason == "error_high"

    def test_defaults_select_ten_percent(self):
        rate, reason = evaluate_rate(cpu_ratio=0.5, error_rate_5m=0.02)
        assert rate == pytest.approx(0.10)
        assert reason == "default"

    def test_cpu_branch_wins_over_error_branch(self):
        """PC30 lists the cpu check first, so a saturated host drops the rate
        even when errors are also elevated."""
        rate, reason = evaluate_rate(cpu_ratio=0.9, error_rate_5m=0.07)
        assert rate == pytest.approx(0.05)
        assert reason == "cpu_high"

    @pytest.mark.parametrize(
        "cpu, expected_reason",
        [(0.79, "default"), (0.80, "default"), (0.801, "cpu_high")],
    )
    def test_cpu_threshold_is_strict(self, cpu, expected_reason):
        """Thresholds are strict >: sitting exactly on the boundary is not yet
        a reason to move the rate."""
        _, reason = evaluate_rate(cpu_ratio=cpu, error_rate_5m=0.0)
        assert reason == expected_reason

    @pytest.mark.parametrize(
        "error, expected_reason",
        [(0.049, "default"), (0.050, "default"), (0.051, "error_high")],
    )
    def test_error_threshold_is_strict(self, error, expected_reason):
        _, reason = evaluate_rate(cpu_ratio=0.0, error_rate_5m=error)
        assert reason == expected_reason

    def test_custom_thresholds_are_honoured(self):
        rate, reason = evaluate_rate(
            cpu_ratio=0.5, error_rate_5m=0.01, cpu_threshold=0.4, error_threshold=0.005
        )
        assert reason == "cpu_high"
        assert rate == pytest.approx(0.05)

    def test_reason_text_quotes_the_driving_metric(self):
        decision = PolicyDecision(
            rate=0.05,
            reason="cpu_high",
            prev_rate=0.10,
            prev_reason="default",
            cpu_ratio=0.92,
            error_rate_5m=0.01,
            changed=True,
        )
        assert "system_cpu_ratio=0.92" in decision.reason_text
        assert "cpu_high" in decision.reason_text


# ---------------------------------------------------------------------------
# Prometheus scraping
# ---------------------------------------------------------------------------


class TestPolicyEngineScraping:
    async def test_reads_both_metrics(self):
        engine = PolicyEngine(client=_prom_client(cpu=0.42, error=0.01))
        cpu, error = await engine.read_signals()
        assert cpu == pytest.approx(0.42)
        assert error == pytest.approx(0.01)
        await engine.close()

    async def test_empty_result_degrades_to_zero(self):
        """No series (pilot-agent not up yet) must not raise — it routes the
        policy to its default rate."""
        engine = PolicyEngine(client=_prom_client(cpu=None, error=None))
        assert await engine.read_signals() == (0.0, 0.0)
        await engine.close()

    async def test_error_rate_uses_worst_agent(self):
        """One unhealthy agent must be able to lift the whole sampling rate,
        so the aggregation over per-agent series is a max, not a mean."""
        engine = PolicyEngine(client=_prom_client(cpu=0.1, error=[0.01, 0.09, 0.03]))
        _, error = await engine.read_signals()
        assert error == pytest.approx(0.09)
        await engine.close()

    async def test_scrape_failure_degrades_instead_of_raising(self, caplog):
        """A Prometheus outage must not wedge the sampler."""

        def _boom(_request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        client = httpx.AsyncClient(
            transport=httpx.MockTransport(_boom), base_url="http://prom"
        )
        engine = PolicyEngine(client=client)
        assert await engine.read_signals() == (0.0, 0.0)
        await engine.close()

    async def test_http_error_status_degrades(self):
        def _fatal(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, text="unavailable")

        client = httpx.AsyncClient(
            transport=httpx.MockTransport(_fatal), base_url="http://prom"
        )
        engine = PolicyEngine(client=client)
        assert await engine.read_signals() == (0.0, 0.0)
        await engine.close()


# ---------------------------------------------------------------------------
# engine state, metrics and callbacks
# ---------------------------------------------------------------------------


class TestPolicyEngineEvaluation:
    async def test_starts_at_default_rate(self):
        engine = PolicyEngine(client=_prom_client())
        assert engine.current_rate == pytest.approx(0.10)
        assert engine.current_reason == "default"
        await engine.close()

    def test_evaluate_moves_rate_and_counts_change(self):
        from agent_obs.metrics import (
            tail_sampler_current_rate,
            tail_sampler_rate_changes_total,
        )

        engine = PolicyEngine()
        before = tail_sampler_rate_changes_total.labels(
            from_reason="default", to_reason="cpu_high"
        )._value.get()

        decision = engine.evaluate(cpu_ratio=0.92, error_rate_5m=0.01)

        assert decision.changed is True
        assert engine.current_rate == pytest.approx(0.05)
        assert engine.current_reason == "cpu_high"
        assert (
            tail_sampler_current_rate.labels(policy_reason="cpu_high")._value.get()
            == pytest.approx(0.05)
        )
        after = tail_sampler_rate_changes_total.labels(
            from_reason="default", to_reason="cpu_high"
        )._value.get()
        assert after == before + 1

    def test_evaluate_is_idempotent_for_same_inputs(self):
        engine = PolicyEngine()
        assert engine.evaluate(cpu_ratio=0.92, error_rate_5m=0.01).changed is True
        # Same inputs, same rate: the second call must not re-report a change,
        # otherwise every 30s tick would write a duplicate audit row.
        second = engine.evaluate(cpu_ratio=0.92, error_rate_5m=0.01)
        assert second.changed is False

    def test_reason_transition_counts_as_a_change(self):
        """Two rules can select the same rate; a reason flip still updates
        state so the gauge label is not stale."""
        engine = PolicyEngine()
        engine.evaluate(cpu_ratio=0.0, error_rate_5m=0.07)  # error_high, 0.30
        assert engine.current_reason == "error_high"
        # Back to default rate via a different reason path.
        decision = engine.evaluate(cpu_ratio=0.0, error_rate_5m=0.0)
        assert decision.rate == pytest.approx(0.10)
        assert engine.current_reason == "default"

    async def test_run_once_publishes_only_on_change(self):
        engine = PolicyEngine(client=_prom_client(cpu=0.9, error=0.0))
        published: list[PolicyDecision] = []

        async def callback(decision: PolicyDecision) -> None:
            published.append(decision)

        first = await engine.run_once(callback)
        assert first is not None
        assert first.rate == pytest.approx(0.05)
        assert len(published) == 1

        # Second tick with the same signals must be a no-op.
        assert await engine.run_once(callback) is None
        assert len(published) == 1
        await engine.close()

    async def test_run_loop_stops_on_event(self):
        engine = PolicyEngine(client=_prom_client(), poll_interval=30.0)
        stop = asyncio.Event()
        task = asyncio.create_task(engine.run(stop_event=stop))
        await asyncio.sleep(0.05)
        stop.set()
        await asyncio.wait_for(task, timeout=2.0)
        await engine.close()

    async def test_run_loop_survives_a_failing_tick(self):
        """A transient failure must not permanently freeze the rate: the next
        tick has to still be evaluated and able to move the rate."""
        calls = {"n": 0}

        async def flaky() -> tuple[float, float]:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("prometheus down")
            return 0.95, 0.0

        engine = PolicyEngine(client=_prom_client(), poll_interval=0.05)
        engine.read_signals = flaky  # type: ignore[assignment]
        stop = asyncio.Event()
        task = asyncio.create_task(engine.run(stop_event=stop))
        for _ in range(40):
            await asyncio.sleep(0.05)
            if engine.current_rate == pytest.approx(0.05):
                break
        stop.set()
        await asyncio.wait_for(task, timeout=2.0)
        assert engine.current_rate == pytest.approx(0.05)
        await engine.close()


# ---------------------------------------------------------------------------
# per-trace sampling in the proxy
# ---------------------------------------------------------------------------


class TestAlwaysKeepTraces:
    def test_error_status_is_always_kept(self):
        assert is_interesting_span(_otlp_span(status_code=2)) is True

    def test_error_status_by_name_is_kept(self):
        span = _otlp_span()
        span["status"] = {"code": "STATUS_CODE_ERROR"}
        assert is_interesting_span(span) is True

    def test_cost_over_budget_is_kept(self):
        span = _otlp_span(attributes={"cost.over_budget": "true"})
        assert is_interesting_span(span) is True

    def test_security_incident_is_kept(self):
        span = _otlp_span(attributes={"security.incident": "true"})
        assert is_interesting_span(span) is True

    def test_bool_valued_attributes_are_kept(self):
        span = _otlp_span()
        span["attributes"] = [
            {"key": "security.incident", "value": {"boolValue": True}}
        ]
        assert is_interesting_span(span) is True

    def test_false_valued_attributes_are_not_interesting(self):
        span = _otlp_span(
            attributes={"cost.over_budget": "false", "security.incident": "false"}
        )
        assert is_interesting_span(span) is False

    def test_normal_span_is_not_interesting(self):
        assert is_interesting_span(_otlp_span()) is False


class TestAdaptiveSamplerDecisions:
    def test_full_rate_keeps_everything(self):
        sampler = AdaptiveSampler(initial_rate=1.0)
        assert sampler.decide_trace("abc", [_otlp_span()]) == "kept_probabilistic"

    def test_zero_rate_drops_normal_traces(self):
        sampler = AdaptiveSampler(initial_rate=0.0)
        assert sampler.decide_trace("abc", [_otlp_span()]) == "dropped"

    def test_zero_rate_still_keeps_interesting_traces(self):
        sampler = AdaptiveSampler(initial_rate=0.0)
        trace = [_otlp_span(status_code=2)]
        assert sampler.decide_trace("abc", trace) == "kept_interesting"

    def test_decision_is_deterministic_per_trace_id(self):
        """A retried batch must reach the same verdict — hash the trace id
        rather than drawing a random number."""
        sampler = AdaptiveSampler(initial_rate=0.5)
        first = [sampler.decide_trace("trace-xyz", [_otlp_span()]) for _ in range(20)]
        assert len(set(first)) == 1

    def test_rate_change_takes_effect_immediately(self):
        sampler = AdaptiveSampler(initial_rate=1.0)
        assert sampler.decide_trace("t", [_otlp_span()]) == "kept_probabilistic"
        sampler.set_rate(0.0, "cpu_high")
        assert sampler.decide_trace("t", [_otlp_span()]) == "dropped"
        sampler.set_rate(1.0, "default")
        assert sampler.decide_trace("t", [_otlp_span()]) == "kept_probabilistic"

    def test_set_rate_ignores_no_op_update(self):
        sampler = AdaptiveSampler(initial_rate=0.10, reason="default")
        changed_at = sampler.status()["last_changed_at"]
        sampler.set_rate(0.10, "default")
        assert sampler.status()["last_changed_at"] == changed_at

    def test_status_reports_current_policy(self):
        sampler = AdaptiveSampler()
        sampler.set_rate(0.30, "error_high")
        status = sampler.status()
        assert status["current_rate"] == pytest.approx(0.30)
        assert status["policy_reason"] == "error_high"
        assert status["last_change_reason"] == "error_high"


class TestPayloadSampling:
    def test_extract_spans_flattens_nesting(self):
        payload = _otlp_payload(_otlp_span("t1", "a"), _otlp_span("t1", "b"))
        assert len(extract_spans(payload)) == 2

    def test_extract_spans_tolerates_malformed_entries(self):
        payload = {
            "resourceSpans": [
                {"scopeSpans": [{"spans": [_otlp_span(), "not-a-dict", 42]}]},
                "not-a-dict",
            ]
        }
        assert len(extract_spans(payload)) == 1

    def test_group_spans_by_trace(self):
        spans = [_otlp_span("t1", "a"), _otlp_span("t2", "b"), _otlp_span("t1", "c")]
        grouped = group_spans_by_trace(spans)
        assert set(grouped) == {"t1", "t2"}
        assert len(grouped["t1"]) == 2

    def test_interesting_trace_keeps_all_its_spans(self):
        """Dropping a root span while keeping its child would leave an orphan,
        so the decision is taken per trace and applied to every span."""
        proxy = SamplerProxy(
            downstream_url="http://langfuse:3000", sampler=AdaptiveSampler(0.0)
        )
        payload = _otlp_payload(
            _otlp_span("t1", "root"),
            _otlp_span("t1", "child", status_code=2),
        )
        filtered, counts = proxy.sample_payload(payload)
        assert counts == {"kept_interesting": 1, "kept_probabilistic": 0, "dropped": 0}
        assert len(extract_spans(filtered)) == 2

    def test_full_drop_yields_empty_payload(self):
        proxy = SamplerProxy(
            downstream_url="http://langfuse:3000", sampler=AdaptiveSampler(0.0)
        )
        filtered, counts = proxy.sample_payload(_otlp_payload(_otlp_span("t1")))
        assert counts["dropped"] == 1
        assert filtered == {"resourceSpans": []}

    def test_mixed_batch_keeps_only_survivors(self):
        proxy = SamplerProxy(
            downstream_url="http://langfuse:3000", sampler=AdaptiveSampler(0.0)
        )
        payload = _otlp_payload(
            _otlp_span("t-drop", "a"),
            _otlp_span("t-err", "b", status_code=2),
            _otlp_span("t-budget", "c", attributes={"cost.over_budget": "true"}),
        )
        filtered, counts = proxy.sample_payload(payload)
        assert counts == {"kept_interesting": 2, "kept_probabilistic": 0, "dropped": 1}
        kept_ids = {span["traceId"] for span in extract_spans(filtered)}
        assert kept_ids == {"t-err", "t-budget"}

    def test_filtered_payload_preserves_resource_and_scope(self):
        proxy = SamplerProxy(
            downstream_url="http://langfuse:3000", sampler=AdaptiveSampler(1.0)
        )
        payload = _otlp_payload(_otlp_span("t1"))
        filtered, _ = proxy.sample_payload(payload)
        resource_span = filtered["resourceSpans"][0]
        assert "resource" in resource_span
        assert resource_span["scopeSpans"][0]["scope"] == {"name": "agent_obs"}

    def test_handle_payload_forwards_only_kept_traces(self):
        posted: list[dict] = []

        def _capture(request: httpx.Request) -> httpx.Response:
            posted.append(json.loads(request.content))
            return httpx.Response(200, json={})

        client = httpx.Client(transport=httpx.MockTransport(_capture))
        proxy = SamplerProxy(
            downstream_url="http://langfuse:3000",
            sampler=AdaptiveSampler(0.0),
            client=client,
        )
        proxy.handle_payload(
            _otlp_payload(_otlp_span("t-drop", "a"), _otlp_span("t-err", "b", status_code=2))
        )
        assert len(posted) == 1
        kept = {s["traceId"] for s in extract_spans(posted[0])}
        assert kept == {"t-err"}
        assert client is proxy._client
        # Caller-owned client must not be closed by the proxy.
        proxy.close()
        assert not client.is_closed

    def test_empty_batch_does_not_call_downstream(self):
        calls = {"n": 0}

        def _capture(_request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(200, json={})

        client = httpx.Client(transport=httpx.MockTransport(_capture))
        proxy = SamplerProxy(
            downstream_url="http://langfuse:3000",
            sampler=AdaptiveSampler(0.0),
            client=client,
        )
        proxy.handle_payload(_otlp_payload(_otlp_span("t1")))
        assert calls["n"] == 0

    def test_basic_auth_header_is_attached(self):
        seen: list[httpx.Headers] = []

        def _capture(request: httpx.Request) -> httpx.Response:
            seen.append(request.headers)
            return httpx.Response(200, json={})

        client = httpx.Client(transport=httpx.MockTransport(_capture))
        proxy = SamplerProxy(
            downstream_url="http://langfuse:3000",
            public_key="pk-test",
            secret_key="sk-test",
            sampler=AdaptiveSampler(1.0),
            client=client,
        )
        proxy.handle_payload(_otlp_payload(_otlp_span("t1")))
        assert seen[0]["authorization"].startswith("Basic ")

    def test_4xx_is_not_retried(self):
        calls = {"n": 0}

        def _reject(_request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(400, text="bad request")

        client = httpx.Client(transport=httpx.MockTransport(_reject))
        proxy = SamplerProxy(
            downstream_url="http://langfuse:3000",
            sampler=AdaptiveSampler(1.0),
            client=client,
            max_retries=3,
        )
        assert proxy.forward(_otlp_payload(_otlp_span())) is False
        assert calls["n"] == 1

    def test_5xx_is_retried_then_succeeds(self):
        calls = {"n": 0}

        def _flaky(_request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] < 3:
                return httpx.Response(503, text="unavailable")
            return httpx.Response(200, json={})

        client = httpx.Client(transport=httpx.MockTransport(_flaky))
        proxy = SamplerProxy(
            downstream_url="http://langfuse:3000",
            sampler=AdaptiveSampler(1.0),
            client=client,
            max_retries=3,
        )
        assert proxy.forward(_otlp_payload(_otlp_span())) is True
        assert calls["n"] == 3


# ---------------------------------------------------------------------------
# audit trail
# ---------------------------------------------------------------------------


class TestSamplerRateChangeAudit:
    def test_factory_populates_every_field(self):
        event = SamplerRateChangeAuditEvent.for_rate_change(
            prev_rate=0.10,
            new_rate=0.05,
            prev_reason="default",
            new_reason="cpu_high",
            system_cpu_ratio=0.92,
            agent_error_rate_5m=0.01,
        )
        assert event.audit_id.startswith("sra-")
        assert event.action == "sampler.rate_change"
        assert event.decision == "applied"
        assert event.actor == {"type": "system", "id": "policy-engine"}
        assert event.resource == {"type": "sampler_policy", "id": "normal-trace"}
        assert event.prev_rate == pytest.approx(0.10)
        assert event.new_rate == pytest.approx(0.05)
        assert event.prev_reason == "default"
        assert event.new_reason == "cpu_high"
        assert event.system_cpu_ratio == pytest.approx(0.92)
        assert event.agent_error_rate_5m == pytest.approx(0.01)
        # Not trace-scoped.
        assert event.trace_id == ""

    def test_reason_text_is_self_contained(self):
        """PC30 requires the reason to be legible without cross-referencing."""
        event = SamplerRateChangeAuditEvent.for_rate_change(
            prev_rate=0.10,
            new_rate=0.05,
            prev_reason="default",
            new_reason="cpu_high",
            system_cpu_ratio=0.92,
            agent_error_rate_5m=0.01,
        )
        assert "0.10 to 0.05" in event.reason
        assert "cpu_high" in event.reason
        assert "0.92" in event.reason

    def test_error_high_reason_quotes_error_metric(self):
        event = SamplerRateChangeAuditEvent.for_rate_change(
            prev_rate=0.10,
            new_rate=0.30,
            prev_reason="default",
            new_reason="error_high",
            system_cpu_ratio=0.10,
            agent_error_rate_5m=0.07,
        )
        assert "agent_error_rate_5m=0.070" in event.reason
        assert f"{ERROR_RATE_THRESHOLD:.3f}" in event.reason

    def test_default_reason_quotes_both_metrics(self):
        event = SamplerRateChangeAuditEvent.for_rate_change(
            prev_rate=0.05,
            new_rate=0.10,
            prev_reason="cpu_high",
            new_reason="default",
            system_cpu_ratio=0.20,
            agent_error_rate_5m=0.01,
        )
        assert "default" in event.reason
        assert f"{CPU_HIGH_THRESHOLD:.2f}" in event.reason

    def test_to_dict_flattens_rate_metadata(self):
        """PC31's query API reads these fields directly."""
        event = SamplerRateChangeAuditEvent.for_rate_change(
            prev_rate=0.10,
            new_rate=0.30,
            prev_reason="default",
            new_reason="error_high",
            system_cpu_ratio=0.10,
            agent_error_rate_5m=0.07,
        )
        data = event.to_dict()
        assert data["prev_rate"] == pytest.approx(0.10)
        assert data["new_rate"] == pytest.approx(0.30)
        assert data["new_reason"] == "error_high"
        assert data["action"] == "sampler.rate_change"

    def test_clickhouse_row_matches_audit_events_hot_columns(self):
        event = SamplerRateChangeAuditEvent.for_rate_change(
            prev_rate=0.10,
            new_rate=0.05,
            prev_reason="default",
            new_reason="cpu_high",
            system_cpu_ratio=0.92,
            agent_error_rate_5m=0.01,
        )
        row = event.to_clickhouse_row()
        # (audit_id, timestamp, trace_id, actor, action, decision, resource,
        #  reason, ip_address, user_agent)
        assert len(row) == 10
        assert row[0] == event.audit_id
        assert row[2] == ""
        assert json.loads(row[3]) == event.actor
        assert row[4] == "sampler.rate_change"
        assert row[5] == "applied"
        assert row[8] == "unknown"
        assert row[9] == "policy-engine"

    def test_clickhouse_row_carries_rates_in_resource_json(self):
        """The DDL has no rate columns, so the numbers ride in resource JSON —
        no migration needed for PC30."""
        event = SamplerRateChangeAuditEvent.for_rate_change(
            prev_rate=0.10,
            new_rate=0.05,
            prev_reason="default",
            new_reason="cpu_high",
            system_cpu_ratio=0.92,
            agent_error_rate_5m=0.01,
        )
        resource = json.loads(event.to_clickhouse_row()[6])
        assert resource["type"] == "sampler_policy"
        assert resource["id"] == "normal-trace"
        assert resource["prev_rate"] == pytest.approx(0.10)
        assert resource["new_rate"] == pytest.approx(0.05)
        assert resource["new_reason"] == "cpu_high"
        assert resource["system_cpu_ratio"] == pytest.approx(0.92)

    def test_engine_builds_event_from_decision(self):
        engine = PolicyEngine()
        decision = engine.evaluate(cpu_ratio=0.92, error_rate_5m=0.01)
        event = engine.build_audit_event(decision)
        assert event.prev_rate == pytest.approx(0.10)
        assert event.new_rate == pytest.approx(0.05)
        assert event.new_reason == "cpu_high"

    def test_audit_write_failure_does_not_break_the_policy(self):
        """A failing audit sink must not stop the rate from being applied."""

        class _BrokenStore:
            def write_audit_event(self, event):
                raise RuntimeError("clickhouse down")

        engine = PolicyEngine(hot_store=_BrokenStore())
        decision = engine.evaluate(cpu_ratio=0.92, error_rate_5m=0.01)
        engine.record_audit(decision)  # must not raise
        assert engine.current_rate == pytest.approx(0.05)

    def test_audit_is_written_to_hot_store_on_change(self):
        written: list = []

        class _Store:
            def write_audit_event(self, event):
                written.append(event)

        engine = PolicyEngine(hot_store=_Store())
        engine.record_audit(engine.evaluate(cpu_ratio=0.92, error_rate_5m=0.01))
        assert len(written) == 1
        assert written[0].new_reason == "cpu_high"

    def test_audit_skipped_without_hot_store(self):
        engine = PolicyEngine(hot_store=None)
        decision = engine.evaluate(cpu_ratio=0.92, error_rate_5m=0.01)
        engine.record_audit(decision)  # no-op, must not raise

    def test_audit_disabled_by_flag(self):
        written: list = []

        class _Store:
            def write_audit_event(self, event):
                written.append(event)

        engine = PolicyEngine(hot_store=_Store(), enable_audit=False)
        engine.record_audit(engine.evaluate(cpu_ratio=0.92, error_rate_5m=0.01))
        assert written == []


# ---------------------------------------------------------------------------
# file mode
# ---------------------------------------------------------------------------


class TestRateFileWriter:
    def test_writes_rate_and_reason(self, tmp_path):
        path = tmp_path / "nested" / "sampler_rate.json"
        writer = RateFileWriter(str(path))
        writer.write(0.05, "cpu_high")
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["rate"] == pytest.approx(0.05)
        assert payload["reason"] == "cpu_high"
        assert payload["updated_at"] > 0

    def test_write_is_atomic(self, tmp_path):
        """No temp file may survive a successful write, and a reader must never
        see a truncated value."""
        path = tmp_path / "sampler_rate.json"
        writer = RateFileWriter(str(path))
        for index in range(5):
            writer.write(index / 100.0, "default")
        assert json.loads(path.read_text(encoding="utf-8"))["rate"] == pytest.approx(0.04)
        assert list(tmp_path.glob("*.tmp")) == []


class TestEngineToSamplerWiring:
    async def test_rate_change_reaches_sampler_and_file(self, tmp_path):
        """The end-to-end wiring: engine decision → sampler rate → rate file."""
        path = tmp_path / "sampler_rate.json"
        rate_file = RateFileWriter(str(path))
        sampler = AdaptiveSampler()
        engine = PolicyEngine(client=_prom_client(cpu=0.9, error=0.0))

        published: list[PolicyDecision] = []

        async def on_change(decision: PolicyDecision) -> None:
            published.append(decision)
            sampler.set_rate(decision.rate, decision.reason)
            rate_file.write(decision.rate, decision.reason)

        await engine.run_once(on_change)

        assert sampler.current_rate == pytest.approx(0.05)
        assert sampler.current_reason == "cpu_high"
        assert json.loads(path.read_text(encoding="utf-8"))["rate"] == pytest.approx(0.05)
        assert len(published) == 1
        await engine.close()


class TestEnvWiring:
    """Regression cover for the audit sink being silently absent.

    Found during a live run against docker compose: build_policy_engine_from_env()
    never passed hot_store, so PolicyEngine.record_audit() returned early on
    every rate change and audit_events_hot stayed empty. record_audit() skips
    quietly when hot_store is None, so unit tests on PolicyEngine alone stayed
    green while the deployed proxy wrote no audit rows at all.
    """

    def test_build_from_env_attaches_hot_store_by_default(self, monkeypatch):
        monkeypatch.delenv("AGENT_OBS_SAMPLER_AUDIT_ENABLED", raising=False)
        monkeypatch.setattr(
            "scripts.sampler.policy_engine._build_hot_store_from_env",
            lambda: object(),
        )
        engine = build_policy_engine_from_env()
        assert engine.hot_store is not None
        assert engine.enable_audit is True

    def test_build_from_env_respects_audit_disable_flag(self, monkeypatch):
        monkeypatch.setenv("AGENT_OBS_SAMPLER_AUDIT_ENABLED", "0")
        engine = build_policy_engine_from_env()
        assert engine.hot_store is None
        assert engine.enable_audit is False

    def test_unusable_clickhouse_degrades_to_no_audit(self, monkeypatch):
        """A dead ClickHouse must not stop the proxy from sampling."""
        monkeypatch.delenv("AGENT_OBS_SAMPLER_AUDIT_ENABLED", raising=False)

        def _boom():
            raise RuntimeError("clickhouse unreachable")

        monkeypatch.setattr(
            "scripts.sampler.policy_engine._build_hot_store_from_env", _boom
        )
        engine = build_policy_engine_from_env()
        assert engine.hot_store is None
        # The engine still works: sampling is the priority, audit is not.
        assert engine.evaluate(cpu_ratio=0.92, error_rate_5m=0.01).rate == (
            pytest.approx(0.05)
        )

    def test_hot_store_builder_never_raises(self, monkeypatch):
        """_build_hot_store_from_env swallows import/connection errors."""
        import scripts.sampler.policy_engine as pe

        # No ClickHouse env at all: HotStore() construction must still return
        # cleanly (either a store or None), never an exception.
        for key in ("CLICKHOUSE_HOST", "CLICKHOUSE_PORT", "CLICKHOUSE_DB"):
            monkeypatch.delenv(key, raising=False)
        result = pe._build_hot_store_from_env()
        assert result is None or hasattr(result, "write_audit_event")


class TestGrpcReceiver:
    """The optional OTLP/gRPC path.

    Found during a live run: start_grpc_receiver() imported only
    trace_service_pb2_grpc but returned trace_service_pb2.ExportTraceServiceResponse(),
    so every gRPC Export call raised NameError. The HTTP-only image cannot catch
    this -- the import is lazy and no default test imports grpc -- so it needs
    its own coverage.
    """

    def test_missing_grpc_deps_degrade_without_raising(self, monkeypatch):
        """A base image without grpcio must log and return, not crash."""
        import scripts.sampler.sampler_proxy as sp

        real_import = __builtins__["__import__"] if isinstance(
            __builtins__, dict
        ) else __builtins__.__import__

        def _fake_import(name, *args, **kwargs):
            if name.startswith("opentelemetry.proto.collector.trace"):
                raise ImportError("no grpc")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr("builtins.__import__", _fake_import)
        # Must return None quietly rather than raise.
        assert start_grpc_receiver(
            SamplerProxy("http://downstream:3000"), 4320
        ) is None

    def test_export_response_type_is_importable(self):
        """The response class must actually be bound in the module namespace."""
        pytest.importorskip("grpc")
        pb2 = pytest.importorskip(
            "opentelemetry.proto.collector.trace.v1.trace_service_pb2"
        )
        # If the lazy import inside start_grpc_receiver is wrong, this is the
        # name that goes missing at request time.
        assert hasattr(pb2, "ExportTraceServiceResponse")

    def test_receiver_returns_server_so_listener_survives(self):
        """The server must be returned, not dropped on the floor.

        grpc.Server keeps its listener alive only while Python holds a
        reference. Returning None left the last reference to the function
        frame, so CPython freed the server immediately and every subsequent
        connection was refused -- while the log still said "listening".
        """
        pytest.importorskip("grpc")
        from concurrent import futures

        import grpc

        fake = grpc.server(futures.ThreadPoolExecutor(max_workers=1))
        started = {}
        fake.start = lambda: started.setdefault("yes", True)

        import scripts.sampler.sampler_proxy as sp

        captured = {}
        real_server_fn = grpc.server
        try:
            grpc.server = lambda *a, **k: fake
            sp.grpc = grpc
            result = sp.start_grpc_receiver(
                SamplerProxy("http://downstream:3000"), 14399
            )
        finally:
            grpc.server = real_server_fn

        captured["server"] = result
        assert captured["server"] is fake
        assert started, "server.start() was not called"
