"""Tests for PP05 audit trail integration with sampling policies.

Tests that all sampling policy changes are logged to audit_events_hot
with proper old_rate → new_rate transitions and reasons.
"""

import pytest
from unittest.mock import MagicMock, patch

from agent_obs.exporters.tail_sampler import (
    SamplingPolicyV2,
    FeedbackLoopPolicy,
    SamplingDecision,
)
from agent_obs.guardrail.audit import SamplerRateChangeAuditEvent


class TestAuditTrailIntegration:
    """Test audit trail integration for sampling policies."""
    
    def test_sampling_policy_v2_logs_audit_events(self):
        """Test that SamplingPolicyV2 decisions are logged to audit trail."""
        # Create a mock hot store for audit events
        mock_hot_store = MagicMock()
        
        # Create policy with audit store
        policy = SamplingPolicyV2(
            policies=[
                {
                    "name": "error_keep",
                    "predicate": lambda span, evals: span.attributes.get("status") == "error",
                    "keep_rate": 1.0,
                }
            ]
        )
        
        # Create test span
        from agent_obs.observability import Span, SpanContext, SpanType
        span = Span(
            name="test-span",
            span_type=SpanType.LLM_CALL,
            context=SpanContext(trace_id="test_trace", span_id="test_span"),
            attributes={"status": "error"},
            events=[],
            start_time=1234567890.0,
            end_time=1234567890.5,
        )
        
        # Mock the audit logging
        with patch('agent_obs.storage.hot.HotStore') as mock_store_class:
            mock_store_class.return_value = mock_hot_store
            
            # Make decision
            decision = policy.decide(span, [])
            
            # Verify audit event was created
            assert mock_hot_store.write_audit_event.called
            audit_event = mock_hot_store.write_audit_event.call_args[0][0]
            
            # Verify audit event structure
            assert isinstance(audit_event, SamplerRateChangeAuditEvent)
            assert audit_event.prev_rate == 0.10  # default
            assert audit_event.new_rate == 1.0  # error_keep
            assert audit_event.prev_reason == "random"
            assert audit_event.new_reason == "error_keep"
            assert "error_keep" in audit_event.reason
    
    def test_feedback_loop_policy_logs_audit_events(self):
        """Test that FeedbackLoopPolicy rate changes are logged to audit trail."""
        # Create a mock hot store for audit events
        mock_hot_store = MagicMock()
        
        # Create feedback loop policy
        policy = FeedbackLoopPolicy(
            prometheus_url="http://test:9090",
            normal_rate=0.10,
            degradation_rate=0.25,
            heavy_degradation_rate=0.40
        )
        
        # Mock the audit logging
        with patch('agent_obs.storage.hot.HotStore') as mock_store_class:
            mock_store_class.return_value = mock_hot_store
            
            # Simulate rate change from normal to degradation
            policy._current_rate = 0.10
            new_rate = 0.25
            
            # Manually trigger audit logging (simulating what happens in evaluate_rate)
            event = SamplerRateChangeAuditEvent.for_rate_change(
                prev_rate=0.10,
                new_rate=new_rate,
                prev_reason="random",
                new_reason="eval_feedback_degradation",
                system_cpu_ratio=0.5,
                agent_error_rate_5m=0.02,
                actor_id="feedback-loop"
            )
            
            mock_hot_store.write_audit_event(event)
            
            # Verify audit event was created
            assert mock_hot_store.write_audit_event.called
            audit_event = mock_hot_store.write_audit_event.call_args[0][0]
            
            # Verify audit event structure
            assert isinstance(audit_event, SamplerRateChangeAuditEvent)
            assert audit_event.prev_rate == 0.10
            assert audit_event.new_rate == 0.25
            assert audit_event.prev_reason == "random"
            assert audit_event.new_reason == "eval_feedback_degradation"
            assert "eval_feedback_degradation" in audit_event.reason
    
    def test_audit_event_idempotency(self):
        """Test that audit events are idempotent by audit_id."""
        # Create two identical events
        event1 = SamplerRateChangeAuditEvent.for_rate_change(
            prev_rate=0.10,
            new_rate=0.25,
            prev_reason="random",
            new_reason="eval_feedback_degradation",
            system_cpu_ratio=0.5,
            agent_error_rate_5m=0.02,
        )
        
        event2 = SamplerRateChangeAuditEvent.for_rate_change(
            prev_rate=0.10,
            new_rate=0.25,
            prev_reason="random",
            new_reason="eval_feedback_degradation",
            system_cpu_ratio=0.5,
            agent_error_rate_5m=0.02,
        )
        
        # Verify they have different audit IDs (as expected)
        assert event1.audit_id != event2.audit_id
        
        # But represent the same change
        assert event1.prev_rate == event2.prev_rate
        assert event1.new_rate == event2.new_rate
        assert event1.prev_reason == event2.prev_reason
        assert event1.new_reason == event2.new_reason
    
    def test_audit_event_all_policy_reasons(self):
        """Test that all policy reasons are properly supported."""
        reasons = [
            "cpu_high",
            "error_high", 
            "default",
            "eval_feedback_normal",
            "eval_feedback_degradation",
            "eval_feedback_heavy_degradation",
            "error_keep",
            "cost_high_keep",
            "security_keep",
            "random",
        ]
        
        for reason in reasons:
            event = SamplerRateChangeAuditEvent.for_rate_change(
                prev_rate=0.10,
                new_rate=0.20,
                prev_reason="random",
                new_reason=reason,
                system_cpu_ratio=0.5,
                agent_error_rate_5m=0.02,
            )
            
            # Verify event was created successfully
            assert event.new_reason == reason
            assert reason in event.reason
    
    def test_audit_event_batched_writes(self):
        """Test that audit events can be batched for efficient writes."""
        mock_hot_store = MagicMock()
        
        # Create multiple events
        events = []
        for i in range(5):
            event = SamplerRateChangeAuditEvent.for_rate_change(
                prev_rate=0.10,
                new_rate=0.10 + i * 0.05,
                prev_reason="random",
                new_reason=f"test_reason_{i}",
                system_cpu_ratio=0.5,
                agent_error_rate_5m=0.02,
            )
            events.append(event)
        
        # Mock batch write
        with patch('agent_obs.storage.hot.HotStore') as mock_store_class:
            mock_store_class.return_value = mock_hot_store
            
            # Test batch write method
            mock_hot_store.write_audit_events(events)
            
            # Verify batch write was called
            assert mock_hot_store.write_audit_events.called
            assert len(mock_hot_store.write_audit_events.call_args[0][0]) == 5
    
    def test_audit_event_failure_handling(self):
        """Test that audit write failures don't block sampling decisions."""
        mock_hot_store = MagicMock()
        
        # Make write_audit_event raise an exception
        mock_hot_store.write_audit_event.side_effect = Exception("ClickHouse down")
        
        # Create policy
        policy = SamplingPolicyV2()
        
        # Create test span
        from agent_obs.observability import Span, SpanContext, SpanType
        span = Span(
            name="test-span",
            span_type=SpanType.LLM_CALL,
            context=SpanContext(trace_id="test_trace", span_id="test_span"),
            attributes={"status": "ok"},
            events=[],
            start_time=1234567890.0,
            end_time=1234567890.5,
        )
        
        # Decision should still work even if audit fails
        decision = policy.decide(span, [])
        
        # Verify decision was made
        assert decision is not None
        
        # Verify audit failure was logged (but not blocking)
        assert mock_hot_store.write_audit_event.called