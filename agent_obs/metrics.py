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
    ["eval_name"],
)

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
