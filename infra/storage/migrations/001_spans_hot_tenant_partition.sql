-- Migration PP32: Change spans_hot partition key to include tenant_id
-- 
-- ClickHouse does not support ALTER TABLE ... MODIFY PARTITION BY, so we:
-- 1. Create a new table with the new partition key
-- 2. Copy all data from the old table
-- 3. Swap tables using EXCHANGE (atomic rename)
-- 4. Drop the old table
--
-- Note: This migration is destructive and requires downtime for the table.
-- For sharded setups (clickhouse_ddl_sharded.sql), run this on each shard.
-- For HA setups (clickhouse_ddl_ha.sql), run this on each shard with ON CLUSTER.
--
-- Apply with:
--   docker compose exec clickhouse1 clickhouse-client --multiquery < migration_001_spans_hot_tenant_partition.sql
--   docker compose exec clickhouse2 clickhouse-client --multiquery < migration_001_spans_hot_tenant_partition.sql
-- (for HA: add ON CLUSTER 'observability_cluster' to all ALTER TABLE statements)

-- Step 1: Create new table with tenant_id partitioning
CREATE TABLE IF NOT EXISTS spans_hot_new
(
    trace_id String,
    span_id String,
    parent_span_id String,
    agent_id String,
    tenant_id String,
    name String,
    span_type String,
    start_time DateTime64(3),
    end_time DateTime64(3),
    status String,
    attributes JSON,
    events JSON,
    cost_usd Decimal(15, 6),
    response_embedding Array(Float32) DEFAULT [],
    created_at DateTime64(3) DEFAULT now()
)
ENGINE = MergeTree()
ORDER BY (tenant_id, agent_id, start_time)
PARTITION BY (tenant_id, toYYYYMMDD(start_time))
TTL start_time + INTERVAL 21 DAY
SETTINGS index_granularity = 8192;

-- Step 2: Copy all data from old table to new
INSERT INTO spans_hot_new
SELECT 
    trace_id, span_id, parent_span_id, agent_id, tenant_id, name, span_type,
    start_time, end_time, status, attributes, events, cost_usd, response_embedding, created_at
FROM spans_hot
ORDER BY (tenant_id, agent_id, start_time);

-- Step 3: Swap tables (atomic)
EXCHANGE TABLES spans_hot_new AND spans_hot;

-- Step 4: Drop the old table (now renamed to spans_hot_new)
DROP TABLE spans_hot_new;

-- Verify partition structure:
-- SELECT partition, count() FROM system.parts WHERE table = 'spans_hot' GROUP BY partition ORDER BY partition;