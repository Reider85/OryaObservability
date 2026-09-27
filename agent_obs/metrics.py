"""Prometheus metrics for agent observability instrumentation."""

from __future__ import annotations

from prometheus_client import Counter, Gauge

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

cost_per_request_usd = Gauge(
    "agent_obs_cost_per_request_usd",
    "Cost of the last completed LLM call in USD",
    ["agent_id", "model"],
)

cost_total_usd = Counter(
    "agent_obs_cost_total_usd",
    "Accumulated total cost of all LLM calls in USD",
    ["agent_id", "model"],
)

guardrail_block_total = Counter(
    "agent_obs_guardrail_block_total",
    "Guardrail blocks raised before a guarded call, by stage",
    ["stage"],
)

guardrail_check_in_enqueue_total = Counter(
    "agent_obs_guardrail_check_in_enqueue_total",
    "Guardrail text checks performed inside _enqueue (last-resort masking barrier)",
)

eval_llm_judge_pending_total = Counter(
    "agent_obs_eval_llm_judge_pending_total",
    "LLM judge evaluation jobs enqueued for async processing",
    ["eval_name"],
)

eval_llm_judge_sampling_skipped_total = Counter(
    "agent_obs_eval_llm_judge_sampling_skipped_total",
    "LLM judge evaluations skipped due to sampling rate",
)
