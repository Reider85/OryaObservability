"""LLM judge worker for async evaluation pipeline.

This module provides the worker function that RQ calls to process LLM judge
evaluation jobs. The worker calls the LLM API, constructs EvalResult objects,
and writes them to ClickHouse via late annotation.

RQ resolves this function by importing it from this module path:
    agent_obs.eval.llm_judge_worker.llm_judge_worker
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

# Timeout for LLM API calls (seconds)
_LLM_TIMEOUT = 30
# Timeout for ClickHouse writes (seconds)
_HOT_STORE_TIMEOUT = 10


def _compute_judge_cost(model: str, usage: dict, now: datetime | None = None) -> float:
    """Compute cost for LLM judge call using PriceBook or fallback.
    
    Args:
        model: The LLM model used
        usage: Usage dict from API response (prompt_tokens, completion_tokens)
        now: Time for pricing lookup (default: now UTC)
    
    Returns:
        Cost in USD, or 0 if pricing fails
    """
    try:
        from agent_obs.cost.compute_cost import compute_cost
        from agent_obs.cost.price_book import PriceBook
        
        # Parse usage
        input_tokens = usage.get("prompt_tokens", 0)
        output_tokens = usage.get("completion_tokens", 0)
        cached_tokens = usage.get("cached_tokens", 0)
        
        from agent_obs.cost.compute_cost import Usage
        usage_obj = Usage(
            input=input_tokens,
            output=output_tokens,
            cached=cached_tokens
        )
        
        # Try PriceBook first
        price_book = PriceBook.load()
        result = compute_cost(usage_obj, model, price_book, at=now)
        return result.cost_usd
        
    except Exception as e:
        logger.warning("PriceBook pricing failed for %s: %s, using fallback", model, e)
        
        # Fallback to flat rate per 1M tokens
        fallback_rate = float(os.environ.get("LLM_JUDGE_COST_PER_1M_TOKENS_USD", "5.0"))
        total_tokens = input_tokens + output_tokens + cached_tokens
        return fallback_rate * total_tokens / 1_000_000


def llm_judge_worker(
    trace_id: str,
    span_id: str,
    prompts: dict[str, str],
    model: str,
    eval_id: str,
    judge_prompt_sha256: str,
    mode: str = "unknown",
) -> None:
    """RQ worker function: process an LLM judge evaluation job.

    This is the synchronous entry point called by RQ. It delegates to an
    async implementation via asyncio.run().

    Args:
        trace_id: The trace identifier.
        span_id: The span identifier being evaluated.
        prompts: Dict mapping metric names to rendered prompts.
        model: The LLM model to use for judging.
        eval_id: Unique evaluation identifier.
        judge_prompt_sha256: SHA256 hash of the rendered prompts for audit.
    """
    try:
        asyncio.run(
            _process_llm_judge_job(
                trace_id=trace_id,
                span_id=span_id,
                prompts=prompts,
                model=model,
                eval_id=eval_id,
                judge_prompt_sha256=judge_prompt_sha256,
                mode=mode,
            )
        )
    except Exception:
        logger.exception(
            "LLM judge worker failed: trace=%s eval_id=%s", trace_id, eval_id
        )
        try:
            from agent_obs.metrics import eval_jobs_failed_total
            eval_jobs_failed_total.inc()
        except Exception:
            pass


async def _process_llm_judge_job(
    trace_id: str,
    span_id: str,
    prompts: dict[str, str],
    model: str,
    eval_id: str,
    judge_prompt_sha256: str,
    mode: str = "unknown",
) -> None:
    """Async implementation of LLM judge job processing.

    1. Call LLM API for each metric prompt
    2. Parse JSON responses into scores
    3. Construct EvalResult
    4. Write to ClickHouse via late annotation (with Redis fallback)
    """
    import httpx

    from agent_obs.eval.base import EvalResult
    from agent_obs.eval.late_annotation import annotate_with_fallback

    start_time = time.time()
    scores: dict[str, float] = {}
    reasoning_parts: dict[str, str] = {}
    all_flags: list[str] = []
    judge_model = model

    # Call LLM for each metric
    total_cost = 0.0
    async with httpx.AsyncClient(timeout=_LLM_TIMEOUT) as client:
        for metric_name, rendered_prompt in prompts.items():
            try:
                result, usage = await _call_llm(client, model, rendered_prompt)
                scores[metric_name] = result.get("score", 0.0)
                reasoning_parts[metric_name] = result.get("reasoning", "")
                flags = result.get("flags", [])
                if isinstance(flags, list):
                    all_flags.extend(flags)
                
                # Accumulate cost
                cost = _compute_judge_cost(model, usage)
                total_cost += cost
                
            except Exception:
                logger.warning(
                    "LLM call failed for metric %s: trace=%s eval_id=%s",
                    metric_name,
                    trace_id,
                    eval_id,
                )
                scores[metric_name] = 0.0
                reasoning_parts[metric_name] = "LLM call failed"
                all_flags.append(f"{metric_name}_failed")

    # Construct EvalResult
    latency = time.time() - start_time
    reasoning_text = "; ".join(
        f"{k}: {v}" for k, v in reasoning_parts.items() if v
    )

    eval_result = EvalResult(
        trace_id=trace_id,
        eval_id=eval_id,
        eval_name="llm_judge",
        eval_version="1.0.0",
        eval_timestamp=start_time,
        eval_latency_seconds=latency,
        scores=scores,
        judge_model=judge_model,
        judge_prompt_sha256=judge_prompt_sha256,
        reasoning=reasoning_text,
        flags=all_flags,
    )

    # Initialize storage clients
    hot_store = _create_hot_store()
    redis_client = _create_redis_client()

    # Write via late annotation with fallback
    await annotate_with_fallback(eval_result, hot_store, redis_client)

    # Record eval cost to Redis
    if redis_client is not None and total_cost > 0:
        _write_eval_cost_to_redis(redis_client, mode, total_cost)

    # Record metrics
    from agent_obs.metrics import eval_job_latency_seconds, eval_jobs_completed_total

    eval_jobs_completed_total.inc()
    eval_job_latency_seconds.observe(latency)

    logger.info(
        "LLM judge job completed: trace=%s eval_id=%s latency=%.2fs scores=%s",
        trace_id,
        eval_id,
        latency,
        scores,
    )


async def _call_llm(client: Any, model: str, prompt: str) -> tuple[dict, dict]:
    """Call the LLM API and parse JSON response, returning content and usage."""
    api_key = os.environ.get("LLM_JUDGE_API_KEY", "")
    api_base = os.environ.get("LLM_JUDGE_API_BASE", "https://api.openai.com/v1")

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "You are an expert evaluator. Return JSON."},
            {"role": "user", "content": prompt},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.0,
    }

    response = await client.post(
        f"{api_base}/chat/completions",
        json=payload,
        headers=headers,
    )
    response.raise_for_status()

    data = response.json()
    content = data["choices"][0]["message"]["content"]
    usage = data.get("usage", {})
    return json.loads(content), usage


def _create_hot_store() -> Any:
    """Create HotStore instance from environment variables."""
    try:
        from agent_obs.storage.hot import HotStore
        return HotStore()
    except Exception:
        logger.warning("Failed to create HotStore; ClickHouse writes will fail")
        return None


def _create_redis_client() -> Any:
    """Create Redis client from environment variables for fallback."""
    try:
        from redis import Redis
        redis_url = os.environ.get("EVAL_REDIS_URL", "redis://localhost:6379/1")
        return Redis.from_url(redis_url)
    except Exception:
        logger.warning("Failed to create Redis client; fallback storage unavailable")
        return None


def _write_eval_cost_to_redis(
    redis_client: Any,
    mode: str,
    cost_usd: float,
    count: int = 1,
) -> None:
    """Write eval cost and count to Redis daily hash.
    
    Args:
        redis_client: Redis client
        mode: Sampling mode (day|night|incident)
        cost_usd: Cost for this eval
        count: Number of evals (default 1)
    """
    try:
        from datetime import datetime, timezone, UTC
        
        # Use UTC date for daily reset
        date_str = datetime.now(UTC).strftime("%Y-%m-%d")
        cost_key = f"agent_obs:eval:cost:{date_str}"
        count_key = f"agent_obs:eval:count:{date_str}"
        
        # Increment cost and count for this mode
        redis_client.hincrbyfloat(cost_key, mode, cost_usd)
        redis_client.hincrbyfloat(count_key, mode, count)
        
        # Set expiration (48h to cover rollover)
        redis_client.expire(cost_key, 48 * 3600)
        redis_client.expire(count_key, 48 * 3600)
        
    except Exception as e:
        logger.warning("Failed to write eval cost to Redis: %s", e)
        # Never fail the job due to Redis errors
