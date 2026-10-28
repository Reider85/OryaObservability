# SLO burn-rate alerts runbook (PP26, T3.6.2)

Multi-window multi-burn-rate alerting per the Google SRE Workbook. Alerts
fire from the `slo-burn-rate` group in `infra/prometheus-rules.yml`, routed
by `infra/alertmanager.yml`:

| Severity | Alert name        | Receiver                    | Meaning                                  |
|----------|-------------------|-----------------------------|------------------------------------------|
| `page`   | `SLOBurnRateHigh` | PagerDuty (`PAGERDUTY_SERVICE_KEY`) | Fast burn — budget gone in hours; wake on-call |
| `ticket` | `SLOBurnRateSlowBurn` | Jira webhook (`JIRA_WEBHOOK_URL`) | Slow burn — remediate within the business day |

The in-process evaluator with identical math lives in
`agent_obs/slo/burn_rate.py` (`SLOBurnRate`), fed by the `slo_burn_rate`
gauges published by `SLORefresher` (5-minute cadence, see
`AGENT_OBS_SLO_REFRESH_SECONDS`).

## Why multi-window (and why single-window is forbidden)

Burn rate over a single short window is noisy: one bad minute spikes a 5m
window to 100x without any real regression. The SRE Workbook correlation
rule requires **both** an SLO's short and long window to exceed the pair
threshold simultaneously before alerting:

| SLO           | Page pair (fast burn)    | Ticket pair (slow burn)   |
|---------------|--------------------------|---------------------------|
| `availability`| 5m + 1h @ 14.4x          | 1h + 6h @ 6.0x            |
| `latency_p95` | 5m + 1h @ 14.4x          | 1h + 6h @ 6.0x            |
| `quality`     | 1h + 6h @ 6.0x           | 6h + 1d @ 3.0x            |
| `cost_p99`    | 1h + 6h @ 6.0x           | 6h + 1d @ 3.0x            |

Burn rate `N` means the error budget is being consumed `N`× faster than the
target allows: at 14.4x, a 30-day budget (~43 minutes of downtime for a
99.9% SLO) is exhausted in ~2 hours. `quality`/`cost_p99` use slower pairs
because LLM-judge scores and cost rollups are late/noisy at 5m granularity.

Thresholds are defined once in `SLO_CATALOG`
(`agent_obs/slo/definitions.py`) and mirrored verbatim in
`infra/prometheus-rules.yml` — if you change one, change the other (the
tests in `tests/test_slo_burn_rate_alerts.py` assert they match).

## First steps (any SLO alert)

1. **Confirm it's real, not a refresher artifact.** Check the gauges:
   ```
   slo_burn_rate{name="<slo>",window="<w>"}
   ```
   If a window's series is missing, `SLORefresher` may be failing — check
   its logs (`SLO refresh failed`) and ClickHouse availability. A stale
   refresher holds the *previous* values, it never fabricates burns.
2. **Look at the SLO dashboard (PP28)** for the affected SLO: which time
   range, which agents/tenants dominate the bad events.
3. **Check recent changes**: deploys, prompt edits, model swaps, provider
   incidents. Most fast burns trace back to one of these.
4. **Drill down**: trace-viewer → affected traces (status/latency/cost
   fields); cold traces via the drill-down button (PP21–PP24).
5. **Mitigate first, root-cause second** for `page` alerts: roll back the
   deploy or pin the model version; the budget keeps burning while you
   investigate.

## Per-SLO guidance

### availability
Target 99.9% of root traces non-error over 30d (`status != 'error'`;
`blocked` guardrail refusals count as good).

- Fast burn usually = a deploy that broke the agent loop or an upstream
  LLM provider outage. Roll back / fail over.
- If errors are concentrated by `agent_id`/`tenant_id`, consider
  quarantining that agent instead of a global rollback.

### latency_p95
Target: 95% of traces complete within 3s (root span `end - start`).

- Check LLM provider latency first (external), then agent-loop hot paths,
  then ClickHouse write latency (exporter backlog shows up as span
  timestamps stretching).
- `SamplerDecisionLatencyHigh` / aggregation alerts firing at the same
  time point at SDK-side saturation — scale the host.

### quality
Target: 90% of late-annotated evals with `faithfulness > 0.7`.

- Compare recent prompts/models against the last known-good version
  (eval results table, golden-set dashboard from PP13).
- A `GoldenEvalRegression` alert firing alongside confirms a regression;
  the golden eval's diff is usually the fastest root-cause path.

### cost_p99
Target: 95% of traces cost ≤ $0.05 (sum of `cost_usd` per trace).

- Look for runaway token usage (agent-loop retries, missing prompt
  caching) and expensive model swaps.
- Note the interaction with PP27 error-budget policy: budget exhaustion
  may already have lowered eval spend and sampling rates.

## Page vs ticket — what on-call owes

- **page (`SLOBurnRateHigh`)**: acknowledge in PagerDuty, mitigate
  (rollback/failover) before root-causing. Budget lifetime at 14.4x burn
  is ~2h.
- **ticket (`SLOBurnRateSlowBurn`)**: Jira ticket is created
  automatically; triage next business day. If the same SLO also fires the
  page alert, the page takes precedence and the ticket is a paper trail.

## Alert silencing

For planned maintenance, silence in Alertmanager (do NOT comment out
rules):

```
amtool silence add alertname="SLOBurnRateHigh" slo="availability" \
  --duration=2h --comment="planned failover"
```

## Related

- SLO catalog & burn-rate math: `agent_obs/slo/definitions.py` (PP25)
- Refresher (gauge source): `agent_obs/slo/refresher.py` (PP25)
- In-process evaluator: `agent_obs/slo/burn_rate.py` (PP26)
- Error budget + policy actions: `agent_obs/slo/error_budget.py` (PP27)
- Grafana dashboard: `infra/grafana/dashboards/slo.json` (PP28)
