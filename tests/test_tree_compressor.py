"""Tests for TraceTreeBuilder and tree compression utilities."""

from unittest.mock import Mock, patch

import pytest

from agent_obs.observability import Span, SpanContext, SpanType
from agent_obs.tree_compressor import (
    CompressionPolicy,
    TraceTree,
    TraceTreeBuilder,
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


class TestCompressionPolicy:
    """Test CompressionPolicy.compress method."""

    def test_compress_50_nodes_to_one_summary(self, test_span_factory):
        """Test: 50 узлов → 1 summary."""
        # Create a linear chain of 50 spans
        spans = []
        parent_id = None
        for i in range(50):
            span = test_span_factory(
                name=f"step{i}", 
                span_type=SpanType.AGENT_REASONING,
                parent_span_id=parent_id,
                start_time=1.0 + i * 0.1
            )
            spans.append(span)
            parent_id = span.context.span_id
        
        tree = TraceTreeBuilder().build(spans)
        policy = CompressionPolicy(max_subtree_size=20)
        compressed = policy.compress(tree)
        
        # Should have exactly one summary node
        summary_spans = []
        # Use synchronous traversal
        stack = [compressed]
        while stack:
            node = stack.pop()
            if node.root.span_type == SpanType.AGENT_REASONING_COMPRESSED:
                summary_spans.append(node.root)
            stack.extend(reversed(node.children))
        
        assert len(summary_spans) == 1
        assert summary_spans[0].name == "compressed:50"
        assert summary_spans[0].attributes["tree.children_count"] == 50
        assert summary_spans[0].attributes["tree.error_count"] == 0
        assert compressed.compression_ratio > 0.0

    def test_compress_50_nodes_with_error_no_compression(self, test_span_factory):
        """Test: 50 узлов с 1 error → НЕ сжимается (error сохраняется в full tree)."""
        # Create a chain of 50 spans, mark 10th as error
        spans = []
        parent_id = None
        for i in range(50):
            status = "error" if i == 10 else "ok"
            span = test_span_factory(
                name=f"step{i}", 
                span_type=SpanType.AGENT_REASONING,
                parent_span_id=parent_id,
                status=status,
                start_time=1.0 + i * 0.1
            )
            spans.append(span)
            parent_id = span.context.span_id
        
        tree = TraceTreeBuilder().build(spans)
        policy = CompressionPolicy(max_subtree_size=20)
        compressed = policy.compress(tree)
        
        # No summary nodes should exist because error is in the middle
        # and all large subtrees contain the error
        summary_spans = []
        # Since error is at position 10, subtrees containing positions 10-49
        # cannot be compressed. Subtrees before position 10 are small.
        assert len(summary_spans) == 0
        # Use synchronous traversal
        stack = [compressed]
        while stack:
            node = stack.pop()
            if node.root.span_type == SpanType.AGENT_REASONING_COMPRESSED:
                summary_spans.append(node.root)
            stack.extend(reversed(node.children))
        
        assert len(summary_spans) == 0
        # Error span should still exist
        error_spans = [s for s in spans if s.attributes.get("status") == "error"]
        assert len(error_spans) == 1

    def test_compress_21_nodes_to_one_summary(self, test_span_factory):
        """Test: 21 узел без errors → 1 summary."""
        # Create a chain of 21 spans
        spans = []
        parent_id = None
        for i in range(21):
            span = test_span_factory(
                name=f"step{i}", 
                span_type=SpanType.AGENT_REASONING,
                parent_span_id=parent_id,
                start_time=1.0 + i * 0.1
            )
            spans.append(span)
            parent_id = span.context.span_id
        
        tree = TraceTreeBuilder().build(spans)
        policy = CompressionPolicy(max_subtree_size=20)
        compressed = policy.compress(tree)
        
        # Should have exactly one summary node
        summary_spans = []
        # Use synchronous traversal
        stack = [compressed]
        while stack:
            node = stack.pop()
            if node.root.span_type == SpanType.AGENT_REASONING_COMPRESSED:
                summary_spans.append(node.root)
            stack.extend(reversed(node.children))
        
        assert len(summary_spans) == 1
        assert summary_spans[0].name == "compressed:21"
        assert summary_spans[0].attributes["tree.children_count"] == 21

    def test_threshold_boundary(self, test_span_factory):
        """Test: exactly 20 → not compressed; 21 → compressed."""
        # Test exactly 20 nodes
        spans_20 = []
        parent_id = None
        for i in range(20):
            span = test_span_factory(
                name=f"step{i}", 
                span_type=SpanType.AGENT_REASONING,
                parent_span_id=parent_id,
                start_time=1.0 + i * 0.1
            )
            spans_20.append(span)
            parent_id = span.context.span_id
        
        tree_20 = TraceTreeBuilder().build(spans_20)
        policy = CompressionPolicy(max_subtree_size=20)
        compressed_20 = policy.compress(tree_20)
        
        # No compression for exactly 20
        summary_spans_20 = []
        # Use synchronous traversal
        stack = [compressed_20]
        while stack:
            node = stack.pop()
            if node.root.span_type == SpanType.AGENT_REASONING_COMPRESSED:
                summary_spans_20.append(node.root)
            stack.extend(reversed(node.children))
        assert len(summary_spans_20) == 0
        
        # Test 21 nodes (should compress)
        span_21 = test_span_factory(
            name="step20", 
            span_type=SpanType.AGENT_REASONING,
            parent_span_id=parent_id,
            start_time=1.0 + 20 * 0.1
        )
        spans_21 = spans_20 + [span_21]
        tree_21 = TraceTreeBuilder().build(spans_21)
        compressed_21 = policy.compress(tree_21)
        
        # Should compress 21-node subtree
        summary_spans_21 = []
        # Use synchronous traversal
        stack = [compressed_21]
        while stack:
            node = stack.pop()
            if node.root.span_type == SpanType.AGENT_REASONING_COMPRESSED:
                summary_spans_21.append(node.root)
            stack.extend(reversed(node.children))
        assert len(summary_spans_21) == 1

    def test_idempotency(self, test_span_factory):
        """Test: compress(compress(tree)) == compress(tree)."""
        # Create a compressible tree
        spans = []
        parent_id = None
        for i in range(25):
            span = test_span_factory(
                name=f"step{i}", 
                span_type=SpanType.AGENT_REASONING,
                parent_span_id=parent_id,
                start_time=1.0 + i * 0.1
            )
            spans.append(span)
            parent_id = span.context.span_id
        
        tree = TraceTreeBuilder().build(spans)
        policy = CompressionPolicy(max_subtree_size=20)
        
        # Compress twice
        compressed1 = policy.compress(tree)
        compressed2 = policy.compress(compressed1)
        
        # Should be identical
        assert compressed1.to_dict() == compressed2.to_dict()

    def test_root_never_replaced(self, test_span_factory):
        """Test: корень никогда не заменяется."""
        # Create tree with root and large subtree
        spans = []
        parent_id = None
        for i in range(25):
            span = test_span_factory(
                name=f"step{i}", 
                span_type=SpanType.AGENT_REASONING if i > 0 else SpanType.AGENT_LOOP,
                parent_span_id=parent_id,
                start_time=1.0 + i * 0.1
            )
            spans.append(span)
            parent_id = span.context.span_id
        
        tree = TraceTreeBuilder().build(spans)
        policy = CompressionPolicy(max_subtree_size=20)
        compressed = policy.compress(tree)
        
        # Root should still be AGENT_LOOP
        assert compressed.root.span_type == SpanType.AGENT_LOOP
        assert compressed.root.name == "step0"
        # But should be marked as containing compression
        assert compressed.compressed

    def test_error_in_one_branch_only(self, test_span_factory):
        """Test: error только в одной ветке → соседняя чистая большая ветка сжимается."""
        # Create two branches from root, one with error, one clean and large
        spans = []
        
        # Root
        root = test_span_factory(name="root", span_type=SpanType.AGENT_LOOP, start_time=1.0)
        spans.append(root)
        
        # Branch 1: clean, large (25 nodes)
        parent_id = root.context.span_id
        for i in range(25):
            span = test_span_factory(
                name=f"clean{i}", 
                span_type=SpanType.AGENT_REASONING,
                parent_span_id=parent_id,
                start_time=2.0 + i * 0.1
            )
            spans.append(span)
            parent_id = span.context.span_id
        
        # Branch 2: with error (15 nodes)
        parent_id = root.context.span_id
        for i in range(15):
            status = "error" if i == 5 else "ok"
            span = test_span_factory(
                name=f"error{i}",
                span_type=SpanType.AGENT_REASONING,
                parent_span_id=parent_id,
                status=status,
                start_time=2.0 + 25 * 0.1 + i * 0.1
            )
            spans.append(span)
            parent_id = span.context.span_id
    
        tree = TraceTreeBuilder().build(spans)
        policy = CompressionPolicy(max_subtree_size=20)
        compressed = policy.compress(tree)
        
        # Clean branch should be compressed, error branch should not
        summary_spans = []
        # Use synchronous traversal
        stack = [compressed]
        while stack:
            node = stack.pop()
            if node.root.span_type == SpanType.AGENT_REASONING_COMPRESSED:
                summary_spans.append(node.root)
            stack.extend(reversed(node.children))
        
        assert len(summary_spans) == 1
        # Check summary is from clean branch (size should be 25, not 15)
        assert summary_spans[0].attributes["tree.children_count"] == 25

    def test_env_threshold_respected(self, test_span_factory, monkeypatch):
        """Test: env TREE_COMPRESSION_THRESHOLD меняет порог."""
        # Create 25-node tree
        spans = []
        parent_id = None
        for i in range(25):
            span = test_span_factory(
                name=f"step{i}", 
                span_type=SpanType.AGENT_REASONING,
                parent_span_id=parent_id,
                start_time=1.0 + i * 0.1
            )
            spans.append(span)
            parent_id = span.context.span_id
        
        tree = TraceTreeBuilder().build(spans)
        
        # Set threshold to 30 via env
        monkeypatch.setenv("TREE_COMPRESSION_THRESHOLD", "30")
        policy = CompressionPolicy.from_env()
        compressed = policy.compress(tree)
        
        # Should not compress (25 < 30)
        summary_spans = []
        # Use synchronous traversal
        stack = [compressed]
        while stack:
            node = stack.pop()
            if node.root.span_type == SpanType.AGENT_REASONING_COMPRESSED:
                summary_spans.append(node.root)
            stack.extend(reversed(node.children))
        assert len(summary_spans) == 0

    def test_metric_emission(self, test_span_factory):
        """Test: metric gauge установлен."""
        # Create 25-node tree
        spans = []
        parent_id = None
        for i in range(25):
            span = test_span_factory(
                name=f"step{i}", 
                span_type=SpanType.AGENT_REASONING,
                parent_span_id=parent_id,
                start_time=1.0 + i * 0.1
            )
            spans.append(span)
            parent_id = span.context.span_id
        
        tree = TraceTreeBuilder().build(spans)
        policy = CompressionPolicy(max_subtree_size=20)
        
        # Mock the gauge to capture set calls
        # Create a mock gauge and assign it to the module
        mock_gauge = Mock()
        mock_labeled_gauge = Mock()
        mock_gauge.labels.return_value = mock_labeled_gauge
        
        # Patch the gauge in the tree_compressor module
        with patch("agent_obs.tree_compressor.tree_compression_ratio", mock_gauge):
            policy.compress(tree)
            
            
            
            # Verify gauge was called
            mock_gauge.labels.assert_called_once_with(agent_id="test-agent")
            mock_labeled_gauge.set.assert_called_once()
            # Check the value is between 0 and 1
            value = mock_labeled_gauge.set.call_args[0][0]
            assert 0.0 <= value <= 1.0