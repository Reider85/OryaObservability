-- ClickHouse DDL for observability database with sharding
-- This script creates the tables for hot-tier storage: spans_hot, eval_results_hot, audit_events_hot
-- For sharded setup, creates both local tables on each shard and Distributed tables for queries

-- Create the observability database (if it doesn exist)
CREATE DATABASE IF NOT EXISTS observability;

USE observability;

-- Local tables for shard 1 (spans_hot_local, etc.)
CREATE TABLE IF NOT EXISTS spans_hot_local ON CLUSTER 'observability_cluster' (
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
    response_embedding Array(Float32) DEFAULT [],
    created_at DateTime64(3) DEFAULT now()
)
ENGINE = MergeTree()
ORDER BY (tenant_id, agent_id, start_time)
PARTITION BY toYYYYMMDD(start_time)
    TTL start_time + INTERVAL 21 DAY
    SETTINGS index_granularity = 8192;

-- Local table for eval_results_hot on shard 1
CREATE TABLE IF NOT EXISTS eval_results_hot_local ON CLUSTER 'observability_cluster' (
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
TTL eval_timestamp + INTERVAL 14 DAY
SETTINGS index_granularity = 8192;

-- Local table for audit_events_hot on shard 1
CREATE TABLE IF NOT EXISTS audit_events_hot_local ON CLUSTER 'observability_cluster' (
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

-- Distributed tables for querying across shards
CREATE TABLE IF NOT EXISTS spans_hot ON CLUSTER 'observability_cluster' AS spans_hot_local
ENGINE = Distributed('observability_cluster', 'observability', 'spans_hot_local', tenant_id);

CREATE TABLE IF NOT EXISTS eval_results_hot ON CLUSTER 'observability_cluster' AS eval_results_hot_local
ENGINE = Distributed('observability_cluster', 'observability', 'eval_results_hot_local', tenant_id);

CREATE TABLE IF NOT EXISTS audit_events_hot ON CLUSTER 'observability_cluster' AS audit_events_hot_local
ENGINE = Distributed('observability_cluster', 'observability', 'audit_events_hot_local', tenant_id);

-- Create views for easier querying (using Distributed tables)
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

-- PC25: Drift history table for KL-divergence analysis
CREATE TABLE IF NOT EXISTS drift_history ON CLUSTER 'observability_cluster' (
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

-- Grant permissions to observability user (will be created in init script)
-- This will be executed by the init script after creating the user