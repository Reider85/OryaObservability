-- PostgreSQL DDL for warm storage
-- This script creates the tables for warm-tier storage: traces_warm and compliance_catalog
--
-- The database itself is created by the postgres entrypoint from POSTGRES_DB,
-- which also runs this file with psql already connected to it. There is
-- deliberately no `CREATE DATABASE` here: that statement has no
-- `IF NOT EXISTS` form in PostgreSQL, so including one aborts the whole
-- initdb (the entrypoint runs psql with ON_ERROR_STOP=1) and leaves the warm
-- tier with no schema at all.

-- traces_warm table: aggregated trace data for warm tier (90 days retention)
-- Columns input_chars/input_sha256/output_chars/output_sha256 are populated by
-- the PC22 hot→warm migration.  Full-text fields (llm.input_text /
-- llm.output_text) are intentionally absent — only char-count and sha256
-- hashes survive the hot→warm compression.
CREATE TABLE IF NOT EXISTS traces_warm (
    trace_id VARCHAR(255) PRIMARY KEY,
    tenant_id VARCHAR(255) NOT NULL,
    agent_id VARCHAR(255) NOT NULL,
    start_time TIMESTAMP WITH TIME ZONE NOT NULL,
    end_time TIMESTAMP WITH TIME ZONE,
    status VARCHAR(50) NOT NULL,
    cost_usd_total DECIMAL(15, 6) DEFAULT 0.0,
    span_count INTEGER DEFAULT 0,
    error_count INTEGER DEFAULT 0,
    eval_avg JSONB, -- Contains evaluation scores: {"faithfulness": 0.92, "answer_relevancy": 0.88, "completeness": 0.85}
    input_chars INTEGER DEFAULT 0,
    input_sha256 VARCHAR(64) DEFAULT '',
    output_chars INTEGER DEFAULT 0,
    output_sha256 VARCHAR(64) DEFAULT '',
    user_hash VARCHAR(64) DEFAULT '', -- Pseudonymous user identifier for cold-tier distinct-user counts
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now()
);

-- Add user_hash to existing deployments (idempotent)
DO $$
BEGIN
    ALTER TABLE traces_warm ADD COLUMN IF NOT EXISTS user_hash VARCHAR(64) DEFAULT '';
EXCEPTION
    WHEN duplicate_column THEN null; -- already exists
END
$$;

-- Create indexes for better query performance
CREATE INDEX IF NOT EXISTS idx_traces_warm_tenant_agent_start ON traces_warm(tenant_id, agent_id, start_time);
CREATE INDEX IF NOT EXISTS idx_traces_warm_start_time ON traces_warm(start_time);
CREATE INDEX IF NOT EXISTS idx_traces_warm_status ON traces_warm(status);

-- eval_results_warm table: structure-only eval scores (PC21, T2.3.5)
--
-- The hot tier (ClickHouse eval_results_hot) has a 14-day TTL; the daily cron
-- job scripts/cron/cleanup_eval_results.py migrates older rows here and then
-- purges them from hot.
--
-- There is deliberately NO `reasoning` column. The LLM judge's prose is the
-- bulky, unbounded part of an eval result and the warm tier is specified as
-- "structure-only, без reasoning, только scores+timestamp". Omitting the
-- column from the schema makes that reduction impossible to bypass by mistake.
CREATE TABLE IF NOT EXISTS eval_results_warm (
    trace_id VARCHAR(255) NOT NULL,
    eval_id VARCHAR(255) NOT NULL,
    eval_name VARCHAR(255) NOT NULL,
    eval_timestamp TIMESTAMP WITH TIME ZONE NOT NULL,
    eval_latency_seconds DOUBLE PRECISION,
    -- e.g. {"faithfulness": 0.92, "answer_relevancy": 0.88, "completeness": 0.85}
    scores JSONB,
    judge_model VARCHAR(255),
    flags JSONB,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now(),
    -- Same key as the hot ReplacingMergeTree ORDER BY, so the migration is
    -- idempotent: a re-run hits ON CONFLICT DO UPDATE instead of duplicating.
    PRIMARY KEY (trace_id, eval_id, eval_name, eval_timestamp)
);

CREATE INDEX IF NOT EXISTS idx_eval_results_warm_timestamp ON eval_results_warm(eval_timestamp);
CREATE INDEX IF NOT EXISTS idx_eval_results_warm_name ON eval_results_warm(eval_name);

-- Recency rollup for dashboards, mirroring the hot view.
-- CREATE OR REPLACE, not CREATE VIEW IF NOT EXISTS: PostgreSQL supports the
-- IF NOT EXISTS form for tables and indexes but not for views.
CREATE OR REPLACE VIEW eval_results_warm_view AS
SELECT
    trace_id,
    eval_name,
    eval_timestamp,
    eval_latency_seconds,
    scores,
    judge_model,
    created_at
FROM eval_results_warm;

-- compliance_catalog table: PII compliance catalog (generated from masking, 90 days retention)
CREATE TABLE IF NOT EXISTS compliance_catalog (
    id SERIAL PRIMARY KEY,
    agent_id VARCHAR(255) NOT NULL,
    tool VARCHAR(255) NOT NULL,
    field VARCHAR(255) NOT NULL,
    pii_type VARCHAR(50) NOT NULL, -- email, phone, inn, passport, payment, etc.
    frequency INTEGER DEFAULT 1, -- How many times this PII type has been masked
    first_seen TIMESTAMP WITH TIME ZONE DEFAULT now(), -- When this PII type was first seen
    last_seen TIMESTAMP WITH TIME ZONE DEFAULT now(),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now()
);

-- Create indexes for compliance catalog
CREATE INDEX IF NOT EXISTS idx_compliance_catalog_agent ON compliance_catalog(agent_id);
CREATE INDEX IF NOT EXISTS idx_compliance_catalog_pii_type ON compliance_catalog(pii_type);
CREATE INDEX IF NOT EXISTS idx_compliance_catalog_last_seen ON compliance_catalog(last_seen);
-- Unique key required by ON CONFLICT in update_compliance_catalog()
CREATE UNIQUE INDEX IF NOT EXISTS uq_compliance_catalog_key
    ON compliance_catalog(agent_id, tool, field, pii_type);

-- Create view for easier compliance reporting
CREATE OR REPLACE VIEW compliance_summary AS
SELECT 
    agent_id,
    tool,
    pii_type,
    COUNT(*) as total_occurrences,
    MAX(last_seen) as last_occurrence,
    MIN(created_at) as first_occurrence
FROM compliance_catalog
GROUP BY agent_id, tool, pii_type;

-- Create function to update compliance catalog (for use by application)
CREATE OR REPLACE FUNCTION update_compliance_catalog(
    p_agent_id VARCHAR(255),
    p_tool VARCHAR(255),
    p_field VARCHAR(255),
    p_pii_type VARCHAR(50)
) RETURNS VOID AS $$
BEGIN
    INSERT INTO compliance_catalog (agent_id, tool, field, pii_type, frequency, last_seen)
    VALUES (p_agent_id, p_tool, p_field, p_pii_type, 1, now())
    ON CONFLICT (agent_id, tool, field, pii_type) 
    DO UPDATE SET 
        frequency = compliance_catalog.frequency + 1,
        last_seen = now(),
        updated_at = now();
END;
$$ LANGUAGE plpgsql;

-- Create function to get compliance statistics for an agent
CREATE OR REPLACE FUNCTION get_agent_compliance_stats(p_agent_id VARCHAR(255)) 
RETURNS TABLE(
    pii_type VARCHAR(50),
    total_occurrences INTEGER,
    last_occurrence TIMESTAMP WITH TIME ZONE
) AS $$
BEGIN
    RETURN QUERY
    SELECT 
        pii_type,
        COUNT(*) as total_occurrences,
        MAX(last_seen) as last_occurrence
    FROM compliance_catalog
    WHERE agent_id = p_agent_id
    GROUP BY pii_type
    ORDER BY total_occurrences DESC;
END;
$$ LANGUAGE plpgsql;

-- Add first_seen column if not exists (populate from created_at)
DO $$
BEGIN
    ALTER TABLE compliance_catalog ADD COLUMN IF NOT EXISTS first_seen TIMESTAMPTZ;
    UPDATE compliance_catalog SET first_seen = created_at WHERE first_seen IS NULL;
EXCEPTION WHEN duplicate_column THEN
    NULL; -- already exists
END $$;

-- PC34: Views for compliance catalog aggregation
CREATE OR REPLACE VIEW compliance_catalog_by_agent AS
SELECT 
    agent_id,
    COUNT(*) as total_pii_count,
    COUNT(DISTINCT pii_type) as distinct_pii_types
FROM compliance_catalog 
GROUP BY agent_id;

CREATE OR REPLACE VIEW compliance_catalog_by_tool AS
SELECT 
    tool,
    COUNT(*) as total_pii_count
FROM compliance_catalog 
GROUP BY tool;

-- Table ownership/grants are handled by postgres_warm_init.sh, which the
-- postgres entrypoint runs immediately after this file.