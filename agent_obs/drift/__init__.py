"""Drift detection module: KL-divergence analysis of response embeddings.

PC25 provides the detector itself; PC26 adds the calibrated threshold store that
the detector reads on every run.
"""

from agent_obs.drift.detector import DriftDetector
from agent_obs.drift.kl_divergence import (
    compute_kl_divergence,
    compute_kl_from_embeddings,
    create_histogram,
    normalize_histogram,
)
from agent_obs.drift.models import DriftReport
from agent_obs.drift.threshold import (
    DEFAULT_HISTORY_WINDOW_DAYS,
    DEFAULT_KL_THRESHOLD,
    DEFAULT_PERCENTILE,
    DEFAULT_THRESHOLDS_PATH,
    MIN_SAMPLES_FOR_CALIBRATION,
    ThresholdStore,
    get_threshold,
    load_thresholds,
    save_thresholds,
)

__all__ = [
    "DEFAULT_HISTORY_WINDOW_DAYS",
    "DEFAULT_KL_THRESHOLD",
    "DEFAULT_PERCENTILE",
    "DEFAULT_THRESHOLDS_PATH",
    "MIN_SAMPLES_FOR_CALIBRATION",
    "DriftDetector",
    "DriftReport",
    "ThresholdStore",
    "compute_kl_divergence",
    "compute_kl_from_embeddings",
    "create_histogram",
    "get_threshold",
    "load_thresholds",
    "normalize_histogram",
    "save_thresholds",
]
