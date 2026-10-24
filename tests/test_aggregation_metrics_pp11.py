"""Tests for PP11 span aggregation metrics.

Verifies that aggregation flushes emit metrics with correct labels and values,
audit events are generated, and alert rules/dashboards are correctly configured.
"""

from __future__ import annotations

from threading import Thread
from urllib.request import urlopen
from wsgiref.simple_server import make_server

import pytest
from prometheus_client import REGISTRY, generate_latest
from prometheus_client.exposition import make_wsgi_app

from agent_obs.guardrail.audit import AggregationFlushAuditEvent
from agent_obs.metrics import (
    aggregation_ratio,
    bytes_saved_total,
    spans_aggregated_total,
    spans_micro_input_total,
)
from agent_obs.observability import _PP11_SPANS_AGGREGATED_TOTAL, _PP11_SPANS_MICRO_INPUT_TOTAL
from agent_obs.observability import ObservabilitySDK, Span, SpanContext, SpanType, get_pp11_metrics
from agent_obs.span_aggregator import SpanAggregator, SpanAggregate


# ---------------------------------------------------------------------------
# Metric definitions and labels
# ---------------------------------------------------------------------------


def test_metric_definitions_exist():
    """Test that all PP11 metrics are defined with correct labels."""
    # Check that metrics exist and have correct names via their _name attribute
    assert spans_aggregated_total._name == "spans_aggregated"
    assert spans_aggregated_total._labelnames == ("agent_id", "tenant_id", "span_type")
    
    assert spans_micro_input_total._name == "spans_micro_input"
    assert spans_micro_input_total._labelnames == ("agent_id", "tenant_id")
    
    assert bytes_saved_total._name == "bytes_saved"
    assert bytes_saved_total._labelnames == ("agent_id", "tenant_id")
    
    assert aggregation_ratio._name == "aggregation_ratio"
    assert aggregation_ratio._labelnames == ("agent_id",)


# ---------------------------------------------------------------------------
# SDK integration tests
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_span():
    """Create a mock span for testing."""
    ctx = SpanContext(
        trace_id="test-trace-123",
        span_id="test-span-456",
        agent_id="test-agent",
    )
    return Span(
        name="test-reasoning-step",
        span_type="agent.reasoning",
        context=ctx,
        start_time=1234567890.0,
        attributes={"tenant_id": "test-tenant"},
    )


@pytest.fixture
def mock_sdk():
    """Create a mock SDK with aggregation enabled."""
    # Set environment variable to control max_count
    import os
    os.environ["AGENT_OBS_AGGREGATION_MAX_COUNT"] = "50"
    
    sdk = ObservabilitySDK(
        exporters=[],
        config={
            "aggregation_enabled": True,
            "aggregation": {
                "max_count": 50,
                "window_seconds": 1.0
            }
        }
    )
    # Ensure aggregation is enabled
    assert sdk._aggregation_enabled, "Aggregation should be enabled"
    assert sdk._span_aggregator is not None, "SpanAggregator should be created"
    # Check aggregator config
    assert sdk._span_aggregator.max_count == 50, f"Expected max_count=50, got {sdk._span_aggregator.max_count}"
    assert sdk._span_aggregator.max_window_seconds == 1.0, f"Expected max_window_seconds=1.0, got {sdk._span_aggregator.max_window_seconds}"
    return sdk


class TestAggregationMetrics:
    """Test metrics emitted during span aggregation."""
    
    @pytest.mark.asyncio
    async def test_100_micro_spans_produce_multiple_aggregates(self, mock_sdk, mock_span):
        """Test that 100 micro-spans produce at least 2 aggregates."""
        # Reset counters using get_pp11_metrics to ensure we're using the same metric instances
        metrics = get_pp11_metrics()
        metrics['spans_aggregated_total'].clear()
        metrics['spans_micro_input_total'].clear()
    
        # Create 100 micro-spans and add them
        for i in range(100):
            span = Span(
                name=f"step-{i}",
                span_type=SpanType.AGENT_REASONING,
                context=mock_span.context,
                start_time=1234567890.0 + i * 0.1,
                attributes={"tenant_id": "test-tenant"},
            )
            # Debug: print span info
            print(f"Adding span {i}: {span.span_type}, {span.name}, has_agg_attr={'aggregation.children_count' in span.attributes}")
            # Debug: print aggregator state before adding
            if mock_sdk._span_aggregator:
                print(f"Before add: buffer size={len(mock_sdk._span_aggregator._buffer)}, first_time={mock_sdk._span_aggregator._first_span_time}")
            await mock_sdk._enqueue(span)
            # Debug: print aggregator state after adding
            if mock_sdk._span_aggregator:
                print(f"After add: buffer size={len(mock_sdk._span_aggregator._buffer)}, first_time={mock_sdk._span_aggregator._first_span_time}")
    
        # Debug: print aggregator state
        print(f"Final aggregator: {mock_sdk._span_aggregator}")
        if mock_sdk._span_aggregator:
            print(f"Final buffer size: {len(mock_sdk._span_aggregator._buffer)}")
            print(f"Final first span time: {mock_sdk._span_aggregator._first_span_time}")
    
    # Check metrics using the same get_pp11_metrics as in _record_aggregation_metrics
            metrics = get_pp11_metrics()
            aggregated_metric = metrics['spans_aggregated_total'].labels(
                agent_id="test-agent",
                tenant_id="test-tenant",
                span_type="agent.reasoning",
            )
            micro_metric = metrics['spans_micro_input_total'].labels(
                agent_id="test-agent",
                tenant_id="test-tenant",
            )
            
            print(f"DEBUG: aggregated_metric object: {aggregated_metric}")
            print(f"DEBUG: micro_metric object: {micro_metric}")
            print(f"DEBUG: id(aggregated_metric): {id(aggregated_metric)}")
            print(f"DEBUG: id(micro_metric): {id(micro_metric)}")
            print(f"DEBUG: aggregated_metric value: {aggregated_metric._value.get()}")
            print(f"DEBUG: micro_metric value: {micro_metric._value.get()}")
            
            # Try a different approach: check the Prometheus registry directly
            from prometheus_client.parser import text_string_to_metric_families
            from prometheus_client.exposition import generate_latest
            
            # Generate metrics and check them
            metrics_data = generate_latest(REGISTRY).decode('utf-8')
            print(f"DEBUG: All metrics data:\n{metrics_data}")
            
# Parse the metrics to find our specific ones
            micro_value = None
            aggregated_value = None
            
            # Parse the metrics to find our specific ones
            micro_value = None
            aggregated_value = None
            
            for family in text_string_to_metric_families(metrics_data):
                if family.name == 'spans_aggregated_total':
                    for sample in family.samples:
                        if sample.labels.get('agent_id') == 'test-agent' and sample.labels.get('tenant_id') == 'test-tenant' and sample.labels.get('span_type') == 'agent.reasoning':
                            aggregated_value = sample.value
                            assert aggregated_value == 9, f"Expected 9 aggregates, got {aggregated_value}"
                            break
                elif family.name == 'spans_micro_input':
                    for sample in family.samples:
                        if sample.labels.get('agent_id') == 'test-agent' and sample.labels.get('tenant_id') == 'test-tenant':
                            micro_value = sample.value
                            assert micro_value == 100, f"Expected 100 micro spans, got {micro_value}"
                            break
            
            assert micro_value == 100, f"Expected 100 micro-spans, got {micro_value}"
        assert bytes_saved_total.labels(
            agent_id="test-agent",
            tenant_id="test-tenant",
        )._value.get() > 0, "Expected some bytes saved"
    
    @pytest.mark.asyncio
    async def test_window_flush_produces_metrics(self, mock_sdk, mock_span):
        """Test that window-based flush produces metrics."""
        # Reset counters
        spans_aggregated_total.clear()
        spans_micro_input_total.clear()
        
        # Create 60 spans with 1.5 second window (should flush based on count)
        for i in range(60):
            span = Span(
                name=f"step-{i}",
                span_type=SpanType.AGENT_REASONING,
                context=mock_span.context,
                start_time=1234567890.0 + i * 0.1,
                attributes={"tenant_id": "test-tenant"},
            )
            await mock_sdk._enqueue(span)
        
        # Check metrics
        aggregated_value = spans_aggregated_total.labels(
            agent_id="test-agent",
            tenant_id="test-tenant",
            span_type="agent.reasoning",
        )._value.get()
        
        assert aggregated_value >= 1, f"Expected at least 1 aggregate, got {aggregated_value}"
    
    @pytest.mark.asyncio
    async def test_empty_flush_does_not_increment(self, mock_sdk):
        """Test that empty flush does not increment metrics."""
        # Reset counters
        spans_aggregated_total.clear()
        spans_micro_input_total.clear()
        
        # Flush empty aggregator
        await mock_sdk._flush_aggregation()
        
        # Check no increment - Counter doesn't have _value.get(), but we can check
        # that the value is 0 by checking the label values haven't changed
        # Since we cleared them, they should still be 0
        value = spans_aggregated_total.labels(
            agent_id="test-agent",
            tenant_id="test-tenant",
            span_type="agent.reasoning",
        )._value.get()
        assert value == 0
    
    @pytest.mark.asyncio
    async def test_aggregation_ratio_calculation(self, mock_sdk, mock_span):
        """Test that aggregation ratio is calculated correctly."""
        # Reset counters and gauge
        spans_aggregated_total.clear()
        spans_micro_input_total.clear()
        aggregation_ratio.clear()
        
# Add 50 micro-spans (should produce 5 aggregates with max_count=10)
        for i in range(50):
            span = Span(
                name=f"step-{i}",
                span_type="agent.reasoning",
                context=mock_span.context,
                start_time=1234567890.0 + i * 0.1,
                attributes={"tenant_id": "test-tenant"},
            )
            await mock_sdk._enqueue(span)
    
        # Check ratio (5 aggregates / 50 micro-spans = 0.1)
        ratio_value = aggregation_ratio.labels(agent_id="test-agent")._value.get()
        assert 0.09 <= ratio_value <= 0.11, f"Expected ratio ~0.1, got {ratio_value}"
    
    @pytest.mark.asyncio
    async def test_agent_id_tenant_id_labels(self, mock_sdk, mock_span):
        """Test that agent_id and tenant_id labels are correctly applied."""
        # Reset counters
        spans_aggregated_total.clear()
        spans_micro_input_total.clear()
        
        # Set different tenant_id
        mock_span.attributes["tenant_id"] = "tenant-xyz"
        
        # Add spans (need 50 to trigger flush with max_count=50)
        for i in range(50):
            span = Span(
                name=f"step-{i}",
                span_type="agent.reasoning",
                context=mock_span.context,
                start_time=1234567890.0 + i * 0.1,
                attributes={"tenant_id": "tenant-xyz"},
            )
            await mock_sdk._enqueue(span)
        
        # Check labels
        aggregated_value = spans_aggregated_total.labels(
            agent_id="test-agent",
            tenant_id="tenant-xyz",
            span_type="agent.reasoning",
        )._value.get()
        
        assert aggregated_value >= 1, f"Expected at least 1 aggregate, got {aggregated_value}"


# ---------------------------------------------------------------------------
# Audit event tests
# ---------------------------------------------------------------------------


class TestAggregationAuditEvent:
    """Test AggregationFlushAuditEvent creation and serialization."""
    
    def test_audit_event_creation(self):
        """Test AggregationFlushAuditEvent creation with aggregation context."""
        event = AggregationFlushAuditEvent.for_aggregation_flush(
            children_count=25,
            bytes_saved=1024,
            span_type="agent.reasoning",
            agent_id="test-agent",
            trace_id="test-trace-123",
        )
        
        assert event.audit_id.startswith("afe-")
        assert event.action == "span.aggregate_flush"
        assert event.resource["type"] == "span_aggregation"
        assert event.resource["children_count"] == 25
        assert event.resource["bytes_saved"] == 1024
        assert event.resource["agent_id"] == "test-agent"
        assert "aggregated 25 micro-spans" in event.reason
    
    def test_audit_event_serialization(self):
        """Test AggregationFlushAuditEvent to_dict() and to_clickhouse_row()."""
        event = AggregationFlushAuditEvent.for_aggregation_flush(
            children_count=10,
            bytes_saved=512,
            span_type="agent.reasoning",
            agent_id="test-agent",
        )
        
        # Test to_dict
        event_dict = event.to_dict()
        assert event_dict["action"] == "span.aggregate_flush"
        assert event_dict["resource"]["children_count"] == 10
        
        # Test to_clickhouse_row
        row = event.to_clickhouse_row()
        assert len(row) == 10  # All columns present
        assert row[1] is not None  # timestamp is not None
        assert row[4] == "span.aggregate_flush"  # action column
    
    @pytest.mark.asyncio
    async def test_sdk_emits_audit_on_flush(self, mock_sdk, mock_span, caplog):
        """Test that SDK logs audit record on aggregation flush."""
        caplog.set_level("INFO")
        
        # Add spans to trigger flush
        for i in range(50):
            span = Span(
                name=f"step-{i}",
                span_type="agent.reasoning",
                context=mock_span.context,
                start_time=1234567890.0 + i * 0.1,
                attributes={"tenant_id": "test-tenant"},
            )
            await mock_sdk._enqueue(span)
        
        # Check that audit record was logged
        assert "Aggregation flushed:" in caplog.text
        assert "children_count=" in caplog.text
        assert "bytes_saved=" in caplog.text


# ---------------------------------------------------------------------------
# Exposition tests (like PP07)
# ---------------------------------------------------------------------------


def test_metrics_exposition():
    """Test that PP11 metrics appear in generate_latest() output."""
    # First increment the metrics to ensure they're exported
    spans_aggregated_total.labels(agent_id="test", tenant_id="test", span_type="agent.reasoning").inc()
    spans_micro_input_total.labels(agent_id="test", tenant_id="test").inc()
    bytes_saved_total.labels(agent_id="test", tenant_id="test").inc()
    aggregation_ratio.labels(agent_id="test").set(0.02)
    
    exposition = generate_latest(REGISTRY)
    output = exposition.decode("utf-8")
    
    # Check that all metric names are present
    assert "spans_aggregated_total" in output or "spans_aggregated" in output
    assert "spans_micro_input_total" in output or "spans_micro_input" in output
    assert "bytes_saved_total" in output or "bytes_saved" in output
    assert "aggregation_ratio" in output
    
    # Check that expected labels are present
    assert "agent_id=" in output
    assert "tenant_id=" in output
    assert "span_type=" in output


# ---------------------------------------------------------------------------
# Alert rule tests (like test_sampler_alert_rules.py)
# ---------------------------------------------------------------------------


class TestAggregationAlertRules:
    """Test AggregationRatioTooLow alert rule configuration."""
    
    @staticmethod
    @pytest.fixture(scope="module")
    def rules():
        import yaml
        from pathlib import Path
        REPO_ROOT = Path(__file__).resolve().parent.parent
        RULES_PATH = REPO_ROOT / "infra" / "prometheus-rules.yml"
        assert RULES_PATH.exists(), f"missing {RULES_PATH}"
        return yaml.safe_load(RULES_PATH.read_text(encoding="utf-8"))

    @staticmethod
    @pytest.fixture(scope="module")
    def aggregation_group(rules):
        groups = [g for g in rules.get("groups", []) if g.get("name") == "span-aggregation"]
        assert len(groups) == 1, "span-aggregation group must exist exactly once"
        return groups[0]

    @staticmethod
    @pytest.fixture(scope="module")
    def alert_rule(aggregation_group):
        matches = [r for r in aggregation_group["rules"] if r.get("alert") == "AggregationRatioTooLow"]
        assert len(matches) == 1, "AggregationRatioTooLow must appear exactly once"
        return matches[0]
    
    def test_alert_lives_in_its_own_group(self, rules, aggregation_group):
        """Must not be added to other groups."""
        # Check it's not in sampler-monitoring
        sampler = next((g for g in rules["groups"] if g.get("name") == "sampler-monitoring"), None)
        if sampler:
            alert_names = [r.get("alert") for r in sampler.get("rules", [])]
            assert "AggregationRatioTooLow" not in alert_names
        
        assert aggregation_group["name"] == "span-aggregation"
    
    def test_expr_targets_aggregation_ratio(self, alert_rule):
        expr = alert_rule["expr"]
        assert "aggregation_ratio" in expr
        assert "> 0.2" in expr
    
    def test_for_clause_is_15_minutes(self, alert_rule):
        assert alert_rule["for"] == "15m"
    
    def test_severity_is_warning(self, alert_rule):
        assert alert_rule["labels"]["severity"] == "warning"
        assert alert_rule["labels"]["component"] == "aggregation"
    
    def test_annotations_carry_actionable_context(self, alert_rule):
        annotations = alert_rule["annotations"]
        assert "Aggregation ratio too high" in annotations["summary"]
        assert "Span Aggregation" in annotations["description"]
        assert "http://localhost:3001/d/span-aggregation" in annotations["description"]


# ---------------------------------------------------------------------------
# Dashboard tests
# ---------------------------------------------------------------------------


class TestAggregationDashboard:
    """Test span_aggregation.json dashboard configuration."""
    
    def test_dashboard_json_is_valid(self):
        import json
        import os
        from pathlib import Path
        DASHBOARD_PATH = Path(__file__).resolve().parent.parent / "infra" / "grafana" / "dashboards" / "span_aggregation.json"
        assert DASHBOARD_PATH.exists(), f"missing {DASHBOARD_PATH}"
        dashboard = json.loads(DASHBOARD_PATH.read_text(encoding="utf-8"))
        assert dashboard["uid"] == "span-aggregation"
        assert dashboard["title"] == "Span Aggregation"
    
    def test_dashboard_plots_aggregation_ratio(self):
        import json
        from pathlib import Path
        DASHBOARD_PATH = Path(__file__).resolve().parent.parent / "infra" / "grafana" / "dashboards" / "span_aggregation.json"
        dashboard = json.loads(DASHBOARD_PATH.read_text(encoding="utf-8"))
        
        # Find the aggregation ratio gauge panel
        ratio_panels = [
            p
            for p in dashboard["panels"]
            if p.get("type") == "gauge"
            and "aggregation_ratio" in json.dumps(p)
        ]
        assert len(ratio_panels) == 1, "exactly one gauge panel should plot aggregation_ratio"
        
        panel = ratio_panels[0]
        assert panel["title"] == "Aggregation Ratio"
        assert panel["fieldConfig"]["defaults"]["max"] == 0.2
    
    def test_dashboard_has_threshold_annotation(self):
        import json
        from pathlib import Path
        DASHBOARD_PATH = Path(__file__).resolve().parent.parent / "infra" / "grafana" / "dashboards" / "span_aggregation.json"
        dashboard = json.loads(DASHBOARD_PATH.read_text(encoding="utf-8"))
        
        # Check for threshold annotation
        annotations = dashboard["annotations"]["list"]
        threshold_annotations = [
            a for a in annotations
            if a.get("type") == "annotation"
            and "spans_aggregated_total" in a.get("expr", "")
        ]
        assert len(threshold_annotations) == 1, "should have one annotation for aggregation flushes"