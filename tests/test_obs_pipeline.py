"""PP41: ObsPipeline — span enrichment on the LLM proxy.

Unit tests for request/response attribute extraction (tokens, finish_reason,
ttft/latency timing), streaming usage aggregation, cost computation through
the price book, and content-sampling of proxy bodies.
"""

from __future__ import annotations

import json
import logging

import pytest

from agent_obs.cost import PriceBook, Usage, compute_cost
from agent_obs.cost.price_book import ModelPricing, PriceEntry
from agent_obs.observability import Span, SpanContext, SpanType
from agent_obs.proxy import ProxyConfig
from agent_obs.proxy.obs_pipeline import (
    ObsPipeline,
    Timing,
    iter_sse_json,
    parse_sse_chunk,
)

_TEST_BOOK = PriceBook(
    version="test-1",
    currency="USD",
    models={
        "gpt-4o": ModelPricing(
            model="gpt-4o",
            prices=[
                PriceEntry(
                    input_per_1k=0.0025,
                    output_per_1k=0.01,
                    cached_input_per_1k=0.00125,
                )
            ],
        )
    },
)

MESSAGES = [{"role": "user", "content": "hello"}]
REQUEST = json.dumps(
    {"model": "gpt-4o", "messages": MESSAGES, "temperature": 0.2, "stream": False}
).encode("utf-8")
STREAM_REQUEST = json.dumps(
    {"model": "gpt-4o", "messages": MESSAGES, "stream": True}
).encode("utf-8")
RESPONSE = json.dumps(
    {
        "choices": [
            {"message": {"content": "hi there"}, "finish_reason": "stop"}
        ],
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": 4,
            "prompt_tokens_details": {"cached_tokens": 2},
        },
    }
).encode("utf-8")

# Binary-representable timestamps → exact ms values (no float noise).
TIMING_NON_STREAM = Timing(
    t_request_received=1000.0,
    t_upstream_started=1000.125,
    t_upstream_completed=1000.5,
)
TIMING_STREAM = Timing(
    t_request_received=1000.0,
    t_upstream_started=1000.125,
    t_first_token=1000.5,
    t_upstream_completed=1000.875,
)

_SSE_WITH_USAGE = [
    b'data: {"choices":[{"delta":{"content":"He"}}]}\n\n',
    b'data: {"choices":[{"delta":{"content":"llo"}}]}\n\n',
    b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n',
    (
        b'data: {"choices":[],"usage":{"prompt_tokens":7,"completion_tokens":3,'
        b'"prompt_tokens_details":{"cached_tokens":1}}}\n\n'
    ),
    b"data: [DONE]\n\n",
]


@pytest.fixture(autouse=True)
def content_rate_env(monkeypatch):
    monkeypatch.setenv("AGENT_OBS_CONTENT_RATE", "100")
    monkeypatch.delenv("AGENT_OBS_PRICE_BOOK_PATH", raising=False)


def make_span(trace_id: str = "trace-pp41") -> Span:
    return Span(
        name="llm.proxy.call:gpt-4o",
        span_type=SpanType.LLM_PROXY_CALL,
        context=SpanContext(trace_id=trace_id, span_id="span-1", agent_id="proxy"),
        start_time=1000.0,
    )


def make_pipeline(**kwargs) -> ObsPipeline:
    return ObsPipeline(ProxyConfig(mode="on", enabled=True), **kwargs)


# ---------------------------------------------------------------------------
# Timing
# ---------------------------------------------------------------------------


class TestTiming:
    def test_ttft_zero_without_first_token(self):
        assert TIMING_NON_STREAM.ttft_ms == 0.0  # N/A for non-streaming

    def test_ttft_and_latency(self):
        assert TIMING_STREAM.ttft_ms == 375.0
        assert TIMING_STREAM.latency_ms == 875.0

    def test_negative_values_clamped(self):
        timing = Timing(
            t_request_received=10.0,
            t_upstream_started=10.5,
            t_upstream_completed=10.0,
            t_first_token=9.0,
        )
        assert timing.ttft_ms == 0.0
        assert timing.latency_ms == 0.0


# ---------------------------------------------------------------------------
# SSE parsing helpers
# ---------------------------------------------------------------------------


class TestSseParsing:
    def test_parse_sse_chunk_extracts_payloads(self):
        payloads = parse_sse_chunk(
            b'data: {"a":1}\n\ndata: {"b":2}\n\n'
        )
        assert payloads == [{"a": 1}, {"b": 2}]

    def test_parse_sse_chunk_ignores_done_comments_and_junk(self):
        assert parse_sse_chunk(b"data: [DONE]\n\n") == []
        assert parse_sse_chunk(b": keep-alive comment\n\n") == []
        assert parse_sse_chunk(b"data: not-json\n\n") == []

    def test_iter_sse_json_handles_events_split_across_chunks(self):
        chunks = [
            b'data: {"choices":[{"del',
            b'ta":{"content":"a"}}]}\n\ndata: [DONE]\n',
            b"\n",
        ]
        payloads = list(iter_sse_json(chunks))
        assert payloads == [{"choices": [{"delta": {"content": "a"}}]}]

    def test_non_sse_body_yields_nothing(self):
        assert list(iter_sse_json([b'{"error":"boom"}'])) == []


# ---------------------------------------------------------------------------
# Non-streaming enrichment
# ---------------------------------------------------------------------------


class TestEnrichNonStreaming:
    async def test_full_attribute_set(self, content_rate_env):
        pipeline = make_pipeline(price_book=_TEST_BOOK)
        span = make_span()
        await pipeline.enrich_span(
            span, REQUEST, RESPONSE, None, {}, TIMING_NON_STREAM
        )

        assert span.attributes["llm.provider"] == "openai"
        assert span.attributes["llm.model"] == "gpt-4o"
        assert span.attributes["llm.temperature"] == 0.2
        assert span.attributes["llm.input_messages_count"] == 1
        assert span.attributes["llm.output_messages_count"] == 1
        assert span.attributes["llm.input_chars"] == len(
            json.dumps(MESSAGES, ensure_ascii=False)
        )
        assert span.attributes["llm.output_chars"] == len("hi there")
        assert span.attributes["tokens.input"] == 10
        assert span.attributes["tokens.output"] == 4
        assert span.attributes["tokens.cached"] == 2
        assert span.attributes["llm.finish_reason"] == "stop"
        assert span.attributes["llm.stream"] is False
        assert span.attributes["llm.ttft_ms"] == 0.0  # N/A for non-streaming
        assert span.attributes["llm.latency_ms"] == 500.0

        expected = compute_cost(
            Usage(input=10, output=4, cached=2), "gpt-4o", _TEST_BOOK
        )
        assert span.attributes["cost.usd"] == round(expected.cost_usd, 8)
        assert span.attributes["cost.price_book_version"] == "test-1"

        assert "hello" in span.attributes["llm.input_text"]
        assert span.attributes["llm.output_text"] == "hi there"
        assert span.attributes["trace.content_sampled"] is True

    async def test_missing_usage_skips_tokens_and_cost(self, content_rate_env):
        pipeline = make_pipeline(price_book=_TEST_BOOK)
        span = make_span()
        resp = json.dumps({"choices": [{"message": {"content": "yo"}}]}).encode()
        await pipeline.enrich_span(
            span, REQUEST, resp, None, {}, TIMING_NON_STREAM
        )
        assert "tokens.input" not in span.attributes
        assert "cost.usd" not in span.attributes
        assert span.attributes["llm.output_text"] == "yo"

    async def test_unknown_model_keeps_tokens_drops_cost(self, content_rate_env):
        pipeline = make_pipeline(price_book=_TEST_BOOK)
        span = make_span()
        request = json.dumps(
            {"model": "mystery-model-9000", "messages": MESSAGES}
        ).encode()
        await pipeline.enrich_span(
            span, request, RESPONSE, None, {}, TIMING_NON_STREAM
        )
        assert span.attributes["tokens.input"] == 10
        assert "cost.usd" not in span.attributes

    async def test_no_price_book_keeps_tokens_drops_cost(self, content_rate_env):
        pipeline = make_pipeline(price_book=None)
        span = make_span()
        await pipeline.enrich_span(
            span, REQUEST, RESPONSE, None, {}, TIMING_NON_STREAM
        )
        assert span.attributes["tokens.input"] == 10
        assert "cost.usd" not in span.attributes

    async def test_upstream_error_body_does_not_break_enrichment(self, content_rate_env):
        pipeline = make_pipeline(price_book=_TEST_BOOK)
        span = make_span()
        err = b'{"error": "rate limited"}'
        await pipeline.enrich_span(span, REQUEST, err, None, {}, TIMING_NON_STREAM)
        assert span.attributes["llm.output_chars"] == len(err.decode())
        assert "tokens.input" not in span.attributes
        assert "llm.finish_reason" not in span.attributes

    async def test_invalid_request_json_degrades_gracefully(self, content_rate_env):
        pipeline = make_pipeline(price_book=_TEST_BOOK)
        span = make_span()
        await pipeline.enrich_span(
            span, b"not-json", RESPONSE, None, {}, TIMING_NON_STREAM
        )
        assert span.attributes["llm.input_messages_count"] == 0
        assert "llm.model" not in span.attributes

    async def test_missing_temperature_not_stamped(self, content_rate_env):
        pipeline = make_pipeline(price_book=_TEST_BOOK)
        span = make_span()
        await pipeline.enrich_span(
            span, STREAM_REQUEST, RESPONSE, None, {}, TIMING_NON_STREAM
        )
        assert "llm.temperature" not in span.attributes


# ---------------------------------------------------------------------------
# Streaming enrichment
# ---------------------------------------------------------------------------


class TestEnrichStreaming:
    async def test_aggregates_usage_content_and_timing(self, content_rate_env):
        pipeline = make_pipeline(price_book=_TEST_BOOK)
        span = make_span()
        await pipeline.enrich_span(
            span, STREAM_REQUEST, None, list(_SSE_WITH_USAGE), {}, TIMING_STREAM
        )

        assert span.attributes["llm.stream"] is True
        assert span.attributes["llm.output_text"] == "Hello"
        assert span.attributes["tokens.input"] == 7
        assert span.attributes["tokens.output"] == 3
        assert span.attributes["tokens.cached"] == 1
        assert span.attributes["llm.finish_reason"] == "stop"
        assert span.attributes["llm.ttft_ms"] == 375.0  # > 0 for streaming
        assert span.attributes["llm.latency_ms"] == 875.0
        assert span.attributes["llm.latency_ms"] >= span.attributes["llm.ttft_ms"]
        assert span.attributes["cost.usd"] > 0

    async def test_usage_extracted_from_final_chunk_only(self, content_rate_env):
        pipeline = make_pipeline(price_book=_TEST_BOOK)
        span = make_span()
        chunks = [
            b'data: {"choices":[{"delta":{"content":"x"}}]}\n\n',
            (
                b'data: {"choices":[],"usage":{"prompt_tokens":100,'
                b'"completion_tokens":50}}\n\n'
            ),
        ]
        await pipeline.enrich_span(
            span, STREAM_REQUEST, None, chunks, {}, TIMING_STREAM
        )
        assert span.attributes["tokens.input"] == 100
        assert span.attributes["tokens.output"] == 50

    async def test_empty_stream_payloads_fall_back_to_raw_text(self, content_rate_env):
        pipeline = make_pipeline(price_book=_TEST_BOOK)
        span = make_span()
        await pipeline.enrich_span(
            span, STREAM_REQUEST, None, [b'{"error":"upstream"}'], {}, TIMING_STREAM
        )
        assert span.attributes["llm.output_text"] == '{"error":"upstream"}'

    async def test_no_chunks_sets_no_output_bodies(self, content_rate_env):
        pipeline = make_pipeline(price_book=_TEST_BOOK)
        span = make_span()
        await pipeline.enrich_span(span, STREAM_REQUEST, None, [], {}, TIMING_STREAM)
        assert "llm.output_text" not in span.attributes
        assert "llm.output_sha256" not in span.attributes
        assert span.attributes["llm.output_messages_count"] == 0


# ---------------------------------------------------------------------------
# Content sampling (PP41 §3)
# ---------------------------------------------------------------------------


class TestContentSampling:
    async def test_rate_zero_stores_hashes_only(self, monkeypatch):
        monkeypatch.setenv("AGENT_OBS_CONTENT_RATE", "0")
        pipeline = make_pipeline(price_book=_TEST_BOOK)
        span = make_span()
        await pipeline.enrich_span(
            span, REQUEST, RESPONSE, None, {}, TIMING_NON_STREAM
        )
        assert "llm.input_text" not in span.attributes
        assert "llm.output_text" not in span.attributes
        assert len(span.attributes["llm.input_sha256"]) == 64
        assert len(span.attributes["llm.output_sha256"]) == 64
        assert span.attributes["llm.input_chars"] == len(
            json.dumps(MESSAGES, ensure_ascii=False)
        )
        assert span.attributes["llm.output_chars"] == len("hi there")
        assert span.attributes["trace.content_sampled"] is False

    async def test_rate_hundred_stores_full_text(self, monkeypatch):
        monkeypatch.setenv("AGENT_OBS_CONTENT_RATE", "100")
        pipeline = make_pipeline(price_book=_TEST_BOOK)
        span = make_span()
        await pipeline.enrich_span(
            span, REQUEST, RESPONSE, None, {}, TIMING_NON_STREAM
        )
        assert span.attributes["llm.input_text"]
        assert span.attributes["llm.output_text"] == "hi there"
        assert "llm.input_sha256" not in span.attributes
        assert span.attributes["trace.content_sampled"] is True

    async def test_log_bodies_false_never_stores_plaintext(self, monkeypatch):
        monkeypatch.setenv("AGENT_OBS_CONTENT_RATE", "100")
        config = ProxyConfig(mode="on", enabled=True, log_bodies=False)
        pipeline = ObsPipeline(config, price_book=_TEST_BOOK)
        span = make_span()
        await pipeline.enrich_span(
            span, REQUEST, RESPONSE, None, {}, TIMING_NON_STREAM
        )
        assert "llm.input_text" not in span.attributes
        assert "llm.output_text" not in span.attributes
        assert len(span.attributes["llm.input_sha256"]) == 64
        assert span.attributes["llm.input_chars"] > 0

    async def test_oversized_body_truncated(self, monkeypatch):
        monkeypatch.setenv("AGENT_OBS_CONTENT_RATE", "100")
        config = ProxyConfig(mode="on", enabled=True, log_bodies_max_bytes=64)
        pipeline = ObsPipeline(config, price_book=_TEST_BOOK)
        span = make_span()
        request = json.dumps(
            {"model": "gpt-4o", "messages": [{"role": "user", "content": "x" * 5000}]}
        ).encode()
        await pipeline.enrich_span(
            span, request, RESPONSE, None, {}, TIMING_NON_STREAM
        )
        assert span.attributes["llm.input_truncated"] is True
        assert len(span.attributes["llm.input_text"].encode()) <= 64
        # Char counts describe the full logical text.
        assert span.attributes["llm.input_chars"] > 64


# ---------------------------------------------------------------------------
# Price book loading
# ---------------------------------------------------------------------------


class TestPriceBookLoading:
    def test_missing_price_book_degrades_to_none(self, caplog, monkeypatch):
        monkeypatch.setenv("AGENT_OBS_PRICE_BOOK_PATH", "does-not-exist.yaml")
        pipeline = make_pipeline()
        with caplog.at_level(logging.WARNING, logger="agent_obs.proxy.obs_pipeline"):
            assert pipeline.price_book is None
        assert "price book" in caplog.text
        # Lazily cached — a second access does not warn again.
        caplog.clear()
        assert pipeline.price_book is None
        assert not caplog.records

    def test_explicit_none_disables_auto_load(self, monkeypatch):
        monkeypatch.setenv("AGENT_OBS_PRICE_BOOK_PATH", "does-not-exist.yaml")
        pipeline = make_pipeline(price_book=None)
        assert pipeline.price_book is None
