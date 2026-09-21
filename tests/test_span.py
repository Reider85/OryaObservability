"""Tests for the Span dataclass (T1.1.2)."""

import time

import pytest

from agent_obs.observability import (
    Event,
    InvalidSpanTypeError,
    Span,
    SpanContext,
    SpanType,
    _now,
    _to_iso8601,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ctx(**kwargs) -> SpanContext:
    return SpanContext.new(agent_id=kwargs.get("agent_id", "test-agent"))


def _make_span(span_type: SpanType = SpanType.AGENT_LOOP, **kwargs) -> Span:
    ctx = kwargs.pop("context", None) or _make_ctx()
    return Span(
        name=kwargs.pop("name", "test-span"),
        span_type=span_type,
        context=ctx,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# SpanType enum
# ---------------------------------------------------------------------------


class TestSpanType:
    def test_allowed_values(self):
        assert SpanType.AGENT_LOOP.value == "agent.loop"
        assert SpanType.LLM_CALL.value == "llm.call"
        assert SpanType.TOOL_CALL.value == "tool.call"

    def test_all_three_types_present(self):
        assert len(SpanType) == 3


# ---------------------------------------------------------------------------
# Span creation
# ---------------------------------------------------------------------------


class TestSpanCreation:
    def test_create_with_enum(self):
        span = _make_span(span_type=SpanType.LLM_CALL, name="llm.call:gpt-4o")
        assert span.name == "llm.call:gpt-4o"
        assert span.span_type == SpanType.LLM_CALL
        assert span.attributes == {}
        assert span.events == []
        assert span.end_time is None

    def test_create_with_string_type(self):
        span = _make_span(span_type="llm.call")
        assert span.span_type == SpanType.LLM_CALL

    def test_invalid_string_type_raises(self):
        with pytest.raises(InvalidSpanTypeError, match="Invalid span_type"):
            _make_span(span_type="invalid.type")

    def test_invalid_enum_member_raises(self):
        with pytest.raises(ValueError):
            SpanType("nonexistent")

    def test_default_start_time(self):
        span = _make_span()
        assert span.start_time == 0.0

    def test_end_time_none_by_default(self):
        span = _make_span()
        assert span.end_time is None


# ---------------------------------------------------------------------------
# duration_ms
# ---------------------------------------------------------------------------


class TestDuration:
    def test_duration_zero_when_no_end(self):
        span = _make_span(start_time=100.0)
        assert span.duration_ms == 0.0

    def test_duration_calculation(self):
        span = _make_span(start_time=100.0, end_time=100.5)
        assert span.duration_ms == pytest.approx(500.0)

    def test_duration_short_span(self):
        span = _make_span(start_time=1000.0, end_time=1000.001)
        assert span.duration_ms == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# set_attribute / add_event
# ---------------------------------------------------------------------------


class TestAttributesAndEvents:
    def test_set_attribute(self):
        span = _make_span()
        span.set_attribute("llm.model", "gpt-4o")
        span.set_attribute("tokens.input", 150)
        assert span.attributes["llm.model"] == "gpt-4o"
        assert span.attributes["tokens.input"] == 150

    def test_set_attribute_overwrites(self):
        span = _make_span()
        span.set_attribute("key", "old")
        span.set_attribute("key", "new")
        assert span.attributes["key"] == "new"

    def test_add_event(self):
        span = _make_span()
        span.add_event("llm.call.started")
        assert len(span.events) == 1
        assert span.events[0].name == "llm.call.started"
        assert span.events[0].timestamp  # non-empty
        assert span.events[0].attributes == {}

    def test_add_event_with_attrs(self):
        span = _make_span()
        span.add_event("retry", attrs={"attempt": 3, "reason": "timeout"})
        event = span.events[0]
        assert event.attributes["attempt"] == 3
        assert event.attributes["reason"] == "timeout"

    def test_add_multiple_events(self):
        span = _make_span()
        span.add_event("started")
        time.sleep(0.001)
        span.add_event("completed")
        assert len(span.events) == 2
        assert span.events[0].name == "started"
        assert span.events[1].name == "completed"


# ---------------------------------------------------------------------------
# to_dict serialization
# ---------------------------------------------------------------------------


class TestToDict:
    def test_basic_structure(self):
        ctx = _make_ctx()
        span = Span(
            name="agent.loop:my-agent",
            span_type=SpanType.AGENT_LOOP,
            context=ctx,
            start_time=1700000000.0,
            end_time=1700000001.0,
        )
        span.attributes["status"] = "ok"
        data = span.to_dict()

        assert data["trace_id"] == ctx.trace_id
        assert data["span_id"] == ctx.span_id
        assert data["parent_span_id"] is None
        assert data["name"] == "agent.loop:my-agent"
        assert data["span_type"] == "agent.loop"
        assert "2023" in data["start_time"]
        assert "2023" in data["end_time"]
        assert data["status"] == "ok"
        assert data["attributes"]["status"] == "ok"
        assert data["events"] == []

    def test_to_dict_keys_match_schema(self):
        span = _make_span()
        span.attributes["status"] = "ok"
        data = span.to_dict()
        expected_keys = {
            "trace_id",
            "span_id",
            "parent_span_id",
            "name",
            "span_type",
            "start_time",
            "end_time",
            "status",
            "attributes",
            "events",
        }
        assert set(data.keys()) == expected_keys

    def test_to_dict_with_events(self):
        span = _make_span()
        span.add_event("llm.call.started", attrs={"model": "gpt-4o"})
        data = span.to_dict()
        assert len(data["events"]) == 1
        event = data["events"][0]
        assert event["name"] == "llm.call.started"
        assert "timestamp" in event
        assert event["attributes"]["model"] == "gpt-4o"

    def test_to_dict_end_time_none(self):
        span = _make_span(start_time=1700000000.0)
        data = span.to_dict()
        assert data["end_time"] is None

    def test_to_dict_llm_call_attributes(self):
        ctx = _make_ctx()
        span = Span(
            name="llm.call:gpt-4o",
            span_type=SpanType.LLM_CALL,
            context=ctx,
            attributes={
                "llm.model": "gpt-4o",
                "llm.provider": "openai",
                "llm.temperature": 0.7,
                "tokens.input": 150,
                "tokens.output": 50,
                "tokens.cached": 0,
                "cost.usd": 0.001,
                "cost.price_book_version": "2026-09-01",
                "status": "ok",
            },
        )
        data = span.to_dict()
        assert data["attributes"]["llm.model"] == "gpt-4o"
        assert data["attributes"]["cost.usd"] == 0.001
        assert data["span_type"] == "llm.call"

    def test_to_dict_tool_call_attributes(self):
        ctx = _make_ctx()
        span = Span(
            name="tool.call:search",
            span_type=SpanType.TOOL_CALL,
            context=ctx,
            attributes={
                "tool.name": "search",
                "tool.input_hash": "abc123",
                "tool.input_summary": "query: test",
                "tool.rows_returned": 5,
                "status": "ok",
            },
        )
        data = span.to_dict()
        assert data["attributes"]["tool.name"] == "search"
        assert data["span_type"] == "tool.call"


# ---------------------------------------------------------------------------
# _now / _to_iso8601 helpers
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_now_returns_float(self):
        assert isinstance(_now(), float)

    def test_to_iso8601(self):
        result = _to_iso8601(1700000000.0)
        assert result is not None
        assert "2023" in result

    def test_to_iso8601_none(self):
        assert _to_iso8601(None) is None

    def test_iso8601_has_utc_timezone(self):
        result = _to_iso8601(1700000000.0)
        assert "+00:00" in result or result.endswith("Z")


# ---------------------------------------------------------------------------
# Integration: child span with parent
# ---------------------------------------------------------------------------


class TestSpanNesting:
    def test_child_span_shares_trace_id(self):
        parent_ctx = _make_ctx()
        child_ctx = SpanContext.new(agent_id="child", parent=parent_ctx)
        parent = Span(
            name="agent.loop:parent",
            span_type=SpanType.AGENT_LOOP,
            context=parent_ctx,
        )
        child = Span(
            name="llm.call:gpt-4o",
            span_type=SpanType.LLM_CALL,
            context=child_ctx,
        )
        assert child.context.trace_id == parent.context.trace_id
        assert child.context.parent_span_id == parent.context.span_id

    def test_to_dict_preserves_nesting(self):
        parent_ctx = _make_ctx()
        child_ctx = SpanContext.new(agent_id="child", parent=parent_ctx)
        child = Span(
            name="llm.call:gpt-4o",
            span_type=SpanType.LLM_CALL,
            context=child_ctx,
            attributes={"status": "ok"},
        )
        data = child.to_dict()
        assert data["trace_id"] == parent_ctx.trace_id
        assert data["parent_span_id"] == parent_ctx.span_id
