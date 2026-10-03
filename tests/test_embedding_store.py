"""Tests for PC24: response embedding storage in ClickHouse Hot store.

Covers:
- Embedding extraction from span attributes into the embedding queue
- Batched ClickHouse writes via HotStore
- Redis fallback when ClickHouse is unavailable
- Metrics increment on success and failure
- Queue-full dropping
- Shutdown drains the embedding queue
"""

from __future__ import annotations

import asyncio
import json
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_obs.observability import ObservabilitySDK, Span, SpanContext, SpanType, _now


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def sdk():
    """SDK with no exporters (export worker not started)."""
    return ObservabilitySDK(exporters=[], enabled=True)


@pytest.fixture
def span_with_embedding():
    """A span that carries a response_embedding attribute."""
    ctx = SpanContext.new(agent_id="test-agent")
    return Span(
        name="llm.call:test",
        span_type=SpanType.LLM_CALL,
        context=ctx,
        attributes={
            "llm.output_text": "Hello world",
            "response_embedding": [0.1, 0.2, 0.3, 0.4],
        },
        start_time=time.time() - 0.1,
        end_time=time.time(),
    )


@pytest.fixture
def span_without_embedding():
    """A span without a response_embedding attribute."""
    ctx = SpanContext.new(agent_id="test-agent")
    return Span(
        name="llm.call:test",
        span_type=SpanType.LLM_CALL,
        context=ctx,
        attributes={"llm.output_text": "Hello world"},
        start_time=time.time() - 0.1,
        end_time=time.time(),
    )


# ---------------------------------------------------------------------------
# _enqueue extracts embedding
# ---------------------------------------------------------------------------

class TestEnqueueExtractsEmbedding:
    """PC24: _enqueue must pull response_embedding off the span and route it
    to the embedding queue, then strip it from the span attributes before the
    span enters the ring buffer."""

    async def test_embedding_moved_to_queue(self, sdk, span_with_embedding):
        await sdk._enqueue(span_with_embedding)

        # The embedding should have been extracted into the queue.
        assert not sdk._embedding_queue.empty()
        trace_id, span_id, agent_id, tenant_id, embedding = sdk._embedding_queue.get_nowait()
        assert trace_id == span_with_embedding.context.trace_id
        assert span_id == span_with_embedding.context.span_id
        assert agent_id == span_with_embedding.context.agent_id
        assert tenant_id == ""  # span_with_embedding doesn't have tenant_id
        assert embedding == [0.1, 0.2, 0.3, 0.4]

    async def test_embedding_removed_from_span(self, sdk, span_with_embedding):
        await sdk._enqueue(span_with_embedding)
        assert "response_embedding" not in span_with_embedding.attributes

    async def test_no_embedding_no_queue_entry(self, sdk, span_without_embedding):
        await sdk._enqueue(span_without_embedding)
        assert sdk._embedding_queue.empty()

    async def test_span_still_enters_ring_buffer(self, sdk, span_with_embedding):
        await sdk._enqueue(span_with_embedding)
        # The span (minus the embedding) should be in the ring buffer.
        assert not sdk._ring_buffer.empty()
        queued_span = sdk._ring_buffer.get_nowait()
        assert queued_span is span_with_embedding


# ---------------------------------------------------------------------------
# Queue-full handling
# ---------------------------------------------------------------------------

class TestEmbeddingQueueFull:
    """When the embedding queue is full, the embedding is dropped and the
    metric embeddings_storage_failed_total is incremented."""

    async def test_full_queue_drops_embedding(self, sdk, span_with_embedding):
        # maxsize=1, already full → put_nowait raises QueueFull
        sdk._embedding_queue = asyncio.Queue(maxsize=1)
        await sdk._embedding_queue.put(("x", "y", "agent", "", [0.0]))  # fill it
        with patch("agent_obs.metrics.embeddings_storage_failed_total") as mock_metric:
            mock_metric.inc = MagicMock()
            await sdk._enqueue(span_with_embedding)
            mock_metric.inc.assert_called_once()


# ---------------------------------------------------------------------------
# _flush_embedding_batch — ClickHouse-first with Redis fallback (PC24)
# ---------------------------------------------------------------------------

class TestFlushEmbeddingBatch:
    """PC24: embeddings are written to ClickHouse span_embeddings table,
    with Redis fallback on failure. Only successful ClickHouse writes
    increment embeddings_stored_total; failures increment 
    embeddings_storage_failed_total."""

    async def test_flush_uses_clickhouse_then_redis_fallback(self, sdk):
        batch = [
            ("trace-1", "span-1", "agent1", "", [0.1, 0.2]),
            ("trace-2", "span-2", "agent2", "", [0.3, 0.4]),
        ]
        with patch("agent_obs.storage.hot.HotStore") as mock_hot_cls, \
             patch("agent_obs.metrics.embeddings_stored_total") as mock_stored, \
             patch("agent_obs.metrics.embeddings_storage_failed_total") as mock_failed, \
             patch("agent_obs.metrics.embeddings_batch_size") as mock_batch_size, \
             patch.object(sdk, "_embedding_redis_fallback", new_callable=AsyncMock) as mock_fallback:
            mock_stored.inc = MagicMock()
            mock_failed.inc = MagicMock()
            mock_batch_size.observe = MagicMock()
            
            # Make ClickHouse write raise an exception to trigger Redis fallback
            mock_hot_cls.return_value._get_client.return_value.execute.side_effect = Exception("ClickHouse failed")

            await sdk._flush_embedding_batch(batch)

            # Writes to ClickHouse first, then Redis fallback on failure
            mock_hot_cls.assert_called_once()
            mock_fallback.assert_called_once_with(batch)
            mock_stored.inc.assert_not_called()  # only called on success
            mock_failed.inc.assert_called_once_with(2)  # on CH failure
            mock_batch_size.observe.assert_called_once_with(2)

    async def test_flush_empty_batch_noop(self, sdk):
        with patch("agent_obs.storage.hot.HotStore") as mock_hot_cls, \
             patch("agent_obs.metrics.embeddings_stored_total") as mock_stored, \
             patch("agent_obs.metrics.embeddings_storage_failed_total") as mock_failed, \
             patch("agent_obs.metrics.embeddings_batch_size") as mock_batch_size, \
             patch.object(sdk, "_embedding_redis_fallback", new_callable=AsyncMock) as mock_fallback:
            await sdk._flush_embedding_batch([])
            mock_hot_cls.assert_not_called()
            mock_fallback.assert_not_called()
            mock_stored.inc.assert_not_called()
            mock_failed.inc.assert_not_called()
            mock_batch_size.observe.assert_not_called()


# ---------------------------------------------------------------------------
# Redis fallback
# ---------------------------------------------------------------------------

class TestEmbeddingRedisFallback:
    """PC24: embeddings persist in Redis with TTL 1h as degraded fallback
    when ClickHouse is unavailable. Increments embeddings_redis_fallback_total."""

    async def test_redis_fallback_exception_is_caught(self, sdk):
        """Verify the fallback doesn't crash even if Redis import fails."""
        batch = [("trace-1", "span-1", "agent", "", [0.1, 0.2])]
        # Should not raise — the method catches all exceptions internally.
        await sdk._embedding_redis_fallback(batch)

    async def test_flush_calls_redis_fallback(self, sdk):
        """End-to-end: _flush_embedding_batch routes embeddings to Redis."""
        batch = [("trace-1", "span-1", "agent", "", [0.1])]
        with patch.object(sdk, "_embedding_redis_fallback", new_callable=AsyncMock) as mock_fallback:
            await sdk._flush_embedding_batch(batch)
            mock_fallback.assert_called_once_with(batch)


# ---------------------------------------------------------------------------
# _embedding_worker batching
# ---------------------------------------------------------------------------

class TestEmbeddingWorkerBatching:
    """PC24: the embedding worker collects items and flushes in batches."""

    async def test_worker_flushes_batch(self, sdk):
        """Put items in the queue, then verify the worker drains them."""
        # Seed the queue with items.
        for i in range(5):
            await sdk._embedding_queue.put((f"trace-{i}", f"span-{i}", "agent", "", [float(i)]))

        with patch.object(sdk, "_flush_embedding_batch", new_callable=AsyncMock) as mock_flush:
            sdk._embedding_shutdown.set()
            # Run the worker for a short time.
            worker = asyncio.create_task(sdk._embedding_worker())
            try:
                await asyncio.wait_for(worker, timeout=3.0)
            except asyncio.TimeoutError:
                worker.cancel()
                try:
                    await worker
                except asyncio.CancelledError:
                    pass

            # The worker should have flushed at least once.
            assert mock_flush.call_count >= 1
            all_items = []
            for call in mock_flush.call_args_list:
                all_items.extend(call[0][0])
            assert len(all_items) == 5

    async def test_worker_respects_shutdown(self, sdk):
        """Worker exits when _embedding_shutdown is set and queue is empty."""
        sdk._embedding_shutdown.set()
        worker = asyncio.create_task(sdk._embedding_worker())
        await asyncio.wait_for(worker, timeout=2.0)
        assert worker.done()


# ---------------------------------------------------------------------------
# Shutdown drains embedding queue
# ---------------------------------------------------------------------------

class TestShutdownDrainsEmbeddingQueue:
    """PC24: SDK.shutdown() must signal the embedding worker and wait for it."""

    async def test_shutdown_sets_embedding_shutdown_event(self):
        """Even when the worker was never started, shutdown sets the event."""
        sdk = ObservabilitySDK(exporters=[], enabled=True)
        # Simulate that the worker was started (e.g. when constructed in async context).
        sdk._embedding_worker_task = asyncio.create_task(asyncio.sleep(100))
        await sdk._embedding_queue.put(("t", "s", "agent", "", [0.1]))

        with patch.object(sdk, "_flush_embedding_batch", new_callable=AsyncMock):
            await sdk.shutdown()

        assert sdk._embedding_shutdown.is_set()
        assert sdk._embedding_worker_task is None


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

class TestEmbeddingMetrics:
    """PC24: verify that the embedding metrics are defined and importable."""

    def test_metrics_importable(self):
        from agent_obs.metrics import (
            embeddings_batch_size,
            embeddings_stored_total,
            embeddings_storage_failed_total,
        )
        assert embeddings_stored_total is not None
        assert embeddings_storage_failed_total is not None
        assert embeddings_batch_size is not None
