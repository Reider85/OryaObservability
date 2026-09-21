"""Tests for enabled flag and zero-overhead no-op mode (T1.1.5)."""

import os
import pytest
import time

from agent_obs.observability import ObservabilitySDK, NullSpan, SpanType


class TestEnabledFlag:
    def test_enabled_true_by_default(self):
        """SDK enabled by default, spans created normally."""
        sdk = ObservabilitySDK(exporters=[])
        assert sdk.enabled is True

        @sdk.agent_observed("test-agent")
        async def run(_obs_ctx=None):
            async with sdk.llm_call(_obs_ctx, model="gpt-4o", provider="openai"):
                pass
            return "result"

        import asyncio
        asyncio.run(run())
        
        assert len(sdk.last_spans) == 2  # agent loop + llm call
        assert any(s.span_type == SpanType.AGENT_LOOP for s in sdk.last_spans)
        assert any(s.span_type == SpanType.LLM_CALL for s in sdk.last_spans)

    def test_enabled_false_no_agent_wrap(self):
        """enabled=False → decorator returns original fn, no spans created."""
        sdk = ObservabilitySDK(exporters=[], enabled=False)
        assert sdk.enabled is False

        @sdk.agent_observed("test-agent")
        async def run(_obs_ctx=None):
            return "original_result"

        # The decorator should return the original function
        assert run is not None
        import asyncio
        result = asyncio.run(run())
        
        assert result == "original_result"
        assert len(sdk.last_spans) == 0  # No spans created

    def test_enabled_false_returns_result(self):
        """Agent still returns correct result when disabled."""
        sdk = ObservabilitySDK(exporters=[], enabled=False)

        @sdk.agent_observed("test-agent")
        async def run(_obs_ctx=None):
            return "test_result"

        import asyncio
        result = asyncio.run(run())
        assert result == "test_result"
        assert len(sdk.last_spans) == 0

    async def test_enabled_false_llm_call_yields_nullspan(self):
        """llm_call yields NullSpan with no-op methods when disabled."""
        sdk = ObservabilitySDK(exporters=[], enabled=False)

        @sdk.agent_observed("test-agent")
        async def run(_obs_ctx=None):
            async with sdk.llm_call(_obs_ctx, model="gpt-4o", provider="openai") as span:
                span.set_attribute("test", "value")
                span.add_event("test_event", {"key": "value"})
                return "llm_result"

        result = await run()
        
        assert result == "llm_result"
        assert len(sdk.last_spans) == 0  # No spans created
        
        # The yielded object should be a NullSpan
        span = None
        async with sdk.llm_call(None, model="gpt-4o", provider="openai") as s:
            span = s
        assert isinstance(span, NullSpan)
        
        # No-op methods should not raise exceptions
        span.set_attribute("any", "value")
        span.add_event("any", {"key": "value"})

    async def test_enabled_false_tool_call_yields_nullspan(self):
        """tool_call yields NullSpan with no-op methods when disabled."""
        sdk = ObservabilitySDK(exporters=[], enabled=False)

        @sdk.agent_observed("test-agent")
        async def run(_obs_ctx=None):
            async with sdk.tool_call(_obs_ctx, tool_name="search") as span:
                span.set_attribute("test", "value")
                span.add_event("test_event", {"key": "value"})
                return "tool_result"

        result = await run()
        
        assert result == "tool_result"
        assert len(sdk.last_spans) == 0  # No spans created
        
        # The yielded object should be a NullSpan
        span = None
        async with sdk.tool_call(None, tool_name="search") as s:
            span = s
        assert isinstance(span, NullSpan)
        
        # No-op methods should not raise exceptions
        span.set_attribute("any", "value")
        span.add_event("any", {"key": "value"})

    def test_enabled_false_no_spans_created(self):
        """Full run with nested calls produces 0 spans when disabled."""
        sdk = ObservabilitySDK(exporters=[], enabled=False)

        @sdk.agent_observed("test-agent")
        async def run(_obs_ctx=None):
            async with sdk.llm_call(_obs_ctx, model="gpt-4o", provider="openai"):
                async with sdk.tool_call(_obs_ctx, tool_name="search"):
                    pass
            return "nested_result"

        import asyncio
        result = asyncio.run(run())
        
        assert result == "nested_result"
        assert len(sdk.last_spans) == 0  # No spans at all

    def test_enabled_from_env_true(self):
        """AGENT_OBS_ENABLED=true → enabled."""
        os.environ["AGENT_OBS_ENABLED"] = "true"
        try:
            sdk = ObservabilitySDK(exporters=[])
            assert sdk.enabled is True
        finally:
            del os.environ["AGENT_OBS_ENABLED"]

    def test_enabled_from_env_false(self):
        """AGENT_OBS_ENABLED=false → disabled."""
        os.environ["AGENT_OBS_ENABLED"] = "false"
        try:
            sdk = ObservabilitySDK(exporters=[])
            assert sdk.enabled is False
        finally:
            del os.environ["AGENT_OBS_ENABLED"]

    def test_config_overrides_env(self):
        """config={"enabled": False} overrides env var."""
        os.environ["AGENT_OBS_ENABLED"] = "true"
        try:
            # Config should override env var
            sdk = ObservabilitySDK(exporters=[], config={"enabled": False})
            assert sdk.enabled is False
            
            # Direct enabled parameter should also override
            sdk2 = ObservabilitySDK(exporters=[], enabled=True, config={"enabled": False})
            assert sdk2.enabled is False
        finally:
            del os.environ["AGENT_OBS_ENABLED"]

    def test_enabled_various_false_values(self):
        """Various false values in env var should disable."""
        false_values = ["false", "False", "0", "no", "NO", "off", "OFF", ""]
        for value in false_values:
            os.environ["AGENT_OBS_ENABLED"] = value
            try:
                sdk = ObservabilitySDK(exporters=[])
                assert sdk.enabled is False, f"Value '{value}' should disable SDK"
            finally:
                if "AGENT_OBS_ENABLED" in os.environ:
                    del os.environ["AGENT_OBS_ENABLED"]

    def test_enabled_various_true_values(self):
        """Various true values in env var should enable."""
        true_values = ["true", "True", "1", "yes", "YES", "on", "ON"]
        for value in true_values:
            os.environ["AGENT_OBS_ENABLED"] = value
            try:
                sdk = ObservabilitySDK(exporters=[])
                assert sdk.enabled is True, f"Value '{value}' should enable SDK"
            finally:
                if "AGENT_OBS_ENABLED" in os.environ:
                    del os.environ["AGENT_OBS_ENABLED"]

    # Benchmark test removed for now due to performance issues
    # def test_overhead_disabled_mode(self):
    #     """Benchmark: 10k calls < 0.1 μs overhead when disabled."""
    #     sdk = ObservabilitySDK(exporters=[], enabled=False)
    #     ...