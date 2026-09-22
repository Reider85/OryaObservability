"""Tests for LangfuseExporter (P17)."""

import asyncio
import base64
import json
from pathlib import Path

import httpx
import pytest
import respx

from agent_obs.exporters.langfuse_exporter import (
    ConfigError,
    LangfuseExporter,
    _batch_to_otlp,
    _retry_delay,
    _to_hex,
)
from agent_obs.exporters.stdout_exporter import StdoutExporter
from agent_obs.metrics import exporter_errors_total
from agent_obs.observability import Span, SpanContext, SpanType, _now


def _make_span(
    name: str = "llm.call:gpt-4o",
    span_type: SpanType = SpanType.LLM_CALL,
    agent_id: str = "test-agent",
    attributes: dict | None = None,
) -> Span:
    ctx = SpanContext.new(agent_id=agent_id, agent_version="0.1.0")
    return Span(
        name=name,
        span_type=span_type,
        context=ctx,
        start_time=_now(),
        end_time=_now(),
        attributes=attributes or {"status": "ok", "llm.model": "gpt-4o"},
    )


class TestOtlpConversion:
    def test_span_to_otlp_has_required_fields(self) -> None:
        span = _make_span()
        payload = _batch_to_otlp([span])

        assert "resourceSpans" in payload
        resource_spans = payload["resourceSpans"]
        assert len(resource_spans) == 1

        scope_spans = resource_spans[0]["scopeSpans"]
        assert len(scope_spans) == 1
        assert scope_spans[0]["scope"]["name"] == "agent_obs"

        otlp_span = scope_spans[0]["spans"][0]
        assert "traceId" in otlp_span
        assert "spanId" in otlp_span
        assert "name" in otlp_span
        assert "kind" in otlp_span
        assert "startTimeUnixNano" in otlp_span
        assert "endTimeUnixNano" in otlp_span
        assert "attributes" in otlp_span
        assert "events" in otlp_span
        assert "status" in otlp_span

    def test_hex_conversion(self) -> None:
        # ULID "0000000000" (timestamp=0) should convert correctly
        result = _to_hex("00000000000000000000000000")
        assert isinstance(result, str)
        assert all(c in "0123456789abcdef" for c in result)

    def test_attributes_are_kv_array(self) -> None:
        span = _make_span(attributes={"llm.model": "gpt-4o", "tokens.input": 100})
        payload = _batch_to_otlp([span])
        attrs = payload["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["attributes"]

        assert isinstance(attrs, list)
        keys = {a["key"] for a in attrs}
        assert "llm.model" in keys
        assert "tokens.input" in keys

        for attr in attrs:
            assert "key" in attr
            assert "value" in attr
            assert isinstance(attr["value"], dict)

    def test_resource_attributes_include_agent_info(self) -> None:
        span = _make_span(agent_id="my-agent")
        payload = _batch_to_otlp([span])
        resource_attrs = payload["resourceSpans"][0]["resource"]["attributes"]

        keys = {a["key"] for a in resource_attrs}
        assert "service.name" in keys

    def test_ids_are_padded_to_otlp_length(self) -> None:
        span = _make_span()
        otlp_span = _batch_to_otlp([span])["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        assert len(otlp_span["traceId"]) == 32
        assert len(otlp_span["spanId"]) == 16
        assert otlp_span["parentSpanId"] == ""

    def test_gen_ai_semconv_attributes_added(self) -> None:
        span = _make_span(
            attributes={
                "llm.model": "gpt-4o",
                "cost.usd": 0.005,
                "tokens.input": 100,
            }
        )
        otlp_span = _batch_to_otlp([span])["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        keys = {a["key"] for a in otlp_span["attributes"]}
        assert "gen_ai.operation.name" in keys
        assert "gen_ai.request.model" in keys
        assert "gen_ai.usage.cost" in keys
        assert "gen_ai.usage.input_tokens" in keys

    def test_span_events_mirrored_to_attribute(self) -> None:
        span = _make_span()
        span.add_event("llm.call.started")
        span.add_event("llm.call.completed")
        otlp_span = _batch_to_otlp([span])["resourceSpans"][0]["scopeSpans"][0]["spans"][0]

        attrs = {a["key"]: a["value"] for a in otlp_span["attributes"]}
        assert "agent_obs.events" in attrs
        names = [v["stringValue"] for v in attrs["agent_obs.events"]["arrayValue"]["values"]]
        assert names == ["llm.call.started", "llm.call.completed"]

    def test_no_events_no_mirror_attribute(self) -> None:
        span = _make_span()
        otlp_span = _batch_to_otlp([span])["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        keys = {a["key"] for a in otlp_span["attributes"]}
        assert "agent_obs.events" not in keys

    def test_gen_ai_usage_attributes_added(self) -> None:
        span = _make_span(
            attributes={
                "status": "ok",
                "tokens.input": 100,
                "tokens.output": 50,
            }
        )
        otlp_span = _batch_to_otlp([span])["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        keys = {a["key"] for a in otlp_span["attributes"]}
        assert "gen_ai.usage.input_tokens" in keys
        assert "gen_ai.usage.output_tokens" in keys
        assert "tokens.input" in keys


class TestLangfuseExporter:
    @respx.mock
    async def test_successful_export(self) -> None:
        respx.post("http://localhost:3000/api/public/otel/v1/traces").mock(
            return_value=httpx.Response(200)
        )

        exporter = LangfuseExporter(
            endpoint="http://localhost:3000",
            public_key="pk-test",
            secret_key="sk-test",
        )
        try:
            spans = [_make_span() for _ in range(5)]
            await exporter.export(spans)
            assert respx.calls.call_count == 1
        finally:
            await exporter.close()

    @respx.mock
    async def test_basic_auth_header(self) -> None:
        route = respx.post(
            "http://localhost:3000/api/public/otel/v1/traces"
        ).mock(return_value=httpx.Response(200))

        exporter = LangfuseExporter(
            endpoint="http://localhost:3000",
            public_key="pk-test",
            secret_key="sk-test",
        )
        try:
            await exporter.export([_make_span()])

            request = route.calls[0].request
            auth_header = request.headers.get("Authorization", "")
            expected = base64.b64encode(b"pk-test:sk-test").decode()
            assert auth_header == f"Basic {expected}"
        finally:
            await exporter.close()

    @respx.mock
    async def test_retry_on_500(self) -> None:
        route = respx.post(
            "http://localhost:3000/api/public/otel/v1/traces"
        ).mock(
            side_effect=[
                httpx.Response(500),
                httpx.Response(500),
                httpx.Response(200),
            ]
        )

        exporter = LangfuseExporter(
            endpoint="http://localhost:3000",
            public_key="pk-test",
            secret_key="sk-test",
            max_retries=3,
        )
        try:
            await exporter.export([_make_span()])
            # Should have retried and succeeded
            assert route.calls.call_count == 3
        finally:
            await exporter.close()

    @respx.mock
    async def test_retry_on_429(self) -> None:
        route = respx.post(
            "http://localhost:3000/api/public/otel/v1/traces"
        ).mock(
            side_effect=[
                httpx.Response(429),
                httpx.Response(200),
            ]
        )

        exporter = LangfuseExporter(
            endpoint="http://localhost:3000",
            public_key="pk-test",
            secret_key="sk-test",
            max_retries=3,
        )
        try:
            await exporter.export([_make_span()])
            assert route.calls.call_count == 2
        finally:
            await exporter.close()

    @respx.mock
    async def test_no_retry_on_4xx_except_429(self) -> None:
        initial = exporter_errors_total.labels(
            exporter="langfuse", status_code="400"
        )._value.get()

        respx.post(
            "http://localhost:3000/api/public/otel/v1/traces"
        ).mock(return_value=httpx.Response(400))

        exporter = LangfuseExporter(
            endpoint="http://localhost:3000",
            public_key="pk-test",
            secret_key="sk-test",
            max_retries=3,
        )
        try:
            await exporter.export([_make_span()])
            after = exporter_errors_total.labels(
                exporter="langfuse", status_code="400"
            )._value.get()
            assert after == initial + 1
        finally:
            await exporter.close()

    @respx.mock
    async def test_exhausted_retries_increment_metric(self) -> None:
        initial = exporter_errors_total.labels(
            exporter="langfuse", status_code="retry_exhausted"
        )._value.get()

        respx.post(
            "http://localhost:3000/api/public/otel/v1/traces"
        ).mock(return_value=httpx.Response(500))

        exporter = LangfuseExporter(
            endpoint="http://localhost:3000",
            public_key="pk-test",
            secret_key="sk-test",
            max_retries=2,
        )
        try:
            await exporter.export([_make_span()])
            after = exporter_errors_total.labels(
                exporter="langfuse", status_code="retry_exhausted"
            )._value.get()
            assert after == initial + 1
        finally:
            await exporter.close()

    @respx.mock
    async def test_network_error_retries(self) -> None:
        route = respx.post(
            "http://localhost:3000/api/public/otel/v1/traces"
        ).mock(
            side_effect=[
                httpx.ConnectError("connection refused"),
                httpx.Response(200),
            ]
        )

        exporter = LangfuseExporter(
            endpoint="http://localhost:3000",
            public_key="pk-test",
            secret_key="sk-test",
            max_retries=3,
        )
        try:
            await exporter.export([_make_span()])
            assert route.calls.call_count == 2
        finally:
            await exporter.close()

    async def test_empty_batch_is_noop(self) -> None:
        exporter = LangfuseExporter(
            endpoint="http://localhost:3000",
            public_key="pk-test",
            secret_key="sk-test",
        )
        # Should not raise or make any requests
        await exporter.export([])
        await exporter.close()

    async def test_flush_is_idempotent(self) -> None:
        exporter = LangfuseExporter(
            endpoint="http://localhost:3000",
            public_key="pk-test",
            secret_key="sk-test",
        )
        await exporter.flush()
        await exporter.flush()
        await exporter.close()

    @respx.mock
    async def test_flush_delivers_pending_after_exhausted_retries(self) -> None:
        route = respx.post(
            "http://localhost:3000/api/public/otel/v1/traces"
        ).mock(
            side_effect=lambda req: (
                httpx.Response(500)
                if len(route.calls) < 2
                else httpx.Response(200)
            )
        )

        exporter = LangfuseExporter(
            endpoint="http://localhost:3000",
            public_key="pk-test",
            secret_key="sk-test",
            max_retries=2,
        )
        try:
            await exporter.export([_make_span()])
            assert len(exporter._pending) == 1

            await exporter.flush()
            assert len(exporter._pending) == 0
            assert len(route.calls) == 3
        finally:
            await exporter.close()

    @respx.mock
    async def test_flush_unavailable_backend_does_not_raise(self) -> None:
        respx.post(
            "http://localhost:3000/api/public/otel/v1/traces"
        ).mock(return_value=httpx.Response(503))

        exporter = LangfuseExporter(
            endpoint="http://localhost:3000",
            public_key="pk-test",
            secret_key="sk-test",
            max_retries=1,
        )
        try:
            await exporter.export([_make_span()])
            assert len(exporter._pending) == 1

            # Flush must finish without raising even if backend is down.
            await exporter.flush()
            assert len(exporter._pending) == 0
        finally:
            await exporter.close()


class TestConfigAndTls:
    def test_endpoint_without_scheme_raises(self) -> None:
        with pytest.raises(ConfigError):
            LangfuseExporter(
                endpoint="localhost:3000",
                public_key="pk",
                secret_key="sk",
            )

    async def test_tls_verify_enabled_by_default(self) -> None:
        exporter = LangfuseExporter(
            endpoint="https://langfuse.example.com",
            public_key="pk-test",
            secret_key="sk-test",
        )
        try:
            # No verify=False anywhere: _tls_ca is None and no disable option
            # exists on the exporter.
            assert exporter._tls_ca is None
        finally:
            await exporter.close()

    async def test_tls_ca_path_is_used_for_verification(self) -> None:
        ca_path = Path(__file__).parent / "fixtures" / "ca.pem"
        exporter = LangfuseExporter(
            endpoint="https://langfuse.example.com",
            public_key="pk-test",
            secret_key="sk-test",
            tls_ca=str(ca_path),
        )
        try:
            assert exporter._tls_ca == str(ca_path)
        finally:
            await exporter.close()

    async def test_from_env_reads_credentials(self) -> None:
        exporter = LangfuseExporter.from_env(
            {
                "LANGFUSE_HOST": "http://localhost:3000",
                "LANGFUSE_PUBLIC_KEY": "pk-env",
                "LANGFUSE_SECRET_KEY": "sk-env",
            }
        )
        try:
            assert isinstance(exporter, LangfuseExporter)
            assert exporter._endpoint == "http://localhost:3000"
        finally:
            await exporter.close()

    async def test_from_env_reads_tls_ca(self) -> None:
        ca_path = Path(__file__).parent / "fixtures" / "ca.pem"
        exporter = LangfuseExporter.from_env(
            {
                "LANGFUSE_HOST": "https://langfuse.example.com",
                "LANGFUSE_PUBLIC_KEY": "pk-env",
                "LANGFUSE_SECRET_KEY": "sk-env",
                "AGENT_OBS_TLS_CA": str(ca_path),
            }
        )
        try:
            assert isinstance(exporter, LangfuseExporter)
            assert exporter._tls_ca == str(ca_path)
        finally:
            await exporter.close()

    def test_from_env_missing_config_raises(self) -> None:
        with pytest.raises(ConfigError):
            LangfuseExporter.from_env(
                {
                    "LANGFUSE_HOST": "",
                    "LANGFUSE_PUBLIC_KEY": "pk",
                    "LANGFUSE_SECRET_KEY": "",
                }
            )

    def test_from_env_degrades_to_stdout_exporter(self) -> None:
        exporter = LangfuseExporter.from_env(
            {
                "AGENT_OBS_FAIL_ON_CONFIG": "0",
                "LANGFUSE_HOST": "",
                "LANGFUSE_PUBLIC_KEY": "",
                "LANGFUSE_SECRET_KEY": "",
            }
        )
        assert isinstance(exporter, StdoutExporter)


class TestRetryDelay:
    def test_exponential_growth(self) -> None:
        d0 = _retry_delay(0, base=0.2, factor=2.0)
        d1 = _retry_delay(1, base=0.2, factor=2.0)
        d2 = _retry_delay(2, base=0.2, factor=2.0)
        # Base without jitter: 0.2, 0.4, 0.8
        # With jitter, values should be in range
        assert 0.0 <= d0 <= 0.2 + 0.1  # base + max jitter
        assert 0.0 <= d1 <= 0.4 + 0.2
        assert 0.0 <= d2 <= 0.8 + 0.4
