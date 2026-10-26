"""Tests for tree summary worker (PP18)."""

import asyncio
import json
import unittest
from unittest.mock import MagicMock, patch

import pytest

from agent_obs.eval.tree_summary_worker import (
    _extract_subtree_spans,
    compute_subtree_content_hash,
    _build_summary_prompt,
    _generate_summary,
    _write_summary_annotation,
    _check_cache,
    _set_cache,
    tree_summary_worker,
    _process_tree_summary_job,
)
from agent_obs.storage.hot import HotStore


class TestSubtreeExtraction:
    """Test subtree span extraction."""
    
    def test_extract_subtree_simple(self):
        """Test extracting a simple subtree."""
        spans = [
            {"span_id": "root", "parent_span_id": "", "name": "root"},
            {"span_id": "child1", "parent_span_id": "root", "name": "child1"},
            {"span_id": "child2", "parent_span_id": "root", "name": "child2"},
            {"span_id": "grandchild", "parent_span_id": "child1", "name": "grandchild"},
        ]
        
        subtree = _extract_subtree_spans(spans, "root")
        span_ids = [s["span_id"] for s in subtree]
        
        assert "root" in span_ids
        assert "child1" in span_ids
        assert "child2" in span_ids
        assert "grandchild" in span_ids
    
    def test_extract_subtree_exclude_compressed(self):
        """Test that compressed nodes are excluded from subtree."""
        spans = [
            {"span_id": "root", "parent_span_id": "", "name": "root"},
            {"span_id": "compressed", "parent_span_id": "root", "name": "compressed", 
             "span_type": "agent.reasoning.compressed"},
            {"span_id": "child", "parent_span_id": "compressed", "name": "child"},
        ]
        
        subtree = _extract_subtree_spans(spans, "root")
        span_ids = [s["span_id"] for s in subtree]
        
        # Compressed node and its children should be excluded
        assert "root" in span_ids
        assert "compressed" not in span_ids
        assert "child" not in span_ids
    
    def test_extract_subtree_not_found(self):
        """Test behavior when summary node not found."""
        spans = [
            {"span_id": "root", "parent_span_id": "", "name": "root"},
        ]
        
        subtree = _extract_subtree_spans(spans, "missing")
        assert len(subtree) == 0


class TestContentHash:
    """Test content hash computation."""
    
    def test_hash_deterministic(self):
        """Test that same content produces same hash."""
        span1 = {
            "span_id": "1", "start_time": 100.0,
            "attributes": {"reasoning": "Step 1", "input": "Hello"}
        }
        span2 = {
            "span_id": "2", "start_time": 200.0,
            "attributes": {"reasoning": "Step 1", "input": "Hello"}
        }
        
        hash1 = compute_subtree_content_hash([span1])
        hash2 = compute_subtree_content_hash([span2])
        
        assert hash1 == hash2
    
    def test_hash_different_content(self):
        """Test that different content produces different hash."""
        span1 = {
            "span_id": "1", "start_time": 100.0,
            "attributes": {"reasoning": "Step 1"}
        }
        span2 = {
            "span_id": "2", "start_time": 200.0,
            "attributes": {"reasoning": "Step 2"}
        }
        
        hash1 = compute_subtree_content_hash([span1])
        hash2 = compute_subtree_content_hash([span2])
        
        assert hash1 != hash2
    
    def test_hash_with_events(self):
        """Test hash includes event content."""
        span = {
            "span_id": "1", "start_time": 100.0,
            "attributes": {"reasoning": "Step 1"},
            "events": [
                {
                    "name": "event1",
                    "attributes": {"message": "Event content"}
                }
            ]
        }
        
        hash_with_events = compute_subtree_content_hash([span])
        assert hash_with_events is not None


class TestPromptBuilding:
    """Test summary prompt building."""
    
    def test_build_prompt_simple(self):
        """Test building a simple prompt."""
        spans = [
            {
                "span_id": "1", "start_time": 100.0,
                "name": "Step 1",
                "attributes": {"reasoning": "First step", "input": "Hello"}
            },
            {
                "span_id": "2", "start_time": 200.0,
                "name": "Step 2",
                "attributes": {"reasoning": "Second step", "output": "World"}
            }
        ]
        
        prompt = _build_summary_prompt(spans)
        
        assert "Суммируй следующие шаги reasoning-цикла:" in prompt
        assert "1. Step 1:" in prompt
        assert "reasoning: First step" in prompt
        assert "input: Hello" in prompt
        assert "2. Step 2:" in prompt
        assert "reasoning: Second step" in prompt
        assert "output: World" in prompt
    
    def test_build_prompt_metadata_filtered(self):
        """Test that metadata keys are filtered from prompt."""
        spans = [
            {
                "span_id": "1", "start_time": 100.0,
                "name": "Step 1",
                "attributes": {
                    "reasoning": "Step content",
                    "status": "ok",
                    "tree.compressed": True,
                    "tree.children_count": 5,
                    "cost.usd": 0.01,
                    "guardrail.redacted_fields": ["pii"]
                }
            }
        ]
        
        prompt = _build_summary_prompt(spans)
        
        # Content should be included
        assert "reasoning: Step content" in prompt
        # Metadata should be excluded
        assert "status: ok" not in prompt
        assert "tree.compressed: True" not in prompt
        assert "tree.children_count: 5" not in prompt
        assert "cost.usd: 0.01" not in prompt
        assert "guardrail.redacted_fields: ['pii']" not in prompt


class TestCacheOperations:
    """Test Redis cache operations."""
    
    def test_check_cache_hit(self):
        """Test checking cache when key exists."""
        redis_client = MagicMock()
        redis_client.get.return_value = json.dumps({"summary": "cached summary"})
        
        result = _check_cache(redis_client, "test_hash")
        
        assert result == "cached summary"
        redis_client.get.assert_called_once_with("agent_obs:tree:summary:test_hash")
    
    def test_check_cache_miss(self):
        """Test checking cache when key doesn't exist."""
        redis_client = MagicMock()
        redis_client.get.return_value = None
        
        result = _check_cache(redis_client, "test_hash")
        
        assert result is None
        redis_client.get.assert_called_once_with("agent_obs:tree:summary:test_hash")
    
    def test_check_cache_error(self):
        """Test cache check when Redis raises error."""
        redis_client = MagicMock()
        redis_client.get.side_effect = Exception("Redis error")
        
        result = _check_cache(redis_client, "test_hash")
        
        assert result is None
    
    def test_set_cache(self):
        """Test setting cache value."""
        redis_client = MagicMock()
        
        _set_cache(redis_client, "test_hash", "test summary")
        
        expected_key = "agent_obs:tree:summary:test_hash"
        expected_data = json.dumps({"summary": "test summary", "content_hash": "test_hash"})
        redis_client.setex.assert_called_once_with(expected_key, 604800, expected_data)


class TestSummaryGeneration:
    """Test LLM summary generation."""
    
    @pytest.mark.asyncio
    async def test_generate_summary_success(self):
        """Test successful summary generation."""
        spans = [
            {
                "span_id": "1", "start_time": 100.0,
                "name": "Step 1",
                "attributes": {"reasoning": "First step", "input": "Hello"}
            }
        ]
        
        # Mock successful LLM response
        with patch('agent_obs.eval.tree_summary_worker.httpx') as mock_httpx:
            mock_response = MagicMock()
            mock_response.json.return_value = {
                "choices": [{"message": {"content": "Generated summary"}}]
            }
            mock_response.raise_for_status.return_value = None
            mock_httpx.AsyncClient.return_value.__aenter__.return_value.post.return_value = mock_response
            
            summary = await _generate_summary(spans)
            
            assert summary == "Generated summary"
    
    @pytest.mark.asyncio
    async def test_generate_summary_error(self):
        """Test summary generation with LLM error."""
        spans = [
            {
                "span_id": "1", "start_time": 100.0,
                "name": "Step 1",
                "attributes": {"reasoning": "First step"}
            }
        ]
        
        # Mock LLM error
        with patch('agent_obs.eval.tree_summary_worker.httpx') as mock_httpx:
            mock_httpx.AsyncClient.return_value.__aenter__.return_value.post.side_effect = Exception("API error")
            
            with pytest.raises(Exception):
                await _generate_summary(spans)


class TestSummaryAnnotation:
    """Test writing summary as late annotation."""
    
    @pytest.mark.asyncio
    async def test_write_summary_annotation(self):
        """Test writing summary annotation successfully."""
        hot_store = MagicMock(spec=HotStore)
        redis_client = MagicMock()
        
        eval_result_mock = MagicMock()
        eval_result_mock.trace_id = "test_trace"
        eval_result_mock.eval_id = "test_node"
        eval_result_mock.eval_name = "tree_summary"
        eval_result_mock.eval_timestamp = 1000.0
        eval_result_mock.eval_latency_seconds = 1.0
        eval_result_mock.scores = {"summary": "test summary"}
        eval_result_mock.reasoning = "test summary"
        
        with patch('agent_obs.eval.tree_summary_worker.annotate_with_fallback') as mock_annotate:
            mock_annotate.return_value = None
            
            await _write_summary_annotation(
                "test_trace", "test_node", "test summary",
                hot_store, redis_client, "test_hash"
            )
            
            mock_annotate.assert_called_once_with(eval_result_mock, hot_store, redis_client)
    
    @pytest.mark.asyncio
    async def test_write_summary_annotation_with_fallback(self):
        """Test writing summary annotation with fallback."""
        hot_store = MagicMock(spec=HotStore)
        redis_client = MagicMock()
        
        eval_result_mock = MagicMock()
        eval_result_mock.trace_id = "test_trace"
        eval_result_mock.eval_id = "test_node"
        eval_result_mock.eval_name = "tree_summary"
        eval_result_mock.eval_timestamp = 1000.0
        eval_result_mock.eval_latency_seconds = 1.0
        eval_result_mock.scores = {"summary": "test summary"}
        eval_result_mock.reasoning = "test summary"
        
        with patch('agent_obs.eval.tree_summary_worker.annotate_with_fallback') as mock_annotate:
            mock_annotate.side_effect = Exception("Fallback failed")
            
            # Should not raise exception
            await _write_summary_annotation(
                "test_trace", "test_node", "test summary",
                hot_store, redis_client, "test_hash"
            )
            
            mock_annotate.assert_called_once_with(eval_result_mock, hot_store, redis_client)


class TestWorkerEntry:
    """Test worker entry point."""
    
    @patch('agent_obs.eval.tree_summary_worker._process_tree_summary_job')
    def test_worker_entry_calls_process(self, mock_process):
        """Test that worker entry calls process method."""
        from agent_obs.eval.tree_summary_worker import tree_summary_worker
        tree_summary_worker("test_trace", "test_node")
        
        mock_process.assert_called_once_with("test_trace", "test_node")


class TestIntegration:
    """Integration tests for the complete workflow."""
    
    def test_full_workflow_simulation(self):
        """Test the complete workflow with mocked components."""
        spans = [
            {
                "span_id": "1", "start_time": 100.0,
                "name": "Step 1",
                "attributes": {"reasoning": "First step", "input": "Hello"}
            },
            {
                "span_id": "2", "start_time": 200.0,
                "name": "Step 2",
                "attributes": {"reasoning": "Second step", "output": "World"}
            }
        ]
        
        # Mock components
        with patch('agent_obs.eval.tree_summary_worker._create_hot_store') as mock_create_hot_store, \
             patch('agent_obs.eval.tree_summary_worker._create_redis_client') as mock_create_redis, \
             patch('agent_obs.eval.tree_summary_worker._extract_subtree_spans') as mock_extract, \
             patch('agent_obs.eval.tree_summary_worker.compute_subtree_content_hash') as mock_hash, \
             patch('agent_obs.eval.tree_summary_worker._check_cache') as mock_cache, \
             patch('agent_obs.eval.tree_summary_worker._generate_summary') as mock_generate, \
             patch('agent_obs.eval.tree_summary_worker._set_cache') as mock_set_cache, \
             patch('agent_obs.eval.tree_summary_worker._write_summary_annotation') as mock_write:
            
            # Configure mocks
            mock_hot_store = MagicMock(spec=HotStore)
            mock_create_hot_store.return_value = mock_hot_store
            mock_create_redis.return_value = None
            mock_extract.return_value = spans
            mock_hash.return_value = "test_hash"
            mock_cache.return_value = None  # Cache miss
            mock_generate.return_value = "Generated summary"
            
            # Run the process
            from agent_obs.eval.tree_summary_worker import _process_tree_summary_job
            import asyncio
            asyncio.run(_process_tree_summary_job("test_trace", "test_node"))
            
            # Verify calls
            mock_extract.assert_called_once()
            mock_hash.assert_called_once()
            mock_cache.assert_called_once()
            mock_generate.assert_called_once()
            mock_set_cache.assert_called_once()
            mock_write.assert_called_once()