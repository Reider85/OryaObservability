-- ClickHouse DDL for observability database
-- This script creates the tables for hot-tier storage: spans_hot, eval_results_hot, audit_events_hot

-- Create the observability database (if it doesn't exist)
CREATE DATABASE IF NOT EXISTS observability;

USE observability;

-- spans_hot table: full span data for hot tier (21 days retention)
--
-- INVARIANT (PC22): the hot TTL must stay STRICTLY LONGER than the cron
-- migration trigger (AGENT_OBS_CRON_MIGRATION_RETENTION_DAYS, default 14d).
-- ClickHouse drops a row at INSERT time if it is already past the TTL, so a
-- TTL equal to the trigger means migrate_spans_to_warm.py can never observe a
-- single row and the warm migration silently becomes a no-op. 21d leaves a
-- 7-day window for the daily job to ship rows to Postgres warm.
CREATE TABLE IF NOT EXISTS spans_hot (
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
    cost_usd Float64,
    response_embedding Array(Float32) NULL,
    created_at DateTime64(3) DEFAULT now()
)
ENGINE = MergeTree()
ORDER BY (tenant_id, agent_id, start_time)
PARTITION BY toYYYYMMDD(start_time)
TTL start_time + INTERVAL 21 DAY
SETTINGS index_granularity = 8192;

-- eval_results_hot table: evaluation results for hot tier
--
-- INVARIANT (PC21): the hot TTL must stay STRICTLY LONGER than the cron
-- archive trigger (AGENT_OBS_CRON_EVAL_RETENTION_DAYS, default 14d).
-- ClickHouse drops a row at INSERT time if it is already past the TTL, so a
-- TTL equal to the trigger means cleanup_eval_results.py can never observe a
-- single row and the warm migration silently becomes a no-op. 21d leaves a
-- 7-day window for the daily job to ship rows to Postgres warm.
CREATE TABLE IF NOT EXISTS eval_results_hot (
    trace_id String,
    eval_id String,
    eval_name String,
    eval_timestamp DateTime64(3),
    eval_latency_seconds Float64,
    scores JSON,
    judge_model String,
    judge_prompt_sha256 String,
    reasoning String,
    flags Array(String),
    created_at DateTime64(3) DEFAULT now()
)
ENGINE = ReplacingMergeTree()
ORDER BY (trace_id, eval_id, eval_name, eval_timestamp)
TTL eval_timestamp + INTERVAL 21 DAY
SETTINGS index_granularity = 8192;

-- audit_events_hot table: security audit events for hot tier
--
-- Same INVARIANT as eval_results_hot (PC21): the hot TTL must outlast the cron
-- archive trigger (AGENT_OBS_CRON_AUDIT_RETENTION_DAYS, default 365d), or
-- ClickHouse deletes the rows before cleanup_audit_events.py can ship them to
-- S3 Parquet. 400d leaves a 35-day window. The cold bucket then keeps each
-- object for 365 days from upload, so total audit retention stays well above
-- the 1-year minimum required by ARCHITECT.md 3.4.
CREATE TABLE IF NOT EXISTS audit_events_hot (
    audit_id String,
    timestamp DateTime64(3),
    trace_id String,
    actor JSON,
    action String,
    decision String,
    resource JSON,
    reason String,
    ip_address String,
    user_agent String,
    created_at DateTime64(3) DEFAULT now()
)
ENGINE = MergeTree()
ORDER BY (timestamp, audit_id)
PARTITION BY toYYYYMMDD(timestamp)
TTL timestamp + INTERVAL 400 DAY
SETTINGS index_granularity = 8192;

-- Create views for easier querying
CREATE VIEW IF NOT EXISTS spans_hot_view AS
SELECT 
    trace_id,
    span_id,
    name,
    span_type,
    start_time,
    end_time,
    status,
    cost_usd,
    created_at
FROM spans_hot;

CREATE VIEW IF NOT EXISTS eval_results_hot_view AS
SELECT 
    trace_id,
    eval_name,
    eval_timestamp,
    eval_latency_seconds,
    scores,
    judge_model,
    created_at
FROM eval_results_hot;

-- Grant permissions to observability user (will be created in init script)
-- This will be executed by the init script after creating the user

-- PC25: Drift history table for KL-divergence analysis
CREATE TABLE IF NOT EXISTS drift_history (
    id UUID DEFAULT generateUUIDv4(),
    trace_id String,
    eval_id String,
    eval_name String DEFAULT 'drift_detection',
    eval_version String DEFAULT '1.0.0',
    eval_timestamp DateTime64(3),
    eval_latency_seconds Float64,
    kl_score Float64,
    threshold Float64,
    is_drift_detected UInt8,
    severity String,
    baseline_window_start DateTime64(3),
    baseline_window_end DateTime64(3),
    last_window_start DateTime64(3),
    last_window_end DateTime64(3),
    sample_size_baseline UInt32,
    sample_size_last UInt32,
    agent_id String,
    model_name String,
    flags Array(String),
    created_at DateTime64(3) DEFAULT now()
)
ENGINE = MergeTree()
ORDER BY (agent_id, created_at)
PARTITION BY toYYYYMMDD(created_at)
TTL created_at + INTERVAL 30 DAY
SETTINGS index_granularity = 8192;