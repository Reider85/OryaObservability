# SLO Dashboard

## Overview

This dashboard answers one question: **are we within our service level objectives, and if not — which traces are burning the budget?** It visualizes the four PP25 catalog SLOs (availability, latency_p95, quality, cost_p99) with their SLI, multi-window burn rate and error budget, plus an at-a-glance overall health gauge.

- **File**: `infra/grafana/dashboards/slo.json`
- **UID**: `slo`
- **Provisioning**: file-based (`infra/grafana/provisioning/dashboards/dashboards.yml`), no manual import
- **URL**: http://localhost:3001 (host port `GRAFANA_PORT`, default 3001)
- **Data sources**: Prometheus (all `slo_*` gauges), ClickHouse (degradation tables + annotations)

All metric names are read verbatim from the PP25 refresher (`agent_obs/slo/refresher.py`), which recomputes them from ClickHouse every 5 minutes (`AGENT_OBS_SLO_REFRESH_SECONDS`, default 300s):

| Metric | Meaning |
|---|---|
| `slo_target{name}` | The SLO target (0..1), constant per SLO |
| `slo_actual_sli{name}` | Rolling 30d SLI, refreshed every 5 minutes |
| `slo_burn_rate{name,window}` | Burn rate per alerting window. `window` is a **duration** (`5m`/`1h`/`6h`/`1d`), not `short`/`long` — the roles map to durations in `agent_obs/slo/burn_rate.py` |
| `slo_error_budget_remaining{name}` | Unspent budget share (0..1) on the rolling window (PP27) |
| `slo_error_budget_actions_taken_total{name,action}` | Counter of PP27 policy transitions (`soft` / `hard` / `recover`) |

## Panels

### 0. Top row: overall health + budget actions

#### SLO overall health (stat)
- **What**: How many of the 4 SLOs have `slo_error_budget_remaining <= 0.2`
- **Colors**: **green** = all 4 in budget, **yellow** = 1–2 SLOs at risk, **red** = more than 2 SLOs exhausted
- **Definition**: "Exhausted" here means *at or below the PP27 soft threshold (0.2)* — the same boundary at which the sampler drops to 5% and the eval budget is halved. The gauge therefore turns yellow **before** a budget is fully spent, leaving room to react. See `docs/error-budget-runbook.md` for the state machine.

#### Error budget actions (policy transitions) (timeseries)
- **What**: `sum(rate(slo_error_budget_actions_taken_total[1h])) by (action)`
- **Key insights**: Each PP27 transition fires once per state change (not per tick), so a spike here means SLOs are flipping between ok → soft → hard → ok. Correlate with the annotations below.

### 1–4. One section per SLO: `SLO: availability`, `SLO: latency_p95`, `SLO: quality`, `SLO: cost_p99`

Each collapsed section has the same four panels (all thresholds come from `SLO_CATALOG` in `agent_obs/slo/definitions.py`):

#### a. SLI vs target (gauge)
- **What**: `slo_actual_sli{name="<slo>"}` with `slo_target{name="<slo>"}` as the second series. The gauge turns green exactly at the target.
- **How to read**: Values are recomputed every 5 minutes over the rolling 30d window. Note the hot-tier retention caveat: spans are kept 21d / evals 14d, so the 30d SLI is a lower bound until warm-tier rollups land (PP25).
- **Targets**: availability 0.999 · latency_p95 0.95 · quality 0.90 · cost_p99 0.95

#### b. Burn rate, short + long windows (timeseries)
- **What**: `slo_burn_rate{name="<slo>",window="<w>"}` — one line per alerting window published by the refresher (`5m`/`1h`/`6h` for availability & latency_p95, `1h`/`6h`/`1d` for quality & cost_p99).
- **How to read**: `burn_rate = (1 - actual SLI) / (1 - target)`. **1.0** = budget is being consumed exactly at the sustainable rate; **> 1.0** = the SLO will be missed at this pace. Threshold lines: yellow 3.0, orange 6.0, red 14.4 (Google SRE Workbook multi-window burn rates, the same numbers PP26 alerting uses).
- **Alerting convention**: PP26 fires only when **both** windows of a pair exceed the threshold (e.g. 5m > 14.4 AND 1h > 14.4), so a single spike on the short line is not yet an alert.

#### c. Error budget remaining (gauge)
- **What**: `slo_error_budget_remaining{name="<slo>"}` (0..1).
- **Colors**: **red** = below 0.2 (PP27 soft threshold — sampler cut to 5%, eval budget halved), **orange** = 0.2–0.5, **green** = above 0.5. At **0** the budget is frozen: the CI/CD gate blocks deploys (PP27 hard action, `freeze_deploy`).

#### d. Degradation traces (table) — the drill-down detail-view
Which traces burned *this* budget in the selected time range (worst first, max 50 rows):

| SLO | Query source | "Bad event" definition (mirrors the SLI) |
|---|---|---|
| availability | `spans_hot` | root span (`span_type='agent.loop'`) with `status='error'` |
| latency_p95 | `spans_hot` | root span slower than 3.0s |
| quality | `eval_results_hot` | eval result with `faithfulness < 0.7` |
| cost_p99 | `spans_hot` | trace with `sum(cost_usd) > 0.05` |

Every table is filtered with `$__timeFilter(...)`, so it always shows the same window as the graphs above it.

## Drill-down into the trace-viewer

An SLI or burn-rate number is only actionable together with the traces behind it:

- **Gauge click**: click the SLI gauge or the error-budget gauge → jumps to that section's *degradation traces* table inside this dashboard (`#/d/slo?viewPanel=<id>`).
- **Table row click**: click a `trace_id` cell → the trace-viewer opens **on that trace** (`?trace_id=<id>`); click a timestamp cell → the trace-viewer opens at that moment (`?timestamp=<time>`).
- **Burn-rate graph click**: click a point → the trace-viewer opens at that timestamp.
- **Header link** "Open trace-viewer" → the trace-viewer root.

The target is the dashboard variable `trace_viewer_url` (textbox, default `http://localhost:3000`). Change it in the variable picker to drill into another UI without editing the JSON:

| Target | Value |
|---|---|
| Langfuse (default, E1.5) | `http://localhost:3000` |
| Built-in PP10 trace viewer | `http://127.0.0.1:8089` |
| Phoenix (PC28/PC31) | `http://localhost:6006` |

## Annotations (deploy + incident markers)

All three are read from the ClickHouse audit trail (`audit_events_hot`) with `$__timeFilter(timestamp)`, so they follow the time picker:

| Annotation | Source `action` | Shows | Meaning |
|---|---|---|---|
| Deploy freezes (purple) | `freeze_deploy` | `<slo> deploy frozen` | PP27 hard action: the error budget hit 0 and the CI/CD gate froze deploys |
| Error budget state changes (red) | `error_budget.state_change` | `<slo> budget soft/hard/recover` | Every PP27 ok → soft → hard → recover transition, with the remaining ratio in the text |
| Incident mode, eval sampler (orange) | `eval_sampler.mode_change` (only `new_mode='incident'`) | `eval sampler -> incident` | The adaptive eval sampler entered incident mode — an incident was declared (PP12/PP14) |

## Time ranges

The picker offers `1h` / `6h` / `24h` / `7d` / `30d` (default `now-24h`). Every panel and annotation follows it. Use `30d` to review the full SLO window, `1h`/`6h` to investigate a live burn.

## Reading the dashboard: action playbook

### All green
Nothing to do. The refresh cadence (1m dashboard / 5m SLI) means the numbers lag reality by at most ~5 minutes.

### Yellow on the overall gauge (1–2 SLOs at risk, remaining <= 0.2)
1. **Confirm which SLO** — the error-budget gauges show which ones dropped below 0.2.
2. **Is it a spike or a trend?** — open the burn-rate panel of that SLO. Short window (`5m`/`1h`) high but long window (`6h`/`1d`) near 1.0 → transient spike, keep watching. Both high → sustained burn, act now.
3. **Find the culprits** — open the section's *degradation traces* table (or click the gauge) and drill into the worst traces in the trace-viewer.
4. **Check the annotations** — did a deploy or an incident coincide with the burn start?
5. Follow `docs/error-budget-runbook.md` (budget mechanics) and `docs/slo-alerts-runbook.md` (alert thresholds).

### Red on the overall gauge (> 2 SLOs exhausted)
1. This is the PP27 hard territory — deploys may already be frozen (`freeze_deploy` annotations) and the on-call paged.
2. Treat as an incident: the eval sampler should already be in incident mode (orange annotation).
3. Per-SLO triage via the degradation tables, worst SLO first (availability before quality — availability errors usually drive the other budgets too).

### Per-SLO scenarios

- **availability burning**: error root spans in the table → open the worst trace, find the failing tool/LLM call. Error budget state change annotations tell you when the sampler switched to keeping more error traces (`error_keep`), so error traces *should* be well represented in the hot tier.
- **latency_p95 burning**: slow root spans (> 3s) in the table → open the worst traces and look at the longest child spans (LLM call latency dominates). Check whether CPU was saturated (`agent_obs_system_cpu_ratio` on the sampler dashboard).
- **quality burning**: low-faithfulness evals in the table → open the traces, review prompts. Note the PP27 soft action halves the eval budget, which thins the very signal this SLO is measured on — prefer prompt fixes over eval cuts while quality is degraded.
- **cost_p99 burning**: expensive traces (> $0.05) in the table → open the worst traces and look at which spans carry `cost_usd` (usually the LLM calls). Check for retry loops or oversized prompts.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Gauges empty, "No data" | The SLO refresher is not running or has not completed its first cycle | Start the refresher; the first cycle queries ClickHouse for all windows and can take a few seconds. Cadence: `AGENT_OBS_SLO_REFRESH_SECONDS` (default 300) |
| Gauges stuck at old values | ClickHouse queries failing — the refresher logs and keeps previous values (SDK invariant: never block) | `docker compose logs` the process running `SLORefresher`; fix ClickHouse connectivity |
| Burn-rate lines missing for some windows | Those windows were never published because no alert pair references them | Expected: only catalog windows (`5m`/`1h`/`6h`/`1d`) exist. `5m`/`1h` exist only for availability & latency_p95; `1d` only for quality & cost_p99 |
| Degradation tables empty, gauges fine | No bad events in the selected window, or the table's SLI source table is empty | Check the time picker; for `quality` confirm the eval worker writes to `eval_results_hot` |
| Annotation query error | `clickhouse` datasource not provisioned / plugin missing | Same fix as the sampler dashboard: see "Troubleshooting" in `docs/sampler-dashboard.md` |
| Drill-down opens the wrong UI | `trace_viewer_url` still points at the default | Set the variable in the picker to the UI you need |
| SLI looks better than reality over 30d | Hot-tier TTLs (spans 21d / evals 14d) are shorter than the 30d SLO window | Known limitation (PP25): the 30d SLI is a lower bound until warm-tier rollups are wired |

## Related docs

- `docs/slo-alerts-runbook.md` — PP26 multi-window burn-rate alerting (thresholds 14.4 / 6.0 / 3.0)
- `docs/error-budget-runbook.md` — PP27 error-budget states and the sampler/eval/deploy actions
- `docs/sampler-dashboard.md` — sampling-rate dashboard (the error-budget low action drops the rate to 5%)
- `docs/trace-viewer.md` — the built-in PP10 trace viewer (`http://127.0.0.1:8089`)
