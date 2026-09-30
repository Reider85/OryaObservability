# Adaptive tail sampler (PC30 — ticket T2.7.2)

Chooses the keep-rate for *normal* traces from the two load/error signals the
SDK already exports (PC29), and applies it without restarting anything.

```
SDK ──OTLP/HTTP──▶ sampler-proxy ──OTLP/HTTP──▶ Langfuse  (mode=proxy, default)
                          ▲
                          │ rate pushed on change
                    PolicyEngine ──30s tick──▶ Prometheus
                                                agent_obs_system_cpu_ratio
                                                agent_obs_agent_error_rate_5m
```

## Why a proxy instead of reconfiguring the Collector

PC30 anticipated three options and asked for the choice to be justified:

1. **Write the rate into an env file and restart the Collector.** Works, but
   every rate change is a container restart, and the Collector spends
   `decision_wait` (10s) with no pipeline while it boots. During an incident —
   exactly when the rate moves most often — that is the worst time to be
   restarting the trace pipeline.
2. **OTel Collector admin API.** Does not exist. `otel/opentelemetry-collector-contrib`
   has no runtime reconfiguration endpoint; `${env:TAIL_SAMPLER_NORMAL_RATE}` is
   interpolated once at process start. The only extension present is
   `health_check`, which is a read-only liveness probe.
3. **Python mini-proxy (MVP P20 option B).** Chosen.

The proxy keeps its rate in memory, so applying a new one is a single assignment
guarded by a lock — no restart, no reload, and the change takes effect for the
next batch. The cost is one extra hop inside the Docker network.

The Collector's own `tail_sampling` policies are untouched, so a deployment that
routes through the Collector still gets error / cost / security retention there.

## Two modes

Selected with `AGENT_OBS_SAMPLER_MODE`:

| Mode | Path | Rate applied by | Restart needed |
|---|---|---|---|
| `proxy` (default) | SDK → proxy → Langfuse | proxy, in memory | no |
| `file` | SDK → Collector → Langfuse | Collector, on boot | yes, out-of-band |

`file` mode is the escape hatch for deployments that must keep the Collector in
the trace path. The engine writes the rate atomically to a shared volume and the
Collector is restarted out-of-band to pick it up. It is correct but restart-bound,
which is why it is not the default.

## The rules

```
if system_cpu_ratio > 0.8:        rate = 0.05   reason=cpu_high
elif agent_error_rate_5m > 0.05:   rate = 0.30   reason=error_high
else:                              rate = 0.10   reason=default
```

`cpu_high` is checked first: a saturated host endangers the export path itself,
so protecting it outranks retaining extra error traces. Thresholds are strict
`>` — a signal sitting exactly on the threshold is not yet a reason to move.

`agent_error_rate_5m` is reported per agent; the engine takes the **max** across
agents, so one unhealthy agent can lift the rate on its own.

`evaluate_rate()` is a pure function, so the rules are tested without any I/O.

## Always-kept traces

Independent of the rate, a trace is kept when any of its spans carries:

- `status.code == STATUS_CODE_ERROR`
- `cost.over_budget == "true"`
- `security.incident == "true"`

These are the same keys the Collector's P20 policies match on, so behaviour does
not change with the mode.

The decision is taken **per trace, not per span**. A batch from `_export_worker`
mixes spans of many traces; dropping a trace's root while keeping a child would
leave an orphan in Langfuse.

For normal traces the decision hashes the trace id instead of drawing a random
number, so a retried or duplicated batch reaches the same verdict.

## Audit trail

Every rate change writes a `SamplerRateChangeAuditEvent` to
`audit_events_hot` (§3.4 ARCHITECT.md, 365-day retention). The row carries
`prev_rate`, `new_rate`, `prev_reason`, `new_reason`, `system_cpu_ratio` and
`agent_error_rate_5m` inside the `resource` JSON — the DDL needed no migration.

A missing trace can then be explained: *"the sampler was at 5% because
`system_cpu_ratio` was 0.92"*. PC31 adds the query API over these rows.

An audit write failure never blocks the rate change — it is logged and counted
in `agent_obs_tail_sampler_policy_evaluations_total{outcome="audit_failed"}`.

The sink is optional in the same direction: if `HotStore` cannot be constructed
(ClickHouse down, driver missing), the proxy logs a warning and starts with the
audit trail disabled rather than refusing to sample. Set
`AGENT_OBS_SAMPLER_AUDIT_ENABLED=0` to skip the audit sink deliberately.

> **Live-verified caveat:** the audit row only lands if the `observability`
> database exists. `infra/storage/clickhouse_init.sh` only runs on a *fresh*
> ClickHouse data directory, so an existing volume never gets it and writes fail
> with `Database ... does not exist`. Check with
> `SELECT count() FROM system.databases WHERE name='observability'`.

## Metrics

Exposed by the proxy on `:9095/metrics`:

| Metric | Type | Labels |
|---|---|---|
| `agent_obs_tail_sampler_current_rate` | Gauge | `policy_reason` |
| `agent_obs_tail_sampler_rate_changes_total` | Counter | `from_reason`, `to_reason` |
| `agent_obs_tail_sampler_traces_sampled_total` | Counter | `decision` |
| `agent_obs_tail_sampler_policy_evaluations_total` | Counter | `outcome` |

Labels carry *reasons*, not rates, so cardinality stays at 3×3 instead of growing
with every float value.

`GET :4321/sampler/status` returns the same state as JSON.

## Failure behaviour

| Condition | Behaviour |
|---|---|
| Prometheus unreachable | Signals read as 0.0 → default rate; warning logged. Never raises. |
| Prometheus returns no series | Same as above (pilot-agent not up yet). |
| One tick raises | Logged; the loop keeps ticking. The rate never freezes. |
| ClickHouse unavailable | Audit write fails, is logged and counted; the rate still applies. |
| Downstream 4xx | Not retried — a malformed batch would spin forever. |
| Downstream 5xx / 429 | Retried with backoff, up to `max_retries`. |

Absent metrics resolve to the **default** rate deliberately. Guessing high from
missing data would silently triple storage spend; guessing low would hide errors.

## Running

```bash
# Locally, forwarding to Langfuse
python -m scripts.sampler.sampler_proxy \
    --downstream-url http://localhost:3000 \
    --prometheus-url http://localhost:9090

# One-shot evaluation, prints the decision (useful for verifying a threshold)
python -c "import asyncio; from scripts.sampler.policy_engine import PolicyEngine; \
           e=PolicyEngine(); print(asyncio.run(e.run_once()))"

# Force a rate by hand — there is no override endpoint. POST to
# /sampler/status is the OTLP ingest path and will reject a rate body.
# Drive the rate through the policy inputs instead (e.g. load the CPU) and
# let the next poll cycle pick it up, or run the engine out of band:
#   docker exec -it <container> python -c "
#     import asyncio, sys; sys.path.insert(0,'/app')
#     from scripts.sampler.policy_engine import build_policy_engine_from_env
#     e = build_policy_engine_from_env(); print(asyncio.run(e.read_signals()))"
```

Docker:

```bash
docker build -f scripts/sampler/Dockerfile -t agent-obs-sampler .
docker build -f scripts/sampler/Dockerfile --target sampler-grpc -t agent-obs-sampler-grpc .

docker run -p 4321:4321 -p 9095:9095 \
    -e LANGFUSE_PUBLIC_KEY=... -e LANGFUSE_SECRET_KEY=... \
    -e PROMETHEUS_URL=http://host.docker.internal:9090 \
    agent-obs-sampler
```

Compose brings the service up with `docker compose up -d sampler-proxy`; see
`infra/docker-compose.yml`.

## Environment

| Variable | Default | Purpose |
|---|---|---|
| `AGENT_OBS_SAMPLER_MODE` | `proxy` | `proxy` or `file` |
| `AGENT_OBS_SAMPLER_POLL_INTERVAL` | `30` | Seconds between evaluations |
| `AGENT_OBS_SAMPLER_CPU_THRESHOLD` | `0.8` | cpu_high trigger |
| `AGENT_OBS_SAMPLER_ERROR_THRESHOLD` | `0.05` | error_high trigger |
| `PROMETHEUS_URL` | `http://localhost:9090` | Where to read the PC29 gauges |
| `DOWNSTREAM_URL` | `http://langfuse:3000` | Langfuse, or the Collector |
| `SAMPLER_PROXY_PORT_HTTP` | `4321` | OTLP/HTTP receiver |
| `SAMPLER_PROXY_PORT_GRPC` | `4320` | OTLP/gRPC receiver (needs `--grpc`) |
| `SAMPLER_PROXY_METRICS_PORT` | `9095` | Prometheus metrics + `/sampler/status` |
| `SAMPLER_RATE_FILE` | `/shared/sampler_rate.json` | `file` mode target |

## Pointing the SDK at the proxy

The SDK reads `LANGFUSE_HOST` (`LangfuseExporter.from_env`) and appends
`/api/public/otel/v1/traces`. In `proxy` mode, set it to the proxy so the
exporter targets the proxy instead of Langfuse:

```bash
LANGFUSE_HOST=http://localhost:4321
LANGFUSE_PUBLIC_KEY=...   # forwarded downstream as Basic Auth
LANGFUSE_SECRET_KEY=...
```

The proxy re-appends the `/api/public/otel/v1/traces` suffix when forwarding, so
`DOWNSTREAM_URL` must be the Langfuse *base* URL, not the traces path.
