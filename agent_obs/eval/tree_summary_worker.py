"""Worker for generating async LLM summaries for compressed tree nodes."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent_obs.storage.hot import HotStore

logger = logging.getLogger(__name__)


def tree_summary_worker(trace_id: str, summary_node_id: str) -> None:
    """Worker entry point for generating LLM summary for a compressed node.
    
    Args:
        trace_id: Trace ID containing the summary node
        summary_node_id: Span ID of the compressed summary node
    """
    import asyncio
    asyncio.run(_process_tree_summary_job(trace_id, summary_node_id))


async def _process_tree_summary_job(trace_id: str, summary_node_id: str) -> None:
    """Process tree summary job: extract subtree, call LLM, write result.
    
    Args:
        trace_id: Trace ID
        summary_node_id: Span ID of the compressed node to summarize
    """
    # Initialize storage clients
    hot_store = _create_hot_store()
    redis_client = _create_redis_client()
    
    try:
        # Get all spans for the trace
        loop = asyncio.get_running_loop()
        spans = await loop.run_in_executor(None, hot_store.get_spans, trace_id)
        
        if not spans:
            logger.warning("No spans found for trace %s", trace_id)
            return
        
        # Extract subtree spans starting from summary_node_id
        subtree_spans = _extract_subtree_spans(spans, summary_node_id)
        if not subtree_spans:
            logger.warning("No subtree found for summary node %s in trace %s", 
                          summary_node_id, trace_id)
            return
        
        # Compute content hash for caching
        content_hash = compute_subtree_content_hash(subtree_spans)
        
        # Check cache first
        if redis_client:
            cached_summary = _check_cache(redis_client, content_hash)
            if cached_summary:
                logger.info("Using cached summary for hash %s", content_hash)
                await _write_summary_annotation(
                    trace_id, summary_node_id, cached_summary, 
                    hot_store, redis_client, content_hash
                )
                return
        
        # Generate summary via LLM
        summary_text = await _generate_summary(subtree_spans)
        
        # Cache the result
        if redis_client:
            _set_cache(redis_client, content_hash, summary_text)
        
        # Write as late annotation
        await _write_summary_annotation(
            trace_id, summary_node_id, summary_text, 
            hot_store, redis_client, content_hash
        )
        
    except Exception as e:
        logger.exception("Failed to process tree summary job for trace=%s node=%s", 
                        trace_id, summary_node_id)
        # Increment failure metric (handled by caller)


def _extract_subtree_spans(spans: list[dict], summary_node_id: str) -> list[dict]:
    """Extract all descendant spans of the summary node.
    
    Args:
        spans: All spans in the trace
        summary_node_id: Span ID of the compressed node (root of subtree)
        
    Returns:
        List of spans in the subtree, sorted by start_time
    """
    # Build parent->children map
    parent_map = {}
    root_span = None
    
    for span in spans:
        parent_id = span.get("parent_span_id", "")
        if parent_id not in parent_map:
            parent_map[parent_id] = []
        parent_map[parent_id].append(span)
        
        # Find the summary node (which keeps the original root's span_id)
        if span.get("span_id") == summary_node_id:
            root_span = span
    
    if not root_span:
        return []
    
    # BFS traversal to collect all descendants
    subtree_spans = []
    queue = [root_span]
    
    while queue:
        current = queue.pop(0)
        subtree_spans.append(current)
        
        # Add children (exclude compressed nodes to avoid cycles)
        children = parent_map.get(current.get("span_id", ""), [])
        for child in children:
            if child.get("span_type") != "agent.reasoning.compressed":
                queue.append(child)
    
    # Sort by start_time for deterministic ordering
    return sorted(subtree_spans, key=lambda s: s.get("start_time", 0.0))


def compute_subtree_content_hash(spans: list[dict]) -> str:
    """Compute deterministic SHA256 hash of subtree content.
    
    Args:
        spans: List of span dicts in the subtree
        
    Returns:
        SHA256 hash string
    """
    import hashlib
    
    # Sort spans by start_time
    sorted_spans = sorted(spans, key=lambda s: s.get("start_time", 0.0))
    
    # Extract text content from attributes
    content_parts = []
    for span in sorted_spans:
        attributes = span.get("attributes", {})
        
        # Filter out metadata keys, keep string values
        metadata_keys = {
            "status", "tree.compressed", "tree.children_count", "tree.total_cost",
            "tree.error_count", "tree.summary", "tree.policy_version",
            "aggregation.children_count", "aggregation.bytes_saved", "aggregation.source",
            "cost.usd", "cost.usd_sum", "cost.tokens", "cost.tokens_sum",
            "guardrail.redacted_fields", "guardrail.verdicts"
        }
        
        for key, value in attributes.items():
            if key not in metadata_keys and isinstance(value, str) and value:
                content_parts.append(f"{key}:{value}")
        
        # Add events content for aggregated spans
        events = span.get("events", [])
        for event in events:
            event_attrs = event.get("attributes", {})
            for attr_key, attr_value in event_attrs.items():
                if attr_key not in metadata_keys and isinstance(attr_value, str) and attr_value:
                    content_parts.append(f"event.{attr_key}:{attr_value}")
    
    # Concatenate and hash
    full_content = "|".join(content_parts)
    return hashlib.sha256(full_content.encode("utf-8")).hexdigest()


def _check_cache(redis_client, content_hash: str) -> str | None:
    """Check if summary is cached in Redis.
    
    Args:
        redis_client: Redis client
        content_hash: Content hash to check
        
    Returns:
        Cached summary text if found, None otherwise
    """
    import os
    from agent_obs.eval.late_annotation import _FALLBACK_REDIS_TTL
    
    cache_ttl = int(os.environ.get("AGENT_OBS_TREE_SUMMARY_CACHE_TTL", "604800"))  # 7 days
    key = f"agent_obs:tree:summary:{content_hash}"
    
    try:
        cached_data = redis_client.get(key)
        if cached_data:
            data = json.loads(cached_data)
            return data.get("summary")
    except Exception as e:
        logger.warning("Failed to check cache for key %s: %s", key, str(e))
    
    return None


def _set_cache(redis_client, content_hash: str, summary: str) -> None:
    """Cache the generated summary in Redis.
    
    Args:
        redis_client: Redis client
        content_hash: Content hash for the cache key
        summary: Summary text to cache
    """
    import os
    from agent_obs.eval.late_annotation import _FALLBACK_REDIS_TTL
    
    cache_ttl = int(os.environ.get("AGENT_OBS_TREE_SUMMARY_CACHE_TTL", "604800"))
    key = f"agent_obs:tree:summary:{content_hash}"
    data = {"summary": summary, "content_hash": content_hash}
    
    try:
        redis_client.setex(key, cache_ttl, json.dumps(data))
    except Exception as e:
        logger.warning("Failed to set cache for key %s: %s", key, str(e))


async def _generate_summary(subtree_spans: list[dict]) -> str:
    """Generate summary using LLM.
    
    Args:
        subtree_spans: List of spans in the subtree
        
    Returns:
        Generated summary text
    """
    import os
    import httpx
    from agent_obs.eval.llm_judge_worker import _call_llm
    
    # Build prompt
    prompt = _build_summary_prompt(subtree_spans)
    
    # LLM call parameters
    model = os.environ.get("LLM_JUDGE_MODEL", "gpt-4o-mini")
    api_base = os.environ.get("LLM_JUDGE_API_BASE", "https://api.openai.com/v1")
    api_key = os.environ.get("LLM_JUDGE_API_KEY", "")
    
    # Call LLM
    try:
        client = httpx.AsyncClient(timeout=30.0)
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": "Ты — эксперт по анализу цепочек рассуждений. Проанализируй предоставленные шаги и создай краткое, содержательное резюме."},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.0,
        }
        
        if api_key:
            payload["api_key"] = api_key
        
        response = await client.post(f"{api_base}/chat/completions", json=payload)
        response.raise_for_status()
        data = response.json()
        
        # Extract summary from response
        content = data["choices"][0]["message"]["content"]
        return content.strip()
        
    except Exception as e:
        logger.error("LLM call failed: %s", str(e))
        raise


def _build_summary_prompt(subtree_spans: list[dict]) -> str:
    """Build prompt for LLM summary generation.
    
    Args:
        subtree_spans: List of spans in the subtree
        
    Returns:
        Prompt text
    """
    prompt = "Суммируй следующие шаги reasoning-цикла:\n\n"
    
    for i, span in enumerate(subtree_spans, 1):
        name = span.get("name", f"Шаг {i}")
        attributes = span.get("attributes", {})
        
        # Extract reasoning text from attributes
        reasoning_text = ""
        for key, value in attributes.items():
            if key not in {
                "status", "tree.compressed", "tree.children_count", "tree.total_cost",
                "tree.error_count", "tree.summary", "tree.policy_version",
                "aggregation.children_count", "aggregation.bytes_saved", "aggregation.source",
                "cost.usd", "cost.usd_sum", "cost.tokens", "cost.tokens_sum",
                "guardrail.redacted_fields", "guardrail.verdicts"
            } and isinstance(value, str) and value:
                reasoning_text += f"{key}: {value}\n"
        
        if reasoning_text:
            prompt += f"{i}. {name}:\n{reasoning_text.strip()}\n\n"
    
    prompt += "Предоставь краткое, содержательное резюме этого reasoning-процесса (2-4 предложения)."
    
    return prompt


async def _write_summary_annotation(trace_id: str, summary_node_id: str, 
                                   summary: str, hot_store: HotStore,
                                   redis_client, content_hash: str) -> None:
    """Write summary as late annotation.
    
    Args:
        trace_id: Trace ID
        summary_node_id: Span ID of the summary node
        summary: Generated summary text
        hot_store: HotStore instance
        redis_client: Redis client (optional)
        content_hash: Content hash for the summary
    """
    from agent_obs.eval.base import EvalResult
    from agent_obs.eval.late_annotation import annotate_with_fallback
    from agent_obs.metrics import eval_jobs_completed_total, eval_job_latency_seconds
    
    start_time = time.time()
    
    # Create eval result
    eval_id = f"tree_summary_{summary_node_id}"
    eval_result = EvalResult(
        trace_id=trace_id,
        eval_id=eval_id,
        eval_name="tree_summary",
        eval_version="1.0.0",
        eval_timestamp=start_time,
        eval_latency_seconds=0.0,  # Will be updated below
        scores={"summary": summary, "content_hash": content_hash},
        reasoning=summary,
    )
    
    # Write with fallback
    try:
        await annotate_with_fallback(eval_result, hot_store, redis_client)
        eval_jobs_completed_total.inc()
        
        # Update latency
        latency = time.time() - start_time
        eval_job_latency_seconds.observe(latency)
        
        logger.info("Summary annotation written: trace=%s node=%s summary_length=%d",
                   trace_id, summary_node_id, len(summary))
        
    except Exception as e:
        logger.error("Failed to write summary annotation: %s", str(e))
        eval_jobs_completed_total.inc()  # Still count as completed, even with fallback issues


def _create_hot_store() -> HotStore:
    """Create HotStore instance."""
    from agent_obs.storage.hot import HotStore
    return HotStore()


def _create_redis_client():
    """Create Redis client for caching."""
    import os
    from agent_obs.eval.llm_judge_worker import _create_redis_client as create_redis
    
    # Use the same Redis URL as eval infrastructure
    redis_url = os.environ.get("EVAL_REDIS_URL", "redis://localhost:6379/1")
    return create_redis(redis_url)