"""Unit tests for PP58 PII encryption functionality."""

import base64
import json
import os
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from agent_obs.guardrail.vault_client import VaultClient
from agent_obs.metrics import pii_encrypted_total, pii_vault_transit_failures_total
from agent_obs.observability import Span, SpanContext
from agent_obs.proxy.obs_pipeline import ObsPipeline, Timing
from agent_obs.proxy import ProxyConfig


class TestPIIEncryption:
    """Test PII encryption functionality in ObsPipeline."""

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
        
        # Create ObsPipeline with mocked dependencies
        self.pipeline = ObsPipeline(
            config=self.config,
            vault_client=self.vault_client,
        )
        
        # Mock span
        self.span = Span(
            name="test_span",
            span_type="llm.proxy.call",
            context=SpanContext(
                trace_id="test_trace_id",
                span_id="test_span_id",
                tenant_id="test_tenant",
            ),
        )

    @pytest.mark.asyncio
    async def test_detect_and_encrypt_pii_with_email(self):
        """Test PII detection and encryption with email."""
        text = "Hello, my email is test@example.com and my phone is 123-456-7890."
        
        encrypted_text, encrypted_fields = await self.pipeline._detect_and_encrypt_pii(
            text, "test_tenant"
        )
        
        # Debug: print detected fields
        print("Detected fields:", encrypted_fields)
        print("Encrypted text:", encrypted_text)
        
        # Verify PII was detected
        assert len(encrypted_fields) == 2
        assert any(field["field"] == "email.19" for field in encrypted_fields)
        assert any(field["field"] == "phone.52" for field in encrypted_fields)
        
        # Verify encryption was called
        self.vault_client.secrets.transit.encrypt_data.assert_called()
        
        # Verify text was replaced with encrypted markers
        assert "[ENCRYPTED:email:16]" in encrypted_text
        assert "[ENCRYPTED:phone:12]" in encrypted_text

    @pytest.mark.asyncio
    async def test_detect_and_encrypt_pii_no_pii(self):
        """Test PII detection with no PII found."""
        text = "Hello, this is a normal message without any PII."
        
        encrypted_text, encrypted_fields = await self.pipeline._detect_and_encrypt_pii(
            text, "test_tenant"
        )
        
        # Verify no PII was detected
        assert len(encrypted_fields) == 0
        assert encrypted_text == text

    @pytest.mark.asyncio
    async def test_detect_and_encrypt_pii_disabled(self):
        """Test PII encryption when disabled."""
        self.pipeline.pii_enabled = False
        text = "Hello, my email is test@example.com."
        
        encrypted_text, encrypted_fields = await self.pipeline._detect_and_encrypt_pii(
            text, "test_tenant"
        )
        
        # Verify no PII was processed
        assert len(encrypted_fields) == 0
        assert encrypted_text == text

    @pytest.mark.asyncio
    async def test_detect_and_encrypt_pii_no_tenant(self):
        """Test PII encryption when no tenant provided."""
        text = "Hello, my email is test@example.com."
        
        encrypted_text, encrypted_fields = await self.pipeline._detect_and_encrypt_pii(
            text, None
        )
        
        # Verify no PII was processed
        assert len(encrypted_fields) == 0
        assert encrypted_text == text

    @pytest.mark.asyncio
    async def test_encrypt_pii_value_success(self):
        """Test successful PII value encryption."""
        value = "test@example.com"
        tenant_id = "test_tenant"
        
        encrypted_value = await self.pipeline._encrypt_pii_value(value, tenant_id)
        
        # Verify encryption was called with correct parameters
        expected_key = "test_tenant_pii_field_key"
        expected_plaintext = base64.b64encode(value.encode()).decode()
        
        self.vault_client.secrets.transit.encrypt_data.assert_called_once_with(
            key_name=expected_key,
            plaintext=expected_plaintext
        )
        
        # Verify encrypted value
        assert encrypted_value == "vault:encrypted:data"

    @pytest.mark.asyncio
    async def test_encrypt_pii_value_vault_failure(self):
        """Test PII value encryption when Vault fails."""
        self.vault_client.secrets.transit.encrypt_data.side_effect = Exception("Vault error")
        
        value = "test@example.com"
        tenant_id = "test_tenant"
        
        encrypted_value = await self.pipeline._encrypt_pii_value(value, tenant_id)
        
        # Verify fallback to base64 encoding
        assert encrypted_value == base64.b64encode(value.encode()).decode()

    @pytest.mark.asyncio
    async def test_encrypt_pii_value_no_vault_client(self):
        """Test PII value encryption when no vault client available."""
        self.pipeline.vault_client = None
        
        value = "test@example.com"
        tenant_id = "test_tenant"
        
        encrypted_value = await self.pipeline._encrypt_pii_value(value, tenant_id)
        
        # Verify fallback to base64 encoding
        assert encrypted_value == base64.b64encode(value.encode()).decode()

    def test_replace_with_encrypted_markers(self):
        """Test replacement of PII with encrypted markers."""
        text = "Email: test@example.com, Phone: 123-456-7890"
        pii_matches = [
            type('PIIMatch', (), {
                'entity_type': 'email',
                'value': 'test@example.com',
                'span_start': 7,
                'span_end': 22,
                'mask': '[EMAIL]'
            })(),
            type('PIIMatch', (), {
                'entity_type': 'phone',
                'value': '123-456-7890',
                'span_start': 25,
                'span_end': 37,
                'mask': '[PHONE]'
            })()
        ]
        
        result = self.pipeline._replace_with_encrypted_markers(text, pii_matches)
        
        # Verify PII was replaced with markers
        assert "[ENCRYPTED:email:16]" in result
        assert "[ENCRYPTED:phone:12]" in result
        assert "test@example.com" not in result
        assert "123-456-7890" not in result

    @pytest.mark.asyncio
    async def test_enrich_span_with_pii(self):
        """Test span enrichment with PII encryption."""
        request_body = json.dumps({
            "messages": [
                {"role": "user", "content": "My email is test@example.com"},
                {"role": "assistant", "content": "Hello user"}
            ]
}).encode()
        
        response_body = json.dumps({
            "choices": [{"message": {"content": "My phone is 123-456-7890"}}]
        }).encode()
        
        timing = Timing(
            t_request_received=1.0,
            t_upstream_started=1.1,
            t_upstream_completed=1.2,
        )
        
        headers = {
            "X-OryaObs-Tenant": "test_tenant",
            "X-OryaObs-Agent": "test_agent",
            "X-OryaObs-User": "test_user",
        }
        
        enriched_span = await self.pipeline.enrich_span(
            span=self.span,
            request_body=request_body,
            response_body=response_body,
            stream_chunks=None,
            headers=headers,
            timing=timing,
        )
        
        # Verify PII encryption was applied
        assert "pii.encrypted_fields" in enriched_span.attributes
        assert enriched_span.attributes["pii.encryption_enabled"] is True
        assert len(enriched_span.attributes["pii.encrypted_fields"]) > 0
        
        # Verify vault client was called
        self.vault_client.secrets.transit.encrypt_data.assert_called()

    @pytest.mark.asyncio
    async def test_enrich_span_pii_disabled(self):
        """Test span enrichment with PII encryption disabled."""
        self.pipeline.pii_enabled = False
         
        request_body = json.dumps({
            "messages": [{"role": "user", "content": "My email is test@example.com"}]
        }).encode()
         
        timing = Timing(
            t_request_received=1.0,
            t_upstream_started=1.1,
            t_upstream_completed=1.2,
        )
        
        headers = {
            "X-OryaObs-Tenant": "test_tenant",
            "X-OryaObs-Agent": "test_agent",
            "X-OryaObs-User": "test_user",
        }
        
        enriched_span = await self.pipeline.enrich_span(
            span=self.span,
            request_body=request_body,
            response_body=None,
            stream_chunks=None,
            headers=headers,
            timing=timing,
        )
        
        # Verify PII encryption was not applied
        assert "pii.encrypted_fields" not in enriched_span.attributes
        assert enriched_span.attributes.get("pii.encryption_enabled") is not True

    def test_metrics_counters(self):
        """Test PII encryption metrics counters."""
        # Test that metrics are properly initialized
        assert hasattr(pii_encrypted_total, 'inc')
        assert hasattr(pii_vault_transit_failures_total, 'inc')
        
        # Test incrementing counters (with labels for Counter)
        try:
            pii_encrypted_total.inc(5)
        except ValueError:
            # If labels are required, use them
            pii_encrypted_total.labels(entity_type="input", tenant_id="test").inc(5)
        try:
            pii_vault_transit_failures_total.inc(1)
        except ValueError:
            # If labels are required, use them
            pii_vault_transit_failures_total.labels(tenant_id="test", error_type="vault_error").inc(1)

    def test_environment_configuration(self):
        """Test environment variable configuration."""
        # Test default behavior
        os.environ["PII_E2E_ENCRYPTION"] = "true"
        pipeline = ObsPipeline(config=self.config)
        assert pipeline.pii_enabled is True
        
        # Test explicit enable
        os.environ["PII_E2E_ENCRYPTION"] = "1"
        pipeline = ObsPipeline(config=self.config)
        assert pipeline.pii_enabled is True
        
        # Test disable
        os.environ["PII_E2E_ENCRYPTION"] = "false"
        pipeline = ObsPipeline(config=self.config)
        assert pipeline.pii_enabled is False
        
        # Test explicit disable
        os.environ["PII_E2E_ENCRYPTION"] = "0"
        pipeline = ObsPipeline(config=self.config)
        assert pipeline.pii_enabled is False
        
        # Clean up
        del os.environ["PII_E2E_ENCRYPTION"]


class TestPIIEncryptionIntegration:
    """Integration tests for PII encryption functionality."""

    @pytest.mark.asyncio
    async def test_full_pii_encryption_flow(self):
        """Test complete PII encryption flow."""
        config = ProxyConfig(
            upstream_provider="openai",
            upstream_base_url="https://api.openai.com/v1",
            upstream_api_key_env="OPENAI_API_KEY",
            log_bodies=True,
            log_bodies_max_bytes=10000,
        )
        
        # Mock vault client
        vault_client = MagicMock(spec=VaultClient)
        vault_client.secrets = MagicMock()
        vault_client.secrets.transit = MagicMock()
        vault_client.secrets.transit.encrypt_data.return_value = {
            "data": {"ciphertext": "vault:encrypted:data"}
        }
        
        pipeline = ObsPipeline(config=config, vault_client=vault_client)
        
        # Test data with PII
        request_body = json.dumps({
            "messages": [
                {"role": "user", "content": "My email is test@example.com and phone is 123-456-7890"},
                {"role": "assistant", "content": "Hello user, your email is test@example.com"}
            ]
        }).encode()
        
        response_body = json.dumps({
            "choices": [{"message": {"content": "Hello user, your phone is 123-456-7890"}}]
        }).encode()
        
        timing = Timing(
            t_request_received=1.0,
            t_upstream_started=1.1,
            t_upstream_completed=1.2,
        )
        
        headers = {
            "X-OryaObs-Tenant": "test_tenant",
            "X-OryaObs-Agent": "test_agent",
            "X-OryaObs-User": "test_user",
        }
        
        span = Span(
            name="test_span",
            span_type="llm.call",
            context=SpanContext(
                trace_id="test_trace_id",
                span_id="test_span_id",
                tenant_id="test_tenant",
            ),
        )
        
        # Perform enrichment
        enriched_span = await pipeline.enrich_span(
            span=span,
            request_body=request_body,
            response_body=response_body,
            stream_chunks=None,
            headers=headers,
            timing=timing,
        )
        
        # Verify results
        assert "pii.encrypted_fields" in enriched_span.attributes
        assert enriched_span.attributes["pii.encryption_enabled"] is True
        assert len(enriched_span.attributes["pii.encrypted_fields"]) >= 2
        
        # Verify vault was called for both request and response
        assert vault_client.secrets.transit.encrypt_data.call_count >= 2
        
        # Verify that original PII is not in the span attributes
        span_dict = enriched_span.to_dict()
        span_text = json.dumps(span_dict)
        assert "test@example.com" not in span_text
        assert "123-456-7890" not in span_text


if __name__ == "__main__":
    pytest.main([__file__, "-v"])