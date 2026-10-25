"""Trace tree construction and traversal for long traces compression (PP16)."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from agent_obs.observability import Span, SpanContext, SpanType


@dataclass
class TraceTree:
    """Tree structure for trace spans with compression support.

    Canonical contract from PP0 PRODUCTION-PROMPTS.md — cannot be modified.
    """

    root: Span
    children: list[TraceTree] = field(default_factory=list)
    compressed: bool = False
    compression_ratio: float = 0.0

    def to_dict(self) -> dict:
        """Serialize recursively to {root: dict, children: [...], compressed, compression_ratio}."""
        return {
            "root": self.root.to_dict(),
            "children": [child.to_dict() for child in self.children],
            "compressed": self.compressed,
            "compression_ratio": self.compression_ratio,
        }

    async def iter_dfs(self) -> AsyncIterator[TraceTree]:
        """Depth-first traversal (async generator)."""
        stack = [self]
        while stack:
            node = stack.pop()
            yield node
            # Add children in reverse order for left-to-right DFS
            stack.extend(reversed(node.children))

    async def iter_bfs(self) -> AsyncIterator[TraceTree]:
        """Breadth-first traversal (async generator)."""
        queue: asyncio.Queue[TraceTree] = asyncio.Queue()
        await queue.put(self)
        while not queue.empty():
            node = await queue.get()
            yield node
            for child in node.children:
                await queue.put(child)


@dataclass(frozen=True)
class TreeMetrics:
    """Computed metrics for a trace tree."""

    max_depth: int
    total_spans: int
    leaf_count: int
    error_leaf_count: int


def is_aggregate_span(span: Span) -> bool:
    """Check if span is an aggregate (contains aggregation metadata)."""
    return "aggregation.children_count" in span.attributes


def compute_tree_metrics(tree: TraceTree) -> TreeMetrics:
    """Compute metrics for a trace tree via DFS."""
    class _MetricsState:
        def __init__(self):
            self.max_depth = 0
            self.total_spans = 0
            self.leaf_count = 0
            self.error_leaf_count = 0

    state = _MetricsState()

    def _compute_metrics_dfs(node: TraceTree, depth: int) -> None:
        # Skip virtual root from metrics, but continue with children at depth 1
        if node.root.attributes.get("tree.virtual_root"):
            for child in node.children:
                _compute_metrics_dfs(child, 1)
            return
            
        state.max_depth = max(state.max_depth, depth)
        state.total_spans += 1

        if not node.children:
            state.leaf_count += 1
            # Check if leaf is an error
            if node.root.attributes.get("status") == "error":
                state.error_leaf_count += 1

        for child in node.children:
            _compute_metrics_dfs(child, depth + 1)

    # Run function
    _compute_metrics_dfs(tree, 1)

    return TreeMetrics(
        max_depth=state.max_depth,
        total_spans=state.total_spans,
        leaf_count=state.leaf_count,
        error_leaf_count=state.error_leaf_count,
    )


class TraceTreeBuilder:
    """Builds TraceTree from flat span list by parent_span_id."""

    def build(self, spans: list[Span]) -> TraceTree:
        """Build tree from flat span list.
        
        Args:
            spans: List of spans with parent_span_id references
            
        Returns:
            TraceTree with root and children
            
        Raises:
            ValueError: If spans list is empty, contains duplicates, or has cycles
        """
        if not spans:
            raise ValueError("empty span list")

        # Check for duplicate span_ids
        span_ids = set()
        for span in spans:
            if span.context.span_id in span_ids:
                raise ValueError(f"duplicate span_id: {span.context.span_id}")
            span_ids.add(span.context.span_id)

        # Index spans by span_id - include aggregate spans so children can find them
        span_index: dict[str, Span] = {span.context.span_id: span for span in spans}
# Find root candidates: spans with no parent or parent not in index
        root_candidates: list[Span] = []
        
        for span in spans:
            parent_span_id = span.context.parent_span_id
            if not parent_span_id or parent_span_id not in span_index:
                root_candidates.append(span)

        # Detect cycles and build reachability
        visited: set[str] = set()
        cycle_spans: set[str] = set()

        def _mark_reachable(start_span: Span, path: set[str]) -> None:
            """Mark all spans reachable from start_span, detecting cycles."""
            if start_span.context.span_id in path:
                cycle_spans.add(start_span.context.span_id)
                return

            if start_span.context.span_id in visited:
                return

            visited.add(start_span.context.span_id)
            path.add(start_span.context.span_id)

            # Find children (spans whose parent is this span)
            for child in spans:
                if child.context.parent_span_id == start_span.context.span_id:
                    _mark_reachable(child, path.copy())

        # Mark reachable from all candidates
        for candidate in root_candidates:
            _mark_reachable(candidate, set())

        # Any unvisited span indicates a cycle
        unvisited = span_ids - visited
        if unvisited:
            raise ValueError(f"cycle detected involving spans: {unvisited}")

        # Build tree structure
        tree_roots: list[TraceTree] = []

        def _build_children(parent: Span) -> list[TraceTree]:
            """Recursively build children trees for a parent span."""
            children_trees = []
            for span in spans:
                if span.context.parent_span_id == parent.context.span_id:
                    child_tree = TraceTree(
                        root=span,
                        children=_build_children(span),
                        compressed=False,  # Will be set by PP17
                        compression_ratio=0.0,  # Will be set by PP17
                    )
                    children_trees.append(child_tree)
            
            # Sort children by start_time for deterministic order (descending for DFS pre-order)
            children_trees.sort(key=lambda t: t.root.start_time, reverse=True)
            return children_trees

        # Build trees for root candidates
        for candidate in root_candidates:
            tree = TraceTree(
                root=candidate,
                children=_build_children(candidate),
                compressed=False,
                compression_ratio=0.0,
            )
            tree_roots.append(tree)

        # Determine root: if exactly one candidate and it's agent.loop, use it directly
        if len(tree_roots) == 1 and tree_roots[0].root.span_type == SpanType.AGENT_LOOP:
            return tree_roots[0]

        # Otherwise, create virtual root
        if not tree_roots:
            # This shouldn't happen since we have root_candidates
            raise ValueError("no valid root candidates found")

        # Get trace_id from first span for virtual root
        trace_id = spans[0].context.trace_id

        # Create virtual root context
        virtual_root_context = SpanContext(
            trace_id=trace_id,
            span_id=f"virtual_root_{time.time()}",
            parent_span_id=None,
            agent_id=spans[0].context.agent_id,
            agent_version=spans[0].context.agent_version,
            user_id=spans[0].context.user_id,
            session_id=spans[0].context.session_id,
        )

        # Create virtual root span
        virtual_root_span = Span(
            name="trace:virtual_root",
            span_type=SpanType.AGENT_LOOP,
            context=virtual_root_context,
            attributes={"tree.virtual_root": True},
            start_time=min(span.start_time for span in spans),
        )

        # Create virtual root tree with all root candidates as children
        virtual_root = TraceTree(
            root=virtual_root_span,
            children=tree_roots,
            compressed=False,
            compression_ratio=0.0,
        )

        return virtual_root