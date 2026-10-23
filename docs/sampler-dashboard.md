# Sampler Policy History Dashboard

## Overview

This dashboard provides real-time visibility into the adaptive sampling policy engine (PP03-PP06) and helps SRE teams understand sampling rate changes and their impact.

- **File**: `infra/grafana/dashboards/sampler_policy_history.json`
- **UID**: `sampler-policy-history`
- **Provisioning**: file-based (`infra/grafana/provisioning/dashboards/dashboards.yml`), no manual import
- **URL**: http://localhost:3001 (host port `GRAFANA_PORT`, default 3001)

## Panels

The dashboard has 7 visualizations grouped into the 5 logical panels of the ticket.

### 1. Current sampling rate (gauge) + Sampler rate over time (graph)
- **What**: Current sampling rate for normal traces
- **Metrics**: `agent_obs_tail_sampler_current_rate` by `policy_reason`
- **Key Insights**:
  - `cpu_high` (0.05): System CPU > 80%, aggressive sampling to protect exporter
  - `error_high` (0.30): Agent error rate > 5%, preserve error traces
  - `default` (0.10): Normal operation, 10% sampling
  - `eval_feedback_*` (0.25/0.40): Quality-based degradation (PP04)
  - `error_keep`/`cost_high_keep`/`security_keep` (1.0): Critical traces preserved
  - `random` (0.10): Baseline random sampling
- **Red Flags**: Sudden drops to 0.05 (cpu_high) or spikes to 1.0 (critical policies)
- **Drill-down**: click a point on the graph → trace-viewer opens at that timestamp

### 2. Policy evaluation outcomes (pie)
- **What**: Distribution of policy-engine evaluations
- **Metrics**: `rate(agent_obs_tail_sampler_policy_evaluations_total)` by `outcome`
- **Key Insights**: `changed` vs `unchanged` vs `audit_failed`
- **Actions**:
  - High `unchanged`: the policy engine sees a stable system (normal)
  - Growing `audit_failed`: rate changes are no longer reaching the audit trail — check ClickHouse

### 3. Sampling decisions by reason (table)
- **What**: The audit trail of every sampling-rate decision
- **Source**: ClickHouse `audit_events_hot`, `action = 'sampler.rate_change'`
- **Columns**: previous reason, new reason, old rate → new rate, human-readable cause
- **Drill-down**: click a row timestamp → trace-viewer opens at that timestamp

### 4. Kept spans by reason (stacked bar)
- **What**: Spans kept by the sampler per decision (stacked)
- **Metrics**: `rate(agent_obs_tail_sampler_traces_sampled_total)` by `decision`
- **Key Insights**: Impact of sampling policies on data retention
- **Actions**: Monitor if critical policies are preserving expected data volume

### 5. Eval score correlation (overlay)
- **What**: Average faithfulness score over a 5-minute window
- **Metrics**: `avg_over_time(faithfulness_score[5m])` (the same query the PP04 feedback loop uses)
- **Correlations**:
  - Score drops → Rate increases to 25% (degradation)
  - Score < 0.6 → Rate increases to 40% (heavy degradation)
  - Score recovers → Rate returns to 10% (normal)

### 6. System correlation (overlay)
- **What**: System health metrics
- **Metrics**: `agent_obs_system_cpu_ratio`, `agent_obs_agent_error_rate_5m`
- **Correlations**:
  - CPU > 80% → Rate drops to 5% (cpu_high)
  - Error rate > 5% → Rate increases to 30% (error_high)

## Annotations

### Policy reason changes (Red)
- **Source**: Prometheus
- **Query**: `changes(agent_obs_tail_sampler_current_rate[2m]) > 0`
- **Shows**: Every sampling rate change, labelled with the `policy_reason` that caused it
- **Usage**: hover the marker to see which policy fired

### Sampling decisions (Orange)
- **Source**: ClickHouse `audit_events_hot` via the `grafana-clickhouse-datasource` plugin
- **Query**: `action = 'sampler.rate_change'` filtered with `$__timeFilter(timestamp)`, so the
  annotations follow the time picker (1h / 6h / 24h / 7d) instead of a fixed window
- **Shows**: old rate → new rate plus the reason string of every decision
- **Requires**: the ClickHouse datasource plugin. It is installed automatically by the compose
  stack (`GF_INSTALL_PLUGINS: grafana-clickhouse-datasource 4.14.0`) and provisioned as the
  `clickhouse` datasource. Plugin version 4.14.0 is pinned because 4.15.0+ requires
  Grafana >= 11.6.0, while the stack runs Grafana 11.2.0. Note that Grafana 11.2 splits
  `GF_INSTALL_PLUGINS` on whitespace, so the version is a separate token — `<id>==<version>`
  is rejected with `404: Plugin not found`.
- **Database**: the plugin needs `jsonData.defaultDatabase` (not `database`) — without it the
  queries run in `default` and `audit_events_hot` is not found.

## Drill-down into the trace-viewer

Rate changes are only useful together with the traces around them, so the dashboard links
straight to the trace-viewer (Langfuse by default, per E1.5 "базовый trace-viewer"):

- **Header link** "Open trace-viewer" → the trace-viewer root.
- **Graph** "Sampler rate over time" → click a point → `?timestamp=<point time>`.
- **Table** "Sampling decisions by reason" → click a row timestamp → `?timestamp=<row time>`.

The target is a dashboard variable `trace_viewer_url` (textbox, default `http://localhost:3000`).
Change it in the variable picker to drill into another UI without editing the JSON:

| Target | Value |
|---|---|
| Langfuse (default, E1.5) | `http://localhost:3000` |
| Phoenix (PC28/PC31) | `http://localhost:6006` |

## Time Ranges

The time picker offers `1h` / `6h` / `24h` / `7d` (plus the Grafana defaults) and every panel
and annotation follows it:

- **1h**: Recent changes, debugging current issues
- **6h**: Pattern analysis, daily operational review
- **24h**: Long-term trends, capacity planning
- **7d**: Historical analysis, optimization opportunities

## Alerting Scenarios

### CPU High (Red Annotations)
- **Condition**: Rate drops to 5% with `cpu_high` reason
- **Action**: Check system CPU, scale infrastructure if needed
- **Escalation**: On-call engineer if sustained > 10 minutes

### Error High (Rate Spike to 30%)
- **Condition**: Rate increases to 30% with `error_high` reason
- **Action**: Check agent health, investigate error root cause
- **Escalation**: On-call engineer if error rate > 10%

### Eval Feedback Degradation (Quality Drop)
- **Condition**: Rate increases to 25% with `eval_feedback_degradation` reason
- **Action**: Check LLM prompts, evaluate quality metrics
- **Escalation**: ML engineer if sustained degradation

### Security Keep (Critical Traces)
- **Condition**: Rate increases to 100% with `security_keep` reason
- **Action**: Investigate security incident, check logs
- **Escalation**: Security team immediately

## Debugging Workflow

1. **Identify Issue**: Check the "Sampler rate over time" graph for unusual rate changes
2. **Check Context**: View the annotations on the graph for the specific reason
3. **Correlate**: Check the eval-score and system panels for quality/load correlation
4. **Audit Trail**: Read the "Sampling decisions by reason" table (ClickHouse) for old → new rate
5. **Drill-down**: Click the rate-change point or the table timestamp to open the trace-viewer
6. **Impact Assessment**: Check the "Kept spans by reason" bar for data retention impact

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Orange annotations missing | ClickHouse plugin not installed (offline build) | `docker compose up -d grafana` with network access, or check `docker compose logs grafana \| grep -i plugin` |
| Annotation query error | `clickhouse` datasource not provisioned | Check `infra/grafana/provisioning/datasources/clickhouse.yml` and the `CLICKHOUSE_*` env of the grafana service |
| Annotation query error, wrong table | ClickHouse schema not initialised | `docker compose run --rm clickhouse-init` |
| Table empty, graph fine | No `sampler.rate_change` rows in the window | The policy engine only writes an audit event when the rate actually changes |
| Eval panel empty | `faithfulness_score` is not exported by the eval worker yet | The PP04 feedback loop reads the same metric; both stay empty until the eval worker exports it |
| Drill-down opens the wrong UI | `trace_viewer_url` still points at the default | Set the variable in the picker to the UI you need |

## Integration with Other Dashboards

- **Agent Health Dashboard**: Cross-reference with error rates and latency
- **LLM Performance Dashboard**: Correlate with eval scores and quality metrics
- **Storage Dashboard**: Monitor impact on storage costs and retention

## Maintenance

- **Refresh Rate**: 30 seconds (real-time monitoring)
- **Data Retention**: 6-12 hours for operational use
- **Archival**: Long-term trends should be analyzed from Prometheus/ClickHouse directly
- **Plugin**: `grafana-clickhouse-datasource 4.14.0` — bump together with the Grafana image
  (plugin 4.15.0+ needs Grafana >= 11.6.0)

## PP03-PP06 Features

This dashboard implements the complete adaptive sampling v2 feature set:

- **PP03**: Eval-aware content sampling (faithfulness < 0.7 → 30% sampling)
- **PP04**: Feedback loop from eval scores to global rate
- **PP05**: Complete audit trail for all policy decisions
- **PP06**: Visualization of policy history with correlations
