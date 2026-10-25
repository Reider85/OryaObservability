"""
Tests for PP10 Langfuse event mapping in langfuse_exporter.py.

Tests that events from aggregated spans are converted to child OTLP spans
with langfuse.observation.type=event and proper parent-child relationships.
"""

import json
import pytest
from datetime import datetime, timezone
from agent_obs.exporters.langfuse_exporter import (
    _event_to_child_span,
    _generate_event_span_id,
    _iso_to_epoch,
    _batch_to_otlp,
    _to_hex,
    MAX_EVENT_SPANS_PER_SPAN
)
from agent_obs.observability import Span, SpanContext, Event


class TestEventMappingPP10:
    """Test PP10 event mapping to Langfuse EVENT observations"""
    
    def setup_method(self):
        """Setup test data"""
        self.parent_span_id = "01H2ABC123"
        self.trace_id = "01H2XYZ789"
        
        # Create test events with ISO timestamps
        self.event1 = Event(
            name="step_completed",
            timestamp="2024-01-15T10:30:00.000Z",
            attributes={"status": "ok", "step": "reasoning"}
        )
        self.event2 = Event(
            name="error_occurred",
            timestamp="2024-01-15T10:30:01.000Z",
            attributes={"status": "error", "error_code": "500"}
        )
        
        # Create parent span
        self.parent_span = Span(
            context=SpanContext(
                trace_id=self.trace_id,
                span_id=self.parent_span_id,
                parent_span_id=None
            ),
            name="agent.reasoning:aggregated:5",
            span_type="agent.reasoning",
            start_time=1705326600.0,  # 2024-01-15T10:30:00Z
            end_time=1705326602.0,
            attributes={
                "aggregation.children_count": 5,
                "aggregation.bytes_saved": 1024
            }
        )
        self.parent_span.events = [self.event1, self.event2]
    
    def test_generate_event_span_id_deterministic(self):
        """Test that event span ID generation is deterministic"""
        span_id1 = _generate_event_span_id(self.parent_span_id, "test_event", "2024-01-15T10:30:00Z")
        span_id2 = _generate_event_span_id(self.parent_span_id, "test_event", "2024-01-15T10:30:00Z")
        span_id3 = _generate_event_span_id(self.parent_span_id, "different_event", "2024-01-15T10:30:00Z")
        
        assert span_id1 == span_id2  # Same inputs → same ID
        assert span_id1 != span_id3  # Different name → different ID
    
    def test_iso_to_epoch_conversion(self):
        """Test ISO timestamp to epoch conversion"""
        # Test valid ISO string (Z = UTC)
        iso_ts = "2024-01-15T10:30:00.000Z"
        epoch = _iso_to_epoch(iso_ts)
        # Create datetime in UTC
        dt_utc = datetime(2024, 1, 15, 10, 30, 0, tzinfo=timezone.utc)
        expected = dt_utc.timestamp()
        assert abs(epoch - expected) < 0.001
        
        # Test None input
        assert _iso_to_epoch(None) == 0.0
    
    def test_event_to_child_span_basic(self):
        """Test basic conversion of event to child span"""
        child_span = _event_to_child_span(self.parent_span, self.event1, 0)
        
        # Check trace and parent IDs (hex conversion)
        expected_trace_id = _to_hex(self.trace_id).zfill(32)
        expected_parent_id = _to_hex(self.parent_span_id).zfill(16)[-16:]
        assert child_span["traceId"] == expected_trace_id
        assert child_span["parentSpanId"] == expected_parent_id
        
        # Check span properties
        assert child_span["name"] == self.event1.name
        assert child_span["kind"] == 1  # INTERNAL
        
        # Check timestamps (should be equal for zero-duration event)
        assert child_span["startTimeUnixNano"] == child_span["endTimeUnixNano"]
    
    def test_event_to_child_span_attributes(self):
        """Test that event attributes are mapped to langfuse.observation.metadata"""
        child_span = _event_to_child_span(self.parent_span, self.event1, 0)
        
        # Check langfuse.observation.type
        type_attr = next((a for a in child_span["attributes"] if a["key"] == "langfuse.observation.type"), None)
        assert type_attr is not None
        assert type_attr["value"]["stringValue"] == "event"
        
        # Check metadata mapping
        metadata_attrs = [a for a in child_span["attributes"] if a["key"].startswith("langfuse.observation.metadata.")]
        assert len(metadata_attrs) >= 1
        
        # Check specific attribute
        status_attr = next((a for a in metadata_attrs if a["key"] == "langfuse.observation.metadata.status"), None)
        assert status_attr is not None
        assert status_attr["value"]["stringValue"] == "ok"
    
    def test_event_to_child_span_error_status(self):
        """Test that error status is properly mapped"""
        child_span = _event_to_child_span(self.parent_span, self.event2, 1)
        
        # Check error level
        level_attr = next((a for a in child_span["attributes"] if a["key"] == "langfuse.observation.level"), None)
        assert level_attr is not None
        assert level_attr["value"]["stringValue"] == "ERROR"
        
        # Check status code
        assert child_span["status"]["code"] == 2  # ERROR
        
        # Non-error event should have OK status
        child_span_ok = _event_to_child_span(self.parent_span, self.event1, 0)
        assert child_span_ok["status"]["code"] == 0  # OK
    
    def test_batch_to_otlp_includes_child_spans(self):
        """Test that _batch_to_otlp includes child event spans"""
        batch = _batch_to_otlp([self.parent_span])
        
        # Debug: print the batch structure
        print("Batch structure:", json.dumps(batch, indent=2))
        
        # Extract all spans from the batch
        all_spans = []
        for resource_span in batch["resourceSpans"]:
            for scope_span in resource_span["scopeSpans"]:
                all_spans.extend(scope_span["spans"])
        
        # Should have original span + 2 child spans
        assert len(all_spans) == 3
        
        # Find child spans
        child_spans = [s for s in all_spans if s["parentSpanId"] == _to_hex(self.parent_span_id).zfill(16)[-16:]]
        assert len(child_spans) == 2
        
        # Check child span names
        child_names = [s["name"] for s in child_spans]
        assert "step_completed" in child_names
        assert "error_occurred" in child_names
    
    def test_batch_to_otlp_preserves_original_span(self):
        """Test that original span is still included in the batch"""
        batch = _batch_to_otlp([self.parent_span])
        
        all_spans = []
        for resource_span in batch["resourceSpans"]:
            for scope_span in resource_span["scopeSpans"]:
                all_spans.extend(scope_span["spans"])
        
        # Find original span (no parentSpanId)
        original_span = next((s for s in all_spans if s["parentSpanId"] == ""), None)
        assert original_span is not None
        assert original_span["name"] == "agent.reasoning:aggregated:5"
    
    def test_batch_to_otlp_event_cap(self):
        """Test that MAX_EVENT_SPANS_PER_SPAN cap is respected"""
        # Create many events
        many_events = []
        for i in range(MAX_EVENT_SPANS_PER_SPAN + 10):
            event = Event(
                name=f"event_{i}",
                timestamp="2024-01-15T10:30:00.000Z",
                attributes={"index": i}
            )
            many_events.append(event)
        
        self.parent_span.events = many_events
        
        batch = _batch_to_otlp([self.parent_span])
        
        all_spans = []
        for resource_span in batch["resourceSpans"]:
            for scope_span in resource_span["scopeSpans"]:
                all_spans.extend(scope_span["spans"])
        
        # Should have original + capped child spans
        expected_parent_id = _to_hex(self.parent_span_id).zfill(16)[-16:]
        child_spans = [s for s in all_spans if s["parentSpanId"] == expected_parent_id]
        assert len(child_spans) == MAX_EVENT_SPANS_PER_SPAN
        
        # Check that first and last events are included (middle might be truncated)
        child_names = [s["name"] for s in child_spans]
        assert "event_0" in child_names
        assert f"event_{MAX_EVENT_SPANS_PER_SPAN-1}" in child_names
    
    def test_batch_to_otlp_multiple_spans(self):
        """Test that multiple spans each get their own child spans"""
        # Create another span with events
        another_span = Span(
            context=SpanContext(
                trace_id=self.trace_id,
                span_id="01H2DEF456",
                parent_span_id=self.parent_span_id
            ),
            name="tool.call",
            span_type="tool.call",
            start_time=1705326603.0,
            end_time=1705326604.0,
            attributes={}
        )
        another_span.events = [self.event1]  # One event
        
        batch = _batch_to_otlp([self.parent_span, another_span])
        
        all_spans = []
        for resource_span in batch["resourceSpans"]:
            for scope_span in resource_span["scopeSpans"]:
                all_spans.extend(scope_span["spans"])
        
        # Should have 2 original spans + 2 (from parent) + 1 (from another_span) = 5 total
        assert len(all_spans) == 5
        
        # Check that both parents have child spans
        child_spans = [s for s in all_spans if s["parentSpanId"] != ""]
        assert len(child_spans) == 4  # 2 from parent, 1 from another_span, + another_span itself as child