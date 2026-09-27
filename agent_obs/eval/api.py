"""FastAPI Query API for eval results retrieval."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Path
from pydantic import BaseModel

from agent_obs.eval.late_annotation import get_eval_results

app = FastAPI(
    title="Agent Observability Eval API",
    description="Query API for retrieving eval results by trace_id",
    version="1.0.0",
)


class EvalResultResponse(BaseModel):
    """Response model for eval results."""
    trace_id: str
    eval_results: list[dict[str, Any]]


@app.get("/traces/{trace_id}/evals", response_model=EvalResultResponse)
async def get_trace_eval_results(
    trace_id: str = Path(..., description="The trace ID to query for eval results")
) -> EvalResultResponse:
    """Get all evaluation results for a specific trace.
    
    Returns eval results from ClickHotse (primary) with Redis fallback.
    Returns empty list if no eval results found for the trace.
    """
    try:
        # Create mock redis client for now (in production this would be configured)
        redis_client = None
        
        # Get eval results from ClickHouse with Redis fallback
        eval_results = await get_eval_results(trace_id, redis_client=redis_client)
        
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
            detail=f"Failed to retrieve eval results: {str(e)}"
        )


@app.get("/health")
async def health_check() -> dict[str, str]:
    """Health check endpoint."""
    return {"status": "healthy", "service": "eval-api"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)