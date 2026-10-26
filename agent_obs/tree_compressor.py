"""Trace tree construction and traversal for long traces compression (PP16+PP17)."""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from agent_obs.metrics import tree_compression_ratio
from agent_obs.observability import Span, SpanContext, SpanType

# PP17 Policy version for idempotency
COMPRESSION_POLICY_VERSION = "1"


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


def make_summary_node(replaced: TraceTree, subtree_size: int, total_cost: float) -> Span:
    """Create a summary span to replace a subtree.
    
    Args:
        replaced: The TraceTree node being replaced
        subtree_size: Total number of nodes in the compressed subtree
        total_cost: Sum of cost.usd + cost.usd_sum over all nodes in subtree
    
    Returns:
        A Span representing the compressed subtree
    """
    # Copy context from replaced node to preserve referential integrity for drill-down
    summary_context = SpanContext(
        trace_id=replaced.root.context.trace_id,
        span_id=replaced.root.context.span_id,  # Keep original span_id for PP19 drill-down
        parent_span_id=replaced.root.context.parent_span_id,
        agent_id=replaced.root.context.agent_id,
        agent_version=replaced.root.context.agent_version,
        user_id=replaced.root.context.user_id,
        session_id=replaced.root.context.session_id,
        tool_name=replaced.root.context.tool_name,
    )
    
    # Build attributes per PP17 spec
    attributes = {
        "tree.compressed": True,
        "tree.children_count": subtree_size,
        "tree.total_cost": total_cost,
        "tree.error_count": 0,  # Error subtrees never compressed
        "tree.summary": "",  # Placeholder; PP18 will set "pending"
        "tree.policy_version": COMPRESSION_POLICY_VERSION,
    }
    
    return Span(
        name=f"compressed:{subtree_size}",
        span_type=SpanType.AGENT_REASONING_COMPRESSED,
        context=summary_context,
        attributes=attributes,
        start_time=replaced.root.start_time,
    )


def _compute_subtree_metrics(tree: TraceTree) -> tuple[dict[str, int], dict[str, bool]]:
    """Compute subtree size and has_error for each node via iterative post-order traversal.
    
    Returns:
        Tuple of (size_map: node_id -> size, error_map: node_id -> has_error)
    """
    size_map = {}
    error_map = {}
    
    # Use a stack for iterative post-order traversal
    # Each element: (node, children_iterator)
    stack: list[tuple[TraceTree, AsyncIterator[TraceTree]]] = []
    # Start with the root and its children iterator
    stack.append((tree, iter(tree.children)))
    
    while stack:
        node, children_iter = stack[-1]
        node_id = node.root.context.span_id
        
        try:
            # Get next child
            child = next(children_iter)
            # Push child onto stack with its own children iterator
            stack.append((child, iter(child.children)))
        except StopIteration:
            # All children processed, compute metrics for this node
            stack.pop()
            
            # Compute size and error for this node
            size = 1  # Self
            has_error = node.root.attributes.get("status") == "error"
            
            # Add metrics from all children
            for child in node.children:
                child_id = child.root.context.span_id
                size += size_map[child_id]
                has_error = has_error or error_map[child_id]
            
            size_map[node_id] = size
            error_map[node_id] = has_error

    return size_map, error_map


def _propagate_error_status(tree: TraceTree, error_map: dict[str, bool]) -> dict[str, bool]:
    """Propagate error status from ancestors to descendants.
    
    Args:
        tree: The trace tree
        error_map: Initial error map from _compute_subtree_metrics
        
    Returns:
        Updated error_map with error status propagated from ancestors
    """
    # Use BFS to propagate error status from ancestors to descendants
    from collections import deque
    
    # Start propagation from each child of the root separately
    for child in tree.children:
        queue = deque([child])
        
        while queue:
            current = queue.popleft()
            current_id = current.root.context.span_id
            
            # If current node has error, propagate to all descendants
            if error_map.get(current_id, False):
                # Use BFS to propagate to all descendants
                descendant_queue = deque([current])
                while descendant_queue:
                    node = descendant_queue.popleft()
                    node_id = node.root.context.span_id
                    # Mark this node as having error
                    error_map[node_id] = True
                    # Add children to queue for propagation
                    descendant_queue.extend(node.children)
            
            # Add current node's children to main queue
            queue.extend(current.children)
    
    return error_map


def _rebuild_tree_with_compression(
    tree: TraceTree, 
    size_map: dict[str, int], 
    error_map: dict[str, bool],
    policy: "CompressionPolicy"
) -> tuple[TraceTree, int]:
    """Rebuild tree with compression applied top-down.
    
    Args:
        tree: Input tree to compress
        size_map: Precomputed subtree sizes
        error_map: Precomputed error flags
        policy: Compression policy with threshold
    
    Returns:
        Tuple of (new TraceTree with compression applied, number of new summary nodes created)
    """
    new_summaries = 0

    # For non-root nodes, check if eligible for compression
    def _should_compress(node: TraceTree) -> bool:
        node_id = node.root.context.span_id
        
        # Skip if already compressed or root
        is_compressed = node.root.span_type == SpanType.AGENT_REASONING_COMPRESSED or node.compressed
        is_root = node.root.attributes.get("tree.virtual_root") or tree.root == node
        
        # Skip if node_id not in size_map (already compressed tree)
        if node_id not in size_map:
            return False
        
        size = size_map[node_id]
        has_error = error_map[node_id]
        
        return (
            not is_root and 
            not is_compressed and 
            size > policy.max_subtree_size and 
            not has_error
        )
    
    # Recursive rebuild with compression
    def _build_compressed_children(current: TraceTree) -> list[TraceTree]:
        nonlocal new_summaries
        children = []
        
        for child_tree in current.children:
            if _should_compress(child_tree):
                # Replace child with summary node
                child_id = child_tree.root.context.span_id
                subtree_size = size_map[child_id]
                
                # Compute total cost
                total_cost = 0.0
                # Use synchronous traversal for cost computation
                stack = [child_tree]
                while stack:
                    node = stack.pop()
                    cost_usd = node.root.attributes.get("cost.usd", 0.0)
                    cost_usd_sum = node.root.attributes.get("cost.usd_sum", 0.0)
                    total_cost += float(cost_usd) + float(cost_usd_sum)
                    stack.extend(reversed(node.children))  # For DFS order
                
                summary_span = make_summary_node(child_tree, subtree_size, total_cost)
                summary_tree = TraceTree(
                    root=summary_span,
                    children=[],  # Summary nodes are leaves
                    compressed=True,
                    compression_ratio=0.0,
                )
                children.append(summary_tree)
                new_summaries += 1
            else:
                # Keep the child node, recurse into its descendants
                child_copy = TraceTree(
                    root=child_tree.root,
                    children=_build_compressed_children(child_tree),
                    compressed=child_tree.compressed,
                    compression_ratio=child_tree.compression_ratio,
                )
                children.append(child_copy)
        
        return children
    
    # Rebuild root and mark compressed nodes
    new_children = _build_compressed_children(tree)
    
    # Mark nodes containing summaries as compressed
    def _mark_compressed_nodes(node: TraceTree) -> None:
        has_compressed_child = False
        for child in node.children:
            if child.compressed:
                has_compressed_child = True
                break
        if has_compressed_child:
            node.compressed = True
        for child in node.children:
            _mark_compressed_nodes(child)
    
    new_tree = TraceTree(
        root=tree.root,
        children=new_children,
        compressed=False,  # Will be set by _mark_compressed_nodes
        compression_ratio=0.0,  # Will be set later
    )
    
    _mark_compressed_nodes(new_tree)
    return new_tree, new_summaries


@dataclass
class CompressionPolicy:
    """Policy for compressing trace trees when subtrees exceed size threshold.
    
    PP17: Compression policy: поддерево > 20 узлов сжимается в summary-node
    """
    
    max_subtree_size: int
    policy_version: str = COMPRESSION_POLICY_VERSION
    
    @classmethod
    def from_env(cls, policy_version: str = COMPRESSION_POLICY_VERSION) -> "CompressionPolicy":
        """Create policy from environment variable.
        
        Priority: param > env > default (20)
        """
        default_size = 20
        try:
            env_size = int(os.environ.get("TREE_COMPRESSION_THRESHOLD", str(default_size)))
            if env_size <= 0:
                raise ValueError(f"Invalid TREE_COMPRESSION_THRESHOLD: {env_size}, must be > 0")
        except ValueError as e:
            import warnings
            warnings.warn(f"Invalid TREE_COMPRESSION_THRESHOLD: {e}, using default {default_size}")
            env_size = default_size
        
        return cls(max_subtree_size=env_size, policy_version=policy_version)
    
    def compress(self, tree: TraceTree) -> TraceTree:
        """Compress tree by replacing subtrees > max_subtree_size with summary nodes.
        
        Args:
            tree: Input TraceTree to compress
            
        Returns:
            New TraceTree with compression applied
        """
        # Compute subtree sizes and error flags
        size_map, error_map = _compute_subtree_metrics(tree)
        
        # Propagate error status from ancestors to descendants
        error_map = _propagate_error_status(tree, error_map)
        
        # Rebuild tree with compression
        compressed_tree, new_summaries = _rebuild_tree_with_compression(tree, size_map, error_map, self)
        
        # Idempotency: if no new summaries were created, preserve existing compression_ratio
        if new_summaries == 0 and tree.compression_ratio > 0.0:
            compression_ratio = tree.compression_ratio
        else:
            # Compute compression ratio
            # Find the original root span ID in the size_map
            original_count = None
            for node_id, size in size_map.items():
                if tree.root.attributes.get("tree.virtual_root") and "virtual_root" in node_id:
                    original_count = size
                    break
                elif node_id == tree.root.context.span_id:
                    original_count = size
                    break
            
            if original_count is None:
                # Fallback: count all non-compressed nodes
                original_count = 0
                for node_id, size in size_map.items():
                    if "virtual_root" not in node_id:
                        original_count += size
            
            compressed_count = 0
            
            def count_nodes(t: TraceTree) -> None:
                nonlocal compressed_count
                if t.root.span_type != SpanType.AGENT_REASONING_COMPRESSED:
                    compressed_count += 1
            
            count_nodes(compressed_tree)
            compression_ratio = max(0.0, min(1.0, (original_count - compressed_count) / original_count))
        
        # Update compression ratio on all nodes
        def set_ratio(t: TraceTree) -> None:
            t.compression_ratio = compression_ratio
            for child in t.children:
                set_ratio(child)
        
        set_ratio(compressed_tree)
        
# Update metric
        agent_id = tree.root.context.agent_id or "unknown"
        tree_compression_ratio.labels(agent_id=agent_id).set(compression_ratio)
        
        return compressed_tree


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