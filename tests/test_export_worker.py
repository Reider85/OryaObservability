"""Tests for the export worker (P08)."""

import asyncio
from typing import List

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
        self.export_count = 0

    async def export(self, batch: list[Span]) -> None:
        self.batches.append(list(batch))
        self.export_count += 1

    async def flush(self) -> None:
        pass


class FailingExporter(BaseExporter):
    """Exporter that always raises to test error resilience."""

    async def export(self, batch: list[Span]) -> None:
        raise RuntimeError("export failed")

    async def flush(self) -> None:
        pass


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


class TestExportWorker:
    async def test_worker_receives_spans_from_queue(self) -> None:
        exporter = MockExporter()
        sdk = ObservabilitySDK(
            exporters=[exporter],
            enabled=True,
            ring_buffer_maxsize=1000,
        )
        try:
            span = _make_span()
            await sdk._enqueue(span)

            # Wait for worker to process
            await asyncio.sleep(1.0)

            assert len(exporter.batches) >= 1
            assert span in exporter.batches[0]
        finally:
            await sdk.shutdown()

    async def test_batch_max_512(self) -> None:
        exporter = MockExporter()
        sdk = ObservabilitySDK(
            exporters=[exporter],
            enabled=True,
            ring_buffer_maxsize=1000,
        )
        try:
            spans = [_make_span(name=f"span-{i}") for i in range(600)]
            for span in spans:
                await sdk._enqueue(span)

            # Wait for worker to drain
            await asyncio.sleep(2.0)

            # All spans should have been exported, but in batches of <= 512
            all_exported = []
            for batch in exporter.batches:
                assert len(batch) <= 512
                all_exported.extend(batch)

            assert len(all_exported) == 600
        finally:
            await sdk.shutdown()

    async def test_fan_out_to_multiple_exporters(self) -> None:
        exp1 = MockExporter()
        exp2 = MockExporter()
        sdk = ObservabilitySDK(
            exporters=[exp1, exp2],
            enabled=True,
            ring_buffer_maxsize=1000,
        )
        try:
            span = _make_span()
            await sdk._enqueue(span)

            await asyncio.sleep(1.0)

            assert len(exp1.batches) >= 1
            assert len(exp2.batches) >= 1
            # Both should receive the same span
            assert span in exp1.batches[0]
            assert span in exp2.batches[0]
        finally:
            await sdk.shutdown()

    async def test_failing_exporter_does_not_crash_worker(self) -> None:
        good = MockExporter()
        bad = FailingExporter()
        sdk = ObservabilitySDK(
            exporters=[bad, good],
            enabled=True,
            ring_buffer_maxsize=1000,
        )
        try:
            span = _make_span()
            await sdk._enqueue(span)

            await asyncio.sleep(1.0)

            # The good exporter should still receive the batch
            assert len(good.batches) >= 1
            assert span in good.batches[0]
            # Worker should still be alive
            assert not sdk._worker_task.done()
        finally:
            await sdk.shutdown()
