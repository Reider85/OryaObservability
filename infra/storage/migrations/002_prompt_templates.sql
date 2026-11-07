-- Migration PP53: Create prompt_templates table for system prompt versioning
-- 
-- This table stores mappings of system prompt SHA256 hashes to their text content,
-- enabling A/B testing and traceability of which prompt versions were used.
--
-- TTL is set to 90 days to prevent unbounded growth while maintaining
-- sufficient history for analysis.
--
-- Apply with:
--   docker compose exec clickhouse1 clickhouse-client --multiquery < migration_002_prompt_templates.sql
--   docker compose exec clickhouse2 clickhouse-client --multiquery < migration_002_prompt_templates.sql
-- (for HA: add ON CLUSTER 'observability_cluster' to all CREATE TABLE statements)
 
-- Create prompt_templates table
CREATE TABLE IF NOT EXISTS prompt_templates
(
    sha256 String,
    version_label String,
    first_seen DateTime64(3),
    last_seen DateTime64(3),
    sample_text String,
    created_at DateTime64(3) DEFAULT now()
)
ENGINE = MergeTree()
PARTITION BY toYYYYMMDD(first_seen)
ORDER BY (sha256, first_seen)
TTL created_at + INTERVAL 90 DAY
SETTINGS index_granularity = 8192;
 
-- Create index for efficient querying by SHA256
CREATE INDEX IF NOT EXISTS idx_prompt_templates_sha256 
ON prompt_templates (sha256)
TYPE tokenbf_v1
GRANULARITY 4;
 
-- Create index for efficient querying by version_label
CREATE INDEX IF NOT EXISTS idx_prompt_templates_version 
ON prompt_templates (version_label)
TYPE tokenbf_v1
GRANULARITY 4;
 
-- Create materialized view for tracking prompt version changes
CREATE MATERIALIZED VIEW IF NOT EXISTS prompt_templates_changes_mv
TO prompt_templates_changes
AS
SELECT 
    sha256,
    version_label,
    first_seen,
    last_seen,
    sample_text,
    created_at,
    'insert' as action
FROM prompt_templates
WHERE created_at = now()
 
UNION ALL
 
SELECT 
    sha256,
    version_label,
    first_seen,
    last_seen,
    sample_text,
    created_at,
    'update' as action
FROM prompt_templates
WHERE last_updated = now();
 
-- Create table for tracking prompt version changes
CREATE TABLE IF NOT EXISTS prompt_templates_changes
(
    sha256 String,
    version_label String,
    first_seen DateTime64(3),
    last_seen DateTime64(3),
    sample_text String,
    created_at DateTime64(3),
    action String
)
ENGINE = MergeTree()
PARTITION BY toYYYYMMDD(created_at)
ORDER BY (created_at, sha256)
SETTINGS index_granularity = 8192;
 
-- Verify table structure:
-- DESCRIBE prompt_templates;
-- SELECT count() FROM prompt_templates;
-- SELECT count() FROM prompt_templates_changes;