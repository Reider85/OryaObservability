"""Tests for TraceTreeBuilder and tree compression utilities."""

import asyncio
import time
from dataclasses import dataclass
from unittest.mock import patch

import pytest

from agent_obs.observability import Span, SpanContext, SpanType
from agent_obs.tree_compressor import (
    TraceTree,
    TraceTreeBuilder,
    TreeMetrics,
    compute_tree_metrics,
    is_aggregate_span,
)


@pytest.fixture
def mock_clock():
    """Mock clock for deterministic time tests."""
    now = 1700000000.0
    with patch("time.time", side_effect=lambda: now) as mock:
        yield mock


@pytest.fixture
def default_context():
    """Default SpanContext for testing."""
    return SpanContext.new(agent_id="test-agent")


@pytest.fixture
def test_span_factory(mock_clock, default_context):
    """Factory for test spans using mock clock."""
    def _make_span(
        name="test-span",
        span_type=SpanType.AGENT_LOOP,
        parent_span_id=None,
        status="ok",
        start_time=None,
        is_aggregate=False,
        **kwargs
    ):
        # Handle context parameter to avoid duplication
        context = kwargs.pop("context", default_context)
        
        # Create new context with unique span_id
        if parent_span_id is not None:
            # Create new context with this span as parent
            context = SpanContext(
                trace_id=context.trace_id,
                span_id=_new_ulid(),
                parent_span_id=parent_span_id,
                agent_id=context.agent_id,
                agent_version=context.agent_version,
                user_id=context.user_id,
                session_id=context.session_id,
                tool_name=context.tool_name,
            )
        else:
            # Create new context with no parent
            context = SpanContext(
                trace_id=context.trace_id,
                span_id=_new_ulid(),
                parent_span_id=None,
                agent_id=context.agent_id,
                agent_version=context.agent_version,
                user_id=context.user_id,
                session_id=context.session_id,
                tool_name=context.tool_name,
            )
        
        # Use provided clock or default to mock_clock
        use_start_time = start_time or mock_clock()
        
        attributes = {"status": status}
        if is_aggregate:
            attributes["aggregation.children_count"] = 2
            attributes["aggregation.bytes_saved"] = 100
        
        return Span(
            name=name,
            span_type=span_type,
            context=context,
            start_time=use_start_time,
            attributes=attributes,
            **kwargs,
        )
    return _make_span


def _new_ulid():
    """Helper for generating test span IDs."""
    import uuid
    return f"span_{uuid.uuid4().hex[:8]}"


class TestTraceTree:
    """Test TraceTree dataclass and methods."""

    def test_to_dict_structure(self):
        """Test TraceTree.to_dict() produces expected fields."""
        context = SpanContext.new(agent_id="test")
        root_span = Span(
            name="root",
            span_type=SpanType.AGENT_LOOP,
            context=context,
            start_time=1700000000.0,
        )
        tree = TraceTree(
            root=root_span,
            children=[],
            compressed=True,
            compression_ratio=0.75,
        )
        result = tree.to_dict()
        
        assert result["root"]["name"] == "root"
        assert result["root"]["span_type"] == "agent.loop"
        assert result["children"] == []
        assert result["compressed"] is True
        assert result["compression_ratio"] == 0.75

    def test_to_dict_with_children(self):
        """Test to_dict with recursive children."""
        parent_context = SpanContext.new(agent_id="test")
        child_context = SpanContext.new(agent_id="test", parent=parent_context)
        
        parent_span = Span(
            name="parent",
            span_type=SpanType.AGENT_LOOP,
            context=parent_context,
            start_time=1700000000.0,
        )
        child_span = Span(
            name="child",
            span_type=SpanType.LLM_CALL,
            context=child_context,
            start_time=1700000001.0,
        )
        
        child_tree = TraceTree(root=child_span, children=[])
        parent_tree = TraceTree(
            root=parent_span,
            children=[child_tree],
            compressed=False,
            compression_ratio=0.0,
        )
        
        result = parent_tree.to_dict()
        assert len(result["children"]) == 1
        assert result["children"][0]["root"]["name"] == "child"

    @pytest.mark.asyncio
    async def test_iter_dfs_order(self, test_span_factory):
        """Test DFS traversal order."""
        # Create spans first to get their actual span_ids
        span_a = test_span_factory(name="A", span_type=SpanType.AGENT_LOOP, start_time=1)
        span_b = test_span_factory(name="B", span_type=SpanType.LLM_CALL, parent_span_id=span_a.context.span_id, start_time=2)
        span_c = test_span_factory(name="C", span_type=SpanType.LLM_CALL, parent_span_id=span_a.context.span_id, start_time=3)
        
        spans = [span_a, span_b, span_c]
        
        # Build simple tree: A -> B, A -> C
        tree = TraceTreeBuilder().build(spans)
        
        # DFS should visit A, then C, then B (pre-order, children sorted by start_time)
        visited = []
        async for node in tree.iter_dfs():
            visited.append(node.root.name)
        
        assert visited == ["A", "C", "B"]  # Children sorted by start_time (C=3, B=2)

    @pytest.mark.asyncio
    async def test_iter_bfs_order(self, test_span_factory):
        """Test BFS traversal order."""
        # Create spans first to get their actual span_ids
        span_a = test_span_factory(name="A", span_type=SpanType.AGENT_LOOP, start_time=1)
        span_b = test_span_factory(name="B", span_type=SpanType.LLM_CALL, parent_span_id=span_a.context.span_id, start_time=2)
        span_c = test_span_factory(name="C", span_type=SpanType.LLM_CALL, parent_span_id=span_a.context.span_id, start_time=3)
        span_d = test_span_factory(name="D", span_type=SpanType.LLM_CALL, parent_span_id=span_b.context.span_id, start_time=4)
        
        spans = [span_a, span_b, span_c, span_d]
        
        # Build tree: A -> B -> D, A -> C
        tree = TraceTreeBuilder().build(spans)
        
        # BFS: A, C, B, D (sorted by start_time)
        visited = []
        async for node in tree.iter_bfs():
            visited.append(node.root.name)
        
        assert visited == ["A", "C", "B", "D"]  # C (start_time=3) before B (start_time=2) due to sorting


class TestTreeMetrics:
    """Test TreeMetrics and compute_tree_metrics."""

    def test_compute_metrics_simple_tree(self, test_span_factory):
        """Test metrics computation for a simple tree."""
        # Create spans first to get their actual span_ids
        span_a = test_span_factory(name="A", span_type=SpanType.AGENT_LOOP, start_time=1)
        span_b = test_span_factory(name="B", span_type=SpanType.LLM_CALL, parent_span_id=span_a.context.span_id, start_time=2)
        span_c = test_span_factory(name="C", span_type=SpanType.LLM_CALL, parent_span_id=span_a.context.span_id, start_time=3)
        
        spans = [span_a, span_b, span_c]
        
        tree = TraceTreeBuilder().build(spans)
        metrics = compute_tree_metrics(tree)
        
        assert metrics.max_depth == 2
        assert metrics.total_spans == 3
        assert metrics.leaf_count == 2
        assert metrics.error_leaf_count == 0

    def test_compute_metrics_with_error(self, test_span_factory):
        """Test metrics with error leaves."""
        # Create spans first to get their actual span_ids
        span_a = test_span_factory(name="A", span_type=SpanType.AGENT_LOOP, start_time=1)
        span_b = test_span_factory(name="B", span_type=SpanType.LLM_CALL, parent_span_id=span_a.context.span_id, start_time=2, status="error")
        span_c = test_span_factory(name="C", span_type=SpanType.LLM_CALL, parent_span_id=span_a.context.span_id, start_time=3, status="ok")
        
        spans = [span_a, span_b, span_c]
        
        tree = TraceTreeBuilder().build(spans)
        metrics = compute_tree_metrics(tree)
        
        assert metrics.max_depth == 2
        assert metrics.total_spans == 3
        assert metrics.leaf_count == 2
        assert metrics.error_leaf_count == 1

    def test_compute_metrics_deep_tree(self, test_span_factory):
        """Test metrics for a deeper tree."""
        # Create spans first to get their actual span_ids
        span_a = test_span_factory(name="A", span_type=SpanType.AGENT_LOOP, start_time=1)
        span_b = test_span_factory(name="B", span_type=SpanType.LLM_CALL, parent_span_id=span_a.context.span_id, start_time=2)
        span_c = test_span_factory(name="C", span_type=SpanType.LLM_CALL, parent_span_id=span_a.context.span_id, start_time=3)
        span_d = test_span_factory(name="D", span_type=SpanType.LLM_CALL, parent_span_id=span_b.context.span_id, start_time=4)
        span_e = test_span_factory(name="E", span_type=SpanType.LLM_CALL, parent_span_id=span_c.context.span_id, start_time=5)
        
        spans = [span_a, span_b, span_c, span_d, span_e]
        
        # Build 3-level tree: A -> B -> D, A -> C -> E
        tree = TraceTreeBuilder().build(spans)
        metrics = compute_tree_metrics(tree)
        
        assert metrics.max_depth == 3
        assert metrics.total_spans == 5
        assert metrics.leaf_count == 2
        assert metrics.error_leaf_count == 0


class TestIsAggregateSpan:
    """Test is_aggregate_span helper."""

    def test_is_aggregate_true(self):
        """Test detection of aggregate spans."""
        context = SpanContext.new(agent_id="test")
        span = Span(
            name="aggregated",
            span_type=SpanType.AGENT_REASONING,
            context=context,
            attributes={"aggregation.children_count": 3, "aggregation.bytes_saved": 200},
        )
        assert is_aggregate_span(span) is True

    def test_is_aggregate_false(self):
        """Test detection of non-aggregate spans."""
        context = SpanContext.new(agent_id="test")
        span = Span(
            name="normal",
            span_type=SpanType.LLM_CALL,
            context=context,
            attributes={"status": "ok"},
        )
        assert is_aggregate_span(span) is False


class TestTraceTreeBuilder:
    """Test TraceTreeBuilder.build method."""

    def test_empty_list_raises(self):
        """Test empty span list raises ValueError."""
        builder = TraceTreeBuilder()
        with pytest.raises(ValueError, match="empty span list"):
            builder.build([])

    def test_duplicate_span_id_raises(self):
        """Test duplicate span_id raises ValueError."""
        context = SpanContext.new(agent_id="test")
        span = Span(name="test", span_type=SpanType.AGENT_LOOP, context=context)
        spans = [span, span]  # Duplicate span
        
        builder = TraceTreeBuilder()
        with pytest.raises(ValueError, match="duplicate span_id"):
            builder.build(spans)

    def test_simple_3_level_tree(self, test_span_factory):
        """Test building 3-level tree from 5 spans."""
        # Create spans first to get their actual span_ids
        span_root = test_span_factory(name="root", span_type=SpanType.AGENT_LOOP, start_time=1)
        span_mid1 = test_span_factory(name="mid1", span_type=SpanType.LLM_CALL, parent_span_id=span_root.context.span_id, start_time=2)
        span_mid2 = test_span_factory(name="mid2", span_type=SpanType.LLM_CALL, parent_span_id=span_root.context.span_id, start_time=3)
        span_leaf1 = test_span_factory(name="leaf1", span_type=SpanType.TOOL_CALL, parent_span_id=span_mid1.context.span_id, start_time=4)
        span_leaf2 = test_span_factory(name="leaf2", span_type=SpanType.TOOL_CALL, parent_span_id=span_mid2.context.span_id, start_time=5)
        
        spans = [span_root, span_mid1, span_mid2, span_leaf1, span_leaf2]
        
        # Build tree: root -> mid1 -> leaf1, root -> mid2 -> leaf2
        tree = TraceTreeBuilder().build(spans)
        
        assert tree.root.name == "root"
        assert len(tree.children) == 2
        assert tree.children[0].root.name == "mid2"  # Sorted by start_time reverse (3 > 2)
        assert tree.children[1].root.name == "mid1"
        assert len(tree.children[0].children) == 1
        assert len(tree.children[1].children) == 1  # mid1 has leaf1 as child

    def test_root_is_agent_loop(self, test_span_factory):
        """Test single agent.loop span becomes root without virtual root."""
        spans = [
            test_span_factory(name="root", span_type=SpanType.AGENT_LOOP, start_time=1),
        ]
        
        tree = TraceTreeBuilder().build(spans)
        
        assert tree.root.name == "root"
        assert tree.root.span_type == SpanType.AGENT_LOOP
        assert len(tree.children) == 0
        # No virtual root
        assert tree.root.attributes.get("tree.virtual_root") is not True

    def test_orphan_span_creates_virtual_root(self, test_span_factory):
        """Test orphan span creates virtual root."""
        spans = [
            test_span_factory(name="orphan", span_type=SpanType.LLM_CALL, start_time=1),
        ]
        
        tree = TraceTreeBuilder().build(spans)
        
        # Should have virtual root
        assert tree.root.name == "trace:virtual_root"
        assert tree.root.attributes.get("tree.virtual_root") is True
        assert len(tree.children) == 1
        assert tree.children[0].root.name == "orphan"

    def test_multiple_parentless_spans_create_virtual_root(self, test_span_factory):
        """Test multiple parentless spans create virtual root."""
        spans = [
            test_span_factory(name="span1", span_type=SpanType.LLM_CALL, start_time=1),
            test_span_factory(name="span2", span_type=SpanType.LLM_CALL, start_time=2),
        ]
        
        tree = TraceTreeBuilder().build(spans)
        
        assert tree.root.name == "trace:virtual_root"
        assert len(tree.children) == 2
        # Children sorted by start_time
        assert tree.children[0].root.name == "span1"
        assert tree.children[1].root.name == "span2"

    def test_cycle_detection_raises(self):
        """Test cycle A->B->A raises ValueError."""
        context1 = SpanContext.new(agent_id="test")
        context2 = SpanContext.new(agent_id="test", parent=context1)
        
        # Make span1 parent of span2, and span2 parent of span1 (cycle)
        spans = [
            Span(name="span1", span_type=SpanType.LLM_CALL, context=context1, start_time=1),
            Span(name="span2", span_type=SpanType.LLM_CALL, context=context2, start_time=2),
        ]
        
        # Manually create cycle by modifying parent_span_id
        spans[0].context.parent_span_id = spans[1].context.span_id
        
        builder = TraceTreeBuilder()
        with pytest.raises(ValueError, match="cycle detected"):
            builder.build(spans)

    def test_self_cycle_detection_raises(self):
        """Test self-parent cycle raises ValueError."""
        context = SpanContext.new(agent_id="test")
        span = Span(name="self_cycle", span_type=SpanType.LLM_CALL, context=context, start_time=1)
        
        # Self-parent
        span.context.parent_span_id = span.context.span_id
        
        builder = TraceTreeBuilder()
        with pytest.raises(ValueError, match="cycle detected"):
            builder.build([span])

    def test_aggregate_span_handling(self, test_span_factory):
        """Test aggregate span handling (events not nodes, children preserved)."""
        # Create spans first to get their actual span_ids
        span_root = test_span_factory(name="root", span_type=SpanType.AGENT_LOOP, start_time=1)
        span_aggregate = test_span_factory(
            name="aggregate",
            span_type=SpanType.AGENT_REASONING,
            start_time=2,
            is_aggregate=True,
        )
        span_child = test_span_factory(name="child", span_type=SpanType.LLM_CALL, start_time=3)
        
        # Make aggregate parent of child (normal parent-child relationship)
        span_child.context.parent_span_id = span_aggregate.context.span_id
        
        spans = [span_root, span_aggregate, span_child]
        
        tree = TraceTreeBuilder().build(spans)
        
        # Virtual root should have both root and aggregate as children
        assert len(tree.children) == 2
        # Children should be sorted by start_time: root (1), aggregate (2)
        assert tree.children[0].root.name == "root"
        assert tree.children[1].root.name == "aggregate"
        # Aggregate should have the child span as its child
        assert len(tree.children[1].children) == 1
        assert tree.children[1].children[0].root.name == "child"

    def test_deterministic_child_ordering(self, test_span_factory):
        """Test children are sorted by start_time."""
        # Create spans first to get their actual span_ids
        span_root = test_span_factory(name="root", span_type=SpanType.AGENT_LOOP, start_time=1)
        span_child1 = test_span_factory(name="child1", span_type=SpanType.LLM_CALL, parent_span_id=span_root.context.span_id, start_time=3)
        span_child2 = test_span_factory(name="child2", span_type=SpanType.LLM_CALL, parent_span_id=span_root.context.span_id, start_time=2)
        span_child3 = test_span_factory(name="child3", span_type=SpanType.LLM_CALL, parent_span_id=span_root.context.span_id, start_time=4)
        
        spans = [span_root, span_child1, span_child2, span_child3]
        
        tree = TraceTreeBuilder().build(spans)
        
        # Children should be sorted by start_time reverse: child3 (4), child1 (3), child2 (2)
        assert [child.root.name for child in tree.children] == ["child3", "child1", "child2"]

    def test_metrics_computation_integration(self, test_span_factory):
        """Test that metrics are computed correctly for complex tree."""
        # Create spans first to get their actual span_ids
        span_root = test_span_factory(name="root", span_type=SpanType.AGENT_LOOP, start_time=1)
        span_mid1 = test_span_factory(name="mid1", span_type=SpanType.LLM_CALL, parent_span_id=span_root.context.span_id, start_time=2)
        span_mid2 = test_span_factory(name="mid2", span_type=SpanType.LLM_CALL, parent_span_id=span_root.context.span_id, start_time=3)
        span_leaf1 = test_span_factory(name="leaf1", span_type=SpanType.TOOL_CALL, parent_span_id=span_mid1.context.span_id, start_time=4, status="error")
        span_leaf2 = test_span_factory(name="leaf2", span_type=SpanType.TOOL_CALL, parent_span_id=span_mid2.context.span_id, start_time=5)
        
        spans = [span_root, span_mid1, span_mid2, span_leaf1, span_leaf2]
        
        tree = TraceTreeBuilder().build(spans)
        metrics = compute_tree_metrics(tree)
        
        assert metrics.max_depth == 3
        assert metrics.total_spans == 5
        assert metrics.leaf_count == 2
        assert metrics.error_leaf_count == 1  # leaf1 has status="error"