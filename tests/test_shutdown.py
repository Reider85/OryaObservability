"""Tests for shutdown behavior (P10): drain buffer, flush exporters, timeout handling."""

import asyncio
import logging
from typing import List
from unittest.mock import patch

import pytest

from agent_obs.exporters.base import BaseExporter
from agent_obs.observability import (
    ObservabilitySDK,
    Span,
    SpanContext,
    SpanType,
    _now,
)


class MockExporter(BaseExporter):
    """Mock exporter that collects exported batches for testing."""

    def __init__(self) -> None:
        self.batches: List[List[Span]] = []
        self.flush_count = 0

    async def export(self, batch: list[Span]) -> None:
        self.batches.append(list(batch))

    async def flush(self) -> None:
        self.flush_count += 1


class SlowExporter(BaseExporter):
    """Exporter that simulates slow flush for timeout testing."""

    def __init__(self, delay: float = 3.0):
        self.delay = delay
        self.flush_count = 0

    async def export(self, batch: list[Span]) -> None:
        pass

    async def flush(self) -> None:
        self.flush_count += 1
        await asyncio.sleep(self.delay)


class HangingExporter(BaseExporter):
    """Exporter that hangs indefinitely to test timeout behavior."""

    def __init__(self):
        self.flush_called = False

    async def export(self, batch: list[Span]) -> None:
        pass

    async def flush(self) -> None:
        self.flush_called = True
        # Simulate infinite hang
        await asyncio.sleep(999999)


def _make_span(name: str = "test.span", agent_id: str = "test-agent") -> Span:
    ctx = SpanContext.new(agent_id=agent_id, agent_version="0.1.0")
    return Span(
        name=name,
        span_type=SpanType.AGENT_LOOP,
        context=ctx,
        start_time=_now(),
        end_time=_now(),
        attributes={"status": "ok"},
    )


class TestShutdown:
    async def test_shutdown_drains_all_spans(self) -> None:
        """Test that shutdown drains all spans from the ring buffer."""
        exporter = MockExporter()
        sdk = ObservabilitySDK(
            exporters=[exporter],
            enabled=True,
            ring_buffer_maxsize=1000,
        )
        
        # Enqueue 1000 spans
        spans = [_make_span(name=f"span-{i}") for i in range(1000)]
        for span in spans:
            await sdk._enqueue(span)
        
        # Shutdown should drain all spans
        await sdk.shutdown()
        
        # All spans should be exported
        all_exported = []
        for batch in exporter.batches:
            all_exported.extend(batch)
        
        assert len(all_exported) == 1000
        assert all(span in all_exported for span in spans)
        assert exporter.flush_count == 1  # flush() should be called once

    async def test_shutdown_flushes_exporters(self) -> None:
        """Test that shutdown calls flush() on all exporters."""
        exp1 = MockExporter()
        exp2 = MockExporter()
        sdk = ObservabilitySDK(
            exporters=[exp1, exp2],
            enabled=True,
            ring_buffer_maxsize=1000,
        )
        
        span = _make_span()
        await sdk._enqueue(span)
        
        await sdk.shutdown()
        
        # Both exporters should have been flushed
        assert exp1.flush_count == 1
        assert exp2.flush_count == 1

    async def test_shutdown_with_no_exporters(self) -> None:
        """Test shutdown works when no exporters are configured."""
        sdk = ObservabilitySDK(
            exporters=[],
            enabled=True,
            ring_buffer_maxsize=1000,
        )
        
        # Should not raise any exceptions
        await sdk.shutdown()

    async def test_shutdown_with_disabled_sdk(self) -> None:
        """Test shutdown works when SDK is disabled."""
        sdk = ObservabilitySDK(
            exporters=[],
            enabled=False,
            ring_buffer_maxsize=1000,
        )
        
        # Should not raise any exceptions
        await sdk.shutdown()

    async def test_shutdown_timeout_with_slow_flush(self) -> None:
        """Test timeout when flush takes too long."""
        slow_exp = SlowExporter(delay=3.0)  # 3s flush
        sdk = ObservabilitySDK(
            exporters=[slow_exp],
            enabled=True,
            ring_buffer_maxsize=1000,
        )
        
        span = _make_span()
        await sdk._enqueue(span)
        
        # Shutdown should timeout after ~3s (worker finishes quickly, flush takes 3s)
        start_time = _now()
        await sdk.shutdown()
        elapsed = _now() - start_time
        
        # Should be close to 3s (allowing some tolerance)
        assert 2.5 <= elapsed <= 4.0
        assert slow_exp.flush_count == 1  # flush should still be called

    async def test_shutdown_timeout_with_hanging_flush(self) -> None:
        """Test timeout when flush hangs indefinitely."""
        hanging_exp = HangingExporter()
        sdk = ObservabilitySDK(
            exporters=[hanging_exp],
            enabled=True,
            ring_buffer_maxsize=1000,
        )
        
        span = _make_span()
        await sdk._enqueue(span)
        
        # Should timeout and cancel the hanging flush
        start_time = _now()
        await sdk.shutdown()
        elapsed = _now() - start_time
        
        # Should complete within ~5s
        assert elapsed <= 6.0
        assert hanging_exp.flush_called is True

    async def test_shutdown_timeout_logs_undelivered_spans(self) -> None:
        """Test that timeout logs warning with undelivered span count."""
        # Create an exporter that never finishes flushing but the worker hangs
        class WorkerHangingExporter(BaseExporter):
            def __init__(self):
                self.flush_called = False
                self.export_count = 0
            
            async def export(self, batch: list[Span]) -> None:
                self.export_count += 1
                # Worker hangs during export, never finishes
                await asyncio.sleep(999999)
            
            async def flush(self) -> None:
                self.flush_called = True
        
        worker_hanging_exp = WorkerHangingExporter()
        sdk = ObservabilitySDK(
            exporters=[worker_hanging_exp],
            enabled=True,
            ring_buffer_maxsize=1000,
        )
        
        # Enqueue some spans that will be undelivered
        spans = [_make_span() for _ in range(10)]
        for span in spans:
            await sdk._enqueue(span)
        
        with patch.object(logging.getLogger('agent_obs.observability'), 'warning') as mock_warning:
            await sdk.shutdown()
        
        # Should have logged a warning about undelivered spans
        mock_warning.assert_called()
        
        # Check if "cancelling worker" was logged
        call_args_list = [call[0][0] for call in mock_warning.call_args_list]
        assert any("cancelling worker" in msg for msg in call_args_list)

    async def test_async_context_manager(self) -> None:
        """Test that async with sdk calls shutdown automatically."""
        exporter = MockExporter()
        sdk = ObservabilitySDK(
            exporters=[exporter],
            enabled=True,
            ring_buffer_maxsize=1000,
        )
        
        span = _make_span()
        await sdk._enqueue(span)
        
        # Use async context manager
        async with sdk:
            pass
        
        # Should have been shut down and flushed
        assert exporter.flush_count == 1
        assert len(exporter.batches) >= 1
        assert span in exporter.batches[0]

    async def test_shutdown_idempotent(self) -> None:
        """Test that calling shutdown multiple times is safe."""
        exporter = MockExporter()
        sdk = ObservabilitySDK(
            exporters=[exporter],
            enabled=True,
            ring_buffer_maxsize=1000,
        )
        
        span = _make_span()
        await sdk._enqueue(span)
        
        # Call shutdown twice
        await sdk.shutdown()
        await sdk.shutdown()
        
        # Should only flush once
        assert exporter.flush_count == 1

    async def test_shutdown_with_empty_buffer(self) -> None:
        """Test shutdown when no spans are in the buffer."""
        exporter = MockExporter()
        sdk = ObservabilitySDK(
            exporters=[exporter],
            enabled=True,
            ring_buffer_maxsize=1000,
        )
        
        # Shutdown immediately without enqueuing anything
        await sdk.shutdown()
        
        # Should complete without issues
        assert exporter.flush_count == 1