"""Langfuse exporter using OTLP/HTTP transport.

Sends batches of spans to a self-hosted Langfuse instance via the
``/api/public/otel/v1/traces`` OTLP-compatible endpoint with Basic Auth.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import random
import time
from typing import Any

import httpx

from agent_obs.exporters.base import BaseExporter
from agent_obs.metrics import exporter_errors_total
from agent_obs.observability import Span, SpanType

logger = logging.getLogger(__name__)

_SPAN_KIND_MAP = {
    SpanType.AGENT_LOOP: 1,   # INTERNAL
    SpanType.LLM_CALL: 3,     # CLIENT
    SpanType.TOOL_CALL: 3,    # CLIENT
}

_STATUS_CODE_MAP = {
    "ok": 1,       # STATUS_CODE_OK
    "error": 2,    # STATUS_CODE_ERROR
    "unset": 0,    # STATUS_CODE_UNSET
}


def _to_hex(ulid_str: str) -> str:
    """Convert a Crockford base32 ULID string to hex for OTLP."""
    _CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
    value = 0
    for ch in ulid_str.upper():
        value = value * 32 + _CROCKFORD.index(ch)
    return format(value, "x")


def _timestamp_to_nanos(ts: float | None) -> str:
    """Convert a POSIX timestamp (seconds) to nanoseconds as a decimal string."""
    if ts is None:
        return "0"
    return str(int(ts * 1_000_000_000))


def _attr_to_otlp_value(value: Any) -> dict[str, Any]:
    """Convert a Python value to an OTLP KeyValue value."""
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": value}
    if isinstance(value, float):
        return {"doubleValue": value}
    if isinstance(value, list):
        return {
            "arrayValue": {
                "values": [_attr_to_otlp_value(v) for v in value]
            }
        }
    return {"stringValue": str(value)}


def _span_to_otlp(span: Span) -> dict[str, Any]:
    """Convert a single Span to an OTLP Span dict."""
    attributes = [
        {"key": k, "value": _attr_to_otlp_value(v)}
        for k, v in span.attributes.items()
    ]

    events = []
    for evt in span.events:
        evt_attributes = [
            {"key": k, "value": _attr_to_otlp_value(v)}
            for k, v in evt.attributes.items()
        ]
        events.append({
            "timeUnixNano": _timestamp_to_nanos(time.time()),
            "name": evt.name,
            "attributes": evt_attributes,
        })

    status_code = _STATUS_CODE_MAP.get(
        span.attributes.get("status", "unset"), 0
    )

    return {
        "traceId": _to_hex(span.context.trace_id),
        "spanId": _to_hex(span.context.span_id),
        "parentSpanId": _to_hex(span.context.parent_span_id)
        if span.context.parent_span_id
        else "",
        "name": span.name,
        "kind": _SPAN_KIND_MAP.get(span.span_type, 1),
        "startTimeUnixNano": _timestamp_to_nanos(span.start_time),
        "endTimeUnixNano": _timestamp_to_nanos(span.end_time),
        "attributes": attributes,
        "events": events,
        "status": {"code": status_code},
    }


def _batch_to_otlp(spans: list[Span]) -> dict[str, Any]:
    """Convert a batch of spans into the OTLP ResourceSpans payload.

    Groups spans by trace_id into separate Span entries under a single
    ScopeSpans.  Resource attributes carry agent metadata.
    """
    otlp_spans = [_span_to_otlp(s) for s in spans]

    resource_attributes = []
    if spans:
        first = spans[0]
        if first.context.agent_id:
            resource_attributes.append({
                "key": "service.name",
                "value": {"stringValue": first.context.agent_id},
            })
        if first.context.agent_version:
            resource_attributes.append({
                "key": "service.version",
                "value": {"stringValue": first.context.agent_version},
            })

    return {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": resource_attributes,
                },
                "scopeSpans": [
                    {
                        "scope": {
                            "name": "agent_obs",
                            "version": "0.1.0",
                        },
                        "spans": otlp_spans,
                    }
                ],
            }
        ]
    }


class LangfuseExporter(BaseExporter):
    """Export spans to Langfuse via OTLP/HTTP.

    Args:
        endpoint: Langfuse base URL (e.g. ``http://localhost:3000``).
        public_key: Langfuse public API key.
        secret_key: Langfuse secret API key.
        timeout_s: HTTP request timeout in seconds.
        max_retries: Maximum retry attempts for transient failures.
    """

    def __init__(
        self,
        endpoint: str,
        public_key: str,
        secret_key: str,
        timeout_s: float = 5.0,
        max_retries: int = 5,
    ) -> None:
        self._endpoint = endpoint.rstrip("/")
        self._url = f"{self._endpoint}/api/public/otel/v1/traces"
        self._max_retries = max_retries
        self._auth = base64.b64encode(
            f"{public_key}:{secret_key}".encode()
        ).decode()
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_s),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Basic {self._auth}",
            },
        )

    async def export(self, batch: list[Span]) -> None:
        """Export a batch of spans to Langfuse via OTLP/HTTP.

        Retries on 429/5xx/network errors with exponential backoff.
        Does not raise exceptions — all errors are logged and counted.
        """
        if not batch:
            return

        payload = _batch_to_otlp(batch)
        body = json.dumps(payload, default=str).encode()

        last_exc: Exception | None = None
        for attempt in range(self._max_retries):
            try:
                resp = await self._client.post(self._url, content=body)

                if resp.status_code == 200 or resp.status_code == 201:
                    return

                if resp.status_code == 429 or resp.status_code >= 500:
                    last_exc = httpx.HTTPStatusError(
                        f"HTTP {resp.status_code}",
                        request=resp.request,
                        response=resp,
                    )
                    wait = _retry_delay(attempt)
                    logger.warning(
                        "Langfuse export failed (HTTP %d), retrying in %.2fs "
                        "(attempt %d/%d)",
                        resp.status_code,
                        wait,
                        attempt + 1,
                        self._max_retries,
                    )
                    await asyncio.sleep(wait)
                    continue

                # Non-retryable 4xx (except 429)
                exporter_errors_total.labels(
                    exporter="langfuse",
                    status_code=str(resp.status_code),
                ).inc()
                logger.error(
                    "Langfuse export failed with non-retryable HTTP %d: %s",
                    resp.status_code,
                    resp.text[:200],
                )
                return

            except (
                httpx.ConnectError,
                httpx.ReadTimeout,
                httpx.WriteTimeout,
                httpx.PoolTimeout,
                httpx.NetworkError,
            ) as exc:
                last_exc = exc
                wait = _retry_delay(attempt)
                logger.warning(
                    "Langfuse export network error: %s, retrying in %.2fs "
                    "(attempt %d/%d)",
                    exc,
                    wait,
                    attempt + 1,
                    self._max_retries,
                )
                await asyncio.sleep(wait)

        # All retries exhausted
        exporter_errors_total.labels(
            exporter="langfuse",
            status_code="retry_exhausted",
        ).inc()
        logger.error(
            "Langfuse export failed after %d retries. Last error: %s",
            self._max_retries,
            last_exc,
        )

    async def flush(self) -> None:
        """Flush pending requests.

        This is a stub — P18 will implement proper buffer draining.
        The httpx client's connection pool is flushed implicitly on close.
        """
        pass

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()


def _retry_delay(attempt: int, base: float = 0.2, factor: float = 2.0) -> float:
    """Calculate exponential backoff delay with jitter."""
    delay = base * (factor ** attempt)
    jitter = random.uniform(0, delay * 0.5)
    return delay + jitter
