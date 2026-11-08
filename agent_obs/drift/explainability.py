"""Drift explainability module: automatic explanation of drift alerts with UMAP visualization.

PP57: When drift alert fires, automatically compute top-k "most deviating" embeddings,
show them in Phoenix UMAP, and provide LLM-generated explanation of what changed.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

from agent_obs.drift.models import DriftReport
from agent_obs.eval.base import BaseEvaluator
from agent_obs.storage.hot import HotStore

logger = logging.getLogger(__name__)


@dataclass
class DriftAlert:
    """Drift alert context for explainability.
    
    Represents the drift detection result that triggered the explanation.
    """
    drift_report: DriftReport
    detected_at: float = 0.0
    
    def __post_init__(self):
        if self.detected_at == 0.0:
            self.detected_at = time.time()


@dataclass
class DriftExplanation:
    """Explanation of drift detection results.
    
    Contains top-k deviating traces, LLM-generated summary, and hypothesis.
    """
    top_k_traces: list[dict]
    summary: str
    hypothesis: str
    generated_at: float
    agent_id: str
    kl_score: float
    threshold: float
    severity: str
    
    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for ClickHouse storage."""
        return {
            "top_k_traces": self.top_k_traces,
            "summary": self.summary,
            "hypothesis": self.hypothesis,
            "generated_at": self.generated_at,
            "agent_id": self.agent_id,
            "kl_score": self.kl_score,
            "threshold": self.threshold,
            "severity": self.severity,
        }


class DriftExplainer:
    """Drift explainability engine that provides insights on drift detection.
    
    PP57: When drift alert fires, automatically compute top-k "most deviating" embeddings,
    show them in Phoenix UMAP, and provide LLM-generated explanation of what changed.
    """
    
    def __init__(
        self,
        sdk,
        hotstore: HotStore,
        llm_evaluator: BaseEvaluator | None = None,
        top_k: int = 10,
        max_distance_threshold: float = 2.0,
    ):
        self.sdk = sdk
        self.hotstore = hotstore
        self.llm_evaluator = llm_evaluator
        self.top_k = top_k
        self.max_distance_threshold = max_distance_threshold
    
    async def explain(self, drift_alert: DriftAlert) -> DriftExplanation:
        """Generate explanation for drift alert.
        
        Args:
            drift_alert: Drift alert context
            
        Returns:
            DriftExplanation with top-k traces and LLM summary
        """
        start_time = time.time()
        drift_report = drift_alert.drift_report
        
        logger.info(
            f"Generating drift explanation for agent {drift_report.agent_id}, "
            f"KL={drift_report.kl_score:.4f}, severity={drift_report.severity}"
        )
        
        try:
            # Get top-k most deviating embeddings
            top_k_traces = await self._get_top_k_deviating_embeddings(
                drift_report.agent_id,
                drift_report.baseline_window_start,
                drift_report.baseline_window_end,
                drift_report.last_window_start,
                drift_report.last_window_end,
            )
            
            # Generate LLM explanation if evaluator available
            summary = ""
            hypothesis = ""
            if self.llm_evaluator and top_k_traces:
                summary, hypothesis = await self._generate_llm_explanation(
                    drift_report,
                    top_k_traces,
                )
            
            # Create explanation
            explanation = DriftExplanation(
                top_k_traces=top_k_traces,
                summary=summary,
                hypothesis=hypothesis,
                generated_at=time.time(),
                agent_id=drift_report.agent_id,
                kl_score=drift_report.kl_score,
                threshold=drift_report.threshold,
                severity=drift_report.severity,
            )
            
            # Store explanation in ClickHouse
            await self._store_explanation(explanation)
            
            duration = time.time() - start_time
            logger.info(
                f"Drift explanation generated for agent {drift_report.agent_id} "
                f"in {duration:.2f}s: {len(top_k_traces)} traces"
            )
            
            return explanation
            
        except Exception as e:
            logger.error(f"Failed to generate drift explanation: {e}")
            # Return minimal explanation on failure
            return DriftExplanation(
                top_k_traces=[],
                summary=f"Failed to generate explanation: {str(e)}",
                hypothesis="Unable to determine hypothesis due to error",
                generated_at=time.time(),
                agent_id=drift_report.agent_id,
                kl_score=drift_report.kl_score,
                threshold=drift_report.threshold,
                severity=drift_report.severity,
            )
    
    async def _get_top_k_deviating_embeddings(
        self,
        agent_id: str,
        baseline_window_start: float,
        baseline_window_end: float,
        last_window_start: float,
        last_window_end: float,
    ) -> list[dict]:
        """Get top-k embeddings with maximum distance from baseline centroid.
        
        Args:
            agent_id: Target agent identifier
            baseline_window_start: Baseline period start timestamp
            baseline_window_end: Baseline period end timestamp
            last_window_start: Last window start timestamp
            last_window_end: Last window end timestamp
            
        Returns:
            List of trace dicts with embedding and distance from centroid
        """
        # First get baseline centroid
        baseline_centroid = await self._compute_baseline_centroid(
            agent_id, baseline_window_start, baseline_window_end
        )
        
        if not baseline_centroid:
            logger.warning(f"Could not compute baseline centroid for agent {agent_id}")
            raise ValueError(f"Could not compute baseline centroid for agent {agent_id}")
        
        # Get last window embeddings with distances
        query = """
        SELECT 
            trace_id,
            embedding,
            llm_output_text,
            created_at,
            distance_from_centroid
        FROM (
            SELECT 
                trace_id,
                embedding,
                llm_output_text,
                created_at,
                sqrt(sum(pow(x - c, 2)) for x, c in zip(embedding, %(centroid)s)) as distance_from_centroid
            FROM span_embeddings
            WHERE agent_id = %(agent_id)s
            AND length(embedding) > 0
            AND created_at >= toDateTime64(%(last_window_start)s, 3)
            AND created_at < toDateTime64(%(last_window_end)s, 3)
            ORDER BY created_at DESC
        )
        WHERE distance_from_centroid <= %(max_distance)s
        ORDER BY distance_from_centroid DESC
        LIMIT %(top_k)s
        """
        
        try:
            results = await self.hotstore._execute_clickhouse(query, {
                "agent_id": agent_id,
                "centroid": baseline_centroid,
                "last_window_start": last_window_start,
                "last_window_end": last_window_end,
                "max_distance": self.max_distance_threshold,
                "top_k": self.top_k,
            })
        except Exception as e:
            logger.error(f"Failed to query top-k embeddings: {e}")
            return []
        
        top_k_traces = []
        for row in results:
            trace_id, embedding, llm_output_text, created_at, distance = row
            if embedding and distance is not None:
                top_k_traces.append({
                    "trace_id": trace_id,
                    "embedding": embedding,
                    "llm_output_text": llm_output_text or "",
                    "created_at": created_at,
                    "distance_from_centroid": float(distance),
                })
        
        logger.info(f"Found {len(top_k_traces)} top-k deviating embeddings for agent {agent_id}")
        return top_k_traces
    
    async def _compute_baseline_centroid(
        self, agent_id: str, window_start: float, window_end: float
    ) -> list[float] | None:
        """Compute centroid (mean) of embeddings in baseline window.
        
        Args:
            agent_id: Target agent identifier
            window_start: Baseline window start timestamp
            window_end: Baseline window end timestamp
            
        Returns:
            Centroid vector or None if no embeddings found
        """
        query = """
        SELECT embedding
        FROM span_embeddings
        WHERE agent_id = %(agent_id)s
        AND length(embedding) > 0
        AND created_at >= toDateTime64(%(window_start)s, 3)
        AND created_at < toDateTime64(%(window_end)s, 3)
        """
        
        try:
            results = await self.hotstore._execute_clickhouse(query, {
                "agent_id": agent_id,
                "window_start": window_start,
                "window_end": window_end,
            })
        except Exception as e:
            logger.error(f"Failed to query baseline embeddings: {e}")
            return None
        
        embeddings = []
        for row in results:
            if row and row[0]:
                embedding = row[0]
                if isinstance(embedding, list) and len(embedding) > 0:
                    embeddings.append(embedding)
        
        if not embeddings:
            logger.warning(f"No baseline embeddings found for agent {agent_id}")
            return None
        
        # Compute centroid (mean of each dimension)
        dimensions = len(embeddings[0])
        centroid = [0.0] * dimensions
        for embedding in embeddings:
            for i in range(dimensions):
                centroid[i] += embedding[i]
        
        centroid = [x / len(embeddings) for x in centroid]
        logger.info(f"Computed baseline centroid for agent {agent_id}: {len(centroid)} dimensions")
        return centroid
    
    async def _generate_llm_explanation(
        self, drift_report: DriftReport, top_k_traces: list[dict]
    ) -> tuple[str, str]:
        """Generate LLM-based explanation of what changed in responses.
        
        Args:
            drift_report: Original drift detection report
            top_k_traces: Top-k deviating traces
            
        Returns:
            Tuple of (summary, hypothesis)
        """
        if not self.llm_evaluator or not top_k_traces:
            return "", ""
        
        try:
            # Prepare comparison data
            baseline_responses = []
            current_responses = []
            
            # Get baseline responses for comparison
            baseline_window_start = drift_report.baseline_window_start
            baseline_window_end = drift_report.baseline_window_end
            
            baseline_query = """
            SELECT trace_id, llm_output_text
            FROM span_embeddings
            WHERE agent_id = %(agent_id)s
            AND length(embedding) > 0
            AND created_at >= toDateTime64(%(baseline_start)s, 3)
            AND created_at < toDateTime64(%(baseline_end)s, 3)
            ORDER BY created_at ASC
            LIMIT 5
            """
            
            try:
                baseline_results = await self.hotstore._execute_clickhouse(baseline_query, {
                    "agent_id": drift_report.agent_id,
                    "baseline_start": baseline_window_start,
                    "baseline_end": baseline_window_end,
                })
                
                for row in baseline_results:
                    trace_id, text = row
                    if text:
                        baseline_responses.append(text[:500])  # Truncate for context
            except Exception as e:
                logger.warning(f"Could not fetch baseline responses: {e}")
            
            # Current responses (top-k deviating ones)
            for trace in top_k_traces[:5]:  # Use top 5 for comparison
                if trace["llm_output_text"]:
                    current_responses.append(trace["llm_output_text"][:500])
            
            if not baseline_responses or not current_responses:
                return "", ""
            
            # Create prompt for LLM judge
            prompt = f"""
            Compare these current LLM responses with these baseline responses to identify what changed:

            BASELINE RESPONSES:
            {chr(10).join(f"- {response}" for response in baseline_responses)}

            CURRENT RESPONSES:
            {chr(10).join(f"- {response}" for response in current_responses)}

            Drift Analysis:
            - KL-divergence score: {drift_report.kl_score:.4f}
            - Threshold: {drift_report.threshold:.4f}
            - Severity: {drift_report.severity}

            Please provide:
            1. A brief summary (1-2 sentences) of the main differences
            2. A hypothesis about what might have changed in the model or data

            Format your response as:
            SUMMARY: [summary text]
            HYPOTHESIS: [hypothesis text]
            """
            
            # Use LLM evaluator to get explanation
            eval_result = await self.llm_evaluator.evaluate(
                trace_id=f"drift_explanation_{int(time.time() * 1000)}",
                input_text=prompt,
                expected_output="",  # No expected output for explanation
                metadata={
                    "eval_type": "drift_explanation",
                    "agent_id": drift_report.agent_id,
                    "kl_score": drift_report.kl_score,
                    "severity": drift_report.severity,
                }
            )
            
            # Parse the response
            response_text = eval_result.output_text or ""
            summary = ""
            hypothesis = ""
            
            if response_text:
                lines = response_text.strip().split('\n')
                for line in lines:
                    line = line.strip()
                    if line.startswith("SUMMARY:"):
                        summary = line.replace("SUMMARY:", "").strip()
                    elif line.startswith("HYPOTHESIS:"):
                        hypothesis = line.replace("HYPOTHESIS:", "").strip()
            
            logger.info(f"Generated LLM explanation for drift: summary={summary[:50]}...")
            return summary, hypothesis
            
        except Exception as e:
            logger.error(f"Failed to generate LLM explanation: {e}")
            return "", ""
    
    async def _store_explanation(self, explanation: DriftExplanation) -> None:
        """Store drift explanation in ClickHouse.
        
        Args:
            explanation: Drift explanation to store
        """
        query = """
        INSERT INTO drift_explanations (
            top_k_traces,
            summary,
            hypothesis,
            generated_at,
            agent_id,
            kl_score,
            threshold,
            severity
        ) VALUES (
            %(top_k_traces)s,
            %(summary)s,
            %(hypothesis)s,
            toDateTime64(%(generated_at)s, 3),
            %(agent_id)s,
            %(kl_score)s,
            %(threshold)s,
            %(severity)s
        )
        """
        
        try:
            await self.hotstore._execute_clickhouse(query, explanation.to_dict())
            logger.debug(f"Stored drift explanation for agent {explanation.agent_id}")
        except Exception as e:
            logger.error(f"Failed to store drift explanation: {e}")
            # Continue without blocking - explanation generation is still working