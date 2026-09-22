"""Tests for deterministic content sampling (P22)."""

import os
import hashlib

import pytest

from agent_obs.content_sampling import (
    attach_llm_content,
    attach_tool_content,
    content_rate_from_env,
    should_keep_content,
)
from agent_obs.observability import ObservabilitySDK, Span, SpanContext, SpanType, _now


def _make_span(
    trace_id: str,
    agent_id: str = "test-agent",
    span_type: SpanType = SpanType.LLM_CALL,
) -> Span:
    ctx = SpanContext.new(agent_id=agent_id)
    ctx.trace_id = trace_id
    return Span(
        name="test",
        span_type=span_type,
        context=ctx,
        start_time=_now(),
        end_time=_now(),
        attributes={"status": "ok"},
    )


class TestShouldKeepContent:
    def test_deterministic_for_same_trace(self) -> None:
        assert should_keep_content("trace-a", 10) == should_keep_content("trace-a", 10)

    def test_consistent_within_trace_all_spans_agree(self) -> None:
        verdict = should_keep_content("trace-xyz", 10)
        for _ in range(5):
            assert should_keep_content("trace-xyz", 10) == verdict

    def test_rate_zero_never_keeps(self) -> None:
        for i in range(50):
            assert should_keep_content(f"t-{i}", 0) is False

    def test_rate_100_always_keeps(self) -> None:
        for i in range(50):
            assert should_keep_content(f"t-{i}", 100) is True

    def test_approx_rate_respected(self) -> None:
        kept = sum(should_keep_content(f"t-{i}", 10) for i in range(500))
        assert 30 <= kept <= 70  # ~10% of 500


class TestAttachLlmContent:
    def test_sampled_trace_keeps_full_text(self) -> None:
        span = _make_span("keep-trace")
        attach_llm_content(
            span, input_text="hello", output_text="world", rate=100
        )
        assert span.attributes["trace.content_sampled"] is True
        assert span.attributes["llm.input_text"] == "hello"
        assert span.attributes["llm.output_text"] == "world"
        assert "llm.input_sha256" not in span.attributes

    def test_unsampled_trace_keeps_hash_and_chars(self) -> None:
        span = _make_span("drop-trace")
        attach_llm_content(
            span, input_text="hello world", output_text="answer", rate=0
        )
        assert span.attributes["trace.content_sampled"] is False
        assert "llm.input_text" not in span.attributes
        assert "llm.output_text" not in span.attributes
        assert span.attributes["llm.input_sha256"] == hashlib.sha256(b"hello world").hexdigest()
        assert span.attributes["llm.input_chars"] == 11
        assert span.attributes["llm.output_chars"] == 6

    def test_no_text_no_attributes(self) -> None:
        span = _make_span("n/a")
        attach_llm_content(span, rate=100)
        assert "trace.content_sampled" not in span.attributes


class TestAttachToolContent:
    def test_full_text_never_stored_even_when_sampled(self) -> None:
        span = _make_span("tool-trace")
        long_input = "x" * 500
        attach_tool_content(span, input_text=long_input, rate=100)
        assert "tool.input_text" not in span.attributes
        assert span.attributes["tool.input_summary"].endswith("...")
        assert len(span.attributes["tool.input_summary"]) <= 200 + 3
        assert span.attributes["tool.input_chars"] == 500
        assert len(span.attributes["tool.input_sha256"]) == 64

    def test_short_input_summary_is_exact(self) -> None:
        span = _make_span("tool-trace-2")
        attach_tool_content(span, input_text="short", rate=100)
        assert span.attributes["tool.input_summary"] == "short"
        assert not span.attributes["tool.input_summary"].endswith("...")


class TestRateFromEnv:
    def test_default_rate(self, monkeypatch) -> None:
        monkeypatch.delenv("AGENT_OBS_CONTENT_RATE", raising=False)
        assert content_rate_from_env() == 10

    def test_env_rate(self, monkeypatch) -> None:
        monkeypatch.setenv("AGENT_OBS_CONTENT_RATE", "25")
        assert content_rate_from_env() == 25

    def test_invalid_rate_falls_back(self, monkeypatch) -> None:
        monkeypatch.setenv("AGENT_OBS_CONTENT_RATE", "abc")
        assert content_rate_from_env() == 10


class TestSdkIntegration:
    async def test_llm_call_attaches_content(self, monkeypatch) -> None:
        monkeypatch.setenv("AGENT_OBS_CONTENT_RATE", "100")
        sdk = ObservabilitySDK(exporters=[], enabled=True)

        @sdk.agent_observed("agent")
        async def run(_obs_ctx=None):
            async with sdk.llm_call(
                _obs_ctx,
                model="gpt-4o",
                provider="openai",
                input_text="prompt",
                output_text="response",
            ) as span:
                return span

        inner = await run()
        assert inner.attributes["llm.input_text"] == "prompt"
        assert inner.attributes["llm.output_text"] == "response"

    async def test_tool_call_caps_summary(self, monkeypatch) -> None:
        monkeypatch.setenv("AGENT_OBS_CONTENT_RATE", "100")
        sdk = ObservabilitySDK(exporters=[], enabled=True)

        @sdk.agent_observed("agent")
        async def run(_obs_ctx=None):
            async with sdk.tool_call(
                _obs_ctx, tool_name="search", input_text="z" * 500
            ) as span:
                return span

        inner = await run()
        assert "tool.input_text" not in inner.attributes
        assert inner.attributes["tool.input_chars"] == 500