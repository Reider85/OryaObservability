"""Prometheus metrics for agent observability instrumentation."""

from __future__ import annotations

from prometheus_client import Counter

spans_total = Counter(
    "agent_obs_spans_total",
    "Spans enqueued for export",
    ["agent_id"],
)

dropped_spans_total = Counter(
    "agent_obs_dropped_spans_total",
    "Spans dropped due to full ring buffer",
    ["agent_id"],
)

exporter_errors_total = Counter(
    "agent_obs_exporter_errors_total",
    "Exporter errors by type",
    ["exporter", "status_code"],
)
