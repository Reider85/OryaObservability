"""Tests for ring buffer (P07) and overflow handling (P09)."""

import asyncio

import pytest

from agent_obs.metrics import dropped_spans_total
from agent_obs.observability import (
    ObservabilitySDK,
    Span,
    SpanContext,
    SpanType,
    _now,
)


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


class TestRingBuffer:
    def test_ring_buffer_created_in_constructor(self) -> None:
        sdk = ObservabilitySDK(exporters=[], enabled=True, ring_buffer_maxsize=1000)
        assert isinstance(sdk._ring_buffer, asyncio.Queue)
        assert sdk._ring_buffer.maxsize == 1000

    async def test_enqueue_puts_span_in_buffer(self) -> None:
        sdk = ObservabilitySDK(exporters=[], enabled=True, ring_buffer_maxsize=1000)
        span = _make_span()
        await sdk._enqueue(span)
        assert not sdk._ring_buffer.empty()
        assert sdk._ring_buffer.get_nowait() is span

    async def test_enqueue_still_populates_last_spans(self) -> None:
        sdk = ObservabilitySDK(exporters=[], enabled=True, ring_buffer_maxsize=1000)
        span = _make_span()
        await sdk._enqueue(span)
        assert len(sdk.last_spans) == 1

    async def test_enqueue_is_nonblocking(self) -> None:
        sdk = ObservabilitySDK(exporters=[], enabled=True, ring_buffer_maxsize=2)
        for _ in range(2):
            await sdk._enqueue(_make_span())
        # Third enqueue should not block — it drops the span
        await sdk._enqueue(_make_span())
        # Queue should still have only 2 items
        assert sdk._ring_buffer.qsize() == 2

    async def test_overflow_drops_span_and_increments_metric(self) -> None:
        sdk = ObservabilitySDK(exporters=[], enabled=True, ring_buffer_maxsize=2)
        initial = dropped_spans_total.labels(agent_id="test-agent")._value.get()

        for _ in range(2):
            await sdk._enqueue(_make_span(agent_id="test-agent"))

        # This should be dropped
        await sdk._enqueue(_make_span(agent_id="test-agent"))

        after = dropped_spans_total.labels(agent_id="test-agent")._value.get()
        assert after == initial + 1

    async def test_no_worker_started_without_exporters(self) -> None:
        sdk = ObservabilitySDK(exporters=[], enabled=True, ring_buffer_maxsize=1000)
        assert sdk._worker_task is None
