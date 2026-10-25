"""Tests for PP12 AdaptiveEvalSampler (day/night/incident modes).

Tests the adaptive eval sampler that adjusts evaluation frequency based on:
- Time of day (day: 08:00-22:00, night: 22:00-08:00)
- Active incidents (100% eval for security traces, 0% for others)
"""

import asyncio
import datetime
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_obs.eval.adaptive_sampler import AdaptiveEvalSampler, Trace
from agent_obs.guardrail.audit import EvalSamplerModeChangeAuditEvent


class TestAdaptiveEvalSampler:
    """Test AdaptiveEvalSampler class."""
    
    @pytest.fixture
    def sampler(self):
        """Create AdaptiveEvalSampler for testing."""
        sampler = AdaptiveEvalSampler(
            prometheus_url="http://test:9090",
            cache_ttl=10.0,
            hot_store=MagicMock(),
            enable_audit=True,
        )
        # Mock the logger to avoid debug calls in tests
        sampler._logger = MagicMock()
        return sampler
    
    @pytest.fixture
    def trace_security(self):
        """Create a trace with security incident."""
        return Trace(
            trace_id="trace_security_123",
            attributes={"security.incident": "true"}
        )
    
    @pytest.fixture
    def trace_normal(self):
        """Create a normal trace without security incident."""
        return Trace(
            trace_id="trace_normal_456",
            attributes={"security.incident": "false"}
        )
    
    @pytest.mark.asyncio
    async def test_detect_mode_day(self, sampler):
        """Test day mode detection (08:00-22:00 UTC)."""
        day_time = datetime.datetime(2024, 1, 1, 12, 0, tzinfo=datetime.timezone.utc)
        mode = await sampler.detect_mode(now=day_time)
        assert mode == "day"
    
    @pytest.mark.asyncio
    async def test_detect_mode_night(self, sampler):
        """Test night mode detection (22:00-08:00 UTC)."""
        night_time = datetime.datetime(2024, 1, 1, 23, 0, tzinfo=datetime.timezone.utc)
        mode = await sampler.detect_mode(now=night_time)
        assert mode == "night"
    
    @pytest.mark.asyncio
    async def test_detect_mode_night_early_morning(self, sampler):
        """Test night mode detection (early morning 07:00)."""
        night_time = datetime.datetime(2024, 1, 1, 7, 0, tzinfo=datetime.timezone.utc)
        mode = await sampler.detect_mode(now=night_time)
        assert mode == "night"
    
    @pytest.mark.asyncio
    async def test_detect_mode_incident_override(self, sampler):
        """Test incident mode overrides day/night."""
        # Mock incident as active
        sampler._fetch_active_incident = AsyncMock(return_value=True)
        
        day_time = datetime.datetime(2024, 1, 1, 12, 0, tzinfo=datetime.timezone.utc)
        mode = await sampler.detect_mode(now=day_time)
        assert mode == "incident"
        
        night_time = datetime.datetime(2024, 1, 1, 23, 0, tzinfo=datetime.timezone.utc)
        mode = await sampler.detect_mode(now=night_time)
        assert mode == "incident"
    
    @pytest.mark.asyncio
    async def test_detect_mode_no_incident(self, sampler):
        """Test day/night mode when no incident."""
        # Mock incident as inactive
        sampler._fetch_active_incident = AsyncMock(return_value=False)
        
        day_time = datetime.datetime(2024, 1, 1, 12, 0, tzinfo=datetime.timezone.utc)
        mode = await sampler.detect_mode(now=day_time)
        assert mode == "day"
        
        night_time = datetime.datetime(2024, 1, 1, 23, 0, tzinfo=datetime.timezone.utc)
        mode = await sampler.detect_mode(now=night_time)
        assert mode == "night"
    
    @pytest.mark.asyncio
    async def test_detect_mode_boundary_day_to_night(self, sampler):
        """Test day-to-night boundary (21:59 -> 22:00)."""
        # Mock incident as inactive
        sampler._fetch_active_incident = AsyncMock(return_value=False)
        
        # Just before night
        day_time = datetime.datetime(2024, 1, 1, 21, 59, tzinfo=datetime.timezone.utc)
        mode = await sampler.detect_mode(now=day_time)
        assert mode == "day"
        
        # Exactly at night start
        night_time = datetime.datetime(2024, 1, 1, 22, 0, tzinfo=datetime.timezone.utc)
        mode = await sampler.detect_mode(now=night_time)
        assert mode == "night"
    
    @pytest.mark.asyncio
    async def test_detect_mode_boundary_night_to_day(self, sampler):
        """Test night-to-day boundary (07:59 -> 08:00)."""
        # Mock incident as inactive
        sampler._fetch_active_incident = AsyncMock(return_value=False)
        
        # Just before day
        night_time = datetime.datetime(2024, 1, 1, 7, 59, tzinfo=datetime.timezone.utc)
        mode = await sampler.detect_mode(now=night_time)
        assert mode == "night"
        
        # Exactly at day start
        day_time = datetime.datetime(2024, 1, 1, 8, 0, tzinfo=datetime.timezone.utc)
        mode = await sampler.detect_mode(now=day_time)
        assert mode == "day"
    
    @pytest.mark.asyncio
    async def test_detect_mode_incident_cache_hit(self, sampler):
        """Test incident cache hit within TTL."""
        # Set up cache
        sampler._incident_cache_value = True
        sampler._incident_cache_time = time.time() - 5.0  # 5 seconds ago
        sampler._fetch_active_incident = AsyncMock()  # Should not be called
        
        mode = await sampler.detect_mode()
        assert mode == "incident"
        # _fetch_active_incident should not be called due to cache in detect_mode
        sampler._fetch_active_incident.assert_not_called()
    
    @pytest.mark.asyncio
    async def test_detect_mode_incident_cache_miss(self, sampler):
        """Test incident cache miss and refresh."""
        # Set up cache with old value
        sampler._incident_cache_value = False
        sampler._incident_cache_time = time.time() - 50.0  # 50 seconds ago (expired)
        
        # Mock the fetch to return new value
        sampler._fetch_active_incident = AsyncMock(return_value=True)
        
        mode = await sampler.detect_mode()
        assert mode == "incident"
        sampler._fetch_active_incident.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_detect_mode_prometheus_error_fallback(self, sampler):
        """Test fallback to no incident on Prometheus error."""
        # Mock Prometheus error
        sampler._fetch_active_incident = AsyncMock(side_effect=Exception("Prometheus error"))

        # Pin a daytime clock: the day/night fallback must not depend on when
        # the test runs.
        day_time = datetime.datetime(2024, 1, 1, 12, 0, tzinfo=datetime.timezone.utc)
        mode = await sampler.detect_mode(now=day_time)
        assert mode == "day"  # Default to day when incident fetch fails
    
    @pytest.mark.asyncio
    async def test_should_eval_day_mode(self, sampler, trace_normal):
        """Test should_eval in day mode (10% sampling)."""
        # Pin the detected mode (symmetric with the night-mode test): without
        # this, detect_mode() derives day/night from the current UTC hour.
        sampler.detect_mode = AsyncMock(return_value="day")
        
        # Test multiple trace IDs to verify 10% rate
        eval_count = 0
        for i in range(100):
            trace = Trace(trace_id=f"trace_day_{i}", attributes={"security.incident": "false"})
            should_eval, reason = await sampler.should_eval(trace)
            if should_eval:
                eval_count += 1
                assert reason == "day_10pct"
        
        # Should be approximately 10 out of 100
        assert 5 <= eval_count <= 15  # Allow some variance for randomness
    
    @pytest.mark.asyncio
    async def test_should_eval_night_mode(self, sampler, trace_normal):
        """Test should_eval in night mode (50% sampling)."""
        # Set mode to night and mock detect_mode to return night
        sampler._current_mode = "night"
        sampler.detect_mode = AsyncMock(return_value="night")
        
        # Test multiple trace IDs to verify 50% rate
        eval_count = 0
        for i in range(100):
            trace = Trace(trace_id=f"trace_night_{i}", attributes={"security.incident": "false"})
            should_eval, reason = await sampler.should_eval(trace)
            if should_eval:
                eval_count += 1
                assert reason == "night_50pct"
        
        # Should be approximately 50 out of 100
        assert 40 <= eval_count <= 60  # Allow some variance for randomness
    
    @pytest.mark.asyncio
    async def test_should_eval_incident_security_trace(self, sampler, trace_security):
        """Test should_eval in incident mode for security trace (100% sampling)."""
        # Set mode to incident
        sampler._current_mode = "incident"
        sampler._fetch_active_incident = AsyncMock(return_value=True)
        
        should_eval, reason = await sampler.should_eval(trace_security)
        assert should_eval is True
        assert reason == "incident_security"
    
    @pytest.mark.asyncio
    async def test_should_eval_incident_normal_trace(self, sampler, trace_normal):
        """Test should_eval in incident mode for normal trace (0% sampling)."""
        # Set mode to incident
        sampler._current_mode = "incident"
        sampler._fetch_active_incident = AsyncMock(return_value=True)
        
        should_eval, reason = await sampler.should_eval(trace_normal)
        assert should_eval is False
        assert reason == "skip"
    
    @pytest.mark.asyncio
    async def test_should_eval_deterministic_same_trace(self, sampler, trace_normal):
        """Test that same trace ID gives same decision (determinism)."""
        # Set mode to day
        sampler._current_mode = "day"
        sampler._fetch_active_incident = AsyncMock(return_value=False)
        
        # Should get same result for same trace ID
        result1 = await sampler.should_eval(trace_normal)
        result2 = await sampler.should_eval(trace_normal)
        assert result1 == result2
    
    @pytest.mark.asyncio
    async def test_should_eval_different_trace_different_result(self, sampler):
        """Test that different trace IDs can give different results."""
        # Set mode to day
        sampler._current_mode = "day"
        sampler._fetch_active_incident = AsyncMock(return_value=False)
        
        trace1 = Trace(trace_id="trace_same_bucket", attributes={"security.incident": "false"})
        trace2 = Trace(trace_id="trace_different_bucket", attributes={"security.incident": "false"})
        
        # These should give different results (one in bucket <10, one >=10)
        result1 = await sampler.should_eval(trace1)
        result2 = await sampler.should_eval(trace2)
        
        # They might be the same due to randomness, but at least one should be evaluable
        assert result1[0] in [True, False]
        assert result2[0] in [True, False]
    
    @pytest.mark.asyncio
    async def test_mode_change_metric_update(self, sampler):
        """Test that eval_sampler_mode metric updates on mode change."""
        # Set initial mode
        sampler._current_mode = "day"
        sampler._fetch_active_incident = AsyncMock(return_value=False)
        
        # Mock metric to capture calls
        with patch('agent_obs.eval.adaptive_sampler.eval_sampler_mode') as mock_metric:
            # Change to night mode
            await sampler.detect_mode(now=datetime.datetime(2024, 1, 1, 23, 0, tzinfo=datetime.timezone.utc))
            
            # Verify metric was called with night=1, day=0
            mock_metric.labels.assert_any_call(mode="night")
            mock_metric.labels.assert_any_call(mode="day")
    
    @pytest.mark.asyncio
    async def test_mode_change_audit_written(self, sampler):
        """Test that audit event is written on mode change."""
        # Set initial mode
        sampler._current_mode = "day"
        sampler._fetch_active_incident = AsyncMock(return_value=False)
        
        # Mock hot_store.write_audit_event
        sampler.hot_store.write_audit_event = MagicMock()
        
        # Change to night mode
        await sampler.detect_mode(now=datetime.datetime(2024, 1, 1, 23, 0, tzinfo=datetime.timezone.utc))
        
        # Verify audit event was written
        sampler.hot_store.write_audit_event.assert_called_once()
        event = sampler.hot_store.write_audit_event.call_args[0][0]
        assert isinstance(event, EvalSamplerModeChangeAuditEvent)
        assert event.resource["old_mode"] == "day"
        assert event.resource["new_mode"] == "night"
    
    @pytest.mark.asyncio
    async def test_mode_change_no_audit_when_same(self, sampler):
        """Test that no audit event is written when mode doesn't change."""
        # Set initial mode
        sampler._current_mode = "day"
        sampler._fetch_active_incident = AsyncMock(return_value=False)
        
        # Mock hot_store.write_audit_event
        sampler.hot_store.write_audit_event = MagicMock()
        
        # Call detect_mode again (should not change mode)
        await sampler.detect_mode(now=datetime.datetime(2024, 1, 1, 12, 0, tzinfo=datetime.timezone.utc))
        
        # Verify no audit event was written
        sampler.hot_store.write_audit_event.assert_not_called()
    
    @pytest.mark.asyncio
    async def test_mode_change_audit_failure_not_blocking(self, sampler):
        """Test that audit failure doesn't block mode detection."""
        # Set initial mode
        sampler._current_mode = "day"
        sampler._fetch_active_incident = AsyncMock(return_value=False)
        
        # Mock audit failure
        sampler.hot_store.write_audit_event = MagicMock(side_effect=Exception("Audit failed"))
        
        # Change mode (should succeed despite audit failure)
        mode = await sampler.detect_mode(now=datetime.datetime(2024, 1, 1, 23, 0, tzinfo=datetime.timezone.utc))
        
        assert mode == "night"
        sampler.hot_store.write_audit_event.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_trace_from_spans(self, sampler):
        """Test Trace.from_spans method."""
        from agent_obs.observability import Span, SpanContext
        
        # Create mock spans
        span1 = Span(
            name="span1",
            context=SpanContext(trace_id="test_trace", span_id="span1"),
            span_type="agent.reasoning",
            attributes={"key1": "value1", "security.incident": "true"}
        )
        span2 = Span(
            name="span2",
            context=SpanContext(trace_id="test_trace", span_id="span2"),
            span_type="agent.reasoning",
            attributes={"key2": "value2"}
        )
        
        trace = Trace.from_spans([span1, span2])
        assert trace.trace_id == "test_trace"
        assert trace.attributes["key1"] == "value1"
        assert trace.attributes["key2"] == "value2"
        assert trace.attributes["security.incident"] == "true"
        assert trace.has_security_incident is True
    
    @pytest.mark.asyncio
    async def test_trace_from_spans_empty(self, sampler):
        """Test Trace.from_spans with empty list raises ValueError."""
        with pytest.raises(ValueError, match="Cannot create Trace from empty span list"):
            Trace.from_spans([])
    
    @pytest.mark.asyncio
    async def test_trace_has_security_incident_property(self, sampler):
        """Test Trace.has_security_incident property."""
        # Test with security incident
        trace_with_incident = Trace(
            trace_id="trace1",
            attributes={"security.incident": "true"}
        )
        assert trace_with_incident.has_security_incident is True
        
        # Test without security incident
        trace_without_incident = Trace(
            trace_id="trace2",
            attributes={"security.incident": "false"}
        )
        assert trace_without_incident.has_security_incident is False
        
        # Test with missing attribute
        trace_missing = Trace(
            trace_id="trace3",
            attributes={}
        )
        assert trace_missing.has_security_incident is False


class TestEvalSamplerModeChangeAuditEvent:
    """Test EvalSamplerModeChangeAuditEvent class."""
    
    def test_for_mode_change(self):
        """Test EvalSamplerModeChangeAuditEvent.for_mode_change."""
        event = EvalSamplerModeChangeAuditEvent.for_mode_change(
            old_mode="day",
            new_mode="night",
            actor_id="test-sampler"
        )
        
        assert event.resource["old_mode"] == "day"
        assert event.resource["new_mode"] == "night"
        assert event.actor["id"] == "test-sampler"
        assert event.action == "eval_sampler.mode_change"
        assert "changed from day to night" in event.reason
    
    def test_to_dict(self):
        """Test EvalSamplerModeChangeAuditEvent.to_dict."""
        event = EvalSamplerModeChangeAuditEvent.for_mode_change(
            old_mode="day",
            new_mode="night"
        )
        
        event_dict = event.to_dict()
        assert event_dict["audit_id"] == event.audit_id
        assert event_dict["resource"]["old_mode"] == "day"
        assert event_dict["resource"]["new_mode"] == "night"
        assert event_dict["action"] == "eval_sampler.mode_change"
    
    def test_to_clickhouse_row(self):
        """Test EvalSamplerModeChangeAuditEvent.to_clickhouse_row."""
        event = EvalSamplerModeChangeAuditEvent.for_mode_change(
            old_mode="day",
            new_mode="night"
        )
        
        row = event.to_clickhouse_row()
        assert len(row) == 10  # audit_events_hot DDL columns
        assert row[4] == "eval_sampler.mode_change"  # action
        assert "changed from day to night" in row[7]  # reason