# Sampler Metrics

## Overview

This document describes metrics exposed by the adaptive sampling policy engine (PP03-PP07) and sampler proxy. These metrics are exported via Prometheus and provide visibility into sampling decisions, content sampling rates, and decision latency.

## Metric Reference

| Metric | Type | Labels | Description |
|---|---|---|---|
| `tail_sampler_kept_spans_total` | Counter | `reason`, `agent_id`, `tenant_id` | Number of spans kept by sampling policy, by reason. Reasons: `error`, `cost`, `eval_fail`, `security`, `random`, or custom policy reason labels. |
| `tail_sampler_dropped_spans_total` | Counter | `agent_id`, `tenant_id` | Number of normal spans dropped by sampling policy (not matching any keep policy). |
| `tail_sampler_content_rate` | Gauge | `policy` | Current content sampling rate by policy. Values: `default=10`, `eval_fail_override=30` (percent). |
| `tail_sampler_decision_latency_seconds` | Histogram | - | Latency of sampling policy decisions in seconds. p99 < 1ms. |
| `agent_obs_tail_sampler_current_rate` | Gauge | `policy_reason` | Current sampling rate for normal traces. Values: `default`, `cpu_high`, `error_high`, `eval_feedback_normal`, `eval_feedback_degradation`, `eval_feedback_heavy_degradation`. |

## Emission Points

- **SDK Policy Engine (`agent_obs/exporters/tail_sampler.py`)**: `SamplingPolicyV2.decide()` emits kept/dropped spans by reason and content rate. Metrics are exposed via `ObservabilitySDK.start_metrics_server()` (port 9090).
- **Sampler Proxy (`scripts/sampler/sampler_proxy.py`)**: `sample_payload()` emits kept/dropped spans by reason from live trace decisions. Metrics are exposed on port 9095 (scraped by Prometheus job `sampler-proxy`).

## Policy ↔ Policy Reason Mapping

The `agent_obs_tail_sampler_current_rate` gauge uses `policy_reason` labels. For PromQL queries requiring `policy` labels, use the following mapping:

| `policy` | `policy_reason` | Description |
|---|---|---|
| `random` | `default` | Baseline random sampling (10%) |
| `cpu_driven` | `cpu_high` | System CPU > 80%, aggressive sampling (5%) |
| `cpu_driven` | `error_high` | Agent error rate > 5%, preserve error traces (30%) |
| `eval_feedback` | `eval_feedback_normal` | Quality recovery, normal rate (10%) |
| `eval_feedback` | `eval_feedback_degradation` | Quality drop, degraded rate (25%) |
| `eval_feedback` | `eval_feedback_heavy_degradation` | Poor quality, heavy degradation (40%) |

## PromQL Queries

### Kept Ratio by Reason
```promql
# Share of kept spans with eval_fail reason
sum(rate(tail_sampler_kept_spans_total{reason="eval_fail"}[5m])) 
/ 
sum(rate(tail_sampler_kept_spans_total[5m]))

# Kept spans stacked by reason (dashboard)
sum(rate(tail_sampler_kept_spans_total[5m])) by (reason)
```

### Current Rate Trends
```promql
# Current sampling rate by policy reason
agent_obs_tail_sampler_current_rate

# Rate trend for eval feedback degradation
agent_obs_tail_sampler_current_rate{policy_reason="eval_feedback_degradation"}
```

### Kept/Dropped Ratio
```promql
# Overall kept/dropped ratio
sum(rate(tail_sampler_kept_spans_total[5m])) 
/ 
sum(rate(tail_sampler_kept_spans_total[5m]) + rate(tail_sampler_dropped_spans_total[5m]))
```

### Decision Latency
```promql
# p99 decision latency
histogram_quantile(0.99, sum by (le) (rate(tail_sampler_decision_latency_seconds_bucket[5m])))
```

### Content Rate
```promql
# Current content sampling rate by policy
tail_sampler_content_rate
```

## Alerts

### SamplerRateUnstable
- **Condition**: `sum(changes(agent_obs_tail_sampler_current_rate[10m])) > 5`
- **For**: 5m
- **Severity**: Warning
- **Meaning**: Feedback loop is flapping the sampling rate more than 5 times in 10 minutes
- **Action**: Investigate eval score stability and policy engine health

### SamplerDecisionLatencyHigh
- **Condition**: `histogram_quantile(0.99, sum by (le) (rate(tail_sampler_decision_latency_seconds_bucket[5m]))) > 0.005`
- **For**: 5m
- **Severity**: Warning
- **Meaning**: p99 decision latency exceeds 5ms
- **Action**: Check policy engine performance and reduce policy complexity if needed

## Related Dashboards

- [Sampler Policy History](sampler-dashboard.md) - Real-time sampling rate changes and correlations

## Data Retention

- Metrics are retained by Prometheus according to the configured retention policy
- Long-term analysis: Use recording rules to derive `job:tail_sampler_kept_ratio:ratio` and `job:tail_sampler_kept_by_reason:ratio` for tiered storage