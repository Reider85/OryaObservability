"""Prometheus metrics for agent observability instrumentation."""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

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
)

# PP47: Performance isolation metrics for shared proxy
shared_proxy_security_latency_seconds = Histogram(
    "shared_proxy_security_latency_seconds",
    "Security pipeline latency with PP47 performance isolation",
    ["agent_id", "tenant_id"],
)

shared_proxy_obs_latency_seconds = Histogram(
    "shared_proxy_obs_latency_seconds", 
    "Observability pipeline latency with PP47 performance isolation",
    ["agent_id", "tenant_id"],
)

shared_proxy_total_latency_seconds = Histogram(
    "shared_proxy_total_latency_seconds",
    "Total pipeline latency with PP47 performance isolation",
    ["agent_id", "tenant_id"],
)

shared_proxy_security_timeouts_total = Counter(
    "shared_proxy_security_timeouts_total",
    "Security pipeline timeouts with PP47 fail-open behavior",
    ["agent_id", "tenant_id"],
)

shared_proxy_security_circuit_open_total = Counter(
    "shared_proxy_security_circuit_open_total",
    "Security circuit breaker opens with PP47 performance isolation",
    ["agent_id", "tenant_id"],
)

shared_proxy_obs_circuit_open_total = Counter(
    "shared_proxy_obs_circuit_open_total",
    "Observability circuit breaker opens with PP47 performance isolation",
    ["agent_id", "tenant_id"],
)

shared_proxy_performance_isolation_effective = Gauge(
    "shared_proxy_performance_isolation_effective",
    "PP47 performance isolation effectiveness (1=effective, 0=ineffective)",
    ["agent_id", "tenant_id"],
)

# PP47: Shared proxy performance isolation metrics
try:
    shared_proxy_security_latency_seconds = Histogram(
        "shared_proxy_security_latency_seconds",
        "Security pipeline processing latency",
        ["agent_id", "tenant_id"],
    )

    shared_proxy_obs_latency_seconds = Histogram(
        "shared_proxy_obs_latency_seconds", 
        "Observability pipeline processing latency",
        ["agent_id", "tenant_id"],
    )

    shared_proxy_total_latency_seconds = Histogram(
        "shared_proxy_total_latency_seconds",
        "Total shared pipeline processing latency",
        ["agent_id", "tenant_id"],
    )

    shared_proxy_security_timeouts_total = Counter(
        "shared_proxy_security_timeouts_total",
        "Security pipeline timeouts",
        ["agent_id", "tenant_id"],
    )

    shared_proxy_security_circuit_open_total = Counter(
        "shared_proxy_security_circuit_open_total",
        "Security circuit breaker opens",
        ["agent_id", "tenant_id"],
    )

    shared_proxy_obs_circuit_open_total = Counter(
        "shared_proxy_obs_circuit_open_total",
        "Observability circuit breaker opens", 
        ["agent_id", "tenant_id"],
    )
except:
    # Metrics already exist, skip registration
    pass

eval_llm_judge_sampling_skipped_total = Counter(
    "agent_obs_eval_llm_judge_sampling_skipped_total",
    "LLM judge evaluations skipped due to sampling rate",
)

eval_annotation_total = Counter(
    "agent_obs_eval_annotation_total",
    "Late annotation writes to ClickHouse",
    ["status"],
)

eval_annotation_redis_fallback_total = Counter(
    "agent_obs_eval_annotation_redis_fallback_total",
    "Late annotations stored in Redis due to ClickHouse unavailability",
)

eval_jobs_completed_total = Counter(
    "agent_obs_eval_jobs_completed_total",
    "LLM judge jobs completed successfully",
)

eval_jobs_failed_total = Counter(
    "agent_obs_eval_jobs_failed_total",
    "LLM judge jobs that failed",
)

eval_job_latency_seconds = Histogram(
    "agent_obs_eval_job_latency_seconds",
    "LLM judge job processing latency",
    buckets=[1, 2, 5, 10, 15, 30, 60],
)

# PP13 (T3.3.2): nightly golden-set regression control. Set once per nightly
# run by scripts/nightly_golden_eval.py (long-lived cron scheduler keeps the
# gauge scrapeable between runs). GoldenEvalRegression fires when the value
# drops more than 0.10 below its 7-day moving average.
eval_golden_pass_rate = Gauge(
    "agent_obs_eval_golden_pass_rate",
    "Fraction of golden-set cases passing the nightly LLM-judge eval (last run)",
)

# --- Cron maintenance jobs (PC21) -------------------------------------------
# These are emitted by the out-of-process cron service (scripts/cron/scheduler.py),
# never by the SDK in-process. `job_name` is a bounded set: cleanup_vault,
# cleanup_audit_events, cleanup_eval_results.

cron_runs_total = Counter(
    "agent_obs_cron_runs_total",
    "Cron maintenance job runs started",
    ["job_name"],
)

cron_errors_total = Counter(
    "agent_obs_cron_errors_total",
    "Cron maintenance job runs that failed",
    ["job_name"],
)

cron_rows_deleted_total = Counter(
    "agent_obs_cron_rows_deleted_total",
    "Rows deleted or archived by a cron maintenance job",
    ["job_name"],
)

cron_duration_seconds = Histogram(
    "agent_obs_cron_duration_seconds",
    "Cron maintenance job wall-clock duration",
    ["job_name"],
    buckets=[0.5, 1, 2.5, 5, 10, 30, 60, 300, 900],
)

cron_last_success_timestamp_seconds = Gauge(
    "agent_obs_cron_last_success_timestamp_seconds",
    "Unix timestamp of the last successful cron maintenance run",
    ["job_name"],
)

# --- PC22 Hot→Warm migration metrics ---
migration_hot_to_warm_rows_total = Counter(
    "agent_obs_migration_hot_to_warm_rows_total",
    "Trace rows migrated from hot to warm tier",
    ["job_name"],
)

migration_hot_to_warm_duration_seconds = Histogram(
    "agent_obs_migration_hot_to_warm_duration_seconds",
    "Hot-to-warm migration job wall-clock duration",
    ["job_name"],
    buckets=[1, 5, 15, 30, 60, 300, 900, 3600],
)

migration_hot_to_warm_errors_total = Counter(
    "agent_obs_migration_hot_to_warm_errors_total",
    "Errors during hot-to-warm migration",
    ["job_name"],
)

# --- PC23 Warm→Cold migration metrics ---
migration_warm_to_cold_files_total = Counter(
    "agent_obs_migration_warm_to_cold_files_total",
    "Parquet files written to cold storage",
    ["job_name"],
)

migration_warm_to_cold_rows_total = Counter(
    "agent_obs_migration_warm_to_cold_rows_total",
    "Trace rows aggregated into cold storage",
    ["job_name"],
)

migration_warm_to_cold_bytes_total = Counter(
    "agent_obs_migration_warm_to_cold_bytes_total",
    "Bytes written to cold storage",
    ["job_name"],
)

migration_warm_to_cold_duration_seconds = Histogram(
    "agent_obs_migration_warm_to_cold_duration_seconds",
    "Cold migration job wall-clock duration",
    ["job_name"],
    buckets=[1, 5, 15, 30, 60, 300, 900, 3600],
)

# --- PC24 Embedding storage metrics ------------------------------------------

embeddings_stored_total = Counter(
    "agent_obs_embeddings_stored_total",
    "Response embeddings successfully written to ClickHouse",
)

embeddings_storage_failed_total = Counter(
    "agent_obs_embeddings_storage_failed_total",
    "Embedding writes to ClickHouse that failed",
)

embeddings_redis_fallback_total = Counter(
    "agent_obs_embeddings_redis_fallback_total",
    "Embedding batches written to Redis fallback after ClickHouse failure",
)

# --- PC21 DrillDown API metrics ---------------------------------------------

drill_down_latency_seconds = Histogram(
    "agent_obs_drill_down_latency_seconds",
    "Drill-down query latency by storage tier",
    ["tier"],
    buckets=[0.01, 0.05, 0.1, 0.5, 1, 5, 10, 30, 60, 120])

drill_down_rate_limited_total = Counter(
    "agent_obs_drill_down_rate_limited_total",
    "Number of drill-down requests blocked by rate limiting",
    ["tier", "outcome"])

# --- PP22 Athena integration metrics -----------------------------------------

athena_query_latency_seconds = Histogram(
    "agent_obs_athena_query_latency_seconds",
    "Athena cold-storage query latency (start to results fetched)",
    buckets=[0.5, 1, 2, 5, 10, 30, 60, 120])

athena_data_scanned_bytes_total = Counter(
    "agent_obs_athena_data_scanned_bytes_total",
    "Total bytes scanned by Athena queries",
)

athena_cost_guard_triggered_total = Counter(
    "agent_obs_athena_cost_guard_triggered_total",
    "Athena queries stopped by the cost guard (ATHENA_MAX_SCAN_BYTES exceeded)",
)

# --- PP23 DrillDown cache metrics -------------------------------------------

drill_down_cache_hits_total = Counter(
    "agent_obs_drill_down_cache_hits_total",
    "Drill-down requests served from the Redis cache",
)

drill_down_cache_misses_total = Counter(
    "agent_obs_drill_down_cache_misses_total",
    "Drill-down requests that missed the Redis cache",
)

drill_down_cache_hit_ratio = Gauge(
    "agent_obs_drill_down_cache_hit_ratio",
    "Drill-down cache hit ratio (hits / (hits + misses)), 0..1. "
    "Expected healthy range 0.3-0.7",
)

drill_down_cache_size_bytes = Gauge(
    "agent_obs_drill_down_cache_size_bytes",
    "Approximate total size of drill-down cache values in Redis (bytes). "
    "Alert if > 1 GiB",
)

embeddings_batch_size = Histogram(
    "agent_obs_embeddings_batch_size",
    "Number of embeddings in each ClickHouse batch write",
    buckets=[1, 5, 10, 25, 50, 100, 250, 500],
)

# --- PC25 Drift detection metrics ---

drift_kl_score = Gauge(
    "agent_obs_drift_kl_score",
    "Current KL-divergence score for drift detection",
    ["agent_id", "baseline_window_start", "baseline_window_end"],
)

drift_runs_total = Counter(
    "agent_obs_drift_runs_total",
    "Drift detection runs completed",
    ["agent_id"],
)

drift_alerts_total = Counter(
    "agent_obs_drift_alerts_total",
    "Drift alerts generated by severity",
    ["agent_id", "severity"],
)

# --- PC26 Drift threshold calibration metrics ---

drift_threshold_value = Gauge(
    "agent_obs_drift_threshold_value",
    "Active drift alert threshold (p99 of 30d KL history once calibrated)",
    ["agent_id"],
)

drift_threshold_calibrated_at = Gauge(
    "agent_obs_drift_threshold_calibrated_at",
    "Unix timestamp of the last successful drift threshold calibration",
    ["agent_id"],
)

# --- PP57 Drift explainability metrics ---

drift_explanations_generated_total = Counter(
    "agent_obs_drift_explanations_generated_total",
    "Number of drift explanations generated",
    ["agent_id", "severity"],
)

drift_explanation_generation_seconds = Histogram(
    "agent_obs_drift_explanation_generation_seconds",
    "Time taken to generate drift explanations",
    ["agent_id"],
    buckets=[1, 5, 10, 30, 60, 120],
)

drift_explanation_llm_errors_total = Counter(
    "agent_obs_drift_explanation_llm_errors_total",
    "LLM errors during drift explanation generation",
    ["agent_id"],
)

drift_calibration_runs_total = Counter(
    "agent_obs_drift_calibration_runs_total",
    "Drift threshold calibration runs completed",
    ["agent_id", "outcome"],
)

drift_sanity_check_total = Counter(
    "agent_obs_drift_sanity_check_total",
    "Quarterly drift baseline sanity-check results",
    ["agent_id", "result"],
)

# --- PC29 Adaptive sampler metrics -------------------------------------------

system_cpu_ratio = Gauge(
    "agent_obs_system_cpu_ratio",
    "System CPU utilization ratio (0.0-1.0), updated every 10s by the SDK",
)

agent_error_rate_5m = Gauge(
    "agent_obs_agent_error_rate_5m",
    "Fraction of error spans in the sliding 5-minute window",
    ["agent_id"],
)

# --- PC30 Adaptive sampling policy metrics ------------------------------------

# The policy engine (scripts/sampler/policy_engine.py) sets this gauge on every
# rate change. ``policy_reason`` is one of cpu_high | error_high | default, so
# the label set stays bounded (PC0 rule: no unbounded metric cardinality).
tail_sampler_current_rate = Gauge(
    "agent_obs_tail_sampler_current_rate",
    "Current sampling rate applied to normal (non-interesting) traces",
    ["policy_reason"],
)

# Counts rate transitions. Labels are the *reasons* rather than the raw rates so
# the series count is bounded at 3x3 instead of growing with every float value.
tail_sampler_rate_changes_total = Counter(
    "agent_obs_tail_sampler_rate_changes_total",
    "Number of adaptive sampling rate changes, labelled by reason transition",
    ["from_reason", "to_reason"],
)

tail_sampler_policy_evaluations_total = Counter(
    "agent_obs_tail_sampler_policy_evaluations_total",
    "Policy engine evaluations of system load and error rate",
    ["outcome"],
)

tail_sampler_traces_sampled_total = Counter(
    "agent_obs_tail_sampler_traces_sampled_total",
    "Traces kept by the sampler proxy",
    ["decision"],
)

# --- PC31 Phoenix annotation metrics ------------------------------------

tail_sampler_phoenix_annotations_total = Counter(
    "agent_obs_tail_sampler_phoenix_annotations_total",
    "Phoenix annotations sent for sampler rate changes",
    ["outcome"],
)

# --- PP07 Tail sampler metrics by reason (T3.1.5) ---

# Counts spans kept by the sampling policy engine, labelled by reason
tail_sampler_kept_spans_total = Counter(
    "tail_sampler_kept_spans_total",
    "Spans kept by sampling policy, by reason (error|cost|eval_fail|security|random)",
    ["reason", "agent_id", "tenant_id"],
)

# Counts spans dropped by the sampling policy engine (normal traces not sampled)
tail_sampler_dropped_spans_total = Counter(
    "tail_sampler_dropped_spans_total",
    "Normal spans dropped by sampling policy (not matching any keep policy)",
    ["agent_id", "tenant_id"],
)

# Current content sampling rate (default or eval_fail override)
# Seeded at import: default=10%, eval_fail_override=30%
tail_sampler_content_rate = Gauge(
    "tail_sampler_content_rate",
    "Current content sampling rate by policy (default=10%, eval_fail_override=30%)",
    ["policy"],
)

# Histogram of sampling decision latency (p99 < 1ms)
tail_sampler_decision_latency_seconds = Histogram(
    "tail_sampler_decision_latency_seconds",
    "Latency of sampling policy decisions in seconds",
    buckets=[0.0001, 0.00025, 0.0005, 0.001, 0.0025, 0.005, 0.01, 0.05],
)

# Seed content rates at import (matches AGENT_OBS_CONTENT_RATE default and override)
tail_sampler_content_rate.labels(policy="default").set(10)
tail_sampler_content_rate.labels(policy="eval_fail_override").set(30)

# --- PP11 Span aggregation metrics ---

# Counter for aggregated spans produced by the SDK
spans_aggregated_total = Counter(
    "spans_aggregated_total",
    "Spans aggregated into one span with events",
    ["agent_id", "tenant_id", "span_type"],
)

# Counter for micro-spans that were input to aggregation
spans_micro_input_total = Counter(
    "spans_micro_input_total",
    "Micro-spans input to span aggregation buffer",
    ["agent_id", "tenant_id"],
)

# Counter for total bytes saved by aggregation
bytes_saved_total = Counter(
    "bytes_saved_total",
    "Total bytes saved by span aggregation (sum of all flushes)",
    ["agent_id", "tenant_id"],
)

# Gauge for current aggregation ratio (aggregated / micro-input)
aggregation_ratio = Gauge(
    "aggregation_ratio",
    "Ratio of aggregated spans to micro-spans input (expected 0.02–0.05)",
    ["agent_id"],
)

# --- PP12 Adaptive eval sampler metrics ---

# Current adaptive eval sampler mode (day=1, night=1, incident=1, others=0)
eval_sampler_mode = Gauge(
    "eval_sampler_mode",
    "Current adaptive eval sampler mode (day=1, night=1, incident=1, others=0)",
    ["mode"],
)

# Incident evals granted by the adaptive eval sampler (PP14)
eval_sampler_incident_evals_total = Counter(
    "eval_sampler_incident_evals_total",
    "Incident evals granted by the adaptive eval sampler, by reason (security|injection|cost|other)",
    ["reason"],
)

# PP15 Eval cost and budget metrics

# Daily eval cost by mode (gauge, reset at 00:00 UTC)
eval_cost_today_usd = Gauge(
    "eval_cost_today_usd",
    "Daily eval cost by mode (USD), resets at 00:00 UTC",
    ["mode"],
)

# Daily eval count by mode (counter, monotonic)
eval_count_by_mode = Counter(
    "eval_count_by_mode",
    "Daily eval count by mode (day|night|incident), incremented at decision time",
    ["mode"],
)

# Average cost per eval (gauge, reset at 00:00 UTC)
eval_cost_per_eval_usd = Gauge(
    "eval_cost_per_eval_usd",
    "Average cost per eval (USD), resets at 00:00 UTC",
)

# Budget utilization ratio (gauge, 0..1.2)
budget_utilization_ratio = Gauge(
    "budget_utilization_ratio",
    "Budget utilization ratio (current_cost / budget), 0..1.2",
)

# Current budget state (gauge: 0=ok, 1=soft, 2=paused)
eval_budget_state = Gauge(
    "eval_budget_state",
    "Current budget state (0=ok, 1=soft, 2=paused)",
)

# PP17 Tree compression ratio (gauge, 0..1)
tree_compression_ratio = Gauge(
    "tree_compression_ratio",
    "Ratio of compressed spans to total spans in tree (0..1)",
    ["agent_id"],
)

# PP18 Async LLM summary pending count (counter)
tree_compression_summary_pending_total = Counter(
    "tree_compression_summary_pending_total",
    "Count of compressed nodes awaiting async LLM summary (PP18)",
    ["agent_id"],
)

# PP20 Embedded subtree ratio (gauge)
tree_embedded_ratio = Gauge(
    "tree_embedded_ratio",
    "Fraction of tree nodes with embedded subtree (0..1)",
    ["agent_id"],
)

# --- PP25 SLO metrics (T3.6.1) ------------------------------------------------
# Names intentionally lack the agent_obs_ prefix: PP26 alert rules and the
# PP28 Grafana dashboard reference them verbatim (slo_burn_rate{name=...}).
# All gauges are updated by agent_obs.slo.refresher.SLORefresher (5-min cadence),
# never by the in-process SDK hot path. Label `name` is a bounded set: the 4
# keys of SLO_CATALOG (PC0 rule: no unbounded metric cardinality).

slo_target = Gauge(
    "slo_target",
    "SLO target value (share of good events, 0..1)",
    ["name"],
)

slo_actual_sli = Gauge(
    "slo_actual_sli",
    "Actual rolling-window SLI (good/total events), updated every ~5 minutes",
    ["name"],
)

# `window` label is the alerting window duration ("5m"|"1h"|"6h"|"1d"), not
# the short/long role — PP26's SLOBurnRate maps roles onto durations.
slo_burn_rate = Gauge(
    "slo_burn_rate",
    "Error-budget burn rate (1 - SLI) / (1 - target) per alerting window",
    ["name", "window"],
)

slo_error_budget_remaining = Gauge(
    "slo_error_budget_remaining",
    "Remaining error budget ratio on the rolling window (0..1)",
    ["name"],
)

# PP27: counts error-budget *policy actions* (transitions), not scrapes — one
# increment per state change (ok->soft, soft->hard, *->ok), never per cycle.
# `action` is a bounded set: soft | hard | recover.
slo_error_budget_actions_taken_total = Counter(
    "slo_error_budget_actions_taken_total",
    "Error-budget policy actions fired (soft/hard on degradation, recover on return to ok)",
    ["name", "action"],
)

# --- PP59 Golden Signals metrics (Google SRE Four Golden Signals) -------------
# PP59 extends SLO metrics with LATENCY, TRAFFIC, ERRORS, SATURATION golden signals.
# All gauges updated by SLORefresher, following the same 5-minute cadence.
# `name` label includes both original SLOs and golden signals from SLO_CATALOG.

# Golden Signal SLI values (actual SLI for each golden signal)
golden_signal_latency_sli = Gauge(
    "golden_signal_latency_sli",
    "Golden Signal: latency SLI (p99 latency <= 2 seconds), rolling 30d",
    ["name"],
)

golden_signal_traffic_sli = Gauge(
    "golden_signal_traffic_sli", 
    "Golden Signal: traffic SLI (within 20% of 7-day moving average), rolling 30d",
    ["name"],
)

golden_signal_error_sli = Gauge(
    "golden_signal_error_sli",
    "Golden Signal: error SLI (share of requests without errors), rolling 30d",
    ["name"],
)

golden_signal_saturation_sli = Gauge(
    "golden_signal_saturation_sli",
    "Golden Signal: saturation SLI (system utilization <= 80%), rolling 30d",
    ["name"],
)

# Golden Signal health indicators (1=healthy, 0=unhealthy)
golden_signal_healthy = Gauge(
    "golden_signal_healthy",
    "Golden Signal health status (1=healthy, 0=unhealthy)",
    ["name"],
)

# Golden Signal burn rates per alerting window
golden_signal_burn_rate = Gauge(
    "golden_signal_burn_rate",
    "Golden Signal error-budget burn rate per alerting window",
    ["name", "window"],
)

# Golden Signal target values
golden_signal_latency_target = Gauge(
    "golden_signal_latency_target",
    "Golden Signal: latency target (p99 <= 2 seconds)",
    ["name"],
)

golden_signal_traffic_target = Gauge(
    "golden_signal_traffic_target",
    "Golden Signal: traffic target (20% above baseline tolerance)",
    ["name"],
)

golden_signal_error_target = Gauge(
    "golden_signal_error_target",
    "Golden Signal: error target (< 0.5% error rate)",
    ["name"],
)

golden_signal_saturation_target = Gauge(
    "golden_signal_saturation_target",
    "Golden Signal: saturation target (<= 80% utilization)",
    ["name"],
)

# --- PP38 health probes + circuit breakers (T3.8.4) -------------------------
# `dependency` is a bounded set (DEPENDENCIES in agent_obs.ha.health_checks):
# langfuse | ch | pg | vault | redis | minio.

# Updated by HealthChecker.apply_gauges on each probe cycle. 1 = healthy,
# 0 = unhealthy; dependencies that are not configured are never set (no
# series, no DependencyUnhealthy alert).
dependency_health = Gauge(
    "agent_obs_dependency_health",
    "Active health-probe result per dependency (1=healthy, 0=unhealthy)",
    ["dependency"],
)

# 0 = closed, 1 = half-open, 2 = open (CircuitState.gauge_value).
circuit_breaker_state = Gauge(
    "agent_obs_circuit_breaker_state",
    "Circuit breaker state per dependency (0=closed, 1=half-open, 2=open)",
    ["dependency"],
)

# Counted by CircuitBreaker.record_failure on every recorded operation
# failure — not per scrape, not per rejected request.
circuit_breaker_failures_total = Counter(
    "agent_obs_circuit_breaker_failures_total",
    "Circuit-breaker-recorded operation failures per dependency",
    ["dependency"],
)

# --- PP43 proxy upstream failover (T3.9.5) ------------------------------------
# Bounded label set: timeout|429|5xx|circuit_open
proxy_upstream_failures_total = Counter(
    "proxy_upstream_failures_total",
    "Upstream operation failures in proxy mode by reason",
    ["reason"],
)

# --- PP42 tenant-aware cost attribution (T3.9.4) ------------------------------
# Labels are bounded by deployment shape (tenant count × agent count); updated
# once per flush cycle by CostAttributionTracker.flush_once, never per request.

cost_per_tenant_usd = Gauge(
    "cost_per_tenant_usd",
    "Daily LLM cost per tenant and agent (USD), refreshed once per flush cycle",
    ["tenant_id", "agent_id"],
)

# --- PP51 Observability-of-observability metrics (T3.13) ----------------------
# Meta-metrics for monitoring the health of the observability system itself

# Ring buffer health metrics
ring_buffer_depth = Gauge(
    "agent_obs_ring_buffer_depth",
    "Current ring buffer depth (number of spans waiting to be processed)",
    ["agent_id"],
)

ring_buffer_pressure_ratio = Gauge(
    "agent_obs_ring_buffer_pressure_ratio",
    "Ring buffer pressure ratio (current depth / maxsize), 0..1. Alert if > 0.8",
    ["agent_id"],
)

ring_buffer_enqueue_rate = Counter(
    "agent_obs_ring_buffer_enqueue_rate",
    "Spans enqueued per second",
    ["agent_id"],
)

ring_buffer_dequeue_rate = Counter(
    "agent_obs_ring_buffer_dequeue_rate",
    "Spans dequeued per second",
    ["agent_id"],
)

ring_buffer_backlog_growth = Gauge(
    "agent_obs_ring_buffer_backlog_growth",
    "Backlog growth trend (positive values indicate growing backlog)",
    ["agent_id"],
)

# Worker task health metrics
worker_task_healthy = Gauge(
    "agent_obs_worker_task_healthy",
    "Worker task health (1=healthy, 0=unhealthy)",
    ["agent_id"],
)

worker_processing_latency_seconds = Histogram(
    "agent_obs_worker_processing_latency_seconds",
    "Worker processing latency for span export",
    ["agent_id"],
    buckets=[0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1.0, 5.0],
)

worker_task_restart_total = Counter(
    "agent_obs_worker_task_restart_total",
    "Worker task restarts",
    ["agent_id"],
)

# Exporter health metrics
exporter_latency_seconds = Histogram(
    "agent_obs_exporter_latency_seconds",
    "Exporter processing latency",
    ["exporter"],
    buckets=[0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1.0, 5.0],
)

exporter_success_rate = Gauge(
    "agent_obs_exporter_success_rate",
    "Exporter success rate (0..1)",
    ["exporter"],
)

exporter_data_loss_total = Counter(
    "agent_obs_exporter_data_loss_total",
    "Data loss events by exporter",
    ["exporter", "reason"],
)

# System resource metrics
observability_cpu_usage = Gauge(
    "agent_obs_system_cpu_usage",
    "Observability system CPU usage percentage",
    ["agent_id"],
)

observability_memory_usage_bytes = Gauge(
    "agent_obs_system_memory_usage_bytes",
    "Observability system memory usage in bytes",
    ["agent_id"],
)

observability_disk_usage_bytes = Gauge(
    "agent_obs_system_disk_usage_bytes",
    "Observability system disk usage in bytes",
    ["agent_id"],
)

# Component dependency metrics
component_dependency_health = Gauge(
    "agent_obs_component_dependency_health",
    "Component dependency health (1=healthy, 0=unhealthy)",
    ["component"],
)

component_dependency_latency_seconds = Histogram(
    "agent_obs_component_dependency_latency_seconds",
    "Component dependency latency",
    ["component"],
    buckets=[0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1.0, 5.0],
)

# Export latency for observability spans (meta-observation)
obs_export_latency_seconds = Histogram(
    "agent_obs_export_latency_seconds",
    "Time from span enqueue to export completion (meta-observation)",
    ["agent_id"],
)

# Obs stack health overall gauge
obs_stack_health = Gauge(
    "agent_obs_stack_health",
    "Overall observability stack health (1=healthy, 0=unhealthy)",
    ["agent_id"],
)

# PP58: PII encryption metrics
pii_encrypted_total = Counter(
    "agent_obs_pii_encrypted_total",
    "Total PII fields encrypted",
    ["entity_type", "tenant_id"],
)

pii_vault_transit_failures_total = Counter(
    "agent_obs_pii_vault_transit_failures_total",
    "Vault encryption failures",
    ["tenant_id", "error_type"],
)

pii_encryption_latency_seconds = Histogram(
    "agent_obs_pii_encryption_latency_seconds",
    "PII encryption latency",
    ["tenant_id", "entity_type"],
    buckets=[0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1.0, 5.0],
)

pii_decryption_latency_seconds = Histogram(
    "agent_obs_pii_decryption_latency_seconds",
    "PII decryption latency",
    ["tenant_id", "entity_type"],
    buckets=[0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1.0, 5.0],
)

pii_encryption_enabled = Gauge(
    "agent_obs_pii_encryption_enabled",
    "PII encryption enabled (1=enabled, 0=disabled)",
    ["tenant_id"],
)
