# Observability Infrastructure

Local self-hosted observability stack for the `agent-obs` MVP
(Level 1 from `analytics/ROADMAP.md`).

**Services**

| Service | Image | Port | Purpose |
|---|---|---|---|
| `vault` | `hashicorp/vault:1.18` | `8200` | HashiCorp Vault for PII recovery storage (PC02) |
| `langfuse` | `langfuse/langfuse:3` | `3000` | Trace UI + OTLP ingest |
| `langfuse-worker` | `langfuse/langfuse-worker:3` | internal | Ingestion/queue workers (ClickhouseWriter, otel-ingestion) |
| `postgres` | `postgres:17` | internal | Langfuse metadata database (compose network only) |
| `redis` | `redis:7` | internal | Langfuse queues (compose network only, `requirepass`) |
| `clickhouse` | `clickhouse/clickhouse-server` | `8123`, `9000` | Langfuse event/observation storage (compose network only) |
| `minio` | `quay.io/minio/minio:latest` | `9000` | S3-compatible storage for Langfuse (S3 buckets; compose network only) |
| `otel-collector` | `otel/opentelemetry-collector-contrib:0.128.0` | `4317` gRPC, `4318` HTTP, `8888` self-telemetry | OTLP → tail-sampling → batch → Langfuse |
| `prometheus` | `prom/prometheus:latest` | `9091` (host) → `9090` | Scrapes Collector self-telemetry; recording rules for `tail_sampler_kept_ratio` (P21) |

> Langfuse v3 **requires** `CLICKHOUSE_URL`/`CLICKHOUSE_MIGRATION_URL` and an
> `ENCRYPTION_KEY` + `NEXTAUTH_SECRET` + `LANGFUSE_SALT` in `.env`. MinIO hosts
> the S3 bucket Langfuse uses for media/event uploads (fully offline mode via
> the compose network; Traefik/Let's Encrypt are **not** used).
>
> Postgres/Redis/ClickHouse/MinIO are deliberately **not** exposed on the host:
> their ports are commonly occupied by a local service. The SDK only talks to
> Langfuse (`:3000`) and the Collector (`:4317`/`:4318`). To debug the database
> directly, run `docker compose exec postgres psql -U langfuse`.

**Data flow**

```
SDK --OTLP--> otel-collector(:4317/:4318) --tail_sampling--> --batch--> langfuse(/api/public/otel/v1/traces)
                                          `-- metrics --> prometheus(:8888)
```

---

## 1. Start the stack

```bash
cd infra
docker compose up -d
docker compose ps          # all services should be Up/healthy
docker compose logs -f     # watch startup
```

First boot runs the Langfuse schema migrations (Postgres) and ClickHouse
migrations. Give it 30–60 seconds; `langfuse-worker` may log transient
`background_migrations` errors until the web migrations finish — that is benign.

Smoke check:

```bash
# Langfuse UI
curl http://localhost:3000/api/public/health        # {"status":"OK","version":"3.x", ...}
open http://localhost:3000                          # UI

# OTel Collector health
curl http://localhost:4318/v1/traces -XPOST -d '{}' # 400 Bad Request (receiver is up)
```

The Collector is healthy when its logs show the `health_check` endpoint
reporting "Everything is ready" on `:13133`.

## 2. Create a project and get API keys

1. Open the Langfuse UI at <http://localhost:3000>.
2. Sign in with the admin credentials from `.env`
   (`LANGFUSE_INIT_USER_EMAIL` / `LANGFUSE_INIT_USER_PASSWORD`).
3. A project `agent-obs` (`LANGFUSE_INIT_PROJECT_ID=prj-agent-obs`,
   `LANGFUSE_INIT_ORG_ID=org-agent-obs`) is created automatically on first boot
   with the placeholder API keys from `.env`. Verify the initial project via:

   ```bash
   curl http://localhost:3000/api/public/projects \
     -u "pk-lf-your-public-key-here:sk-lf-your-secret-key-here"
   ```
4. Go to **Project Settings → API Keys** and copy:
   - **Public Key** (`pk-lf-...`)
   - **Secret Key** (`sk-lf-...`)

## 3. Configure credentials

```bash
cd infra
cp .env.example .env
# edit .env, set:
#   LANGFUSE_SALT=<random string>
#   NEXTAUTH_SECRET=<random string>
#   ENCRYPTION_KEY=<64 hex chars>
#   LANGFUSE_INIT_USER_PASSWORD=<admin password>
#   LANGFUSE_PUBLIC_KEY=pk-lf-<from UI>
#   LANGFUSE_SECRET_KEY=sk-lf-<from UI>
docker compose up -d   # restart with real values
```

Never commit `.env` — it is gitignored (see `infra/.gitignore`).
The OTel Collector reads `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` from the
environment and uses Basic Auth on every request to Langfuse.

> Note: if you set key placeholders via `LANGFUSE_INIT_PROJECT_*`, the initial
> project in Langfuse gets those exact keys — convenient for local dev.

## 4. Sending telemetry

Send OTLP traces to:

- gRPC: `http://localhost:4317`
- HTTP/protobuf: `http://localhost:4318/v1/traces`

The Collector batches for 5 s (≤ 512 spans per batch) and pushes to
`http://langfuse:3000/api/public/otel/v1/traces` with Basic Auth.

## 5. Tail sampler configuration

The Collector runs a **tail-sampling** processor between OTLP receiver and the
batch processor (see `otel-collector/config.yaml`). Policies (P20):

| Order | Policy | Verdict |
|---|---|---|
| 1 | `status_code == ERROR` (any span of the trace) | keep 100% |
| 2 | `cost.over_budget == "true"` (root attribute) | keep 100% |
| 3 | `security.incident == "true"` (MVP stub, SDK always writes `"false"`) | keep 100% |
| 4 | fallback (`normal-sample`) | probabilistic keep |

The root-span attributes (`cost.over_budget`, `security.incident`, `status`)
are stamped by the SDK's per-trace aggregation in `_enqueue` (P20/P24), so the
sampler only ever looks at root attributes.

Sampling policy environment variables (in `.env`):

| Variable | Default | Meaning |
|---|---|---|
| `TAIL_SAMPLER_COST_THRESHOLD` | `0.05` | Traces whose summed `cost.usd` exceeds the threshold are flagged `cost.over_budget=true` and kept 100% |
| `TAIL_SAMPLER_NORMAL_RATE` | `10` | Keep ratio for "normal" traces, **percent 0–100** |

## 6. Sampler observability (P21)

The Collector exposes its own Prometheus telemetry on `:8888`; Prometheus
scrapes it and derives two recording rules (see `prometheus-rules.yml`):

- `job:tail_sampler_kept_ratio:ratio` — kept/(kept+dropped) share
- `job:tail_sampler_kept_by_reason:ratio` — per-policy breakdown

The native counter is `otelcol_processor_tail_sampling_count_traces_sampled`
with labels `decision` (`sampled`/`dropped`) and `reason` (policy name).

```bash
curl http://localhost:9091/api/v1/query --data-urlencode \
  'query=job:tail_sampler_kept_ratio:ratio'
```

## 7. Useful commands

```bash
docker compose logs -f langfuse          # Langfuse web logs
docker compose logs -f langfuse-worker   # Langfuse worker/ingestion logs
docker compose logs -f otel-collector    # Collector logs (debug exporter output)
docker compose logs -f prometheus        # Prometheus logs
docker compose down                      # stop (keeps volumes)
docker compose down -v                   # stop and wipe data
```

> `docker compose down -v` wipes the volumes. The `up -d` re-run boots a fresh
> Langfuse v3 stack that re-runs Postgres + ClickHouse migrations (allow 30–60 s).

## 8. The SDK side (later waves)

`agent_obs` uses `LANGFUSE_HOST` / `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY`
from `.env` (see P17–P19 in `analytics/MVP-PROMPT.md`). Keep these in sync with
the same `.env` file used here. `AGENT_OBS_CONTENT_RATE` (default 10) controls
deterministic content sampling (P22); `AGENT_OBS_TLS_CA` points to a CA bundle
for self-signed TLS (P19); `AGENT_OBS_FAIL_ON_CONFIG=0` degrades to a stdout
exporter when Langfuse config is missing.