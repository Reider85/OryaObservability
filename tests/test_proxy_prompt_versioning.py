"""Tests for prompt template versioning (PP53)."""

from __future__ import annotations

import json
import pytest
from datetime import datetime, UTC
from unittest.mock import AsyncMock, MagicMock, patch

from agent_obs.proxy.prompt_templates import (
    extract_first_system_prompt,
    extract_version_label,
    compute_prompt_sha256,
    PromptTemplateManager,
    PromptTemplateEnricher,
    create_prompt_enricher,
)
from agent_obs.observability import Span, SpanContext
from agent_obs.storage.hot import HotStore


class TestPromptExtraction:
    """Test system prompt extraction and processing."""
    
    def test_extract_first_system_prompt_with_system_message(self):
        """Test extraction of system prompt from request body."""
        request_body = json.dumps({
            "messages": [
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": "Hello!"}
            ]
        }).encode()
        
        result = extract_first_system_prompt(request_body)
        assert result == "You are a helpful assistant."
    
    def test_extract_first_system_prompt_no_system_message(self):
        """Test extraction when no system message is present."""
        request_body = json.dumps({
            "messages": [
                {"role": "user", "content": "Hello!"}
            ]
        }).encode()
        
        result = extract_first_system_prompt(request_body)
        assert result is None
    
    def test_extract_first_system_prompt_empty_messages(self):
        """Test extraction with empty messages array."""
        request_body = json.dumps({"messages": []}).encode()
        
        result = extract_first_system_prompt(request_body)
        assert result is None
    
    def test_extract_first_system_prompt_invalid_json(self):
        """Test extraction with invalid JSON."""
        request_body = b"invalid json"
        
        result = extract_first_system_prompt(request_body)
        assert result is None
    
    def test_extract_version_label_version_pattern(self):
        """Test version label extraction with VERSION: pattern."""
        prompt = "VERSION: 1.2.3\nYou are a helpful assistant."
        result = extract_version_label(prompt)
        assert result == "1.2.3"
    
    def test_extract_version_label_comment_pattern(self):
        """Test version label extraction with comment pattern."""
        prompt = "# Version 2.1.0\nYou are a helpful assistant."
        result = extract_version_label(prompt)
        assert result == "2.1.0"
    
    def test_extract_version_label_no_version(self):
        """Test version label extraction when no version is present."""
        prompt = "You are a helpful assistant."
        result = extract_version_label(prompt)
        assert result is None
    
    def test_compute_prompt_sha256(self):
        """Test SHA256 computation."""
        prompt = "You are a helpful assistant."
        result = compute_prompt_sha256(prompt)
        
        # SHA256 should be 64 characters long
        assert len(result) == 64
        
        # Same prompt should produce same hash
        result2 = compute_prompt_sha256(prompt)
        assert result == result2
        
        # Different prompt should produce different hash
        result3 = compute_prompt_sha256("Different prompt")
        assert result != result3


class TestPromptTemplateManager:
    """Test PromptTemplateManager functionality."""
    
    @pytest.fixture
    def mock_hot_store(self):
        """Create a mock HotStore for testing."""
        hot_store = MagicMock(spec=HotStore)
        hot_store.execute = AsyncMock()
        return hot_store
    
    @pytest.fixture
    def prompt_manager(self, mock_hot_store):
        """Create a PromptTemplateManager for testing."""
        manager = PromptTemplateManager(mock_hot_store, content_rate=20)
        return manager
    
    @pytest.mark.asyncio
    async def test_store_prompt_template_success(self, prompt_manager, mock_hot_store):
        """Test successful storage of prompt template."""
        trace_id = "test-trace-123"
        prompt_text = "You are a helpful assistant."
        prompt_sha256 = compute_prompt_sha256(prompt_text)
        version_label = "1.2.3"
        
        result = await prompt_manager.store_prompt_template(
            trace_id=trace_id,
            prompt_text=prompt_text,
            prompt_sha256=prompt_sha256,
            version_label=version_label,
        )
        
        assert result is True
        mock_hot_store.execute.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_store_prompt_template_not_sampled(self, prompt_manager, mock_hot_store):
        """Test storage when trace is not sampled."""
        # Mock the sampling to return False
        with patch('agent_obs.content_sampling.should_keep_content', return_value=False):
            trace_id = "test-trace-123"
            prompt_text = "You are a helpful assistant."
            prompt_sha256 = compute_prompt_sha256(prompt_text)
            
            result = await prompt_manager.store_prompt_template(
                trace_id=trace_id,
                prompt_text=prompt_text,
                prompt_sha256=prompt_sha256,
            )
            
            assert result is False
            mock_hot_store.execute.assert_not_called()
    
    @pytest.mark.asyncio
    async def test_store_prompt_template_failure(self, prompt_manager, mock_hot_store):
        """Test storage when ClickHouse operation fails."""
        # Mock the storage to fail
        mock_hot_store.execute.side_effect = Exception("Storage failed")
        
        trace_id = "test-trace-123"
        prompt_text = "You are a helpful assistant."
        prompt_sha256 = compute_prompt_sha256(prompt_text)
        
        result = await prompt_manager.store_prompt_template(
            trace_id=trace_id,
            prompt_text=prompt_text,
            prompt_sha256=prompt_sha256,
        )
        
        assert result is False
        mock_hot_store.execute.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_get_prompt_templates(self, prompt_manager, mock_hot_store):
        """Test retrieval of prompt templates."""
        expected_results = [
            {
                "sha256": "abc123",
                "version_label": "1.2.3",
                "first_seen": datetime.now(UTC),
                "last_seen": datetime.now(UTC),
                "sample_text": "Sample text",
            }
        ]
        
        mock_hot_store.execute.return_value = expected_results
        
        results = await prompt_manager.get_prompt_templates(sha256="abc123")
        
        assert results == expected_results
        mock_hot_store.execute.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_get_traces_with_prompt(self, prompt_manager, mock_hot_store):
        """Test retrieval of traces with specific prompt."""
        expected_results = [
            {
                "trace_id": "test-trace-123",
                "span_id": "test-span-123",
                "agent_id": "test-agent",
                "tenant_id": "test-tenant",
                "start_time": datetime.now(UTC),
            }
        ]
        
        mock_hot_store.execute.return_value = expected_results
        
        results = await prompt_manager.get_traces_with_prompt("abc123")
        
        assert results == expected_results
        mock_hot_store.execute.assert_called_once()


class TestPromptTemplateEnricher:
    """Test PromptTemplateEnricher functionality."""
    
    @pytest.fixture
    def mock_hot_store(self):
        """Create a mock HotStore for testing."""
        hot_store = MagicMock(spec=HotStore)
        hot_store.execute = AsyncMock()
        return hot_store
    
    @pytest.fixture
    def prompt_manager(self, mock_hot_store):
        """Create a PromptTemplateManager for testing."""
        manager = MagicMock(spec=PromptTemplateManager)
        manager.hot_store = mock_hot_store
        manager.content_rate = 20
        manager.store_prompt_template = AsyncMock()
        return manager
    
    @pytest.fixture
    def prompt_enricher(self, prompt_manager):
        """Create a PromptTemplateEnricher for testing."""
        return PromptTemplateEnricher(prompt_manager)
    
    @pytest.fixture
    def sample_span(self):
        """Create a sample span for testing."""
        span = Span(
            name="test-span",
            span_type="llm.call",
            context=SpanContext.new(
                agent_id="test-agent",
                user_id="test-user",
                tenant_id="test-tenant",
            ),
        )
        return span
    
    @pytest.mark.asyncio
    async def test_enrich_span_with_prompt(self, prompt_enricher, sample_span):
        """Test span enrichment with system prompt."""
        request_body = json.dumps({
            "messages": [
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": "Hello!"}
            ]
        }).encode()
        
        enriched_span = await prompt_enricher.enrich_span_with_prompt(
            span=sample_span,
            request_body=request_body,
            trace_id="test-trace-123",
        )
        
        # Check that span attributes were updated
        assert "llm.system_prompt_sha256" in enriched_span.attributes
        assert "llm.system_prompt_version" not in enriched_span.attributes  # No version label
        
        # Check that the prompt manager was called
        prompt_enricher.prompt_manager.store_prompt_template.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_enrich_span_with_prompt_and_version(self, prompt_enricher, sample_span):
        """Test span enrichment with system prompt that has version."""
        request_body = json.dumps({
            "messages": [
                {"role": "system", "content": "VERSION: 1.2.3\nYou are a helpful assistant."},
                {"role": "user", "content": "Hello!"}
            ]
        }).encode()
        
        enriched_span = await prompt_enricher.enrich_span_with_prompt(
            span=sample_span,
            request_body=request_body,
            trace_id="test-trace-123",
        )
        
        # Check that span attributes were updated
        assert "llm.system_prompt_sha256" in enriched_span.attributes
        assert "llm.system_prompt_version" in enriched_span.attributes
        assert enriched_span.attributes["llm.system_prompt_version"] == "1.2.3"
    
    @pytest.mark.asyncio
    async def test_enrich_span_no_system_prompt(self, prompt_enricher, sample_span):
        """Test span enrichment when no system prompt is present."""
        request_body = json.dumps({
            "messages": [
                {"role": "user", "content": "Hello!"}
            ]
        }).encode()
        
        enriched_span = await prompt_enricher.enrich_span_with_prompt(
            span=sample_span,
            request_body=request_body,
            trace_id="test-trace-123",
        )
        
        # Span should be unchanged
        assert "llm.system_prompt_sha256" not in enriched_span.attributes
        assert "llm.system_prompt_version" not in enriched_span.attributes
    
    @pytest.mark.asyncio
    async def test_enrich_span_storage_failure(self, prompt_enricher, sample_span):
        """Test span enrichment when storage fails."""
        # Mock the storage to fail
        prompt_enricher.prompt_manager.hot_store.execute.side_effect = Exception("Storage failed")
        
        request_body = json.dumps({
            "messages": [
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": "Hello!"}
            ]
        }).encode()
        
        # Should not raise an exception
        enriched_span = await prompt_enricher.enrich_span_with_prompt(
            span=sample_span,
            request_body=request_body,
            trace_id="test-trace-123",
        )
        
        # Span should still be enriched with attributes
        assert "llm.system_prompt_sha256" in enriched_span.attributes


class TestCreatePromptEnricher:
    """Test the create_prompt_enricher function."""
    
    @pytest.fixture
    def mock_hot_store(self):
        """Create a mock HotStore for testing."""
        hot_store = MagicMock(spec=HotStore)
        return hot_store
    
    def test_create_prompt_enricher(self, mock_hot_store):
        """Test creation of prompt enricher."""
        enricher = create_prompt_enricher(mock_hot_store)
        
        assert enricher is not None
        assert enricher.prompt_manager is not None
        assert enricher.prompt_manager.hot_store is mock_hot_store
    
    @patch.dict('os.environ', {'AGENT_OBS_CONTENT_RATE': '25'})
    def test_create_prompt_enricher_with_custom_rate(self, mock_hot_store):
        """Test creation with custom content rate."""
        enricher = create_prompt_enricher(mock_hot_store)
        
        assert enricher is not None
        assert enricher.prompt_manager.content_rate == 25