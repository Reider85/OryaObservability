"""Integration tests for PP58 PII encryption in SharedPipeline."""

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from agent_obs.guardrail.vault_client import VaultClient
from agent_obs.observability import ObservabilitySDK, Span, SpanContext, SpanType
from agent_obs.proxy.obs_pipeline import ObsPipeline
from agent_obs.proxy.security_client import SecurityClient, SecurityScanResult
from agent_obs.proxy.shared_pipeline import SharedPipeline, SharedPipelineResult
from agent_obs.proxy import ProxyConfig


class TestSharedPipelinePIIEncryption:
    """Test PII encryption functionality in SharedPipeline."""

    def setup_method(self):
        """Set up test fixtures."""
        self.config = ProxyConfig(
            upstream_provider="openai",
            upstream_base_url="https://api.openai.com/v1",
            upstream_api_key_env="OPENAI_API_KEY",
            log_bodies=True,
            log_bodies_max_bytes=10000,
        )
        
        # Mock vault client
        self.vault_client = MagicMock(spec=VaultClient)
        self.vault_client.secrets = MagicMock()
        self.vault_client.secrets.transit = MagicMock()
        self.vault_client.secrets.transit.encrypt_data.return_value = {
            "data": {"ciphertext": "vault:encrypted:data"}
        }
        
        # Mock security client
        self.security_client = MagicMock(spec=SecurityClient)
        self.security_client.pii_enabled = True
        
        # Mock obs pipeline
        self.obs_pipeline = ObsPipeline(
            config=self.config,
            vault_client=self.vault_client,
        )
        self.obs_pipeline.pii_enabled = True
        
        # Mock SDK
        self.sdk = MagicMock(spec=ObservabilitySDK)
        
        # Create SharedPipeline
        self.shared_pipeline = SharedPipeline(
            security_client=self.security_client,
            obs_pipeline=self.obs_pipeline,
            sdk=self.sdk,
        )
        
        # Test data
        self.request_body = json.dumps({
            "messages": [
                {"role": "user", "content": "My email is test@example.com"},
                {"role": "assistant", "content": "Hello user"}
            ]
        }).encode()
        
        self.headers = {
            "X-OryaObs-Tenant": "test_tenant",
            "X-OryaObs-Agent": "test_agent",
            "X-OryaObs-User": "test_user",
        }

    @pytest.mark.asyncio
    async def test_process_with_pii_encryption(self):
        """Test SharedPipeline processing with PII encryption."""
        # Mock security scan result
        security_result = SecurityScanResult(
            verdict="clean",
            score=0.9,
            redacted_fields=[],
            masked_request_body=self.request_body,
            audit_event_id="test_audit_id",
            security_metadata={},
            fail_reason=None
        )
    
        # Mock obs span
        obs_span = Span(
            name="test_span",
            span_type=SpanType.LLM_CALL,
            context=SpanContext(
                trace_id="test_trace_id",
                span_id="test_span_id",
                tenant_id="test_tenant",
            ),
        )
        obs_span.attributes["pii.encrypted_fields"] = [{"field": "email.test", "encrypted_value": "encrypted"}]
        obs_span.attributes["pii.encryption_enabled"] = True
        
        # Mock security client
        self.security_client.scan.return_value = security_result
        
        # Mock obs pipeline
        self.obs_pipeline.enrich_span = AsyncMock(return_value=obs_span)
        
        # Process request
        result = await self.shared_pipeline.process(
            request_body=self.request_body,
            headers=self.headers,
            trace_id="test_trace_id",
            request_id="test_request_id",
        )
        
        # Verify result
        assert isinstance(result, SharedPipelineResult)
        assert result.pii_encryption_enabled is True
        assert len(result.pii_encrypted_fields) > 0
        
        # Verify security client was called
        self.security_client.scan.assert_called_once()
        
        # Verify obs pipeline was called
        self.obs_pipeline.enrich_span.assert_called_once()

    @pytest.mark.asyncio
    async def test_process_with_pii_encryption_disabled(self):
        """Test SharedPipeline processing with PII encryption disabled."""
        # Disable PII encryption
        self.obs_pipeline.pii_enabled = False
        
        # Mock security scan result
        security_result = SecurityScanResult(
            verdict="clean",
            score=0.9,
            redacted_fields=[],
            masked_request_body=self.request_body,
            audit_event_id="test_audit_id",
            security_metadata={},
            fail_reason=None
        )
        
        # Mock obs span
        obs_span = Span(
            name="test_span",
            span_type=SpanType.LLM_CALL,
            context=SpanContext(
                trace_id="test_trace_id",
                span_id="test_span_id",
                tenant_id="test_tenant",
            ),
        )
        
        # Mock security client
        self.security_client.scan.return_value = security_result
        
        # Mock obs pipeline
        self.obs_pipeline.enrich_span = AsyncMock(return_value=obs_span)
        
        # Process request
        result = await self.shared_pipeline.process(
            request_body=self.request_body,
            headers=self.headers,
            trace_id="test_trace_id",
            request_id="test_request_id",
        )
        
        # Verify result
        assert isinstance(result, SharedPipelineResult)
        assert result.pii_encryption_enabled is False
        assert len(result.pii_encrypted_fields) == 0
        
        # Verify security client was called
        self.security_client.scan.assert_called_once()
        
        # Verify obs pipeline was called
        self.obs_pipeline.enrich_span.assert_called_once()

    @pytest.mark.asyncio
    async def test_process_with_pii_encryption_error(self):
        """Test SharedPipeline processing with PII encryption error."""
        # Mock PII encryption to raise an error
        self.obs_pipeline._detect_and_encrypt_pii = AsyncMock(side_effect=Exception("PII encryption error"))
        
        # Mock security scan result
        security_result = SecurityScanResult(
            verdict="clean",
            score=0.9,
            redacted_fields=[],
            masked_request_body=self.request_body,
            audit_event_id="test_audit_id",
            security_metadata={},
            fail_reason=None
        )
        
        # Mock obs span
        obs_span = Span(
            name="test_span",
            span_type=SpanType.LLM_CALL,
            context=SpanContext(
                trace_id="test_trace_id",
                span_id="test_span_id",
                tenant_id="test_tenant",
            ),
        )
        
        # Mock security client
        self.security_client.scan.return_value = security_result
        
        # Mock obs pipeline
        self.obs_pipeline.enrich_span = AsyncMock(return_value=obs_span)
        
        # Process request
        result = await self.shared_pipeline.process(
            request_body=self.request_body,
            headers=self.headers,
            trace_id="test_trace_id",
            request_id="test_request_id",
        )
        
        # Verify result - should continue without PII encryption
        assert isinstance(result, SharedPipelineResult)
        assert result.pii_encryption_enabled is False
        assert len(result.pii_encrypted_fields) == 0
        
        # Verify security client was called with original request body
        self.security_client.scan.assert_called_once_with(self.request_body, self.headers, "test_trace_id")
        
        # Verify obs pipeline was called
        self.obs_pipeline.enrich_span.assert_called_once()

    @pytest.mark.asyncio
    async def test_process_with_security_block_and_pii(self):
        """Test SharedPipeline processing with security block and PII encryption."""
        # Mock security scan result with block
        security_result = SecurityScanResult(
            verdict="block",
            score=0.9,
            redacted_fields=[],
            masked_request_body=self.request_body,
            audit_event_id="test_audit_id",
            security_metadata={},
            fail_reason=None
        )
        
        # Mock obs span
        obs_span = Span(
            name="test_span",
            span_type=SpanType.LLM_CALL,
            context=SpanContext(
                trace_id="test_trace_id",
                span_id="test_span_id",
                tenant_id="test_tenant",
            ),
        )
        obs_span.attributes["pii.encrypted_fields"] = [{"field": "email.test", "encrypted_value": "encrypted"}]
        obs_span.attributes["pii.encryption_enabled"] = True
        
        # Mock security client
        self.security_client.scan.return_value = security_result
        
        # Mock obs pipeline
        self.obs_pipeline.enrich_span = AsyncMock(return_value=obs_span)
        
        # Process request
        result = await self.shared_pipeline.process(
            request_body=self.request_body,
            headers=self.headers,
            trace_id="test_trace_id",
            request_id="test_request_id",
        )
        
        # Verify result
        assert isinstance(result, SharedPipelineResult)
        assert result.decision == "block"  # Security block takes priority
        assert result.pii_encryption_enabled is True
        assert len(result.pii_encrypted_fields) > 0
        
        # Verify security client was called
        self.security_client.scan.assert_called_once()
        
        # Verify obs pipeline was called
        self.obs_pipeline.enrich_span.assert_called_once()

    @pytest.mark.asyncio
    async def test_process_with_security_flag_and_pii(self):
        """Test SharedPipeline processing with security flag and PII encryption."""
        # Mock security scan result with flag
        security_result = SecurityScanResult(
            verdict="flag",
            score=0.9,
            redacted_fields=[],
            masked_request_body=self.request_body,
            audit_event_id="test_audit_id",
            security_metadata={},
            fail_reason=None
        )
        
        # Mock obs span
        obs_span = Span(
            name="test_span",
            span_type=SpanType.LLM_CALL,
            context=SpanContext(
                trace_id="test_trace_id",
                span_id="test_span_id",
                tenant_id="test_tenant",
            ),
        )
        obs_span.attributes["pii.encrypted_fields"] = [{"field": "email.test", "encrypted_value": "encrypted"}]
        obs_span.attributes["pii.encryption_enabled"] = True
        
        # Mock security client
        self.security_client.scan.return_value = security_result
        
        # Mock obs pipeline
        self.obs_pipeline.enrich_span = AsyncMock(return_value=obs_span)
        
        # Process request
        result = await self.shared_pipeline.process(
            request_body=self.request_body,
            headers=self.headers,
            trace_id="test_trace_id",
            request_id="test_request_id",
        )
        
        # Verify result
        assert isinstance(result, SharedPipelineResult)
        assert result.decision == "flag"  # Security flag takes priority
        assert result.pii_encryption_enabled is True
        assert len(result.pii_encrypted_fields) > 0
        
        # Verify security client was called
        self.security_client.scan.assert_called_once()
        
        # Verify obs pipeline was called
        self.obs_pipeline.enrich_span.assert_called_once()

    @pytest.mark.asyncio
    async def test_process_with_no_tenant(self):
        """Test SharedPipeline processing with no tenant ID."""
        # Headers without tenant
        headers = {
            "X-OryaObs-Agent": "test_agent",
            "X-OryaObs-User": "test_user",
        }
        
        # Mock security scan result
        security_result = SecurityScanResult(
            verdict="clean",
            score=0.9,
            redacted_fields=[],
            masked_request_body=self.request_body,
            audit_event_id="test_audit_id",
            security_metadata={},
            fail_reason=None
        )
        
        # Mock obs span
        obs_span = Span(
            name="test_span",
            span_type=SpanType.LLM_CALL,
            context=SpanContext(
                trace_id="test_trace_id",
                span_id="test_span_id",
                tenant_id=None,  # No tenant
            ),
        )
        
        # Mock security client
        self.security_client.scan.return_value = security_result
        
        # Mock obs pipeline
        self.obs_pipeline.enrich_span = AsyncMock(return_value=obs_span)
        
        # Process request
        result = await self.shared_pipeline.process(
            request_body=self.request_body,
            headers=headers,
            trace_id="test_trace_id",
            request_id="test_request_id",
        )
        
        # Verify result
        assert isinstance(result, SharedPipelineResult)
        assert result.pii_encryption_enabled is False
        assert len(result.pii_encrypted_fields) == 0
        
        # Verify security client was called with original request body
        self.security_client.scan.assert_called_once_with(self.request_body, headers, "test_trace_id")
        
        # Verify obs pipeline was called
        self.obs_pipeline.enrich_span.assert_called_once()

    @pytest.mark.asyncio
    async def test_process_with_vault_failure(self):
        """Test SharedPipeline processing with Vault encryption failure."""
        # Mock vault client to fail
        self.vault_client.secrets.transit.encrypt_data.side_effect = Exception("Vault error")
        
        # Mock security scan result
        security_result = SecurityScanResult(
            verdict="clean",
            score=0.9,
            redacted_fields=[],
            masked_request_body=self.request_body,
            audit_event_id="test_audit_id",
            security_metadata={},
            fail_reason=None
        )
        
        # Mock obs span
        obs_span = Span(
            name="test_span",
            span_type=SpanType.LLM_CALL,
            context=SpanContext(
                trace_id="test_trace_id",
                span_id="test_span_id",
                tenant_id="test_tenant",
            ),
        )
        
        # Mock security client
        self.security_client.scan.return_value = security_result
        
        # Mock obs pipeline
        self.obs_pipeline.enrich_span = AsyncMock(return_value=obs_span)
        
        # Process request
        result = await self.shared_pipeline.process(
            request_body=self.request_body,
            headers=self.headers,
            trace_id="test_trace_id",
            request_id="test_request_id",
        )
        
        # Verify result - should continue with fallback
        assert isinstance(result, SharedPipelineResult)
        assert result.pii_encryption_enabled is False
        assert len(result.pii_encrypted_fields) == 0
        
        # Verify security client was called
        self.security_client.scan.assert_called_once()
        
        # Verify obs pipeline was called
        self.obs_pipeline.enrich_span.assert_called_once()

    def test_shared_pipeline_result_structure(self):
        """Test SharedPipelineResult structure with PII fields."""
        # Test that SharedPipelineResult has PII fields
        result = SharedPipelineResult(
            masked_request_body=b"test",
            security_result=SecurityScanResult(
                verdict="clean",
                score=0.9,
                redacted_fields=[],
                masked_request_body=b"test",
                audit_event_id="test_audit_id",
                security_metadata={},
                fail_reason=None
            ),
            obs_span=Span(
                name="test_span",
                span_type=SpanType.LLM_CALL,
                context=SpanContext(
                    trace_id="test_trace_id",
                    span_id="test_span_id",
                    tenant_id="test_tenant",
                ),
            ),
            decision="forward",
            latency_seconds=0.1,
            pii_encrypted_fields=[{"field": "email.test", "encrypted_value": "encrypted"}],
            pii_encryption_enabled=True,
        )
        
        # Verify structure
        assert result.pii_encrypted_fields == [{"field": "email.test", "encrypted_value": "encrypted"}]
        assert result.pii_encryption_enabled is True


if __name__ == "__main__":
    pytest.main([__file__, "-v"])