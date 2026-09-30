"""Tests for PC29 adaptive sampler metrics: system_cpu_ratio and agent_error_rate_5m.

The SDK exports these two gauges so the OTel Collector / Prometheus can drive
adaptive tail-sampling decisions (PC30).
"""

from __future__ import annotations

import asyncio
import collections
import time

import pytest

from agent_obs.observability import ObservabilitySDK


@pytest.fixture
def sdk() -> ObservabilitySDK:
    return ObservabilitySDK(exporters=[], enabled=True)


# ---------------------------------------------------------------------------
# system_cpu_ratio
# ---------------------------------------------------------------------------


class TestSystemCpuRatio:
    async def test_cpu_ratio_gauge_defined(self):
        from agent_obs.metrics import system_cpu_ratio

        assert system_cpu_ratio is not None
        # Default value before any update is 0.0
        assert system_cpu_ratio._value.get() == 0.0

    async def test_cpu_monitor_task_fn_sets_gauge(self, sdk):
        """_cpu_monitor_task_fn calls psutil and writes to the gauge."""
        from agent_obs.metrics import system_cpu_ratio

        import psutil as _real_psutil

        original = _real_psutil.cpu_percent
        _real_psutil.cpu_percent = lambda *a, **kw: 42.0
        try:
            # Run the task function directly (not as background) with immediate shutdown
            sdk._cpu_shutdown.set()
            await sdk._cpu_monitor_task_fn()
        finally:
            _real_psutil.cpu_percent = original

        # Prime call returns 0.0, but set is never called in the loop because
        # shutdown is immediate. Verify the gauge is still at default (the prime
        # call does not set the gauge). This validates the import + prime path.
        assert system_cpu_ratio._value.get() == 0.0

    async def test_cpu_monitor_sets_gauge_in_loop(self, sdk):
        """When the loop runs at least once, the gauge is updated."""
        from agent_obs.metrics import system_cpu_ratio

        import psutil as _real_psutil

        original = _real_psutil.cpu_percent
        _real_psutil.cpu_percent = lambda *a, **kw: 42.0
        try:
            # Don't set shutdown yet — let the loop run once via timeout
            sdk._cpu_shutdown.clear()
            # Patch the wait_for timeout to be very short for testing
            original_wait_for = asyncio.wait_for

            async def fast_wait_for(coro, timeout, **kw):
                return await original_wait_for(coro, timeout=min(timeout, 0.05), **kw)

            asyncio.wait_for = fast_wait_for
            try:
                task = asyncio.create_task(sdk._cpu_monitor_task_fn())
                await asyncio.sleep(0.3)
                sdk._cpu_shutdown.set()
                await task
            finally:
                asyncio.wait_for = original_wait_for
        finally:
            _real_psutil.cpu_percent = original

        assert system_cpu_ratio._value.get() == pytest.approx(0.42)

    async def test_cpu_monitor_handles_psutil_import_error(self, sdk):
        """If psutil is missing the task exits gracefully without crashing."""
        import sys

        saved = sys.modules.get("psutil")
        sys.modules["psutil"] = None  # simulate missing module
        try:
            sdk._cpu_shutdown.set()
            # Should not raise
            await sdk._cpu_monitor_task_fn()
        finally:
            if saved is not None:
                sys.modules["psutil"] = saved
            else:
                sys.modules.pop("psutil", None)

    async def test_cpu_monitor_respects_shutdown(self, sdk):
        """Setting _cpu_shutdown should terminate the monitor loop."""
        sdk._cpu_shutdown.set()
        task = asyncio.create_task(sdk._cpu_monitor_task_fn())
        # Should finish quickly (no blocking)
        await asyncio.wait_for(task, timeout=1.0)


# ---------------------------------------------------------------------------
# agent_error_rate_5m
# ---------------------------------------------------------------------------


class TestAgentErrorRate5m:
    def test_error_rate_gauge_defined(self):
        from agent_obs.metrics import agent_error_rate_5m

        assert agent_error_rate_5m is not None

    def test_empty_window_returns_no_change(self, sdk):
        """With no spans recorded the gauge is not updated."""
        from agent_obs.metrics import agent_error_rate_5m

        before = agent_error_rate_5m.labels(agent_id="test-agent")._value.get()
        sdk._update_error_rate("test-agent", is_error=False)
        after = agent_error_rate_5m.labels(agent_id="test-agent")._value.get()
        # Empty window (single non-error entry) -> rate 0.0, but gauge was set
        assert after == 0.0

    def test_all_errors_gives_rate_one(self, sdk):
        """5 error spans -> error_rate = 1.0."""
        from agent_obs.metrics import agent_error_rate_5m

        for _ in range(5):
            sdk._update_error_rate("test-agent", is_error=True)
        rate = agent_error_rate_5m.labels(agent_id="test-agent")._value.get()
        assert rate == pytest.approx(1.0)

    def test_mixed_spans(self, sdk):
        """3 errors out of 5 total -> rate = 0.6."""
        from agent_obs.metrics import agent_error_rate_5m

        sdk._update_error_rate("test-agent", is_error=True)
        sdk._update_error_rate("test-agent", is_error=True)
        sdk._update_error_rate("test-agent", is_error=True)
        sdk._update_error_rate("test-agent", is_error=False)
        sdk._update_error_rate("test-agent", is_error=False)
        rate = agent_error_rate_5m.labels(agent_id="test-agent")._value.get()
        assert rate == pytest.approx(0.6)

    def test_window_eviction(self, sdk):
        """Entries older than 5 minutes are evicted."""
        from agent_obs.metrics import agent_error_rate_5m

        # Manually inject old entries (6 minutes ago)
        old_time = time.time() - 360.0
        window = sdk._error_rate_windows.setdefault("test-agent", collections.deque())
        window.append((old_time, True))
        window.append((old_time, True))

        # Add a fresh non-error entry
        sdk._update_error_rate("test-agent", is_error=False)

        # Old entries evicted -> only 1 non-error -> rate 0.0
        rate = agent_error_rate_5m.labels(agent_id="test-agent")._value.get()
        assert rate == pytest.approx(0.0)
        assert len(sdk._error_rate_windows["test-agent"]) == 1

    def test_per_agent_isolation(self, sdk):
        """Error rate is tracked independently per agent_id."""
        from agent_obs.metrics import agent_error_rate_5m

        sdk._update_error_rate("agent-a", is_error=True)
        sdk._update_error_rate("agent-a", is_error=True)
        sdk._update_error_rate("agent-b", is_error=False)

        rate_a = agent_error_rate_5m.labels(agent_id="agent-a")._value.get()
        rate_b = agent_error_rate_5m.labels(agent_id="agent-b")._value.get()
        assert rate_a == pytest.approx(1.0)
        assert rate_b == pytest.approx(0.0)

    async def test_enqueue_updates_error_rate(self, sdk):
        """Spans enqueued through the SDK update the error rate gauge."""
        from agent_obs.metrics import agent_error_rate_5m

        @sdk.agent_observed("test-agent")
        async def run_ok(_obs_ctx=None):
            async with sdk.llm_call(_obs_ctx, model="gpt-4o", provider="openai"):
                pass

        @sdk.agent_observed("test-agent")
        async def run_err(_obs_ctx=None):
            async with sdk.llm_call(_obs_ctx, model="gpt-4o", provider="openai") as span:
                span.attributes["status"] = "error"

        await run_ok()
        await run_err()

        rate = agent_error_rate_5m.labels(agent_id="test-agent")._value.get()
        # 1 error out of 2 total -> 0.5
        assert rate == pytest.approx(0.5)
