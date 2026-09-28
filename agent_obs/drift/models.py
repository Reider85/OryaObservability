"""Drift detection data models."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any


@dataclass
class DriftReport:
    """Drift detection result from PC25.
    
    Represents the output of drift analysis comparing recent embeddings
    against a baseline period.
    """
    
    trace_id: str
    eval_id: str
    eval_name: str = "drift_detection"
    eval_version: str = "1.0.0"
    eval_timestamp: float = 0.0
    eval_latency_seconds: float = 0.0
    
    # Drift detection results
    kl_score: float = 0.0
    threshold: float = 0.0
    is_drift_detected: bool = False
    severity: str = "info"  # info, warning, critical
    
    # Window metadata
    baseline_window_start: float = 0.0
    baseline_window_end: float = 0.0
    last_window_start: float = 0.0
    last_window_end: float = 0.0
    
    # Sample sizes
    sample_size_baseline: int = 0
    sample_size_last: int = 0
    
    # Additional metadata
    agent_id: str = ""
    model_name: str = ""
    flags: list[str] = None
    
    def __post_init__(self):
        if self.eval_timestamp == 0.0:
            self.eval_timestamp = time.time()
        if self.flags is None:
            self.flags = []
    
    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "trace_id": self.trace_id,
            "eval_id": self.eval_id,
            "eval_name": self.eval_name,
            "eval_version": self.eval_version,
            "eval_timestamp": self.eval_timestamp,
            "eval_latency_seconds": self.eval_latency_seconds,
            "kl_score": self.kl_score,
            "threshold": self.threshold,
            "is_drift_detected": self.is_drift_detected,
            "severity": self.severity,
            "baseline_window_start": self.baseline_window_start,
            "baseline_window_end": self.baseline_window_end,
            "last_window_start": self.last_window_start,
            "last_window_end": self.last_window_end,
            "sample_size_baseline": self.sample_size_baseline,
            "sample_size_last": self.sample_size_last,
            "agent_id": self.agent_id,
            "model_name": self.model_name,
            "flags": self.flags,
        }