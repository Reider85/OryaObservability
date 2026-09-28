"""Main drift detection orchestrator for PC25."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

from agent_obs.drift.kl_divergence import compute_kl_from_embeddings
from agent_obs.drift.models import DriftReport
from agent_obs.metrics import (
    drift_alerts_total,
    drift_kl_score,
    drift_runs_total,
)
from agent_obs.storage.hot import HotStore

if TYPE_CHECKING:
    from agent_obs.observability import ObservabilitySDK

logger = logging.getLogger(__name__)


class DriftDetector:
    """Drift detection engine using KL-divergence analysis on response embeddings.
    
    PC25: Compares recent LLM response embeddings against a 7-day baseline
    to detect model drift using histogram-based KL-divergence.
    """
    
    def __init__(
        self,
        sdk: ObservabilitySDK,
        hotstore: HotStore,
        baseline_hours: int = 168,  # 7 days
        last_window_hours: int = 1,   # 1 hour
        kl_threshold: float = 0.1,    # Tunable threshold
    ):
        self.sdk = sdk
        self.hotstore = hotstore
        self.baseline_hours = baseline_hours
        self.last_window_hours = last_window_hours
        self.kl_threshold = kl_threshold
        
    async def run_once(self, agent_id: str) -> DriftReport:
        """Execute single drift detection run for an agent.
        
        Args:
            agent_id: Target agent identifier
            
        Returns:
            DriftReport with detection results
        """
        start_time = time.time()
        
        try:
            # Get embedding windows
            baseline_embeddings = await self._get_baseline_embeddings(agent_id)
            last_embeddings = await self._get_last_window_embeddings(agent_id)
            
            # Compute KL-divergence with more bins for stability
            kl_score = compute_kl_from_embeddings(baseline_embeddings, last_embeddings, bins=100)
            
            # Determine drift detection
            is_drift = kl_score > self.kl_threshold
            severity = self._compute_severity(kl_score, self.kl_threshold)
            
            # Create report
            report = DriftReport(
                trace_id=f"drift_{int(time.time() * 1000)}",
                eval_id=f"drift_{agent_id}_{int(time.time() * 1000)}",
                eval_latency_seconds=time.time() - start_time,
                kl_score=kl_score,
                threshold=self.kl_threshold,
                is_drift_detected=is_drift,
                severity=severity,
                baseline_window_start=time.time() - (self.baseline_hours * 3600),
                baseline_window_end=time.time() - (self.last_window_hours * 3600),
                last_window_start=time.time() - (self.last_window_hours * 3600),
                last_window_end=time.time(),
                sample_size_baseline=len(baseline_embeddings),
                sample_size_last=len(last_embeddings),
                agent_id=agent_id,
            )
            
            # Emit metrics
            drift_kl_score.labels(agent_id=agent_id).set(kl_score)
            drift_runs_total.labels(agent_id=agent_id).inc()
            
            if is_drift:
                drift_alerts_total.labels(agent_id=agent_id, severity=severity).inc()
                logger.warning(
                    f"Drift detected for agent {agent_id}: KL={kl_score:.4f} "
                    f"(threshold={self.kl_threshold}) severity={severity}"
                )
            else:
                logger.info(
                    f"Drift check for agent {agent_id}: KL={kl_score:.4f} "
                    f"(threshold={self.kl_threshold}) no drift"
                )
            
            return report
            
        except Exception as e:
            logger.error(f"Drift detection failed for agent {agent_id}: {e}")
            raise
    
    async def _get_baseline_embeddings(self, agent_id: str) -> list[list[float]]:
        """Get embeddings from baseline period (7 days ago, excluding last hour).
        
        Args:
            agent_id: Target agent identifier
            
        Returns:
            List of response embeddings from baseline period
        """
        baseline_start = time.time() - (self.baseline_hours * 3600)
        baseline_end = time.time() - (self.last_window_hours * 3600)
        
        query = f"""
        SELECT response_embedding 
        FROM spans_hot 
        WHERE agent_id = '{agent_id}' 
        AND response_embedding IS NOT NULL
        AND created_at >= toDateTime64({baseline_start}, 3)
        AND created_at < toDateTime64({baseline_end}, 3)
        ORDER BY created ASC
        """
        
        results = await self.hotstore._execute_clickhouse(query)
        embeddings = []
        
        for row in results:
            if row and row[0]:  # response_embedding exists
                embedding = row[0]
                if isinstance(embedding, list) and len(embedding) > 0:
                    embeddings.append(embedding)
        
        return embeddings
    
    async def _get_last_window_embeddings(self, agent_id: str) -> list[list[float]]:
        """Get embeddings from last window (most recent hour).
        
        Args:
            agent_id: Target agent identifier
            
        Returns:
            List of response embeddings from last window
        """
        last_window_start = time.time() - (self.last_window_hours * 3600)
        
        query = f"""
        SELECT response_embedding 
        FROM spans_hot 
        WHERE agent_id = '{agent_id}' 
        AND response_embedding IS NOT NULL
        AND created_at >= toDateTime64({last_window_start}, 3)
        ORDER BY created ASC
        """
        
        results = await self.hotstore._execute_clickhouse(query)
        embeddings = []
        
        for row in results:
            if row and row[0]:  # response_embedding exists
                embedding = row[0]
                if isinstance(embedding, list) and len(embedding) > 0:
                    embeddings.append(embedding)
        
        return embeddings
    
    def _compute_severity(self, kl_score: float, threshold: float) -> str:
        """Compute alert severity based on KL-score relative to threshold.
        
        Args:
            kl_score: Current KL-divergence score
            threshold: Detection threshold
            
        Returns:
            Severity level: 'info', 'warning', or 'critical'
        """
        ratio = kl_score / threshold
        
        if ratio >= 3.0:
            return "critical"
        elif ratio >= 2.0:
            return "warning"
        else:
            return "info"