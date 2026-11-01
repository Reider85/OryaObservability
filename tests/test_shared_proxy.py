"""Tests for shared proxy functionality (PP44 / T3.10.1).

Tests security client, shared pipeline, decision router, and integration with
LLMProxyServer for Mode B (shared obs + security proxy).
"""

import asyncio
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from prometheus_client import Counter
from agent_obs.observability import ObservabilitySDK, Span, SpanContext
from agent_obs.proxy import ProxyConfig
from agent_obs.proxy.security_client import SecurityClient, SecurityScanResult
from agent_obs.proxy.shared_pipeline import SharedPipeline, SharedPipelineResult
from agent_obs.proxy.decision_router import DecisionRouter
from agent_obs.proxy.audit_trail import SharedAuditTrail
from agent_obs.proxy.server import LLMProxyServer


class TestSecurityClient:
    """Test SecurityClient with OryaSecurity integration."""

    @pytest.fixture
    def security_client(self):
        """Create SecurityClient for testing."""
        with patch('agent_obs.proxy.security_client.httpx.AsyncClient'):
            client = SecurityClient(
                base_url="http://orya-security:8000",
                timeout=0.05,
                fail_open=True,
            )
            return client

    @pytest.fixture
    def mock_security_response(self):
        """Mock successful security scan response."""
        return {
            "verdict": "clean",
            "score": 0.1,
            "redacted_fields": [],
            "masked_request_body": '{"messages": []}',
            "security_metadata": {"threat_detected": False},
        }

    @pytest.mark.asyncio
    async def test_scan_success(self, security_client, mock_security_response):
        """Test successful security scan."""
        # Mock HTTP client
        security_client.client.post = AsyncMock()
        security_client.client.post.return_value = MagicMock()
        security_client.client.post.return_value.json.return_value = mock_security_response
        security_client.client.post.return_value.raise_for_status.return_value = None

        # Execute scan
        result = await security_client.scan(
            request_body=b'{"messages": []}',
            headers={"Content-Type": "application/json"},
            trace_id="test-trace-123",
        )

        # Verify result
        assert result.verdict == "clean"
        assert result.score == 0.1
        assert result.redacted_fields == []
        assert result.masked_request_body == b'{"messages": []}'
        assert "threat_detected" in result.security_metadata

    @pytest.mark.asyncio
    async def test_scan_timeout_fail_open(self, security_client):
        """Test timeout behavior with fail-open."""
        # Mock timeout
        security_client.client.post = AsyncMock()
        security_client.client.post.side_effect = asyncio.TimeoutError()

        # Execute scan
        result = await security_client.scan(
            request_body=b'{"messages": []}',
            headers={"Content-Type": "application/json"},
        )

        # Verify fail-open result
        assert result.verdict == "clean"
        assert result.score == 0.0
        assert result.fail_reason == "security_timeout"

    @pytest.mark.asyncio
    async def test_scan_block_verdict(self, security_client):
        """Test block verdict from security service."""
        mock_response = {
            "verdict": "block",
            "score": 0.9,
            "redacted_fields": ["credit_card"],
            "masked_request_body": '{"messages": []}',
            "security_metadata": {"threat_type": "payment_card"},
        }

        security_client.client.post = AsyncMock()
        security_client.client.post.return_value = MagicMock()
        security_client.client.post.return_value.json.return_value = mock_response
        security_client.client.post.return_value.raise_for_status.return_value = None

        result = await security_client.scan(
            request_body=b'{"messages": []}',
            headers={"Content-Type": "application/json"},
        )

        assert result.verdict == "block"
        assert "credit_card" in result.redacted_fields


class TestSharedPipeline:
    """Test SharedPipeline integration."""

    @pytest.fixture
    def mock_sdk(self):
        """Create mock ObservabilitySDK."""
        sdk = MagicMock(spec=ObservabilitySDK)
        sdk._create_span = MagicMock()
        return sdk

    @pytest.fixture
    def mock_obs_pipeline(self):
        """Create mock ObsPipeline."""
        pipeline = MagicMock()
        pipeline.enrich_span = AsyncMock(return_value=MagicMock(spec=Span))
        return pipeline

    @pytest.fixture
    def shared_pipeline(self, mock_sdk, mock_obs_pipeline):
        """Create SharedPipeline for testing."""
        security_client = MagicMock(spec=SecurityClient)
        security_client.scan = AsyncMock(return_value=SecurityScanResult(
            verdict="clean",
            score=0.1,
            redacted_fields=[],
            masked_request_body=b'{"messages": []}',
            audit_event_id="audit-123",
            security_metadata={}
        ))

        return SharedPipeline(
            security_client=security_client,
            obs_pipeline=mock_obs_pipeline,
            sdk=mock_sdk,
        )

    @pytest.mark.asyncio
    async def test_process_clean_verdict(self, shared_pipeline):
        """Test processing with clean security verdict."""
        result = await shared_pipeline.process(
            request_body=b'{"messages": []}',
            headers={"Content-Type": "application/json"},
            trace_id="test-trace-123",
            request_id="test-request-123",
        )

        assert result.decision == "forward"
        assert result.security_result.verdict == "clean"
        assert result.masked_request_body == b'{"messages": []}'

    @pytest.mark.asyncio
    async def test_process_block_verdict(self, shared_pipeline):
        """Test processing with block security verdict."""
        shared_pipeline.security_client.scan.return_value = SecurityScanResult(
            verdict="block",
            score=0.9,
            redacted_fields=["pii"],
            masked_request_body=b'{"messages": []}',
            audit_event_id="audit-block-123",
            security_metadata={"threat_type": "pii"}
        )

        result = await shared_pipeline.process(
            request_body=b'{"messages": []}',
            headers={"Content-Type": "application/json"},
            trace_id="test-trace-123",
            request_id="test-request-123",
        )

        assert result.decision == "block"
        assert result.security_result.verdict == "block"


class TestDecisionRouter:
    """Test DecisionRouter priority logic."""

    @pytest.fixture
    def mock_audit_logger(self):
        """Create mock audit trail logger."""
        return MagicMock()

    @pytest.fixture
    def decision_router(self, mock_audit_logger):
        """Create DecisionRouter for testing."""
        return DecisionRouter(audit_logger=mock_audit_logger)

    @pytest.fixture
    def mock_security_result(self):
        """Create mock security scan result."""
        return SecurityScanResult(
            verdict="clean",
            score=0.1,
            redacted_fields=[],
            masked_request_body=b'{"messages": []}',
            audit_event_id="audit-123",
            security_metadata={}
        )

    @pytest.fixture
    def mock_span(self):
        """Create mock span."""
        span = MagicMock(spec=Span)
        span.attributes = {}
        span.context = MagicMock()
        return span

    @pytest.mark.asyncio
    async def test_security_priority_block(self, decision_router, mock_security_result, mock_span):
        """Test that security block decision takes priority."""
        mock_security_result.verdict = "block"
        
        decision = await decision_router.decide(
            security_result=mock_security_result,
            obs_span=mock_span,
            request_id="test-123",
            trace_id="trace-123",
        )

        assert decision.action == "block_response"
        assert decision.status_code == 403
        assert "blocked_by_security" in decision.body["error"]

    @pytest.mark.asyncio
    async def test_security_priority_flag(self, decision_router, mock_security_result, mock_span):
        """Test that security flag decision takes priority."""
        mock_security_result.verdict = "flag"
        
        decision = await decision_router.decide(
            security_result=mock_security_result,
            obs_span=mock_span,
            request_id="test-123",
            trace_id="trace-123",
        )

        assert decision.action == "forward_with_flag"
        assert decision.status_code == 200
        assert decision.span_attributes["security.flag"] is True

    @pytest.mark.asyncio
    async def test_security_priority_clean(self, decision_router, mock_security_result, mock_span):
        """Test that security clean decision allows forward."""
        decision = await decision_router.decide(
            security_result=mock_security_result,
            obs_span=mock_span,
            request_id="test-123",
            trace_id="trace-123",
        )

        assert decision.action == "forward"
        assert decision.status_code == 200
        assert decision.span_attributes.get("security.decision") == "clean"


class TestSharedAuditTrail:
    """Test SharedAuditTrail correlation and storage."""

    @pytest.fixture
    def audit_trail(self):
        """Create SharedAuditTrail for testing."""
        return SharedAuditTrail()

    @pytest.mark.asyncio
    async def test_write_and_retrieve(self, audit_trail):
        """Test writing and retrieving audit events."""
        # Write audit event
        audit_id = await audit_trail.write(
            request_id="req-123",
            trace_id="trace-123",
            security_audit_id="sec-123",
            obs_span_id="span-123",
            decision="forward",
            timestamp=1234567890.0,
            security_verdict="clean",
            security_score=0.1,
            redacted_fields_count=0,
        )

        # Retrieve by trace_id
        events = await audit_trail.get_by_trace("trace-123")
        
        assert len(events) == 1
        assert events[0].audit_id == audit_id
        assert events[0].decision == "forward"
        assert events[0].security_verdict == "clean"

    @pytest.mark.asyncio
    async def test_security_summary(self, audit_trail):
        """Test security summary statistics."""
        # Write multiple events
        await audit_trail.write(
            request_id="req-1", trace_id="trace-1", security_audit_id="sec-1",
            obs_span_id="span-1", decision="block", timestamp=1234567890.0,
            security_verdict="block", security_score=0.9, redacted_fields_count=1
        )
        await audit_trail.write(
            request_id="req-2", trace_id="trace-2", security_audit_id="sec-2",
            obs_span_id="span-2", decision="forward", timestamp=1234567891.0,
            security_verdict="clean", security_score=0.1, redacted_fields_count=0
        )

        # Get summary
        summary = await audit_trail.get_security_summary()
        
        assert summary["total_events"] == 2
        assert summary["block_count"] == 1
        assert summary["forward_count"] == 1
        assert summary["block_rate"] == 0.5


class TestLLMProxyServerSharedMode:
    """Test LLMProxyServer with shared proxy mode."""

    @pytest.fixture
    def shared_config(self):
        """Create shared proxy configuration."""
        return ProxyConfig(
            mode="on",
            enabled=True,
            shared_proxy_enabled=True,
            shared_proxy_security_url="http://orya-security:8000",
            shared_proxy_security_timeout=0.05,
        )

    @pytest.fixture
    def mock_sdk(self):
        """Create mock ObservabilitySDK."""
        sdk = MagicMock(spec=ObservabilitySDK)
        sdk._audit_trail = MagicMock()
        sdk._create_span = MagicMock()
        return sdk

    @pytest.fixture
    def proxy_server(self, shared_config, mock_sdk):
        """Create LLMProxyServer with shared mode."""
        with patch('agent_obs.proxy.server.httpx.AsyncClient'):
            server = LLMProxyServer(config=shared_config, sdk=mock_sdk)
            return server

    @pytest.mark.asyncio
    async def test_shared_mode_initialization(self, proxy_server):
        """Test that shared mode components are initialized."""
        assert proxy_server.shared_enabled is True
        assert proxy_server.shared_security_url == "http://orya-security:8000"
        assert proxy_server.security_client is not None
        assert proxy_server.shared_pipeline is not None
        assert proxy_server.decision_router is not None

    @pytest.mark.asyncio
    async def test_shared_mode_stop(self, proxy_server):
        """Test that shared mode resources are cleaned up."""
        # Mock shared pipeline close
        proxy_server.shared_pipeline.close = AsyncMock()
        
        # Stop server
        await proxy_server.stop()
        
        # Verify shared pipeline was closed
        proxy_server.shared_pipeline.close.assert_called_once()


class TestIntegrationScenarios:
    """Integration tests for end-to-end scenarios."""

    @pytest.mark.asyncio
    async def test_end_to_end_clean_flow(self):
        """Test complete flow with clean security verdict."""
        # Mock all components
        with patch('agent_obs.proxy.security_client.httpx.AsyncClient'):
            # Create components
            security_client = SecurityClient("http://orya-security:8000")
            mock_sdk = MagicMock()
            mock_obs_pipeline = MagicMock()
            mock_obs_pipeline.enrich_span = AsyncMock(return_value=MagicMock(spec=Span))
            
            # Mock security scan result
            security_client.scan = AsyncMock(return_value=SecurityScanResult(
                verdict="clean",
                score=0.1,
                redacted_fields=[],
                masked_request_body=b'{"messages": []}',
                audit_event_id="audit-123",
                security_metadata={}
            ))
            
            # Create shared pipeline
            shared_pipeline = SharedPipeline(
                security_client=security_client,
                obs_pipeline=mock_obs_pipeline,
                sdk=mock_sdk,
            )
            
            # Process request
            result = await shared_pipeline.process(
                request_body=b'{"messages": []}',
                headers={"Content-Type": "application/json"},
                trace_id="trace-123",
                request_id="req-123",
            )
            
            # Verify end-to-end result
            assert result.decision == "forward"
            assert result.security_result.verdict == "clean"
            assert result.masked_request_body == b'{"messages": []}'

    @pytest.mark.asyncio
    async def test_end_to_end_block_flow(self):
        """Test complete flow with block security verdict."""
        with patch('agent_obs.proxy.security_client.httpx.AsyncClient'):
            # Create components
            security_client = SecurityClient("http://orya-security:8000")
            mock_sdk = MagicMock()
            mock_obs_pipeline = MagicMock()
            mock_obs_pipeline.enrich_span = AsyncMock(return_value=MagicMock(spec=Span))
            
            # Mock block verdict
            security_client.scan = AsyncMock(return_value=SecurityScanResult(
                verdict="block",
                score=0.9,
                redacted_fields=["pii"],
                masked_request_body=b'{"messages": []}',
                audit_event_id="audit-block-123",
                security_metadata={"threat_type": "pii"}
            ))
            
            # Create decision router
            audit_logger = MagicMock()
            decision_router = DecisionRouter(audit_logger=audit_logger)
            
            # Create mock span
            mock_span = MagicMock(spec=Span)
            mock_span.attributes = {}
            
            # Make decision
            decision = await decision_router.decide(
                security_result=security_client.scan.return_value,
                obs_span=mock_span,
                request_id="req-123",
                trace_id="trace-123",
            )
            
            # Verify block decision
            assert decision.action == "block_response"
            assert decision.status_code == 403
            assert "blocked_by_security" in decision.body["error"]