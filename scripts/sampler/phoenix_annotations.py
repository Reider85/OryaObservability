"""Phoenix OTLP annotations for sampler rate changes (PC31).

Sends synthetic OTLP spans to Phoenix for timeline annotations when sampling
rates change. This enables correlation with dropped_spans in the Phoenix UI.

Uses OTLP/HTTP JSON transport (no opentelemetry dependency) following the
same pattern as langfuse_exporter.py.

Fail-open design: Phoenix write failure does not block rate changes.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import httpx

from agent_obs.metrics import tail_sampler_phoenix_annotations_total

logger = logging.getLogger(__name__)

# OTLP timestamp conversion (nanoseconds)
def _timestamp_to_nanos(ts: float | None) -> str:
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


def _span_to_otlp(
    trace_id: str,
    span_id: str,
    name: str,
    attributes: dict[str, Any],
    start_time: float,
    end_time: float,
) -> dict[str, Any]:
    """Convert a span to an OTLP Span dict."""
    return {
        "traceId": trace_id,
        "spanId": span_id,
        "name": name,
        "kind": 1,  # INTERNAL
        "startTimeUnixNano": _timestamp_to_nanos(start_time),
        "endTimeUnixNano": _timestamp_to_nanos(end_time),
        "attributes": [
            {"key": k, "value": _attr_to_otlp_value(v)}
            for k, v in attributes.items()
        ],
        "status": {"code": 0},  # OK
    }


def _batch_to_otlp(
    trace_id: str,
    span_id: str,
    name: str,
    attributes: dict[str, Any],
    start_time: float,
    end_time: float,
) -> dict[str, Any]:
    """Convert a batch into the OTLP ResourceSpans payload."""
    otlp_span = _span_to_otlp(trace_id, span_id, name, attributes, start_time, end_time)
    
    return {
        "resourceSpans": [
            {
                "resource": [
                    {"key": "service.name", "value": {"stringValue": "sampler-policy"}},
                ],
                "scopeSpans": [
                    {
                        "scope": {"name": "agent_obs"},
                        "spans": [otlp_span],
                    }
                ],
            }
        ]
    }


async def annotate_rate_change(
    event: dict[str, Any],
    client: httpx.AsyncClient | None = None,
    otlp_url: str = "http://localhost:4319/v1/traces",
    timeout: float = 5.0,
) -> bool:
    """Send a sampler rate change as an OTLP span to Phoenix.
    
    Parameters
    ----------
    event : dict[str, Any]
        Rate change event from audit event or decision
    client : httpx.AsyncClient | None, optional
        Optional HTTP client for reuse
    otlp_url : str, optional
        Phoenix OTLP endpoint URL
    timeout : float, optional
        Request timeout in seconds
        
    Returns
    -------
    bool
        True if annotation succeeded, False on failure
    """
    try:
        # Generate unique IDs for the annotation
        trace_id = f"phoenix-annotation-{int(time.time() * 1000)}"
        span_id = f"span-{trace_id[-16:]}"
        
        # Extract rate change details from event
        attributes = {
            "sampler.prev_rate": float(event.get("prev_rate", 0)),
            "sampler.new_rate": float(event.get("new_rate", 0)),
            "sampler.prev_reason": str(event.get("prev_reason", "")),
            "sampler.new_reason": str(event.get("new_reason", "")),
            "sampler.system_cpu_ratio": float(event.get("system_cpu_ratio", 0)),
            "sampler.agent_error_rate_5m": float(event.get("agent_error_rate_5m", 0)),
            "sampler.agent_id": str(event.get("agent_id", "*")),
            "sampler.triggering_agent_id": str(event.get("triggering_agent_id", "*")),
            "sampler.reason": str(event.get("reason", "")),
            "annotation.type": "sampler_rate_change",
        }
        
        # Build OTLP payload
        payload = _batch_to_otlp(
            trace_id=trace_id,
            span_id=span_id,
            name="sampler.rate_change",
            attributes=attributes,
            start_time=event.get("timestamp", time.time()),
            end_time=event.get("timestamp", time.time()),
        )
        
        # Use provided client or create one
        if client is None:
            client = httpx.AsyncClient(timeout=timeout)
            
        # Send to Phoenix
        response = await client.post(
            otlp_url,
            json=payload,
            headers={"Content-Type": "application/json"},
        )
        response.raise_for_status()
        
        # Update metrics
        tail_sampler_phoenix_annotations_total.labels(
            outcome="success"
        ).inc()
        
        logger.debug("Phoenix annotation sent for rate change: %s", event.get("audit_id"))
        return True
        
    except Exception as exc:
        # Update metrics
        tail_sampler_phoenix_annotations_total.labels(
            outcome="failed"
        ).inc()
        
        logger.warning(
            "Failed to send Phoenix annotation for rate change %s: %s",
            event.get("audit_id"),
            exc,
        )
        return False