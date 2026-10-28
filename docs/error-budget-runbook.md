# Error budget runbook (PP27, T3.6.3)

What to do when an SLO's error budget is running out — or already gone.

The error budget is the allowance of bad events a target implicitly grants:
`budget_total = (1 - target) * total_events` over the rolling 30-day window.
`remaining_ratio` is the unspent share. Two automated responses exist, fired
by `agent_obs/slo/error_budget.py` (`ErrorBudgetPolicy`, evaluated by
`SLORefresher` every 5 minutes) and mirrored by Prometheus alerts in the
`slo-error-budget` group of `infra/prometheus-rules.yml`:

| State | Condition | Automated actions | Alert |
|---|---|---|---|
| `soft` | remaining < 0.2 (>80% consumed) | tail-sampling of normal traces → 5%; eval cost budget × 0.5 | `SLOErrorBudgetLow` (ticket → Jira) |
| `hard` | remaining ≤ 0 (100% consumed) | CI/CD gate blocks deploys; `freeze_deploy` audit row | `SLOErrorBudgetExhausted` (page → PagerDuty) |

Actions fire **once per state transition** (ok → soft → hard), not per refresh
tick. Recovery (any state → ok) restores normal rates and clears the Redis
policy flag.

## How the signals travel

- **Gauge** `slo_error_budget_remaining{name}` — published by `SLORefresher`
  from ClickHouse SLI counts. The sampler proxy's policy engine reads
  `min(slo_error_budget_remaining)` and drops the normal-trace keep-rate to
  5% with reason `error_budget_low` (checked after `cpu_high`, before
  `error_high`).
- **Redis** key `agent_obs:error_budget:policy_state` — written by the policy,
  read by the eval `BudgetEnforcer`, which halves its effective daily budget
  (`budget_usd × 0.5`) while the state is soft or hard.
- **CI/CD gate** `scripts/cicd/error_budget_gate.py` — queries Prometheus,
  exits 1 while any SLO is exhausted. Wire it as a required status check:

  ```yaml
  # GitHub Actions example — .github/workflows/canary.yml runs this as its
  # first gate (PP29), before the canary ever takes traffic
  - name: Error budget gate
    run: python scripts/cicd/error_budget_gate.py
  ```

  Exit codes: `0` allowed, `1` budget exhausted (blocked), `2` Prometheus
  unreachable (blocked unless `AGENT_OBS_GATE_FAIL_OPEN=1` / `--fail-open`).
  The env knob `AGENT_OBS_ERROR_BUDGET_SOFT_THRESHOLD` (default `0.2`) moves
  the soft boundary; the hard boundary is fixed at 0.

## Soft: budget < 20% remaining

The system is still serving, but the allowance for mistakes is nearly gone.

1. **Confirm which SLO** — `slo_error_budget_remaining` in Grafana (PP28
   dashboard) or `promtool` query. Note which of availability / latency_p95 /
   quality / cost_p99 is low; the others keep full rates.
2. **Stop spending budget on avoidable risk.** Freeze risky changes: model
   swaps, prompt rewrites, big refactors in the agent loop. Normal feature
   work can continue — the soft mode already cut observability spend (sampler
   5%, eval budget halved), so the budget recovers faster than it burns.
3. **Find the burn driver** — the burn-rate alerts (`SLOBurnRateHigh` /
   `SLOBurnRateSlowBurn`, runbook: `slo-alerts-runbook.md`) tell you which
   window is burning and how fast. Fix the root cause; the budget refills as
   the 30-day window slides past the bad events.
4. **Do not "fix" the gauge.** The budget is computed from real ClickHouse
   counts; manually editing the Redis flag or the sampler rate does nothing
   — the next refresh overwrites it.

Recovery is automatic: once `remaining ≥ 0.2` for a refresh, the policy
returns to ok, the sampler resumes its normal rate, the eval budget
multiplier resets to 1.0, and a `recover` action is counted in
`slo_error_budget_actions_taken_total{name,action="recover"}`.

## Hard: budget exhausted (deploys frozen)

The SLO has spent its entire 30-day allowance of bad events. Deploys are a
second incident waiting to happen, so the gate holds them.

1. **Acknowledge the page.** The audit trail has a row with
   `action="freeze_deploy"` (`audit_events_hot`, actor
   `error-budget-policy`) — read the reason: it names the SLO and the exact
   consumed/total counts.
2. **Stabilise, don't ship.** Work the root cause of the burn (usually the
   same one that fired the burn-rate page). Roll back the offending change if
   it is identifiable — that is the fastest way to stop new errors landing.
3. **Unfreeze is a human decision.** The gate re-reads Prometheus on every
   CI run: once the refresher publishes `remaining > 0`, the next pipeline
   passes without any manual step. If you must deploy an emergency fix while
   the budget is still exhausted (e.g. the fix *is* the stabilisation), run
   the pipeline with the gate bypassed and record why in the incident notes —
   do not edit metrics or the Redis flag.
4. **Expect a slow refill.** For a 99.9% SLO the whole month's budget is ~43
   minutes of downtime; a hard breach at mid-month means roughly half a month
   of waiting unless the bad events age out sooner. If the org cannot afford
   that, the fix is raising the target or improving reliability — not
   loosening the gate.

## Metrics reference

| Metric | Type | Labels | Meaning |
|---|---|---|---|
| `slo_error_budget_remaining` | gauge | `name` | Unspent share (0..1, clamped) |
| `slo_error_budget_actions_taken_total` | counter | `name`, `action` (`soft`/`hard`/`recover`) | Policy transitions fired |

## Related runbooks

- `docs/slo-alerts-runbook.md` — burn-rate alerts (PP26): *why* the budget is
  burning, per-SLO first steps.
- `docs/eval-budget-runbook.md` — the eval pipeline's own daily cost budget
  (PP15), which PP27 multiplies by 0.5 during soft/hard.
