"""FastAPI Query API for eval results and sampler rate history (PC13 + PC31)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Path, Query
from pydantic import BaseModel

from agent_obs.eval.late_annotation import get_eval_results
from agent_obs.observability import Span, SpanContext


def _dicts_to_spans(span_dicts: list[dict]) -> list[Span]:
    """Convert ClickHouse span dicts to Span objects.
    
    Args:
        span_dicts: List of span dicts from HotStore.get_spans()
        
    Returns:
        List of Span objects with reconstructed contexts
    """
    spans = []
    
    for span_dict in span_dicts:
        # Convert datetime to float timestamp
        start_time = span_dict.get("start_time", 0.0)
        if isinstance(start_time, datetime):
            start_time = start_time.timestamp()
        
        end_time = span_dict.get("end_time", 0.0)
        if isinstance(end_time, datetime):
            end_time = end_time.timestamp()
        
        # Reconstruct SpanContext
        context = SpanContext(
            trace_id=span_dict.get("trace_id", ""),
            span_id=span_dict.get("span_id", ""),
            parent_span_id=span_dict.get("parent_span_id") or None,
            agent_id=span_dict.get("agent_id", ""),
            user_id=span_dict.get("user_id", ""),
            session_id=span_dict.get("session_id", ""),
            tool_name=span_dict.get("tool_name", ""),
        )
        
        # Ensure status is in attributes
        attributes = span_dict.get("attributes", {})
        if "status" not in attributes:
            attributes["status"] = span_dict.get("status", "")
        
        span = Span(
            name=span_dict.get("name", ""),
            span_type=span_dict.get("span_type", ""),
            context=context,
            attributes=attributes,
            events=span_dict.get("events", []),
            start_time=start_time,
            end_time=end_time,
        )
        
        spans.append(span)
    
    return spans


app = FastAPI(
    title="Agent Observability Eval API",
    description=(
        "Query API for retrieving eval results by trace_id and "
        "sampler rate-change audit history"
    ),
    version="1.1.0",
)


class EvalResultResponse(BaseModel):
    """Response model for eval results."""
    trace_id: str
    eval_results: list[dict[str, Any]]


class SamplerRateHistoryResponse(BaseModel):
    """Response model for sampler rate-change audit history (PC31)."""
    events: list[dict[str, Any]]
    count: int


class TraceTreeResponse(BaseModel):
    """Response model for trace tree with compression and summaries."""
    trace_id: str
    tree: dict[str, Any]


def _parse_range_bound(value: str, *, end_of_day: bool = False) -> datetime:
    """Parse an ISO date or datetime query param into an aware UTC datetime.

    A bare date ``2026-09-20`` becomes ``2026-09-20T00:00:00Z`` (start) or
    ``2026-09-20T23:59:59.999999Z`` (end) so a date-only range covers the
    whole day.
    """
    raw = value.strip()
    if not raw:
        raise ValueError("empty datetime")
    try:
        if len(raw) == 10:  # YYYY-MM-DD
            base = datetime.fromisoformat(raw)
            if end_of_day:
                return base.replace(
                    hour=23, minute=59, second=59, microsecond=999999,
                    tzinfo=UTC,
                )
            return base.replace(tzinfo=UTC)
        parsed = datetime.fromisoformat(raw)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    except ValueError as exc:
        raise ValueError(f"invalid datetime {value!r}: {exc}") from exc


@app.get("/audit/sampler-rate-history", response_model=SamplerRateHistoryResponse)
async def get_sampler_rate_history(
    from_: str = Query(
        ...,
        alias="from",
        description="Start of range (ISO date or datetime, UTC). E.g. 2026-09-20",
    ),
    to: str = Query(
        ...,
        description=(
            "End of range (ISO date or datetime, UTC). A bare date covers "
            "the whole day. E.g. 2026-09-21"
        ),
    ),
    limit: int = Query(
        1000, ge=1, le=10_000, description="Maximum number of events to return"
    ),
) -> SamplerRateHistoryResponse:
    """Get tail-sampler rate-change audit history for incident investigation.

    Reads ``sampler.rate_change`` events from ClickHouse ``audit_events_hot``
    within ``[from, to]``, ordered oldest-first. Each event carries
    ``prev_rate``, ``new_rate``, ``prev_reason``, ``new_reason``,
    ``system_cpu_ratio`` and ``agent_error_rate_5m`` so a dropped trace can be
    explained weeks later.
    """
    try:
        from_ts = _parse_range_bound(from_, end_of_day=False)
        to_ts = _parse_range_bound(to, end_of_day=True)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if from_ts > to_ts:
        raise HTTPException(
            status_code=422,
            detail=f"from ({from_ts.isoformat()}) must be <= to ({to_ts.isoformat()})",
        )

    try:
        from agent_obs.storage.hot import HotStore

        loop = asyncio.get_running_loop()
        hot_store = HotStore()
        events = await loop.run_in_executor(
            None,
            lambda: hot_store.get_sampler_rate_history(from_ts, to_ts, limit=limit),
        )
        # Datetime objects are not JSON-serialisable; emit ISO strings.
        serialisable = []
        for event in events:
            item = dict(event)
            ts = item.get("timestamp")
            if isinstance(ts, datetime):
                item["timestamp"] = ts.isoformat()
            serialisable.append(item)
        return SamplerRateHistoryResponse(events=serialisable, count=len(serialisable))
    except httpx.ConnectError:
        raise HTTPException(
            status_code=503,
            detail="ClickHouse unavailable. Sampler rate history temporarily unavailable.",
        )
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to retrieve sampler rate history: {e!s}",
        )


@app.get("/traces/{trace_id}/evals", response_model=EvalResultResponse)
async def get_trace_eval_results(
    trace_id: str = Path(..., description="The trace ID to query for eval results")
) -> EvalResultResponse:
    """Get all evaluation results for a specific trace.

    Returns eval results from ClickHouse (primary) with Redis fallback.
    Returns empty list if no eval results found for the trace.
    """
    try:
        from agent_obs.storage.hot import HotStore

        # Create hot store (ClickHouse) — same pattern as the sampler
        # rate-history endpoint. Redis fallback stays optional until a
        # production Redis connection is wired up here.
        hot_store = HotStore()
        redis_client = None

        # Get eval results from ClickHouse with Redis fallback
        eval_results = await get_eval_results(trace_id, hot_store, redis_client=redis_client)
        
        # Convert to dict for JSON serialization
        eval_results_dict = [
            {
                "trace_id": result.trace_id,
                "eval_id": result.eval_id,
                "eval_name": result.eval_name,
                "eval_timestamp": result.eval_timestamp,
                "eval_latency_seconds": result.eval_latency_seconds,
                "scores": result.scores,
                "judge_model": result.judge_model,
                "judge_prompt_sha256": result.judge_prompt_sha256,
                "reasoning": result.reasoning,
                "flags": result.flags,
            }
            for result in eval_results
        ]
        
        return EvalResultResponse(
            trace_id=trace_id,
            eval_results=eval_results_dict
        )
        
    except httpx.ConnectError:
        raise HTTPException(
            status_code=503,
            detail="ClickHouse unavailable. Eval results temporarily unavailable."
        )
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to retrieve eval results: {e!s}"
        )


@app.get("/api/v1/traces/{trace_id}/tree", response_model=TraceTreeResponse)
async def get_trace_tree(
    trace_id: str = Path(..., description="The trace ID to build tree for")
) -> TraceTreeResponse:
    """Build trace tree with compression and late summaries.
    
    Returns a TraceTree with compression applied and LLM summaries merged.
    Returns empty tree if no spans found for the trace.
    """
    try:
        from agent_obs.storage.hot import HotStore
        from agent_obs.tree_compressor import (
            CompressionPolicy,
            EmbeddedSubtreePolicy,
            TraceTreeBuilder,
            apply_late_summaries,
        )

        # Create hot store (ClickHouse)
        hot_store = HotStore()
        redis_client = None

        # Get spans from ClickHouse
        loop = asyncio.get_running_loop()
        spans = await loop.run_in_executor(None, hot_store.get_spans, trace_id)
        
        if not spans:
            raise HTTPException(
                status_code=404,
                detail=f"No spans found for trace {trace_id}"
            )
        
        # Convert dicts to Span objects
        sdk_spans = _dicts_to_spans(spans)
        
        # Build trace tree
        tree_builder = TraceTreeBuilder()
        tree = tree_builder.build(sdk_spans)
        
        # Apply compression (with queue disabled for read path)
        policy = CompressionPolicy.from_env()
        policy.summary_queue_enabled = False  # Don't enqueue from read path
        compressed_tree = policy.compress(tree)
        
        # Get late summaries and apply them
        summaries = hot_store.get_tree_summaries(trace_id)
        summaries_map = {s["node_span_id"]: s["summary"] for s in summaries}
        final_tree = apply_late_summaries(compressed_tree, summaries_map)
        
        # PP20: embed small leaf-subtrees into parent events (matreshka)
        embedded_policy = EmbeddedSubtreePolicy.from_env()
        final_tree = embedded_policy.embed(final_tree)
        
        return TraceTreeResponse(
            trace_id=trace_id,
            tree=final_tree.to_dict()
        )
        
    except HTTPException:
        # Re-raise HTTPException (e.g., 404 for no spans found)
        raise
    except httpx.ConnectError:
        raise HTTPException(
            status_code=503,
            detail="ClickHouse unavailable. Trace tree temporarily unavailable."
        )
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to build trace tree: {e!s}"
        )


@app.get("/health")
async def health_check() -> dict[str, str]:
    """Health check endpoint."""
    return {"status": "healthy", "service": "eval-api"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)