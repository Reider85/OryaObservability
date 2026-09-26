-- ClickHouse DDL for observability database
-- This script creates the tables for hot-tier storage: spans_hot, eval_results_hot, audit_events_hot

-- Create the observability database (if it doesn't exist)
CREATE DATABASE IF NOT EXISTS observability;

USE observability;

-- spans_hot table: full span data for hot tier (14 days retention)
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
TTL start_time + INTERVAL 14 DAY
SETTINGS index_granularity = 8192;

-- eval_results_hot table: evaluation results for hot tier (14 days retention)
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
TTL start_time + INTERVAL 14 DAY
SETTINGS index_granularity = 8192;

-- audit_events_hot table: security audit events for hot tier (365 days retention)
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
TTL timestamp + INTERVAL 365 DAY
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