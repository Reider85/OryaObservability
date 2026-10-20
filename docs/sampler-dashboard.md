# Sampler Policy History Dashboard

## Overview

This dashboard provides real-time visibility into the adaptive sampling policy engine (PP03-PP06) and helps SRE teams understand sampling rate changes and their impact.

## Panels

### 1. Sampler Rate Over Time
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

### 2. Sampling Decisions by Reason
- **What**: Distribution of policy triggers
- **Metrics**: `rate(agent_obs_tail_sampler_policy_evaluations_total)` by `reason`
- **Key Insights**: Which policies are firing most often
- **Actions**: 
  - High `error_high`: Check agent health, increasing errors
  - High `eval_feedback_degradation`: Check LLM quality, prompt engineering
  - High `cpu_high`: Scale infrastructure, optimize CPU usage

### 3. Kept Spans by Reason
- **What**: Number of spans preserved by each policy
- **Metrics**: `rate(agent_obs_tail_sampler_traces_sampled_total)` by `decision`
- **Key Insights**: Impact of sampling policies on data retention
- **Actions**: Monitor if critical policies are preserving expected data volume

### 4. Eval Score Correlation
- **What**: Average faithfulness score (PP04 feedback loop)
- **Metrics**: `avg_over_time(faithfulness_score[5m])`
- **Key Insights**: Quality trends vs sampling rate changes
- **Correlations**:
  - Score drops → Rate increases to 25% (degradation)
  - Score < 0.6 → Rate increases to 40% (heavy degradation)
  - Score recovers → Rate returns to 10% (normal)

### 5. System Correlation
- **What**: System health metrics
- **Metrics**: `agent_obs_system_cpu_ratio`, `agent_obs_agent_error_rate_5m`
- **Key Insights**: Baseline system health vs sampling policy triggers
- **Correlations**:
  - CPU > 80% → Rate drops to 5% (cpu_high)
  - Error rate > 5% → Rate increases to 30% (error_high)

## Annotations

### Policy Reason Changes (Red)
- **Trigger**: `changes(agent_obs_tail_sampler_current_rate[2m]) > 0`
- **Shows**: Every sampling rate change with reason
- **Usage**: Click to see what caused the change

### Sampling Decisions (Orange)
- **Source**: `audit_events_hot` table
- **Shows**: Detailed audit trail of rate changes
- **Usage**: Click to see full audit event details

## Time Ranges

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

1. **Identify Issue**: Check Panel 1 for unusual rate changes
2. **Check Context**: View annotations (Panel 1) for specific reasons
3. **Correlate**: Check Panels 4-5 for system/quality correlations
4. **Audit Trail**: Click orange annotations for detailed audit events
5. **Impact Assessment**: Check Panel 3 for data retention impact

## Integration with Other Dashboards

- **Agent Health Dashboard**: Cross-reference with error rates and latency
- **LLM Performance Dashboard**: Correlate with eval scores and quality metrics
- **Storage Dashboard**: Monitor impact on storage costs and retention

## Maintenance

- **Refresh Rate**: 30 seconds (real-time monitoring)
- **Data Retention**: 6-12 hours for operational use
- **Archival**: Long-term trends should be analyzed from Prometheus/ClickHouse directly

## PP03-PP06 Features

This dashboard implements the complete adaptive sampling v2 feature set:

- **PP03**: Eval-aware content sampling (faithfulness < 0.7 → 30% sampling)
- **PP04**: Feedback loop from eval scores to global rate
- **PP05**: Complete audit trail for all policy decisions
- **PP06**: Visualization of policy history with correlations