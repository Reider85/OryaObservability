-- PP22: AWS Athena / Glue Data Catalog for cold-tier Parquet.
--
-- Data is written by agent_obs.storage.maintenance.migrate_traces_to_cold
-- (PC23 weekly cron, scripts/cron/migrate_warm_to_cold.py) into the
-- cold-traces bucket with Hive-style partitions:
--
--   s3://cold-traces/tenant_id=<tenant>/year=<YYYY>/month=<MM>/day=<DD>/part-<ts>.parquet
--
-- The parquet files embed tenant_id and day columns as well; declaring the
-- directory names as PARTITIONED BY columns shadows them (partition values
-- win) and is what enables partition pruning — Athena scans only the
-- partitions named in the WHERE clause.
--
-- How to apply (AWS):
--   aws athena start-query-execution \
--     --query-string "$(cat infra/storage/athena_glue.sql)" \
--     --result-configuration OutputLocation=s3://cold-traces/athena-results/
-- MSCK REPAIR TABLE below discovers all existing partitions.

CREATE DATABASE IF NOT EXISTS observability;

CREATE EXTERNAL TABLE IF NOT EXISTS observability.cold_traces (
  agent_id string,
  traces_count int,
  cost_usd_sum double,
  eval_avg struct<faithfulness:double,relevancy:double,completeness:double>,
  users_count int,
  error_rate double
)
PARTITIONED BY (
  tenant_id string,
  year int,
  month int,
  day int
)
STORED AS PARQUET
LOCATION 's3://cold-traces/'
TBLPROPERTIES ('parquet.compression' = 'SNAPPY');

-- Register partitions already present in the bucket.
MSCK REPAIR TABLE observability.cold_traces;

-- Example partition-pruned drill-down query (agent_obs/storage/athena_client.py):
--   SELECT * FROM observability.cold_traces
--   WHERE tenant_id = '<tenant>' AND year = 2026 AND month = 10 AND day = 28
--   LIMIT 100;
