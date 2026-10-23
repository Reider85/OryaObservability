"""Tests for SpanAggregator."""

import asyncio
import time
from unittest.mock import patch

import pytest

from agent_obs.observability import Span, SpanContext, SpanType
from agent_obs.span_aggregator import SpanAggregator, SpanAggregate


@pytest.fixture
def mock_clock():
    """Mock clock for deterministic time tests."""
    now = time.time()
    with patch("time.time", side_effect=lambda: now) as mock:
        yield mock


@pytest.fixture
def mock_clock():
    """Mock clock for deterministic time tests."""
    now = time.time()
    with patch("time.time", side_effect=lambda: now) as mock:
        yield mock


@pytest.fixture
def default_context():
    """Default SpanContext for testing."""
    return SpanContext.new(agent_id="test-agent")


@pytest.fixture
def test_span_factory(mock_clock, default_context):
    """Factory for test spans using mock clock."""
    def _make_span(name="test-span", clock=None, **kwargs):
        # Handle context parameter to avoid duplication
        context = kwargs.pop("context", default_context)
        # Use provided clock or default to mock_clock
        use_clock = clock or mock_clock
        return Span(
            name=name,
            span_type=SpanType.AGENT_LOOP,
            context=context,
            start_time=use_clock(),  # Use specified clock for start_time
            **kwargs,
        )
    return _make_span



class TestSpanAggregate:
    """Test SpanAggregate dataclass."""

    def test_to_dict_structure(self):
        """Test SpanAggregate.to_dict() produces expected fields."""
        aggregate = SpanAggregate(
            parent_span_id="parent-123",
            span_type="agent.reasoning",
            events=[{"name": "event1", "ts": 1700000000.0, "attrs": {"key": "value"}}],
            children_count=1,
            bytes_saved=100,
        )
        result = aggregate.to_dict()
        
        assert result["parent_span_id"] == "parent-123"
        assert result["span_type"] == "agent.reasoning"
        assert len(result["events"]) == 1
        assert result["events"][0]["name"] == "event1"
        assert result["events"][0]["ts"] == 1700000000.0
        assert result["events"][0]["attrs"]["key"] == "value"
        assert result["children_count"] == 1
        assert result["bytes_saved"] == 100
        assert result["name"] == "agent.reasoning:aggregated:1"

    def test_empty_parent_span_id(self):
        """Test empty parent_span_id handling."""
        aggregate = SpanAggregate(
            parent_span_id="",
            span_type="agent.reasoning",
            events=[],
            children_count=0,
            bytes_saved=0,
        )
        assert aggregate.to_dict()["parent_span_id"] == ""


class TestSpanAggregator:
    """Test SpanAggregator class."""

    @pytest.mark.asyncio
    async def test_add_50_spans_flushes_on_count(self, test_span_factory):
        """Test 50 spans trigger flush on 50th add."""
        agg = SpanAggregator(max_count=50)
        spans = [test_span_factory(f"span{i}") for i in range(50)]
        
        # First 49 adds should return None
        for i in range(49):
            result = await agg.add(spans[i])
            assert result is None
        
        # 50th add should return aggregate
        result = await agg.add(spans[49])
        assert isinstance(result, SpanAggregate)
        assert result.children_count == 50
        assert result.span_type == "agent.reasoning"
        assert len(result.events) == 50
        assert result.bytes_saved > 0

    @pytest.mark.asyncio
    async def test_less_than_50_spans_no_flush(self, test_span_factory):
        """Test 49 spans don't trigger flush."""
        agg = SpanAggregator(max_count=50)
        spans = [test_span_factory(f"span{i}") for i in range(49)]
        
        for span in spans:
            result = await agg.add(span)
            assert result is None
        
        # Buffer should still contain 49 spans
        assert len(agg._buffer) == 49

    @pytest.mark.asyncio
    async def test_window_flush_on_add(self, test_span_factory):
        """Test window expiration triggers flush on next add."""
        # Use a custom clock that we can control
        test_clock_time = 1000.0
        
        def custom_clock():
            nonlocal test_clock_time
            return test_clock_time
        
        agg = SpanAggregator(max_count=50, max_window_seconds=1.0, clock=custom_clock)
        
        # Add 10 spans quickly
        spans = [test_span_factory(f"span{i}", clock=custom_clock) for i in range(10)]
        for span in spans:
            result = await agg.add(span)
            assert result is None
        
        # Advance time beyond window
        test_clock_time += 1.5
        
        # Next add should trigger flush
        new_span = test_span_factory("span11", clock=custom_clock)
        result = await agg.add(new_span)
        
        assert isinstance(result, SpanAggregate)
        assert result.children_count == 11  # 10 old + 1 new
        assert len(result.events) == 11
        assert result.bytes_saved > 0

    @pytest.mark.asyncio
    async def test_window_flush_on_explicit_flush(self, test_span_factory, mock_clock):
        """Test explicit flush after window expiration."""
        agg = SpanAggregator(max_count=50, max_window_seconds=1.0)
        
        # Add 10 spans quickly
        spans = [test_span_factory(f"span{i}") for i in range(10)]
        for span in spans:
            await agg.add(span)
        
        # Advance time beyond window
        mock_clock.return_value += 1.5
        
        # Explicit flush should return aggregate
        result = await agg.flush()
        
        assert isinstance(result, SpanAggregate)
        assert result.children_count == 10
        assert len(result.events) == 10
        assert result.bytes_saved > 0

    @pytest.mark.asyncio
    async def test_empty_flush_returns_none(self, test_span_factory):
        """Test flush on empty buffer returns None."""
        agg = SpanAggregator()
        result = await agg.flush()
        assert result is None

    @pytest.mark.asyncio
    async def test_double_flush_clears_buffer(self, test_span_factory):
        """Test flush after first flush returns None."""
        agg = SpanAggregator(max_count=2)
        
        # Add 2 spans to trigger flush
        await agg.add(test_span_factory("span1"))
        result1 = await agg.add(test_span_factory("span2"))
        assert isinstance(result1, SpanAggregate)
        
        # Second flush should return None
        result2 = await agg.flush()
        assert result2 is None

    @pytest.mark.asyncio
    async def test_bytes_saved_calculation(self, test_span_factory):
        """Test bytes_saved is calculated correctly."""
        agg = SpanAggregator(max_count=2)
        
        # Add spans with known attributes
        span1 = test_span_factory("span1", attributes={"data": "x" * 100})
        span2 = test_span_factory("span2", attributes={"data": "y" * 200})
        
        await agg.add(span1)
        result = await agg.add(span2)
        
        assert isinstance(result, SpanAggregate)
        assert result.bytes_saved > 0
        
        # Verify bytes_saved is positive (aggregate should be smaller)
        assert result.bytes_saved > 0

    @pytest.mark.asyncio
    async def test_aggregate_name_format(self, test_span_factory):
        """Test aggregate name follows format."""
        agg = SpanAggregator(max_count=3)
        
        spans = [test_span_factory(f"span{i}") for i in range(3)]
        await agg.add(spans[0])
        await agg.add(spans[1])
        result = await agg.add(spans[2])
        
        assert isinstance(result, SpanAggregate)
        assert result.to_dict()["name"] == "agent.reasoning:aggregated:3"

    @pytest.mark.asyncio
    async def test_events_format_and_copy(self, test_span_factory):
        """Test events format and attribute copying."""
        agg = SpanAggregator(max_count=2)
        
        # Create span with simple attributes
        span = test_span_factory("span1", attributes={"key": "value", "number": 42})
        await agg.add(span)
        
        # Flush and check event attributes are copied (shallow copy)
        result = await agg.flush()
        assert isinstance(result, SpanAggregate)
        assert result.events[0]["attrs"]["key"] == "value"
        assert result.events[0]["attrs"]["number"] == 42
        
        # Verify modifying original span doesn't affect the event
        span.attributes["key"] = "modified"
        # The event should still have the original value
        assert result.events[0]["attrs"]["key"] == "value"

    @pytest.mark.asyncio
    async def test_parent_span_id_propagation(self, test_span_factory):
        """Test parent_span_id from micro-spans."""
        # Create spans with known parent_span_id using direct construction
        parent_ctx = SpanContext(
            trace_id="trace-123",
            span_id="parent-123",
            parent_span_id="grandparent-123",
            agent_id="test-agent"
        )
        span1 = test_span_factory("span1", context=parent_ctx)
        span2 = test_span_factory("span2", context=parent_ctx)
        
        agg = SpanAggregator(max_count=2)
        await agg.add(span1)
        result = await agg.add(span2)
        
        assert isinstance(result, SpanAggregate)
        assert result.parent_span_id == "grandparent-123"

    @pytest.mark.asyncio
    async def test_custom_span_type(self, test_span_factory):
        """Test custom span_type parameter."""
        agg = SpanAggregator(max_count=2, span_type="custom.span.type")
        
        await agg.add(test_span_factory("span1"))
        result = await agg.add(test_span_factory("span2"))
        
        assert isinstance(result, SpanAggregate)
        assert result.span_type == "custom.span.type"

    @pytest.mark.asyncio
    async def test_custom_max_count_max_window(self, test_span_factory):
        """Test custom max_count and max_window_seconds."""
        agg = SpanAggregator(max_count=3, max_window_seconds=0.5)
        
        # Add 2 spans (below count threshold)
        for i in range(2):
            result = await agg.add(test_span_factory(f"span{i}"))
            assert result is None
        
        # Add 3rd span (should trigger flush)
        result = await agg.add(test_span_factory("span2"))
        assert isinstance(result, SpanAggregate)
        assert result.children_count == 3

    @pytest.mark.asyncio
    async def test_concurrent_add_safety(self, test_span_factory):
        """Test thread-safety with concurrent adds."""
        agg = SpanAggregator(max_count=10)
        spans = [test_span_factory(f"span{i}") for i in range(10)]
        
        async def add_span(span):
            return await agg.add(span)
        
        # Add spans concurrently
        tasks = [add_span(span) for span in spans]
        results = await asyncio.gather(*tasks)
        
        # Should have one aggregate and 9 Nones
        aggregates = [r for r in results if r is not None]
        nones = [r for r in results if r is None]
        
        assert len(aggregates) == 1
        assert len(nones) == 9
        assert aggregates[0].children_count == 10