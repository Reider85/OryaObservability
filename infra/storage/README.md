# Tiered Storage Infrastructure

This directory contains the configuration and initialization scripts for the tiered storage infrastructure required by the CRITICAL observability stack (PC01).

## Overview

The tiered storage architecture provides three storage tiers with different retention policies and access patterns:

| Tier | Technology | Retention | Purpose | Access Pattern |
|------|------------|-----------|---------|----------------|
| **Hot** | ClickHouse | 14 days | Full span data, eval results, audit events | Real-time queries, analytics |
| **Warm** | PostgreSQL | 90 days | Aggregated traces, compliance catalog | Reporting, compliance, slower queries |
| **Cold** | MinIO S3 | 1 year | Parquet aggregates for long-term storage | Cost-effective storage, historical analysis |

## Services

### 1. ClickHouse Hot Storage
- **Port**: `8123` (HTTP API)
- **Database**: `observability`
- **Tables**:
  - `spans_hot`: Full span data with TTL 14 days
  - `eval_results_hot`: Evaluation results with TTL 14 days  
  - `audit_events_hot`: Security audit events with TTL 365 days
- **Initialization**: `clickhouse_init.sh` creates database, user, and tables

### 2. PostgreSQL Warm Storage
- **Port**: `5433` (to avoid conflict with local Postgres)
- **Database**: `warm_store`
- **Tables**:
  - `traces_warm`: Aggregated trace metadata
  - `compliance_catalog`: PII compliance tracking
- **Initialization**: `postgres_warm_init.sh` creates database, user, and tables

### 3. MinIO Cold Storage
- **API Port**: `9000`
- **Console Port**: `9001`
- **Buckets**:
  - `cold-traces`: 1-year retention for trace aggregates
  - `audit-events`: 90-day retention for audit logs
- **Lifecycle Rules**: Applied via `minio_init.sh`

## Quick Start

### 1. Start the stack

```bash
cd infra
docker compose up -d
```

### 2. Verify services are running

```bash
# Check all services are healthy
docker compose ps

# View logs
docker compose logs -f
```

### 3. Verify storage tier connectivity

```bash
# ClickHouse Hot Storage
docker compose exec clickhouse clickhouse-client --query "SHOW DATABASES"

# PostgreSQL Warm Storage  
docker compose exec postgres-warm psql -U warm -d warm_store -c "\dt"

# MinIO Cold Storage
docker compose exec minio mc alias set local http://localhost:9000 minio miniosecret
docker compose exec minio mc ls local/
```

## Connectivity Details

### ClickHouse Hot Storage
- **Host URL (HTTP)**: `http://localhost:8123`
- **Host native TCP**: `localhost:9004` (container `clickhouse:9000`; host `:9000` is intentionally not published — it collides with unrelated MinIO instances)
- **Database**: `observability`
- **In-network user** (Langfuse/cron/sampler): `clickhouse` / `${CLICKHOUSE_PASSWORD}`
- **App user** (HotStore/drift/phoenix defaults): `observability_user` / `observability_password`
  — defined via `observability_user.xml` (users.d mount), not SQL CREATE USER
- **Schema re-apply** (existing volumes): `docker compose run --rm clickhouse-init`

### PostgreSQL Warm Storage
- **Host URL**: `postgresql://localhost:5433/warm_store` (not published by compose; use `docker compose exec postgres-warm psql -U warm -d warm_store`)
- **User**: `warm`
- **Password**: `warm`

### MinIO Cold Storage
- **API Endpoint**: in-network `http://minio:9000` (not published to host)
- **Console**: in-network `:9001` (not published to host)
- **Access Key**: `minio`
- **Secret Key**: `miniosecret`
- **Buckets**: `langfuse`, `cold-traces`, `audit-events`

## Environment Variables

The following environment variables should be set in your `.env` file:

```bash
# ClickHouse Hot Storage
# Compose in-network services get CLICKHOUSE_USER=clickhouse from
# docker-compose.yml. Host-run SDK tools use observability_user, defined via
# observability_user.xml (users.d mount). Do not set CLICKHOUSE_USER in .env
# for compose-only stacks.
CLICKHOUSE_DB=observability
CLICKHOUSE_HOT_URL=http://clickhouse:8123
# Host-run tools (native protocol):
#   CLICKHOUSE_HOST=localhost
#   CLICKHOUSE_PORT=9004
#   CLICKHOUSE_USER=observability_user
#   CLICKHOUSE_PASSWORD=observability_password

# PostgreSQL Warm Storage
WARM_PG_USER=warm_user
WARM_PG_PASSWORD=warm_password
WARM_PG_DSN=postgresql://warm_user:warm_password@postgres-warm:5432/warm_store
WARM_PG_HOST_URL=postgresql://warm:warm@localhost:5433/warm_store

# MinIO Cold Storage
S3_ENDPOINT=http://minio:9000
S3_ACCESS_KEY=minio
S3_SECRET_KEY=miniosecret
S3_COLD_BUCKET=cold-traces
S3_AUDIT_BUCKET=audit-events
S3_COLD_DSN=s3://cold-traces@minio:9000
S3_AUDIT_DSN=s3://audit-events@minio:9000
```

## Data Migration Process

The tiered storage automatically migrates data between tiers:

1. **Hot → Warm (Daily)**: 
   - Script: `scripts/cron/migrate_hot_to_warm.py`
   - Traces older than 14 days are moved from ClickHouse to PostgreSQL
   - Content is compressed (only metadata, no full text)

2. **Warm → Cold (Weekly)**:
    - Script: `scripts/cron/migrate_warm_to_cold.py`
    - Traces older than 90 days are moved from PostgreSQL to MinIO
    - Data is stored as Parquet files for efficient querying
    - Aggregates by (tenant, agent, day) with traces_count, cost_usd_sum, eval_avg, users_count, error_rate

3. **Audit Events → Cold (Daily)**:
   - Audit events older than 90 days are moved to MinIO
   - Retained for 1 year total

## Troubleshooting

### ClickHouse Issues
```bash
# Check ClickHouse logs
docker compose logs clickhouse

# Manual database connection
docker compose exec clickhouse clickhouse-client --database=observability

# Check table status
docker compose exec clickhouse clickhouse-client --query "SELECT name, engine FROM system.tables WHERE database = 'observability'"
```

### PostgreSQL Warm Storage Issues
```bash
# Check PostgreSQL logs
docker compose logs postgres-warm

# Manual database connection
docker compose exec postgres-warm psql -U warm -d warm_store

# Check table status
docker compose exec postgres-warm psql -U warm -d warm_store -c "\dt"
```

### MinIO Issues
```bash
# Check MinIO logs
docker compose logs minio

# Manual bucket listing
docker compose exec minio mc ls local/

# Check lifecycle rules
docker compose exec minio mc ilm ls local/cold-traces
docker compose exec minio mc ilm ls local/audit-events
```

## Security Notes

- ClickHouse and PostgreSQL are configured with separate users for observability data
- MinIO buckets have lifecycle rules for automatic cleanup
- Audit events are retained for 1 year for compliance purposes
- All services are protected by Docker Compose network isolation

## Testing

To verify the tiered storage is working correctly:

1. Send some test traces through the observability SDK
2. Check that data appears in ClickHouse Hot Storage
3. Wait for daily migration and verify data appears in PostgreSQL Warm Storage
4. Wait for weekly migration and verify data appears in MinIO Cold Storage

## Integration with Observability SDK

The SDK uses the following connection strings to access each tier:

- **Hot Storage**: `CLICKHOUSE_HOT_URL` (for real-time span data)
- **Warm Storage**: `WARM_PG_DSN` (for aggregated trace metadata)
- **Cold Storage**: `S3_COLD_DSN` (for long-term Parquet files)

These are configured via environment variables and used by the respective SDK components during data ingestion and querying.