# Observability Infrastructure

Local self-hosted observability stack for the `agent-obs` MVP
(Level 1 from `analytics/ROADMAP.md`).

**Services**

| Service | Image | Port | Purpose |
|---|---|---|---|
| `langfuse` | `langfuse/langfuse:2` | `3000` | Trace UI + OTLP ingest |
| `postgres` | `postgres:16` | internal | Langfuse metadata database (compose network only) |
| `redis` | `redis:7` | internal | Langfuse queues (compose network only) |
| `otel-collector` | `otel/opentelemetry-collector-contrib:0.128.0` | `4317` gRPC, `4318` HTTP | OTLP → batch → Langfuse |

> Postgres/Redis are deliberately **not** exposed on the host: their ports
> (5432/6379) are commonly occupied by a local Postgres/Redis. The SDK only
> talks to Langfuse (`:3000`) and the Collector (`:4317`/`:4318`). To debug the
> database directly, run `docker compose exec postgres psql -U langfuse`.

**Data flow**

```
SDK --OTLP--> otel-collector(:4317/:4318) --batch--> langfuse(/api/public/otel/v1/traces)
```

---

## 1. Start the stack

```bash
cd infra
docker compose up -d
docker compose ps          # all services should be Up/healthy
docker compose logs -f     # watch startup
```

First boot creates the Langfuse database schema (Postgres migrations). Give it 30–60 seconds.

Smoke check:

```bash
# Langfuse UI
curl http://localhost:3000/api/public/health        # {"status":"ok", ...}
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
3. A project `agent-obs` is created automatically on first boot
   (`LANGFUSE_INIT_PROJECT_NAME`). If not, create one via **Create Project**.
4. Go to **Project Settings → API Keys** and copy:
   - **Public Key** (`pk-lf-...`)
   - **Secret Key** (`sk-lf-...`)

## 3. Configure credentials

```bash
cd infra
cp .env.example .env
# edit .env, set:
#   LANGFUSE_SALT=<random string>
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

Sampling policy environment variables are consumed by the Collector config
(expandable, see `otel-collector/config.yaml`):

| Variable | Default | Meaning |
|---|---|---|
| `TAIL_SAMPLER_COST_THRESHOLD` | `0.05` | Traces with `cost.usd` above the threshold are kept 100% |
| `TAIL_SAMPLER_NORMAL_RATE` | `0.1` | Keep ratio for "normal" traces |

Tail-sampling processor is wired in a later wave (MVP prompt P20).

## 6. Useful commands

```bash
docker compose logs -f langfuse          # Langfuse logs
docker compose logs -f otel-collector    # Collector logs (debug exporter output)
docker compose down                      # stop (keeps postgres volume)
docker compose down -v                   # stop and wipe data
```

## 7. The SDK side (later waves)

`agent_obs` uses `LANGFUSE_HOST` / `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY`
from `.env` (see P17–P19 in `analytics/MVP-PROMPT.md`). Keep these in sync with
the same `.env` file used here.