"""Main drift detection orchestrator for PC25."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

from agent_obs.drift.kl_divergence import compute_kl_from_embeddings
from agent_obs.drift.models import DriftReport
from agent_obs.drift.threshold import (
    DEFAULT_KL_THRESHOLD,
    DEFAULT_THRESHOLDS_PATH,
    ThresholdStore,
)
from agent_obs.metrics import (
    drift_alerts_total,
    drift_kl_score,
    drift_runs_total,
    drift_threshold_value,
)
from agent_obs.storage.hot import HotStore

if TYPE_CHECKING:
    from agent_obs.observability import ObservabilitySDK

logger = logging.getLogger(__name__)

#: Sample-size floor for a statistically meaningful KL comparison (PC25).
MIN_SAMPLE_SIZE = 100


class DriftDetector:
    """Drift detection engine using KL-divergence analysis on response embeddings.
    
    PC25: Compares recent LLM response embeddings against a 7-day baseline
    to detect model drift using histogram-based KL-divergence.

    PC26: the alert threshold is no longer fixed. It is read per agent from
    ``configs/drift_thresholds.yaml``, which the monthly calibration job
    rewrites with the p99 of that agent's 30-day KL history. The file is
    re-read on every run (mtime-cached), so a calibration takes effect
    without restarting this process.

    Passing ``kl_threshold`` explicitly pins the threshold and bypasses the
    config file entirely — that keeps PC25 callers and tests deterministic.
    """

    def __init__(
        self,
        sdk: ObservabilitySDK,
        hotstore: HotStore,
        baseline_hours: int = 168,  # 7 days
        last_window_hours: int = 1,   # 1 hour
        kl_threshold: float | None = None,  # PC25 override; None = read from config
        thresholds_path: str = DEFAULT_THRESHOLDS_PATH,
    ):
        self.sdk = sdk
        self.hotstore = hotstore
        self.baseline_hours = baseline_hours
        self.last_window_hours = last_window_hours
        # None means "not pinned" — the config file decides. Kept as a plain
        # attribute for backwards compatibility with the PC25 signature.
        self.kl_threshold = DEFAULT_KL_THRESHOLD if kl_threshold is None else kl_threshold
        self._pinned_threshold = kl_threshold
        self.threshold_store = ThresholdStore(thresholds_path)

    def effective_threshold(self, agent_id: str) -> float:
        """Threshold actually applied to ``agent_id`` on this run.

        An explicit ``kl_threshold`` wins; otherwise the value comes from
        ``configs/drift_thresholds.yaml``, falling back to 0.1 for an agent
        that has not been calibrated yet.
        """
        if self._pinned_threshold is not None:
            return self._pinned_threshold
        return self.threshold_store.threshold(agent_id)

    def reload_threshold(self, agent_id: str) -> float:
        """Force a re-read of the config file and return the new threshold.

        Not needed on the normal path — the mtime cache already picks up a
        calibration between ticks — but useful right after a manual edit, and
        for tests that assert dynamic reload without touching mtime.
        """
        self.threshold_store.reload()
        return self.effective_threshold(agent_id)

    async def run_once(self, agent_id: str) -> DriftReport:
        """Execute single drift detection run for an agent.
        
        Args:
            agent_id: Target agent identifier
            
        Returns:
            DriftReport with detection results
        """
        start_time = time.time()
        threshold = self.effective_threshold(agent_id)
        drift_threshold_value.labels(agent_id=agent_id).set(threshold)
        
        try:
            # Get embedding windows
            baseline_embeddings = await self._get_baseline_embeddings(agent_id)
            last_embeddings = await self._get_last_window_embeddings(agent_id)
            
            # Skip if sample size is too small for statistical significance
            if len(last_embeddings) < MIN_SAMPLE_SIZE:
                logger.info(
                    f"Skipping drift detection for agent {agent_id}: "
                    f"sample_size_last={len(last_embeddings)} < {MIN_SAMPLE_SIZE} (minimum required)"
                )
                
                report = DriftReport(
                    trace_id=f"drift_skip_{int(time.time() * 1000)}",
                    eval_id=f"drift_skip_{agent_id}_{int(time.time() * 1000)}",
                    eval_latency_seconds=time.time() - start_time,
                    kl_score=0.0,
                    threshold=threshold,
                    is_drift_detected=False,
                    severity="info",
                    baseline_window_start=time.time() - (self.baseline_hours * 3600),
                    baseline_window_end=time.time() - (self.last_window_hours * 3600),
                    last_window_start=time.time() - (self.last_window_hours * 3600),
                    last_window_end=time.time(),
                    sample_size_baseline=len(baseline_embeddings),
                    sample_size_last=len(last_embeddings),
                    agent_id=agent_id,
                    flags=["sample_size_too_small"],
                )
                
                # Emit metrics for skipped runs
                drift_runs_total.labels(agent_id=agent_id).inc()
                logger.info(
                    f"Drift check skipped for agent {agent_id} due to insufficient data"
                )
                
                return report
            
            # Compute KL-divergence with more bins for stability
            kl_score = compute_kl_from_embeddings(baseline_embeddings, last_embeddings, bins=100)
            
            # Determine drift detection
            is_drift = kl_score > threshold
            severity = self._compute_severity(kl_score, threshold)
            
            # Create report
            report = DriftReport(
                trace_id=f"drift_{int(time.time() * 1000)}",
                eval_id=f"drift_{agent_id}_{int(time.time() * 1000)}",
                eval_latency_seconds=time.time() - start_time,
                kl_score=kl_score,
                threshold=threshold,
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
                    f"(threshold={threshold}) severity={severity}"
                )
            else:
                logger.info(
                    f"Drift check for agent {agent_id}: KL={kl_score:.4f} "
                    f"(threshold={threshold}) no drift"
                )
            
            # Write report to drift_history table
            try:
                self.hotstore.write_drift_history(report.to_dict())
                logger.debug(f"Drift report written to drift_history for agent {agent_id}")
            except Exception as e:
                logger.error(f"Failed to write drift report to ClickHouse: {e}")
                # Continue without blocking - drift detection is still working
            
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
        
        query = """
        SELECT response_embedding 
        FROM spans_hot 
        WHERE agent_id = %(agent_id)s 
        AND response_embedding IS NOT NULL
        AND created_at >= toDateTime64(%(baseline_start)s, 3)
        AND created_at < toDateTime64(%(baseline_end)s, 3)
        ORDER BY created ASC
        """
        
        results = await self.hotstore._execute_clickhouse(query, {
            "agent_id": agent_id,
            "baseline_start": baseline_start,
            "baseline_end": baseline_end
        })
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
        
        query = """
        SELECT response_embedding 
        FROM spans_hot 
        WHERE agent_id = %(agent_id)s 
        AND response_embedding IS NOT NULL
        AND created_at >= toDateTime64(%(last_window_start)s, 3)
        ORDER BY created ASC
        """
        
        results = await self.hotstore._execute_clickhouse(query, {
            "agent_id": agent_id,
            "last_window_start": last_window_start
        })
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
        # A calibrated threshold could legitimately round to 0 on a dead-flat
        # history; every non-zero score is then infinitely worse than baseline.
        if threshold <= 0:
            return "critical" if kl_score > 0 else "info"

        ratio = kl_score / threshold
        
        if ratio >= 3.0:
            return "critical"
        elif ratio >= 2.0:
            return "warning"
        else:
            return "info"