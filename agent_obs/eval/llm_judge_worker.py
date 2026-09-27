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
from typing import Any

logger = logging.getLogger(__name__)

# Timeout for LLM API calls (seconds)
_LLM_TIMEOUT = 30
# Timeout for ClickHouse writes (seconds)
_HOT_STORE_TIMEOUT = 10


def llm_judge_worker(
    trace_id: str,
    span_id: str,
    prompts: dict[str, str],
    model: str,
    eval_id: str,
    judge_prompt_sha256: str,
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
    async with httpx.AsyncClient(timeout=_LLM_TIMEOUT) as client:
        for metric_name, rendered_prompt in prompts.items():
            try:
                result = await _call_llm(client, model, rendered_prompt)
                scores[metric_name] = result.get("score", 0.0)
                reasoning_parts[metric_name] = result.get("reasoning", "")
                flags = result.get("flags", [])
                if isinstance(flags, list):
                    all_flags.extend(flags)
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


async def _call_llm(client: Any, model: str, prompt: str) -> dict:
    """Call the LLM API and parse JSON response."""
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
    return json.loads(content)


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
