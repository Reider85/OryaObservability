-- PostgreSQL DDL for warm storage
-- This script creates the tables for warm-tier storage: traces_warm and compliance_catalog

-- Create warm_store database (if it doesn't exist)
CREATE DATABASE IF NOT EXISTS warm_store;

-- Switch to warm_store database
\c warm_store;

-- traces_warm table: aggregated trace data for warm tier (90 days retention)
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
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now()
);

-- Create indexes for better query performance
CREATE INDEX IF NOT EXISTS idx_traces_warm_tenant_agent_start ON traces_warm(tenant_id, agent_id, start_time);
CREATE INDEX IF NOT EXISTS idx_traces_warm_start_time ON traces_warm(start_time);
CREATE INDEX IF NOT EXISTS idx_traces_warm_status ON traces_warm(status);

-- compliance_catalog table: PII compliance catalog (generated from masking, 90 days retention)
CREATE TABLE IF NOT EXISTS compliance_catalog (
    id SERIAL PRIMARY KEY,
    agent_id VARCHAR(255) NOT NULL,
    tool VARCHAR(255) NOT NULL,
    field VARCHAR(255) NOT NULL,
    pii_type VARCHAR(50) NOT NULL, -- email, phone, inn, passport, payment, etc.
    frequency INTEGER DEFAULT 1, -- How many times this PII type has been masked
    last_seen TIMESTAMP WITH TIME ZONE DEFAULT now(),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now()
);

-- Create indexes for compliance catalog
CREATE INDEX IF NOT EXISTS idx_compliance_catalog_agent ON compliance_catalog(agent_id);
CREATE INDEX IF NOT EXISTS idx_compliance_catalog_pii_type ON compliance_catalog(pii_type);
CREATE INDEX IF NOT EXISTS idx_compliance_catalog_last_seen ON compliance_catalog(last_seen);

-- Create view for easier compliance reporting
CREATE VIEW IF NOT EXISTS compliance_summary AS
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

-- Grant permissions to warm user (will be created in init script)
-- This will be executed by the init script after creating the user