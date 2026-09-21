"""Load tests for instrumentation overhead and non-blocking behavior (P11)."""

import asyncio
import time
import statistics
import pytest
from typing import List
from unittest.mock import patch

from agent_obs.observability import ObservabilitySDK, Span, SpanContext, SpanType, _now
from agent_obs.exporters.base import BaseExporter


class SlowExporter(BaseExporter):
    """Exporter that simulates unavailable backend with long delay."""
    
    def __init__(self, delay: float = 3600.0):
        self.delay = delay
        self.exported_batches = []
        self.flush_count = 0
    
    async def export(self, batch: List[Span]) -> None:
        self.exported_batches.append(batch)
        # Simulate unavailable backend
        await asyncio.sleep(self.delay)
    
    async def flush(self) -> None:
        self.flush_count += 1


def _make_span(name: str = "test.span", agent_id: str = "load-test") -> Span:
    """Create a test span for load testing."""
    ctx = SpanContext.new(agent_id=agent_id, agent_version="0.1.0")
    return Span(
        name=name,
        span_type=SpanType.AGENT_LOOP,
        context=ctx,
        start_time=_now(),
        end_time=_now(),
        attributes={"status": "ok"},
    )


class TestEnqueueLatency:
    """Test that _enqueue() has p99 latency < 0.1ms."""
    
    @pytest.mark.asyncio
    async def test_enqueue_p99_latency(self):
        """Test _enqueue() p99 < 0.1ms."""
        sdk = ObservabilitySDK(exporters=[], enabled=True, ring_buffer_maxsize=100_000)
        
        # Generate 10,000 spans
        spans = [_make_span(name=f"span-{i}") for i in range(10_000)]
        
        # Measure enqueue latency for each span
        latencies = []
        for span in spans:
            start_time = time.perf_counter()
            await sdk._enqueue(span)
            end_time = time.perf_counter()
            latencies.append((end_time - start_time) * 1000)  # Convert to ms
        
        # Calculate p99
        p99_latency = statistics.quantiles(latencies, n=100)[98]  # 99th percentile
        
        print(f"Enqueue p99 latency: {p99_latency:.6f} ms")
        print(f"Max latency: {max(latencies):.6f} ms")
        print(f"Mean latency: {statistics.mean(latencies):.6f} ms")
        
        # Assert p99 < 0.1ms
        assert p99_latency < 0.1, f"p99 latency {p99_latency:.6f}ms >= 0.1ms"
        
        # Cleanup
        await sdk.shutdown()


class TestAgentObservedOverhead:
    """Test that @agent_observed wrapper has p99 latency < 1ms."""
    
    @pytest.mark.asyncio
    async def test_agent_observed_p99_latency(self):
        """Test @agent_observed p99 < 1ms."""
        sdk = ObservabilitySDK(exporters=[], enabled=True, ring_buffer_maxsize=100_000)
        
        @sdk.agent_observed("load-test-agent")
        async def dummy_agent(_obs_ctx=None):
            """Trivial agent function for testing."""
            return f"result-{time.time()}"
        
        # Warm up
        for _ in range(10):
            await dummy_agent()
        
        # Run 10,000 calls and measure latency
        latencies = []
        for i in range(10_000):
            start_time = time.perf_counter()
            await dummy_agent()
            end_time = time.perf_counter()
            latencies.append((end_time - start_time) * 1000)  # Convert to ms
        
        # Calculate p99
        p99_latency = statistics.quantiles(latencies, n=100)[98]  # 99th percentile
        
        print(f"Agent observed p99 latency: {p99_latency:.6f} ms")
        print(f"Max latency: {max(latencies):.6f} ms")
        print(f"Mean latency: {statistics.mean(latencies):.6f} ms")
        
        # Assert p99 < 1ms
        assert p99_latency < 1.0, f"p99 latency {p99_latency:.6f}ms >= 1ms"
        
        # Cleanup
        await sdk.shutdown()


class TestEventLoopLag:
    """Test event loop lag < 50ms under 10k RPS load."""
    
    @pytest.mark.asyncio
    async def test_event_loop_lag_under_load(self):
        """Test event loop lag < 50ms during 10k spans/sec enqueue."""
        sdk = ObservabilitySDK(exporters=[], enabled=True, ring_buffer_maxsize=100_000)
        
        # Monitor event loop lag
        lag_measurements = []
        lag_task = asyncio.create_task(_monitor_loop_lag(lag_measurements))
        
        # Generate and enqueue 10,000 spans
        spans = [_make_span(name=f"span-{i}") for i in range(10_000)]
        
        enqueue_start = time.perf_counter()
        for span in spans:
            await sdk._enqueue(span)
        enqueue_end = time.perf_counter()
        
        # Stop lag monitoring
        lag_task.cancel()
        try:
            await lag_task
        except asyncio.CancelledError:
            pass
        
        # Calculate metrics
        total_time = enqueue_end - enqueue_start
        rps = 10_000 / total_time
        max_lag = max(lag_measurements) if lag_measurements else 0
        mean_lag = statistics.mean(lag_measurements) if lag_measurements else 0
        
        print(f"Enqueue rate: {rps:.0f} spans/sec")
        print(f"Total time: {total_time:.3f}s")
        print(f"Max event loop lag: {max_lag:.3f}ms")
        print(f"Mean event loop lag: {mean_lag:.3f}ms")
        
        # Assert max lag < 50ms
        assert max_lag < 50.0, f"Max loop lag {max_lag:.3f}ms >= 50ms"
        
        # Cleanup
        await sdk.shutdown()


async def _monitor_loop_lag(lag_measurements: List[float], interval: float = 0.01):
    """Monitor event loop lag by measuring drift in scheduled tasks."""
    last_scheduled = time.perf_counter()
    
    while True:
        try:
            # Schedule a zero-delay task
            scheduled = time.perf_counter()
            await asyncio.sleep(0)
            executed = time.perf_counter()
            
            # Calculate lag (difference between scheduled and actual execution)
            lag = (executed - scheduled) * 1000  # Convert to ms
            lag_measurements.append(lag)
            
            # Wait for next interval
            next_scheduled = last_scheduled + interval
            now = time.perf_counter()
            if next_scheduled > now:
                await asyncio.sleep(next_scheduled - now)
            last_scheduled = max(next_scheduled, now)
        except asyncio.CancelledError:
            break


class TestBackendFailureScenario:
    """Test behavior when backend is unavailable (simulated with long delay)."""
    
    @pytest.mark.asyncio
    async def test_backend_failure_agent_stays_alive(self):
        """Test that agent continues working when backend is unavailable."""
        slow_exporter = SlowExporter(delay=3600.0)  # 1 hour delay
        sdk = ObservabilitySDK(
            exporters=[slow_exporter], 
            enabled=True, 
            ring_buffer_maxsize=100_000
        )
        
        @sdk.agent_observed("resilient-agent")
        async def resilient_agent(_obs_ctx=None):
            """Agent that should continue working even with failing backend."""
            return f"result-{time.time()}"
        
        # Run agent many times (should not fail even though exporter is hanging)
        successful_calls = 0
        for i in range(200):
            try:
                result = await resilient_agent()
                assert result is not None
                successful_calls += 1
            except Exception as e:
                pytest.fail(f"Agent call failed: {e}")
        
        # Verify all calls succeeded
        assert successful_calls == 200
        
        # Check that some spans were dropped (buffer overflow)
        from agent_obs.metrics import dropped_spans_total
        
        # Get initial dropped count (we'll use a mock approach)
        initial_drops = dropped_spans_total._value._value if hasattr(dropped_spans_total, '_value') else 0
        
        # Enqueue more spans than buffer can hold to force drops
        overflow_spans = [_make_span(name=f"overflow-{i}") for i in range(150_000)]
        
        for span in overflow_spans:
            await sdk._enqueue(span)
        
        # Check that some spans were actually dropped
        # If we enqueued 150k spans and buffer is 100k, we should have 50k drops
        total_enqueued = 150_000
        # last_spans contains only successfully enqueued spans
        successfully_enqueued = len(sdk.last_spans)
        actual_drops = total_enqueued - successfully_enqueued
        
        print(f"Total enqueued: {total_enqueued}")
        print(f"Successfully enqueued: {successfully_enqueued}")
        print(f"Actual drops: {actual_drops}")
        
        # Should have dropped some spans (buffer size is 100k)
        assert actual_drops > 0, f"Expected drops > 0, got {actual_drops}"
        assert actual_drops >= 50_000, f"Expected at least 50k drops, got {actual_drops}"
        
        print(f"Dropped spans: {actual_drops}")
        
        # Should have dropped some spans
        assert actual_drops > 0
        
        # Cleanup (will timeout but should not crash)
        await sdk.shutdown()


class TestAgentUnderBackendFailure:
    """Test that business logic continues when backend fails."""
    
    @pytest.mark.asyncio
    async def test_business_logic_continues(self):
        """Test that business logic continues despite backend failure."""
        slow_exporter = SlowExporter(delay=3600.0)
        sdk = ObservabilitySDK(
            exporters=[slow_exporter], 
            enabled=True, 
            ring_buffer_maxsize=100_000
        )
        
        @sdk.agent_observed("business-agent")
        async def business_agent(_obs_ctx=None):
            """Simulate real business logic."""
            # Simulate some work
            await asyncio.sleep(0.001)
            return f"business-result-{time.time()}"
        
        # Run many agent calls - all should succeed
        results = []
        start_time = time.perf_counter()
        
        for i in range(1000):
            result = await business_agent()
            results.append(result)
            
            # Check if we've hit buffer overflow (after ~100k spans)
            if i > 100_000:
                from agent_obs.metrics import dropped_spans_total
                if dropped_spans_total._value._value > 0:
                    print(f"Buffer overflow detected at iteration {i}")
                    break
        
        end_time = time.perf_counter()
        total_time = end_time - start_time
        
        print(f"Completed {len(results)} business calls in {total_time:.3f}s")
        print(f"Success rate: {len(results)/1000*100:.1f}%")
        
        # All calls should have succeeded
        assert len(results) == 1000
        
        # Verify results are unique (agent was actually called)
        unique_results = set(results)
        assert len(unique_results) == 1000
        
        # Cleanup
        await sdk.shutdown()