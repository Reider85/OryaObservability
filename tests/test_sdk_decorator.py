"""Tests for ObservabilitySDK and the @agent_observed decorator (T1.1.3)."""

import pytest

from agent_obs.observability import ObservabilitySDK, SpanContext, SpanType, _obs_user_id, _obs_session_id


class TestBuildContext:
    def test_build_context_creates_root(self):
        sdk = ObservabilitySDK(exporters=[])
        ctx = sdk._build_context(agent_id="my-agent", version="1.2.3")
        assert isinstance(ctx, SpanContext)
        assert ctx.agent_id == "my-agent"
        assert ctx.agent_version == "1.2.3"
        assert ctx.parent_span_id is None
        assert ctx.trace_id
        assert ctx.span_id

    def test_build_context_explicit_user_session(self):
        sdk = ObservabilitySDK(exporters=[])
        ctx = sdk._build_context(
            agent_id="my-agent",
            user_id="u-1",
            session_id="s-1",
        )
        assert ctx.user_id == "u-1"
        assert ctx.session_id == "s-1"

    def test_build_context_falls_back_to_contextvars(self):
        sdk = ObservabilitySDK(exporters=[])
        token_user = _obs_user_id.set("u-cv")
        token_session = _obs_session_id.set("s-cv")
        try:
            ctx = sdk._build_context(agent_id="my-agent")
        finally:
            _obs_user_id.reset(token_user)
            _obs_session_id.reset(token_session)
        assert ctx.user_id == "u-cv"
        assert ctx.session_id == "s-cv"


class TestAgentObserved:
    async def test_success_status_ok(self):
        sdk = ObservabilitySDK(exporters=[])

        @sdk.agent_observed("my-agent", version="1.0.0")
        async def run(_obs_ctx=None):
            return "result"

        result = await run()
        assert result == "result"
        assert len(sdk.last_spans) == 1
        span = sdk.last_spans[0]
        assert span.span_type == SpanType.AGENT_LOOP
        assert span.name == "agent.loop:my-agent"
        assert span.context.agent_id == "my-agent"
        assert span.context.agent_version == "1.0.0"
        assert span.attributes["status"] == "ok"
        assert span.end_time is not None

    async def test_exception_marked_error_and_reraised(self):
        sdk = ObservabilitySDK(exporters=[])

        @sdk.agent_observed("my-agent")
        async def run(_obs_ctx=None):
            raise ValueError("boom")

        with pytest.raises(ValueError, match="boom"):
            await run()
        assert len(sdk.last_spans) == 1
        span = sdk.last_spans[0]
        assert span.attributes["status"] == "error"
        assert span.attributes["error.type"] == "ValueError"
        assert span.end_time is not None

    async def test_obs_ctx_passed_to_function(self):
        sdk = ObservabilitySDK(exporters=[])
        received = {}

        @sdk.agent_observed("my-agent")
        async def run(_obs_ctx=None, **kwargs):
            received["ctx"] = _obs_ctx
            return "ok"

        await run(foo="bar")
        assert received["ctx"].trace_id == sdk.last_spans[0].context.trace_id

    async def test_span_timing_recorded(self):
        sdk = ObservabilitySDK(exporters=[])

        @sdk.agent_observed("my-agent")
        async def run(_obs_ctx=None):
            return "ok"

        await run()
        span = sdk.last_spans[0]
        assert span.start_time > 0
        assert span.end_time >= span.start_time
        assert span.duration_ms >= 0

    async def test_apply_to_class_method(self):
        sdk = ObservabilitySDK(exporters=[])

        class Agent:
            @sdk.agent_observed("class-agent")
            async def run(self, _obs_ctx=None):
                return "class-ok"

        agent = Agent()
        result = await agent.run()
        assert result == "class-ok"
        assert len(sdk.last_spans) == 1
        assert sdk.last_spans[0].context.agent_id == "class-agent"