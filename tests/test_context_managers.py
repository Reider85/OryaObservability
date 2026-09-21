"""Tests for llm_call and tool_call context managers (T1.1.4)."""

import pytest

from agent_obs.observability import ObservabilitySDK, SpanType, _active_span_context


class TestLlmCall:
    async def test_basic_llm_call(self):
        sdk = ObservabilitySDK(exporters=[])

        @sdk.agent_observed("test-agent")
        async def run(_obs_ctx=None):
            async with sdk.llm_call(_obs_ctx, model="gpt-4o", provider="openai") as span:
                return span

        await run()
        llm_spans = [s for s in sdk.last_spans if s.span_type == SpanType.LLM_CALL]
        assert len(llm_spans) == 1
        span = llm_spans[0]
        assert span.name == "llm.call:gpt-4o"
        assert span.attributes["llm.model"] == "gpt-4o"
        assert span.attributes["llm.provider"] == "openai"
        assert span.end_time is not None
        assert span.start_time > 0

    async def test_inherits_trace_id(self):
        sdk = ObservabilitySDK(exporters=[])

        @sdk.agent_observed("test-agent")
        async def run(_obs_ctx=None):
            async with sdk.llm_call(_obs_ctx, model="gpt-4o", provider="openai") as span:
                return span

        await run()
        agent_span = [s for s in sdk.last_spans if s.span_type == SpanType.AGENT_LOOP][0]
        llm_span = [s for s in sdk.last_spans if s.span_type == SpanType.LLM_CALL][0]
        assert llm_span.context.trace_id == agent_span.context.trace_id

    async def test_parent_span_id_is_agent_span(self):
        sdk = ObservabilitySDK(exporters=[])

        @sdk.agent_observed("test-agent")
        async def run(_obs_ctx=None):
            async with sdk.llm_call(_obs_ctx, model="gpt-4o", provider="openai") as span:
                return span

        await run()
        agent_span = [s for s in sdk.last_spans if s.span_type == SpanType.AGENT_LOOP][0]
        llm_span = [s for s in sdk.last_spans if s.span_type == SpanType.LLM_CALL][0]
        assert llm_span.context.parent_span_id == agent_span.context.span_id

    async def test_exception_in_llm_call_closes_span(self):
        sdk = ObservabilitySDK(exporters=[])

        @sdk.agent_observed("test-agent")
        async def run(_obs_ctx=None):
            async with sdk.llm_call(_obs_ctx, model="gpt-4o", provider="openai") as span:
                raise RuntimeError("LLM error")

        with pytest.raises(RuntimeError, match="LLM error"):
            await run()
        llm_spans = [s for s in sdk.last_spans if s.span_type == SpanType.LLM_CALL]
        assert len(llm_spans) == 1
        span = llm_spans[0]
        assert span.end_time is not None
        assert span.end_time >= span.start_time

    async def test_multiple_llm_calls_same_trace(self):
        sdk = ObservabilitySDK(exporters=[])

        @sdk.agent_observed("test-agent")
        async def run(_obs_ctx=None):
            async with sdk.llm_call(_obs_ctx, model="gpt-4o", provider="openai") as s1:
                pass
            async with sdk.llm_call(_obs_ctx, model="gpt-4o-mini", provider="openai") as s2:
                pass
            return "done"

        await run()
        llm_spans = [s for s in sdk.last_spans if s.span_type == SpanType.LLM_CALL]
        assert len(llm_spans) == 2
        assert llm_spans[0].context.trace_id == llm_spans[1].context.trace_id
        assert llm_spans[0].context.span_id != llm_spans[1].context.span_id


class TestToolCall:
    async def test_basic_tool_call(self):
        sdk = ObservabilitySDK(exporters=[])

        @sdk.agent_observed("test-agent")
        async def run(_obs_ctx=None):
            async with sdk.tool_call(_obs_ctx, tool_name="search") as span:
                return span

        await run()
        tool_spans = [s for s in sdk.last_spans if s.span_type == SpanType.TOOL_CALL]
        assert len(tool_spans) == 1
        span = tool_spans[0]
        assert span.name == "tool.call:search"
        assert span.attributes["tool.name"] == "search"
        assert span.end_time is not None

    async def test_inherits_trace_id(self):
        sdk = ObservabilitySDK(exporters=[])

        @sdk.agent_observed("test-agent")
        async def run(_obs_ctx=None):
            async with sdk.tool_call(_obs_ctx, tool_name="search") as span:
                return span

        await run()
        agent_span = [s for s in sdk.last_spans if s.span_type == SpanType.AGENT_LOOP][0]
        tool_span = [s for s in sdk.last_spans if s.span_type == SpanType.TOOL_CALL][0]
        assert tool_span.context.trace_id == agent_span.context.trace_id

    async def test_parent_span_id_is_agent_span(self):
        sdk = ObservabilitySDK(exporters=[])

        @sdk.agent_observed("test-agent")
        async def run(_obs_ctx=None):
            async with sdk.tool_call(_obs_ctx, tool_name="search") as span:
                return span

        await run()
        agent_span = [s for s in sdk.last_spans if s.span_type == SpanType.AGENT_LOOP][0]
        tool_span = [s for s in sdk.last_spans if s.span_type == SpanType.TOOL_CALL][0]
        assert tool_span.context.parent_span_id == agent_span.context.span_id

    async def test_exception_in_tool_call_closes_span(self):
        sdk = ObservabilitySDK(exporters=[])

        @sdk.agent_observed("test-agent")
        async def run(_obs_ctx=None):
            async with sdk.tool_call(_obs_ctx, tool_name="search") as span:
                raise ValueError("tool failed")

        with pytest.raises(ValueError, match="tool failed"):
            await run()
        tool_spans = [s for s in sdk.last_spans if s.span_type == SpanType.TOOL_CALL]
        assert len(tool_spans) == 1
        span = tool_spans[0]
        assert span.end_time is not None
        assert span.end_time >= span.start_time


class TestNesting:
    async def test_llm_call_inside_agent_observed(self):
        sdk = ObservabilitySDK(exporters=[])

        @sdk.agent_observed("test-agent")
        async def run(_obs_ctx=None):
            async with sdk.llm_call(_obs_ctx, model="gpt-4o", provider="openai") as span:
                return span

        await run()
        assert len(sdk.last_spans) == 2
        agent_span = [s for s in sdk.last_spans if s.span_type == SpanType.AGENT_LOOP][0]
        llm_span = [s for s in sdk.last_spans if s.span_type == SpanType.LLM_CALL][0]
        assert llm_span.context.trace_id == agent_span.context.trace_id
        assert llm_span.context.parent_span_id == agent_span.context.span_id

    async def test_llm_and_tool_in_same_agent_loop(self):
        sdk = ObservabilitySDK(exporters=[])

        @sdk.agent_observed("test-agent")
        async def run(_obs_ctx=None):
            async with sdk.llm_call(_obs_ctx, model="gpt-4o", provider="openai") as llm_span:
                pass
            async with sdk.tool_call(_obs_ctx, tool_name="search") as tool_span:
                pass
            return "done"

        await run()
        assert len(sdk.last_spans) == 3
        agent_span = [s for s in sdk.last_spans if s.span_type == SpanType.AGENT_LOOP][0]
        llm_span = [s for s in sdk.last_spans if s.span_type == SpanType.LLM_CALL][0]
        tool_span = [s for s in sdk.last_spans if s.span_type == SpanType.TOOL_CALL][0]
        assert llm_span.context.trace_id == agent_span.context.trace_id
        assert tool_span.context.trace_id == agent_span.context.trace_id
        assert llm_span.context.parent_span_id == agent_span.context.span_id
        assert tool_span.context.parent_span_id == agent_span.context.span_id

    async def test_contextvar_cleaned_up_after_exit(self):
        sdk = ObservabilitySDK(exporters=[])

        @sdk.agent_observed("test-agent")
        async def run(_obs_ctx=None):
            async with sdk.llm_call(_obs_ctx, model="gpt-4o", provider="openai"):
                pass

        await run()
        assert _active_span_context.get() is None

    async def test_nested_llm_calls(self):
        sdk = ObservabilitySDK(exporters=[])

        @sdk.agent_observed("test-agent")
        async def run(_obs_ctx=None):
            async with sdk.llm_call(_obs_ctx, model="gpt-4o", provider="openai") as outer:
                async with sdk.llm_call(_obs_ctx, model="gpt-4o", provider="openai") as inner:
                    return inner

        await run()
        assert len(sdk.last_spans) == 3
        agent_span = [s for s in sdk.last_spans if s.span_type == SpanType.AGENT_LOOP][0]
        llm_spans = [s for s in sdk.last_spans if s.span_type == SpanType.LLM_CALL]
        assert len(llm_spans) == 2
        outer_llm = [s for s in llm_spans if s.context.parent_span_id == agent_span.context.span_id][0]
        inner_llm = [s for s in llm_spans if s.context.parent_span_id == outer_llm.context.span_id][0]
        assert inner_llm.context.trace_id == agent_span.context.trace_id
        assert outer_llm.context.trace_id == agent_span.context.trace_id
        assert outer_llm.context.span_id != inner_llm.context.span_id
