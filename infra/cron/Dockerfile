# Cron maintenance service for PC21 (ticket T2.3.5).
#
# Build context is the repository root (not infra/):
#   docker compose -f infra/docker-compose.yml build cron
#
# Deliberately installs only the maintenance toolchain — none of the SDK's
# heavy ML dependencies (torch, spacy, presidio, transformers). The cron
# process never imports them, and bloat here would slow every stack restart.
FROM python:3.11-slim

WORKDIR /app

# Dependency notes — these three are not in maintenance.py but are pulled in
# transitively by the `agent_obs` / `agent_obs.guardrail` package __init__:
#   pyyaml  <- agent_obs.cost.price_book
#   pyotp   <- agent_obs.guardrail.vault_client
#   hvac    <- agent_obs.guardrail.vault_client
# The guardrail ML stack (presidio, transformers, torch) stays out: those
# modules import their models lazily, so nothing here loads model weights.
RUN pip install --no-cache-dir \
    clickhouse-driver \
    hvac \
    boto3 \
    pyarrow \
    asyncpg \
    prometheus-client \
    pyyaml \
    pyotp

# The repo is bind-mounted read-only at /app by docker-compose; copying the
# package here as well keeps `docker build` usable without the mount.
COPY pyproject.toml README.md ./
COPY agent_obs/ ./agent_obs/
COPY scripts/ ./scripts/

ENV PYTHONPATH=/app \
    PYTHONUNBUFFERED=1 \
    AGENT_OBS_CRON_PORT=9777

EXPOSE 9777

# python:3.11-slim has no curl/wget, so probe the metrics endpoint with urllib.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:9777/metrics', timeout=3).status == 200 else 1)"

CMD ["python", "scripts/cron/scheduler.py"]
