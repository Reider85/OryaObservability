# Canary runbook (PP29, T3.6.5)

How the SLO-gated canary works, what to do when it rolls back, and how to read
the audit trail it leaves behind.

## The loop

```
deploy 5%  →  observe 30 min  →  scripts/canary_check.py  →  ramp to 100%
                                                  └─ exit 1 → rollback + page
```

The whole loop lives in `.github/workflows/canary.yml` (dispatch it with the
`new_version` / `old_version` inputs). Three pluggable shell commands come from
repository **variables** — the repo ships no k8s or service-mesh manifests, so
the workflow deliberately does not know *how* to move traffic:

| Variable | Meaning |
|---|---|
| `CANARY_DEPLOY_CMD` | deploy `new_version` at 5% traffic (feature flag / mesh / compose) |
| `CANARY_RAMP_CMD` | take `new_version` to 100% traffic |
| `CANARY_ROLLBACK_CMD` | revert to `old_version` |

All three must be set — the workflow fails fast otherwise. Secrets consumed by
the gate and the rollback side effects: `CLICKHOUSE_HOST/PORT/DB/USER/PASSWORD`
(audit destination), `SLACK_WEBHOOK_URL`, `AGENT_OBS_TELEGRAM_BOT_TOKEN` +
`AGENT_OBS_TELEGRAM_CHAT_ID`. Optional: `PROMETHEUS_URL` (PP27 pre-deploy gate)
and `AGENT_OBS_CANARY_BURN_THRESHOLD`.

## How the gate decides

`scripts/canary_check.py --new-version X --old-version Y` reads ClickHouse
(`HotStore.get_sli_stats`) for the observation window — 30 minutes by default
(`AGENT_OBS_CANARY_WINDOW`, workflow input `observation_minutes`) — computes
`burn_rate = (1 - SLI) / (1 - target)` per SLO for **each version separately**,
and compares `new_version` against a threshold:

- **threshold** — `--threshold` > `AGENT_OBS_CANARY_BURN_THRESHOLD` > the SLO
  catalog's fast-pair alert threshold (14.4 for availability/latency_p95, 6.0
  for quality/cost_p99). Using the same numbers PP26 pages on keeps the gate
  and the alert rules agreeing on "burning too fast".
- **breach is a strict `>`** — burning exactly at the sustainable rate is not
  an incident (same rule as PP26 alerts).

Exit codes:

| Code | Meaning | Workflow action |
|---|---|---|
| `0` | healthy — every evaluated SLO under threshold | ramp to 100% |
| `1` | unhealthy — at least one SLO over threshold | rollback + page |
| `2` | undeterminable — ClickHouse unreachable, or the new version produced **no telemetry at all** in the window | rollback + page (fail-closed) |

Fail-closed is intentional: a canary that emits nothing cannot prove itself
healthy, and a monitoring outage must never silently enable a ramp. Escape
hatch: `--fail-open` / `AGENT_OBS_CANARY_FAIL_OPEN=1`.

An SLO with zero events for `new_version` *while other SLOs have traffic* is
reported as `skipped: no_data` and does not fail the gate — no events burn no
budget (e.g. the LLM judge is off, so `quality` has nothing to say).

## Where the version comes from

`spans_hot` has no version column. The SDK stamps
`span.attributes["agent_version"]` in `ObservabilitySDK._enqueue` from
`AGENT_OBS_DEPLOY_VERSION` (set by the workflow on the deploy step; the deploy
command should propagate it into the canary pods' environment), falling back to
the version passed into the span context. `get_sli_stats(version=...)` then
filters with `JSONExtractString(attributes, 'agent_version') = ...`; `quality`
is scoped through the `trace_id`s that version produced.

If the gate reports `no telemetry for new_version`, check in this order:

1. The deploy command actually set `AGENT_OBS_DEPLOY_VERSION` to `new_version`.
2. The canary pods are emitting spans (any traffic at all in the window).
3. The exporter really writes `spans_hot` (the version is a span *attribute*,
   it must survive whatever writes the rows).

## Rollback: what actually happens

`scripts/canary.py rollback --new-version X --old-version Y`, in order —
**steps 2 and 3 always run, even when step 1 fails**:

1. **Revert** — runs `CANARY_ROLLBACK_CMD` (`--cmd` overrides, `--dry-run`
   skips execution and records `decision="rollback_simulated"`).
2. **Audit** — inserts a `canary_rollback` row into `audit_events_hot`:
   `action="canary_rollback"`, `decision` ∈ `rolled_back | rollback_failed |
   rollback_simulated`, `audit_id` prefixed `cy-`, and a self-contained
   `resource` payload `{new_version, old_version, slo, window, threshold,
   burn_rates, breached_slos}`.
3. **Notify** — Slack (`SLACK_WEBHOOK_URL`) and Telegram (if both
   `AGENT_OBS_TELEGRAM_*` are set), best-effort: a dead webhook logs a warning
   and never fails the rollback.

Exit codes: `0` rollback executed (or simulated), `1` revert command missing /
non-zero / audit write failed. In GitHub Actions the job is already red when
this step runs, which is what pages the on-call.

### Reading an incident from the audit trail

```sql
SELECT timestamp, decision, reason, resource
FROM audit_events_hot
WHERE action = 'canary_rollback'
ORDER BY timestamp DESC
LIMIT 20;
```

`resource.burn_rates` holds exactly what the gate saw; `breached_slos` names
the SLOs over threshold. The same rows appear as red "Canary rollbacks"
annotations on the PP28 Grafana SLO dashboard.

### Rollback itself failed (`decision="rollback_failed"`)

1. Read the `error` field in `resource` — it contains the revert command's
   exit code and stderr tail.
2. Re-run manually: `python scripts/canary.py rollback --new-version X
   --old-version Y` (or the raw `CANARY_ROLLBACK_CMD`).
3. If the audit row is missing entirely, ClickHouse was unreachable — check the
   job log for `canary rollback audit write FAILED`; the notification was still
   attempted.

## Rehearsing without a cluster

```bash
python scripts/canary.py rollback --new-version 1.4.0 --old-version 1.3.9 \
  --dry-run --burn-rates "availability=20.1,latency_p95=3.2"
```

`--dry-run` skips the revert command and tolerates an unreachable ClickHouse,
so it is safe to run anywhere. Omit `--burn-rates` to let the script re-query
the current burn rates instead.

## Related

- `docs/slo-alerts-runbook.md` — burn-rate alerting (PP26), same thresholds.
- `docs/error-budget-runbook.md` — the PP27 freeze that blocks a canary before
  it takes traffic.
- `docs/slo-dashboard.md` — reading SLI / burn-rate / error-budget panels.
