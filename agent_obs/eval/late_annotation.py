"""Late annotation: write eval results to ClickHouse after trace completion.

Implements the late annotation mechanism from §7.3 ARCHITECT.md: eval results
are written to ClickHouse eval_results_hot keyed by trace_id after the async
LLM-judge job completes (5-30s after the trace). Includes Redis fallback when
ClickHouse is unavailable.
"""

from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agent_obs.eval.base import EvalResult
    from agent_obs.storage.hot import HotStore

logger = logging.getLogger(__name__)

# Redis TTL for fallback storage (1 hour)
_FALLBACK_REDIS_TTL = 3600


async def annotate(eval_result: EvalResult, hot_store: HotStore) -> None:
    """Write an eval result to ClickHouse eval_results_hot table.

    Idempotent: ReplacingMergeTree deduplicates by
    (trace_id, eval_id, eval_name, eval_timestamp).

    Args:
        eval_result: The evaluation result to persist.
        hot_store: ClickHouse hot-tier storage client.
    """
    from agent_obs.metrics import eval_annotation_total

    try:
        # HotStore methods are sync (clickhouse_driver is sync).
        # Run in executor to avoid blocking the event loop.
        import asyncio
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, hot_store.write_eval_result, eval_result)
        eval_annotation_total.labels(status="success").inc()
        logger.debug(
            "Late annotation written: trace=%s eval_id=%s name=%s",
            eval_result.trace_id,
            eval_result.eval_id,
            eval_result.eval_name,
        )
    except Exception:
        eval_annotation_total.labels(status="failed").inc()
        logger.exception(
            "Failed to write late annotation to ClickHouse: trace=%s eval_id=%s",
            eval_result.trace_id,
            eval_result.eval_id,
        )
        raise


async def annotate_with_fallback(
    eval_result: EvalResult,
    hot_store: HotStore | None,
    redis_client: Any | None,
) -> None:
    """Write eval result with Redis fallback when ClickHouse is unavailable.

    Fallback chain:
    1. If hot_store is available and healthy → write directly
    2. If hot_store is unavailable → store in Redis with TTL 1 hour
    3. If both are unavailable → log warning, increment metric, no exception

    Args:
        eval_result: The evaluation result to persist.
        hot_store: ClickHouse hot-tier client (may be None).
        redis_client: Redis client for fallback (may be None).
    """
    from agent_obs.metrics import eval_annotation_redis_fallback_total, eval_annotation_total

    # Try ClickHouse first
    if hot_store is not None:
        try:
            await annotate(eval_result, hot_store)
            return
        except Exception:
            logger.warning(
                "ClickHouse unavailable for late annotation, attempting Redis fallback: "
                "trace=%s eval_id=%s",
                eval_result.trace_id,
                eval_result.eval_id,
            )

    # Fallback to Redis
    if redis_client is not None:
        try:
            import asyncio
            loop = asyncio.get_running_loop()

            def _store_in_redis() -> None:
                key = f"eval_fallback:{eval_result.trace_id}:{eval_result.eval_id}"
                data = json.dumps(
                    {
                        "trace_id": eval_result.trace_id,
                        "eval_id": eval_result.eval_id,
                        "eval_name": eval_result.eval_name,
                        "eval_version": eval_result.eval_version,
                        "eval_timestamp": eval_result.eval_timestamp,
                        "eval_latency_seconds": eval_result.eval_latency_seconds,
                        "scores": eval_result.scores,
                        "judge_model": eval_result.judge_model,
                        "judge_prompt_sha256": eval_result.judge_prompt_sha256,
                        "reasoning": eval_result.reasoning,
                        "flags": eval_result.flags,
                    }
                )
                redis_client.setex(key, _FALLBACK_REDIS_TTL, data)

            await loop.run_in_executor(None, _store_in_redis)
            eval_annotation_redis_fallback_total.inc()
            eval_annotation_total.labels(status="redis_fallback").inc()
            logger.info(
                "Late annotation stored in Redis fallback: trace=%s eval_id=%s (TTL=%ds)",
                eval_result.trace_id,
                eval_result.eval_id,
                _FALLBACK_REDIS_TTL,
            )
            return
        except Exception:
            logger.exception(
                "Redis fallback also failed for late annotation: trace=%s eval_id=%s",
                eval_result.trace_id,
                eval_result.eval_id,
            )

    # Both unavailable
    eval_annotation_total.labels(status="failed").inc()
    logger.warning(
        "Late annotation dropped (no storage available): trace=%s eval_id=%s",
        eval_result.trace_id,
        eval_result.eval_id,
    )


async def get_eval_results(
    trace_id: str,
    hot_store: HotStore | None = None,
    redis_client: Any | None = None,
) -> list[EvalResult]:
    """Retrieve eval results for a trace, checking Redis fallback if needed.

    Args:
        trace_id: The trace identifier.
        hot_store: ClickHouse hot-tier client (may be None).
        redis_client: Redis client for fallback reads (may be None).

    Returns:
        List of EvalResult objects found for the trace.
    """
    from agent_obs.eval.base import EvalResult

    results: list[EvalResult] = []

    # Try ClickHouse first
    if hot_store is not None:
        try:
            import asyncio
            loop = asyncio.get_running_loop()
            results = await loop.run_in_executor(None, hot_store.get_eval_results, trace_id)
            if results:
                return results
        except Exception:
            logger.warning(
                "ClickHouse unavailable for eval result retrieval: trace=%s",
                trace_id,
            )

    # Fallback to Redis
    if redis_client is not None:
        try:
            import asyncio
            loop = asyncio.get_running_loop()

            def _read_from_redis() -> list[EvalResult]:
                pattern = f"eval_fallback:{trace_id}:*"
                keys = redis_client.keys(pattern)
                found: list[EvalResult] = []
                for key in keys:
                    data = redis_client.get(key)
                    if data:
                        d = json.loads(data)
                        found.append(
                            EvalResult(
                                trace_id=d["trace_id"],
                                eval_id=d["eval_id"],
                                eval_name=d["eval_name"],
                                eval_version=d.get("eval_version", "1.0.0"),
                                eval_timestamp=d["eval_timestamp"],
                                eval_latency_seconds=d.get("eval_latency_seconds", 0.0),
                                scores=d.get("scores", {}),
                                judge_model=d.get("judge_model", ""),
                                judge_prompt_sha256=d.get("judge_prompt_sha256", ""),
                                reasoning=d.get("reasoning", ""),
                                flags=d.get("flags", []),
                            )
                        )
                return found

            results = await loop.run_in_executor(None, _read_from_redis)
        except Exception:
            logger.warning(
                "Redis fallback also failed for eval result retrieval: trace=%s",
                trace_id,
            )

    return results
