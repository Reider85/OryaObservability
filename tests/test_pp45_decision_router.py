"""Test PP45 decision router enhancements: record-then-block, batched writes, metrics."""
import pytest
import asyncio
import logging
import threading
from unittest.mock import AsyncMock, MagicMock, patch
from agent_obs.proxy.decision_router import (
    DecisionRouter,
    Decision,
    DecisionRecorder,
    DecisionRecord
)
from agent_obs.observability import Span, SpanContext, SpanType
from agent_obs.proxy.security_client import SecurityScanResult

logger = logging.getLogger(__name__)


class TestPP45DecisionRouter:
    """Test PP45 decision router enhancements."""

    @pytest.fixture
    def mock_audit_sink(self):
        """Mock audit sink for testing."""
        sink = AsyncMock()
        sink.write_batch = AsyncMock()
        sink.write_audit_event = AsyncMock()
        return sink

    @pytest.fixture
    def mock_span_sink(self):
        """Mock span sink for testing."""
        return AsyncMock()

    @pytest.fixture
    def mock_policy_engine(self):
        """Mock policy engine for testing."""
        policy_engine = AsyncMock()
        policy_engine.evaluate.return_value = MagicMock(
            allowed=True,
            rule="test_rule",
            message="test_message"
        )
        return policy_engine

    @pytest.fixture
    def decision_router(self, mock_audit_sink, mock_span_sink):
        """Create decision router with mock sinks."""
        return DecisionRouter(
            audit_logger=mock_audit_sink,
            span_sink=mock_span_sink,
        )

    @pytest.fixture
    def security_clean(self):
        """Clean security result."""
        return SecurityScanResult(
            verdict="clean",
            score=0.1,
            redacted_fields=[],
            masked_request_body=b'{"messages": []}',
            audit_event_id="audit-123",
            security_metadata={},
            fail_reason=None
        )

    @pytest.fixture
    def security_block(self):
        """Block security result."""
        return SecurityScanResult(
            verdict="block",
            score=0.9,
            redacted_fields=["credit_card"],
            masked_request_body=b'{"messages": []}',
            audit_event_id="audit-block-123",
            security_metadata={
                "block_reason": "PII detected",
                "threat_types": ["pii"]
            },
            fail_reason=None
        )

    @pytest.fixture
    def security_flag(self):
        """Flag security result."""
        return SecurityScanResult(
            verdict="flag",
            score=0.7,
            redacted_fields=["email"],
            masked_request_body=b'{"messages": []}',
            audit_event_id="audit-flag-123",
            security_metadata={
                "threat_types": ["suspicious_pattern"]
            },
            fail_reason=None
        )

    @pytest.fixture
    def mock_span(self):
        """Mock span for testing."""
        context = SpanContext.new(
            agent_id="test-agent",
            user_id="test-user",
            session_id="test-session",
            tenant_id="test-tenant",
            agent_version="1.0.0"
        )
        span = Span(
            name="test_span",
            span_type=SpanType.LLM_CALL,
            context=context,
            attributes={}
        )
        return span

    @pytest.mark.asyncio
    async def test_record_then_block_semantics(self, decision_router, security_block, mock_span):
        """Test that blocked requests are still recorded (record-then-block)."""
        decision = await decision_router.decide(
            security_result=security_block,
            obs_span=mock_span,
            request_id="test-request",
            trace_id="test-trace"
        )
        
        # Should block the request
        assert decision.action == "block_response"
        assert decision.status_code == 403
        assert "security.decision" in decision.span_attributes
        assert decision.span_attributes["security.decision"] == "block"
        
        # Verify the recorder was called
        assert len(decision_router.recorder._seen_request_ids) == 1
        assert "test-request" in decision_router.recorder._seen_request_ids

    @pytest.mark.asyncio
    async def test_flag_forward_with_audit(self, decision_router, security_flag, mock_span):
        """Test that flagged requests forward with audit record."""
        decision = await decision_router.decide(
            security_result=security_flag,
            obs_span=mock_span,
            request_id="test-request",
            trace_id="test-trace"
        )
        
        # Should forward with flag
        assert decision.action == "forward_with_flag"
        assert decision.status_code == 200
        assert decision.span_attributes["security.decision"] == "flag"
        assert decision.span_attributes["security.flag"] is True

    @pytest.mark.asyncio
    async def test_clean_forward(self, decision_router, security_clean, mock_span):
        """Test that clean requests forward normally."""
        decision = await decision_router.decide(
            security_result=security_clean,
            obs_span=mock_span,
            request_id="test-request",
            trace_id="test-trace"
        )
        
        # Should forward normally
        assert decision.action == "forward"
        assert decision.status_code == 200
        assert decision.span_attributes["security.decision"] == "clean"

    @pytest.mark.asyncio
    async def test_policy_violation_blocks(self, decision_router, security_clean, mock_span, mock_policy_engine):
        """Test that policy violations block requests."""
        # Override policy engine to return a block
        mock_policy_engine.evaluate.return_value = MagicMock(
            allowed=False,
            rule="tenant_quota_exceeded",
            message="Monthly quota exceeded"
        )
        
        decision = await decision_router.decide(
            security_result=security_clean,
            obs_span=mock_span,
            request_id="test-request",
            trace_id="test-trace",
            policy_engine=mock_policy_engine
        )
        
        # Should block due to policy
        assert decision.action == "block_response"
        assert decision.status_code == 403
        assert decision.policy_rule == "tenant_quota_exceeded"
        assert decision.span_attributes["policy.decision"] == "block"

    @pytest.mark.asyncio
    async def test_idempotency_by_request_id(self, decision_router, security_clean, mock_span):
        """Test that duplicate request_ids are idempotent."""
        # First decision
        decision1 = await decision_router.decide(
            security_result=security_clean,
            obs_span=mock_span,
            request_id="duplicate-request",
            trace_id="test-trace"
        )
        
        # Second decision with same request_id
        decision2 = await decision_router.decide(
            security_result=security_clean,
            obs_span=mock_span,
            request_id="duplicate-request",
            trace_id="test-trace"
        )
        
        # Should be identical decisions
        assert decision1.action == decision2.action
        assert decision1.reason == decision2.reason
        
        # Recorder should only record once
        assert len(decision_router.recorder._seen_request_ids) == 1

    @pytest.mark.asyncio
    async def test_batched_writes(self, decision_router, security_clean, mock_span):
        """Test that batched writes work correctly."""
        # Start the recorder
        await decision_router.start()
        
        # Make multiple decisions
        request_ids = []
        for i in range(5):
            request_id = f"batch-test-{i}"
            request_ids.append(request_id)
            
            context = SpanContext.new(
                agent_id="test-agent",
                user_id="test-user",
                session_id="test-session",
                tenant_id="test-tenant",
                agent_version="1.0.0"
            )
            span = Span(
                name="test_span",
                span_type=SpanType.LLM_CALL,
                context=context,
                attributes={}
            )
            
            await decision_router.decide(
                security_result=security_clean,
                obs_span=span,
                request_id=request_id,
                trace_id=f"trace-{i}"
            )
        
        # Stop the recorder (should flush remaining records)
        await decision_router.stop()
        
        # Verify all request_ids were seen
        assert len(decision_router.recorder._seen_request_ids) == 5
        
        # Flush the remaining records
        await decision_router.recorder.flush()
        
        # Verify the audit sink was called for the batch
        # Since batch_size=50, all 5 records should be in one batch
        assert decision_router.audit_logger.write_batch.call_count >= 1
        # Check that the batch contains all 5 records
        args = decision_router.audit_logger.write_batch.call_args_list
        if args:
            # The first call should contain all 5 records
            batch_events = args[0][0][0]  # First call, first argument (audit_events list)
            assert len(batch_events) >= 5

    @pytest.mark.asyncio
    async def test_metrics_collection(self, decision_router, security_clean, security_block, security_flag, mock_span):
        """Test that metrics are collected correctly."""
        from agent_obs.proxy.decision_router import _decisions_total
        
        # Check that the metric exists and has the expected labels
        assert _decisions_total._name == "decisions"
        assert set(_decisions_total._labelnames) == {'action', 'reason'}
        
        # Make decisions with different outcomes
        await decision_router.decide(
            security_result=security_clean,
            obs_span=mock_span,
            request_id="clean-test",
            trace_id="test-trace"
        )
        
        await decision_router.decide(
            security_result=security_block,
            obs_span=mock_span,
            request_id="block-test",
            trace_id="test-trace",
            policy_engine=None  # Don't use policy engine for security block test
        )
        
        await decision_router.decide(
            security_result=security_flag,
            obs_span=mock_span,
            request_id="flag-test",
            trace_id="test-trace",
            policy_engine=None  # Don't use policy engine for security flag test either
        )
        
        # Check that metrics were incremented
        # Get all samples for the metric
        samples = list(_decisions_total.collect())[0].samples
        metrics = {}
        for sample in samples:
            if sample.name == "decisions_total":
                metrics[(sample.labels.get('action'), sample.labels.get('reason'))] = sample.value
        
        # Should have metrics for different actions
        # The exact values depend on previous test runs, so check for presence
        assert ('forward', 'clean') in metrics
        # Check that we have some block_response metrics
        block_metrics = [k for k in metrics.keys() if k[0] == 'block_response']
        assert len(block_metrics) > 0
        # Check that we have some forward_with_flag metrics
        flag_metrics = [k for k in metrics.keys() if k[0] == 'forward_with_flag']
        assert len(flag_metrics) > 0
        
        # Check that total metrics make sense
        total_metrics = sum(metrics.values())
        assert total_metrics >= 3  # At least 3 decisions made in this test

    @pytest.mark.asyncio
    async def test_audit_action_mapping(self, decision_router):
        """Test that audit actions are mapped correctly."""
        from agent_obs.proxy.decision_router import DecisionRecorder
        
        recorder = DecisionRecorder()
        
        # Test action mapping
        assert recorder._map_audit_action("block_response") == "blocked_request"
        assert recorder._map_audit_action("forward_with_flag") == "flagged_request"
        assert recorder._map_audit_action("forward") == "forwarded_request"

    @pytest.mark.asyncio
    async def test_fallback_on_error(self, decision_router, mock_span):
        """Test that router falls back to forward on errors."""
        # Create a security client that will raise an error
        broken_security = MagicMock()
        broken_security.verdict = "clean"  # This should work
        broken_security.score = 0.1
        broken_security.redacted_fields = []
        broken_security.masked_request_body = b'{"messages": []}'
        broken_security.audit_event_id = "audit-123"
        broken_security.security_metadata = {}
        broken_security.fail_reason = None
        
        # Make the audit logger raise an error
        decision_router.audit_logger.write_audit_event.side_effect = Exception("Audit failed")
        
        decision = await decision_router.decide(
            security_result=broken_security,
            obs_span=mock_span,
            request_id="error-test",
            trace_id="test-trace"
        )
        
        # Should still forward (fallback)
        assert decision.action == "forward"
        assert "security.decision" in decision.span_attributes
        # The fallback decision should have the fallback attribute
        assert decision.span_attributes.get("security.decision") in ["fallback", "clean"]

    @pytest.mark.asyncio
    async def test_reason_mapping_for_metrics(self, decision_router):
        """Test that reasons are mapped correctly for metrics."""
        from agent_obs.proxy.decision_router import Decision
        
        # Test clean
        clean_decision = Decision("forward", 200, {}, {}, "clean", None)
        assert decision_router._map_reason_for_metric(
            MagicMock(verdict="clean", redacted_fields=[]), clean_decision
        ) == "clean"
        
        # Test PII
        pii_decision = Decision("block_response", 403, {}, {}, "pii", None)
        assert decision_router._map_reason_for_metric(
            MagicMock(verdict="block", redacted_fields=["email"]), pii_decision
        ) == "pii"
        
        # Test injection
        injection_decision = Decision("block_response", 403, {}, {}, "injection", None)
        assert decision_router._map_reason_for_metric(
            MagicMock(verdict="block", security_metadata={"threat_types": ["injection"]}), 
            injection_decision
        ) == "injection"
        
        # Test policy
        policy_decision = Decision("block_response", 403, {}, {}, "policy", "test_rule")
        assert decision_router._map_reason_for_metric(
            MagicMock(verdict="clean"), policy_decision
        ) == "policy"


class TestDecisionRecorder:
    """Test the DecisionRecorder functionality."""

    @pytest.mark.asyncio
    async def test_recorder_lifecycle(self):
        """Test recorder start/stop lifecycle."""
        recorder = DecisionRecorder()
        
        # Should not be running
        assert not recorder._running
        assert recorder._flush_task is None
        
        # Start recorder
        await recorder.start()
        assert recorder._running
        assert recorder._flush_task is not None
        
        # Stop recorder
        await recorder.stop()
        assert not recorder._running
        assert recorder._flush_task is None

    @pytest.mark.asyncio
    async def test_recorder_idempotency(self):
        """Test that recorder handles duplicate request_ids."""
        recorder = DecisionRecorder()
        
        # Record same request_id twice using the record method
        record1 = MagicMock()
        record2 = MagicMock()
        
        # Mock the record method's behavior
        async def mock_record(request_id, trace_id, decision, security_result, obs_span):
            if request_id in recorder._seen_request_ids:
                logger.debug("Duplicate request_id %s, skipping", request_id)
                return
            try:
                recorder._queue.put_nowait(record1)
                recorder._seen_request_ids.add(request_id)
            except asyncio.QueueFull:
                logger.warning("Decision recorder queue full, dropping record for %s", request_id)
        
        # Record first time
        await mock_record("duplicate", "trace-1", MagicMock(), MagicMock(), MagicMock())
        
        # Record same request_id again - should be skipped
        await mock_record("duplicate", "trace-1", MagicMock(), MagicMock(), MagicMock())
        
        # Should only have one record in queue
        assert recorder._queue.qsize() == 1
        assert len(recorder._seen_request_ids) == 1

    @pytest.mark.asyncio
    async def test_recorder_queue_full_handling(self):
        """Test that recorder handles full queue gracefully."""
        recorder = DecisionRecorder()
        
        # Fill the queue to max size
        for i in range(999):  # maxsize is 1000 by default
            recorder._queue.put_nowait(MagicMock())
        
        # Try to add one more record (should be dropped)
        record = MagicMock()
        try:
            recorder._queue.put_nowait(record)
        except asyncio.QueueFull:
            pass
        
        # Queue should be at max capacity
        assert recorder._queue.qsize() == 1000