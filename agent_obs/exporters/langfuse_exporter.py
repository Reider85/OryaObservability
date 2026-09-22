"""Langfuse exporter using OTLP/HTTP transport.

Sends batches of spans to a self-hosted Langfuse instance via the
``/api/public/otel/v1/traces`` OTLP-compatible endpoint with Basic Auth.

Transport safety (P19): TLS verification is always enabled by default.
Credentials are read only from the environment through
:meth:`LangfuseExporter.from_env` — never from hard-coded code.

Retry behaviour (P17): transient failures (429, 5xx, network errors) are
retried with exponential backoff; other 4xx responses are not retried.
Batches that exhaust their retries are stashed and delivered by
:meth:`graceful flush` (P18) during shutdown.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import random
import ssl
import time
from dataclasses import dataclass
from typing import Any

import httpx

from agent_obs.exporters.base import BaseExporter
from agent_obs.exporters.otlp_mapping import (
    EVENTS_ATTRIBUTE,
    enrich_otlp_attributes,
    enrich_semconv_attributes,
)
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


class ConfigError(Exception):
    """Raised when the exporter's runtime configuration is invalid."""


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
    """Convert a single Span to an OTLP Span dict.

    Attributes are enriched with OTel semantic-convention usage fields
    (``gen_ai.usage.*``) and type/alias fields (``gen_ai.operation.name``,
    ``gen_ai.request.model``, ``gen_ai.usage.cost``, ``gen_ai.tool.name``) so
    Langfuse v3 classifies LLM calls as ``GENERATION`` observations and
    renders tokens/cost/model natively (P23).
    """
    attributes = [
        {"key": k, "value": _attr_to_otlp_value(v)}
        for k, v in span.attributes.items()
    ]
    attributes = enrich_otlp_attributes(attributes)
    attributes = enrich_semconv_attributes(attributes, span)

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

    # v3's legacy OTLP path drops span events, so mirror the names into an
    # attribute for the UI metadata panel (P23).
    if span.events:
        attributes = list(attributes)
        attributes.append({
            "key": EVENTS_ATTRIBUTE,
            "value": {
                "arrayValue": {
                    "values": [
                        {"stringValue": evt.name} for evt in span.events
                    ]
                }
            },
        })

    status_code = _STATUS_CODE_MAP.get(
        span.attributes.get("status", "unset"), 0
    )

    return {
        "traceId": _to_hex(span.context.trace_id).zfill(32),
        "spanId": _to_hex(span.context.span_id).zfill(16)[-16:],
        "parentSpanId": _to_hex(span.context.parent_span_id).zfill(16)[-16:]
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


@dataclass
class _PostOutcome:
    """Result of a single HTTP POST attempt."""

    success: bool = False
    retryable: bool = False
    error: str = ""


class LangfuseExporter(BaseExporter):
    """Export spans to Langfuse via OTLP/HTTP.

    Args:
        endpoint: Langfuse base URL (e.g. ``http://localhost:3000``).
            Must include the scheme and start with ``http://`` or ``https://``.
        public_key: Langfuse public API key.
        secret_key: Langfuse secret API key.
        timeout_s: HTTP request timeout in seconds.
        max_retries: Maximum retry attempts for transient failures.
        tls_ca: Optional path to a CA bundle for self-signed TLS certs.
            TLS verification stays enabled by default; ``verify=False`` is
            never a supported option.
        pending_cap: Maximum number of undelivered batches kept for flush.
    """

    def __init__(
        self,
        endpoint: str,
        public_key: str,
        secret_key: str,
        timeout_s: float = 5.0,
        max_retries: int = 5,
        tls_ca: str | None = None,
        pending_cap: int = 128,
    ) -> None:
        endpoint = (endpoint or "").strip().rstrip("/")
        if not (endpoint.startswith("http://") or endpoint.startswith("https://")):
            raise ConfigError(
                f"LANGFUSE_HOST must start with http:// or https://, got {endpoint!r}"
            )
        self._endpoint = endpoint
        self._url = f"{self._endpoint}/api/public/otel/v1/traces"
        self._max_retries = max_retries
        self._tls_ca = tls_ca
        self._auth = base64.b64encode(
            f"{public_key}:{secret_key}".encode()
        ).decode()
        verify: bool | ssl.SSLContext = True
        if tls_ca:
            # Self-signed certs supported only through an explicit CA bundle;
            # keep TLS verification on (verify=False is never an option).
            verify = ssl.create_default_context(cafile=tls_ca)
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_s),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Basic {self._auth}",
            },
            verify=verify,
        )
        self._pending: list[list[Span]] = []
        self._pending_cap = max(1, pending_cap)

    @classmethod
    def from_env(
        cls,
        env: dict[str, str] | None = None,
        max_retries: int = 5,
    ) -> "LangfuseExporter" | BaseExporter:
        """Build an exporter from environment variables (P19).

        Reads ``LANGFUSE_HOST``, ``LANGFUSE_PUBLIC_KEY``,
        ``LANGFUSE_SECRET_KEY`` and optionally ``AGENT_OBS_TLS_CA``.
        If keys/host are missing the behaviour depends on
        ``AGENT_OBS_FAIL_ON_CONFIG``: default ``1`` raises
        :class:`ConfigError`; ``0`` degrades to a :class:`StdoutExporter`
        with a warning so the agent keeps working with zero telemetry.
        """
        # Avoid a circular import at module import time.
        from agent_obs.exporters.stdout_exporter import StdoutExporter

        env = os.environ if env is None else env
        host = env.get("LANGFUSE_HOST", "").strip()
        public_key = env.get("LANGFUSE_PUBLIC_KEY", "").strip()
        secret_key = env.get("LANGFUSE_SECRET_KEY", "").strip()
        tls_ca = env.get("AGENT_OBS_TLS_CA", "").strip() or None

        missing = [
            name
            for name, value in (
                ("LANGFUSE_HOST", host),
                ("LANGFUSE_PUBLIC_KEY", public_key),
                ("LANGFUSE_SECRET_KEY", secret_key),
            )
            if not value
        ]
        if missing:
            msg = (
                "Missing required Langfuse configuration: "
                + ", ".join(missing)
            )
            fail_on_config = env.get(
                "AGENT_OBS_FAIL_ON_CONFIG", "1"
            ).strip().lower() not in ("0", "false", "no")
            if fail_on_config:
                raise ConfigError(msg)
            logger.warning(
                "%s; degrading to StdoutExporter "
                "(AGENT_OBS_FAIL_ON_CONFIG=0)",
                msg,
            )
            return StdoutExporter()

        return cls(
            endpoint=host,
            public_key=public_key,
            secret_key=secret_key,
            max_retries=max_retries,
            tls_ca=tls_ca,
        )

    async def _post_once(self, body: bytes) -> _PostOutcome:
        """Perform a single HTTP POST attempt (no retries)."""
        try:
            resp = await self._client.post(self._url, content=body)
        except (
            httpx.ConnectError,
            httpx.ConnectTimeout,
            httpx.ReadTimeout,
            httpx.WriteTimeout,
            httpx.PoolTimeout,
            httpx.NetworkError,
        ) as exc:
            return _PostOutcome(retryable=True, error=str(exc))

        if resp.status_code in (200, 201):
            return _PostOutcome(success=True)

        if resp.status_code == 429 or resp.status_code >= 500:
            return _PostOutcome(
                retryable=True, error=f"HTTP {resp.status_code}"
            )

        # Non-retryable (4xx except 429, or any other unexpected status)
        exporter_errors_total.labels(
            exporter="langfuse",
            status_code=str(resp.status_code),
        ).inc()
        logger.error(
            "Langfuse export failed with non-retryable HTTP %d: %s",
            resp.status_code,
            resp.text[:200],
        )
        return _PostOutcome(
            retryable=False, error=f"HTTP {resp.status_code}"
        )

    async def _post_with_retries(self, body: bytes) -> _PostOutcome:
        """POST with exponential backoff on transient failures.

        Returns the outcome of the attempt.  If the outcome is retryable
        and still failed, the batch is eligible for a later flush.
        """
        for attempt in range(self._max_retries):
            outcome = await self._post_once(body)
            if outcome.success or not outcome.retryable:
                return outcome
            wait = _retry_delay(attempt)
            logger.warning(
                "Langfuse export failed (%s), retrying in %.2fs "
                "(attempt %d/%d)",
                outcome.error,
                wait,
                attempt + 1,
                self._max_retries,
            )
            await asyncio.sleep(wait)

        exporter_errors_total.labels(
            exporter="langfuse",
            status_code="retry_exhausted",
        ).inc()
        logger.error(
            "Langfuse export failed after %d retries. Last error: %s",
            self._max_retries,
            outcome.error,
        )
        return outcome

    async def export(self, batch: list[Span]) -> None:
        """Export a batch of spans to Langfuse via OTLP/HTTP.

        Retries on 429/5xx/network errors with exponential backoff.
        Does not raise exceptions — all errors are logged and counted.
        If retries are exhausted the batch is stashed for a later
        :meth:`flush` and never blocks the agent.
        """
        if not batch:
            return

        body = json.dumps(_batch_to_otlp(batch), default=str).encode()
        outcome = await self._post_with_retries(body)

        if outcome.success or not outcome.retryable:
            return

        self._pending.append(batch)
        if len(self._pending) > self._pending_cap:
            overflowed = self._pending.pop(0)
            exporter_errors_total.labels(
                exporter="langfuse",
                status_code="pending_overflow",
            ).inc()
            logger.warning(
                "Pending batch queue overflow, dropping %d spans",
                len(overflowed),
            )

    async def flush(self) -> None:
        """Deliver batches left over from exhausted retries (P18).

        Best-effort single attempt per pending batch; never raises.  Safe to
        call repeatedly (idempotent): after the first call the pending queue
        is drained.
        """
        pending, self._pending = self._pending, []
        for batch in pending:
            body = json.dumps(_batch_to_otlp(batch), default=str).encode()
            try:
                outcome = await self._post_once(body)
            except Exception:
                logger.exception(
                    "flush: unexpected error while delivering %d spans",
                    len(batch),
                )
                continue
            if not outcome.success:
                logger.warning(
                    "flush: could not deliver %d spans: %s",
                    len(batch),
                    outcome.error,
                )

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()


def _retry_delay(attempt: int, base: float = 0.2, factor: float = 2.0) -> float:
    """Calculate exponential backoff delay with jitter."""
    delay = base * (factor ** attempt)
    jitter = random.uniform(0, delay * 0.5)
    return delay + jitter