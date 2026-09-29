#!/usr/bin/env python3
"""PC26 (T2.5.3) — monthly calibration of the drift alert threshold.

The alert threshold for an agent is the **p99 of its KL-divergence scores over
the trailing 30 days**. Taking p99 rather than max means the threshold sits at
the edge of observed normal behaviour, so only a genuinely unusual hour alerts.

This runs in its own process, monthly, alongside — never inside — the agent
(PC21 rule: "cron-jobs — это НЕ SDK-код"). It writes
``configs/drift_thresholds.yaml``, which ``DriftDetector`` re-reads on its next
tick, so the new value applies without restarting anything (PC26 DoD 2).

Why ClickHouse and not the Prometheus metric
---------------------------------------------
``drift_kl_score`` is a gauge: it holds the latest value and scrapes overwrite
it. There is no history to take a p99 over, so the ``drift_history`` table
(written by PC25 on every run) is the source of truth.

Safety rules
------------
* Fewer than 30 days of history, or fewer than 100 usable samples -> keep the
  default 0.1 and log. A p99 from a partial window is worse than no
  calibration: it would permanently blind the detector to drift.
* The write is atomic, so a concurrent detector read never sees a torn file.
* Re-running is idempotent — the same history yields the same threshold.

Usage::

    python scripts/cron/calibrate_drift_threshold.py
    python scripts/cron/calibrate_drift_threshold.py --agents a,b --dry-run
    python scripts/cron/calibrate_drift_threshold.py --sanity-check-only
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from agent_obs.drift.threshold import (  # noqa: E402
    DEFAULT_HISTORY_WINDOW_DAYS,
    DEFAULT_KL_THRESHOLD,
    DEFAULT_PERCENTILE,
    DEFAULT_THRESHOLDS_PATH,
    MIN_SAMPLES_FOR_CALIBRATION,
    get_threshold,
    load_thresholds,
    save_thresholds,
)
from agent_obs.metrics import (  # noqa: E402
    drift_calibration_runs_total,
    drift_sanity_check_total,
    drift_threshold_calibrated_at,
    drift_threshold_value,
)

logging.basicConfig(
    level=os.environ.get("AGENT_OBS_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("agent_obs.cron.calibrate_drift_threshold")

#: How many "known good" historical runs the quarterly sanity check inspects.
SANITY_CHECK_SAMPLE = 100
#: Fraction of the sample allowed to exceed the new threshold before we call
#: the baseline stale. 0.10 == "if more than 10 of 100 good runs now look like
#: drift, the baseline itself has drifted and must be rebuilt".
SANITY_CHECK_TOLERANCE = 0.10

OUTCOME_CALIBRATED = "calibrated"
OUTCOME_INSUFFICIENT_HISTORY = "insufficient_history"
OUTCOME_NO_DATA = "no_data"
OUTCOME_ERROR = "error"


@dataclass
class AgentCalibration:
    """Outcome of calibrating a single agent."""

    agent_id: str
    threshold: float = DEFAULT_KL_THRESHOLD
    previous_threshold: float = DEFAULT_KL_THRESHOLD
    sample_size: int = 0
    history_days: float = 0.0
    outcome: str = OUTCOME_INSUFFICIENT_HISTORY
    detail: str = ""
    calibrated: bool = False
    calibrated_at: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "threshold": self.threshold,
            "previous_threshold": self.previous_threshold,
            "sample_size": self.sample_size,
            "history_days": round(self.history_days, 2),
            "outcome": self.outcome,
            "detail": self.detail,
            "calibrated": self.calibrated,
            "calibrated_at": self.calibrated_at,
        }


@dataclass
class SanityCheckResult:
    """Result of the quarterly "is the baseline still trustworthy" check.

    Guards against slow system-wide drift: if the baseline window has itself
    drifted, a p99 calibrated from it just ratchets the threshold upward every
    month until nothing ever alerts.
    """

    agent_id: str
    checked: int = 0
    exceeding: int = 0
    baseline_stale: bool = False
    detail: str = ""
    threshold: float = DEFAULT_KL_THRESHOLD

    @property
    def exceed_ratio(self) -> float:
        if self.checked == 0:
            return 0.0
        return self.exceeding / self.checked

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "checked": self.checked,
            "exceeding": self.exceeding,
            "exceed_ratio": round(self.exceed_ratio, 4),
            "baseline_stale": self.baseline_stale,
            "threshold": self.threshold,
            "detail": self.detail,
        }


# ---------------------------------------------------------------------------
# Data access
# ---------------------------------------------------------------------------

# Only rows the detector actually scored are usable. Skipped runs
# (sample_size_too_small) carry kl_score = 0.0 and would drag the p99 down.
_KL_HISTORY_QUERY = """
SELECT kl_score, eval_timestamp
FROM drift_history
WHERE agent_id = %(agent_id)s
  AND is_drift_detected IN (0, 1)
  AND has(flags, 'sample_size_too_small') = 0
  AND eval_timestamp >= toDateTime64(%(since)s, 3)
ORDER BY eval_timestamp ASC
"""

#: "Known good" runs: scored, never flagged as drift, not a skip.
_GOOD_RUNS_QUERY = """
SELECT kl_score, eval_timestamp
FROM drift_history
WHERE agent_id = %(agent_id)s
  AND is_drift_detected = 0
  AND has(flags, 'sample_size_too_small') = 0
ORDER BY eval_timestamp DESC
LIMIT %(limit)s
"""

#: Agents with any history at all, used when no explicit list is configured.
_KNOWN_AGENTS_QUERY = """
SELECT DISTINCT agent_id
FROM drift_history
WHERE eval_timestamp >= toDateTime64(%(since)s, 3)
ORDER BY agent_id ASC
"""


async def _fetch_kl_history(
    hot_store: Any, agent_id: str, since: float
) -> list[tuple[float, float]]:
    """Return ``[(kl_score, eval_timestamp), ...]`` over the window."""
    rows = await hot_store._execute_clickhouse(
        _KL_HISTORY_QUERY, {"agent_id": agent_id, "since": since}
    )
    result: list[tuple[float, float]] = []
    for row in rows or []:
        if not row or row[0] is None:
            continue
        score = float(row[0])
        timestamp = float(row[1].timestamp()) if hasattr(row[1], "timestamp") else float(row[1])
        result.append((score, timestamp))
    return result


async def _fetch_known_good_runs(
    hot_store: Any, agent_id: str, limit: int
) -> list[float]:
    """Return KL scores for the most recent non-drift, non-skip runs."""
    rows = await hot_store._execute_clickhouse(
        _GOOD_RUNS_QUERY, {"agent_id": agent_id, "limit": limit}
    )
    return [float(row[0]) for row in (rows or []) if row and row[0] is not None]


async def discover_agents(hot_store: Any, window_days: int) -> list[str]:
    """List agents with drift history inside the calibration window."""
    since = time.time() - (window_days * 86400)
    rows = await hot_store._execute_clickhouse(
        _KNOWN_AGENTS_QUERY, {"since": since}
    )
    return [str(row[0]) for row in (rows or []) if row and row[0]]


# ---------------------------------------------------------------------------
# Calibration
# ---------------------------------------------------------------------------


def compute_p99(scores: list[float], percentile: float = DEFAULT_PERCENTILE) -> float:
    """Percentile of the KL history, linear interpolation.

    Exposed separately from the ClickHouse round-trip so the statistic can be
    tested directly.
    """
    if not scores:
        return 0.0
    return float(np.percentile(np.asarray(scores, dtype=np.float64), percentile))


def _history_span_days(samples: list[tuple[float, float]]) -> float:
    """Days between the oldest and newest sample in the window."""
    if len(samples) < 2:
        return 0.0
    timestamps = [ts for _, ts in samples]
    return (max(timestamps) - min(timestamps)) / 86400.0


async def calibrate_agent(
    hot_store: Any,
    agent_id: str,
    *,
    current: dict[str, dict[str, Any]],
    window_days: int = DEFAULT_HISTORY_WINDOW_DAYS,
    percentile: float = DEFAULT_PERCENTILE,
    min_samples: int = MIN_SAMPLES_FOR_CALIBRATION,
    require_full_window: bool = True,
    default_threshold: float = DEFAULT_KL_THRESHOLD,
) -> AgentCalibration:
    """Compute the p99 threshold for one agent.

    Args:
        current: currently loaded thresholds, for the previous-value comparison.
        require_full_window: refuse to calibrate unless the history spans
            ``window_days``. Guards the "calibrate from 3 days of data" failure.
    """
    previous = get_threshold(agent_id, current)
    result = AgentCalibration(agent_id=agent_id, previous_threshold=previous, threshold=previous)

    since = time.time() - (window_days * 86400)
    try:
        samples = await _fetch_kl_history(hot_store, agent_id, since)
    except Exception as exc:
        result.outcome = OUTCOME_ERROR
        result.detail = f"query failed: {exc}"
        logger.error("calibration query failed for %s: %s", agent_id, exc)
        return result

    if not samples:
        result.outcome = OUTCOME_NO_DATA
        result.detail = f"no drift_history rows in the last {window_days}d"
        logger.warning(
            "drift threshold not calibrated yet for %s: no history in %dd, using default %.2f",
            agent_id,
            window_days,
            previous,
        )
        return result

    scores = [score for score, _ in samples]
    result.sample_size = len(scores)
    result.history_days = _history_span_days(samples)

    if len(scores) < min_samples:
        result.outcome = OUTCOME_INSUFFICIENT_HISTORY
        result.detail = f"only {len(scores)} samples (need {min_samples})"
        logger.warning(
            "drift threshold not calibrated yet for %s: %d samples (need %d), using default %.2f",
            agent_id,
            len(scores),
            min_samples,
            previous,
        )
        return result

    if require_full_window and result.history_days < window_days * 0.95:
        result.outcome = OUTCOME_INSUFFICIENT_HISTORY
        result.detail = (
            f"history spans {result.history_days:.1f}d of the required {window_days}d"
        )
        logger.warning(
            "drift threshold not calibrated yet for %s: %s, using default %.2f",
            agent_id,
            result.detail,
            previous,
        )
        return result

    p99 = compute_p99(scores, percentile)
    if not np.isfinite(p99) or p99 < 0:
        result.outcome = OUTCOME_ERROR
        result.detail = f"p{percentile:g} produced a non-finite value ({p99})"
        logger.error("calibration produced invalid threshold for %s: %s", agent_id, result.detail)
        return result

    result.threshold = p99
    result.outcome = OUTCOME_CALIBRATED
    result.calibrated = True
    result.calibrated_at = datetime.now(timezone.utc).isoformat()
    result.detail = (
        f"p{percentile:g} of {len(scores)} samples over {result.history_days:.1f}d"
    )
    logger.info(
        "calibrated drift threshold for %s: %.4f -> %.4f (%s)",
        agent_id,
        previous,
        p99,
        result.detail,
    )
    return result


async def sanity_check_baseline(
    hot_store: Any,
    agent_id: str,
    threshold: float,
    *,
    sample_size: int = SANITY_CHECK_SAMPLE,
    tolerance: float = SANITY_CHECK_TOLERANCE,
) -> SanityCheckResult:
    """Quarterly check that known-good runs still look non-drifty.

    Samples the most recent runs that were scored as non-drift, and counts how
    many of them now exceed the newly calibrated threshold. A high exceedance
    rate means the *baseline* drifted, so the threshold is being ratcheted up
    against stale reference data and must be rebuilt.
    """
    result = SanityCheckResult(agent_id=agent_id, threshold=threshold)

    try:
        scores = await _fetch_known_good_runs(hot_store, agent_id, sample_size)
    except Exception as exc:
        result.detail = f"query failed: {exc}"
        logger.error("sanity check query failed for %s: %s", agent_id, exc)
        return result

    if not scores:
        result.detail = "no known-good runs available to check"
        logger.warning("sanity check skipped for %s: %s", agent_id, result.detail)
        return result

    result.checked = len(scores)
    result.exceeding = sum(1 for score in scores if score > threshold)
    result.baseline_stale = result.exceed_ratio > tolerance
    result.detail = (
        f"{result.exceeding}/{result.checked} known-good runs exceed "
        f"{threshold:.4f} (tolerance {tolerance:.0%})"
    )

    if result.baseline_stale:
        logger.error(
            "BASELINE STALE for %s: %s — drift threshold was calibrated against "
            "reference data that has itself drifted; rebuild the baseline from "
            "verified-good traces before trusting it",
            agent_id,
            result.detail,
        )
        drift_sanity_check_total.labels(agent_id=agent_id, result="stale").inc()
    else:
        logger.info("sanity check ok for %s: %s", agent_id, result.detail)
        drift_sanity_check_total.labels(agent_id=agent_id, result="ok").inc()

    return result


async def calibrate_all(
    hot_store: Any,
    agents: Optional[list[str]] = None,
    *,
    thresholds_path: str = DEFAULT_THRESHOLDS_PATH,
    window_days: int = DEFAULT_HISTORY_WINDOW_DAYS,
    percentile: float = DEFAULT_PERCENTILE,
    min_samples: int = MIN_SAMPLES_FOR_CALIBRATION,
    require_full_window: bool = True,
    dry_run: bool = False,
    run_sanity_check: bool = True,
    sanity_sample_size: int = SANITY_CHECK_SAMPLE,
) -> dict[str, Any]:
    """Recalibrate every agent and persist the result.

    The write is skipped entirely in ``dry_run`` mode, so the operator can
    preview the new thresholds before they take effect.
    """
    current = load_thresholds(thresholds_path)

    if agents is None:
        agents = await discover_agents(hot_store, window_days)
        if not agents:
            logger.warning("no agents with drift history in %dd; nothing to do", window_days)

    logger.info(
        "calibrating %d agent(s) over a %dd window at p%.4g",
        len(agents),
        window_days,
        percentile,
    )

    merged: dict[str, dict[str, Any]] = dict(current)
    results: list[AgentCalibration] = []
    sanity: list[SanityCheckResult] = []

    for agent_id in agents:
        calibration = await calibrate_agent(
            hot_store,
            agent_id,
            current=current,
            window_days=window_days,
            percentile=percentile,
            min_samples=min_samples,
            require_full_window=require_full_window,
        )
        results.append(calibration)

        drift_calibration_runs_total.labels(
            agent_id=agent_id, outcome=calibration.outcome
        ).inc()
        drift_threshold_value.labels(agent_id=agent_id).set(calibration.threshold)

        entry = dict(merged.get(agent_id) or {})
        if calibration.calibrated:
            entry["kl_threshold"] = calibration.threshold
            entry["calibrated_at"] = calibration.calibrated_at
            entry["sample_size"] = calibration.sample_size
            drift_threshold_calibrated_at.labels(agent_id=agent_id).set(time.time())
        else:
            # Keep whatever is in force, but record why it was not refreshed so
            # the file is self-explanatory.
            entry.setdefault("kl_threshold", calibration.threshold)
            entry.setdefault("calibrated_at", None)
            entry.setdefault("sample_size", 0)
            entry["calibration_skipped_reason"] = calibration.detail
        merged[agent_id] = entry

        if run_sanity_check and calibration.calibrated:
            sanity.append(
                await sanity_check_baseline(
                    hot_store,
                    agent_id,
                    calibration.threshold,
                    sample_size=sanity_sample_size,
                )
            )

    if dry_run:
        logger.info("dry run: not writing %s", thresholds_path)
    else:
        save_thresholds(merged, thresholds_path)

    calibrated = [r for r in results if r.calibrated]
    logger.info(
        "calibration complete: %d/%d agents calibrated, %d stale baseline(s)",
        len(calibrated),
        len(results),
        sum(1 for s in sanity if s.baseline_stale),
    )

    return {
        "agents": [r.to_dict() for r in results],
        "sanity_checks": [s.to_dict() for s in sanity],
        "calibrated_count": len(calibrated),
        "stale_count": sum(1 for s in sanity if s.baseline_stale),
        "thresholds": {aid: get_threshold(aid, merged) for aid in agents},
        "written": not dry_run,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _default_agents() -> list[str]:
    raw = os.environ.get("AGENT_OBS_CRON_DRIFT_AGENTS", "")
    return [part.strip() for part in raw.split(",") if part.strip()]


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="PC26: calibrate the drift alert threshold as p99 of 30d KL history."
    )
    parser.add_argument(
        "--agents",
        help="Comma-separated agent_ids. Default: AGENT_OBS_CRON_DRIFT_AGENTS, "
        "else every agent present in drift_history.",
    )
    parser.add_argument(
        "--thresholds-path", default=DEFAULT_THRESHOLDS_PATH,
        help="Path to the threshold YAML (default: %(default)s)",
    )
    parser.add_argument(
        "--window-days", type=int, default=DEFAULT_HISTORY_WINDOW_DAYS,
        help="KL history window in days (default: %(default)s)",
    )
    parser.add_argument(
        "--percentile", type=float, default=DEFAULT_PERCENTILE,
        help="Percentile used as the threshold (default: %(default)s)",
    )
    parser.add_argument(
        "--min-samples", type=int, default=MIN_SAMPLES_FOR_CALIBRATION,
        help="Minimum KL samples required to calibrate (default: %(default)s)",
    )
    parser.add_argument(
        "--allow-partial-window", action="store_true",
        help="Calibrate even when history spans less than the full window "
             "(skips the 30-day completeness guard)",
    )
    parser.add_argument(
        "--sanity-check", dest="sanity_check", action="store_true", default=True,
        help="Run the quarterly baseline sanity check (default: on)",
    )
    parser.add_argument(
        "--no-sanity-check", dest="sanity_check", action="store_false",
        help="Skip the baseline sanity check",
    )
    parser.add_argument(
        "--sanity-sample-size", type=int, default=SANITY_CHECK_SAMPLE,
        help="Known-good runs to inspect (default: %(default)s)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Compute and log the new thresholds without writing the file",
    )
    return parser.parse_args(argv)


async def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)

    agents = [part.strip() for part in (args.agents or "").split(",") if part.strip()]
    if not agents:
        agents = _default_agents() or None

    from agent_obs.storage.hot import HotStore

    hot_store = HotStore()
    try:
        if not hot_store.is_available():
            logger.error("ClickHouse is not reachable; leaving thresholds unchanged")
            return 1

        summary = await calibrate_all(
            hot_store,
            agents,
            thresholds_path=args.thresholds_path,
            window_days=args.window_days,
            percentile=args.percentile,
            min_samples=args.min_samples,
            require_full_window=not args.allow_partial_window,
            dry_run=args.dry_run,
            run_sanity_check=args.sanity_check,
            sanity_sample_size=args.sanity_sample_size,
        )
    finally:
        hot_store.close()

    for entry in summary["agents"]:
        logger.info(
            "agent=%s outcome=%s threshold=%.4f samples=%d %s",
            entry["agent_id"],
            entry["outcome"],
            entry["threshold"],
            entry["sample_size"],
            entry["detail"],
        )
    for check in summary["sanity_checks"]:
        logger.info(
            "sanity agent=%s checked=%d exceeding=%d stale=%s",
            check["agent_id"],
            check["checked"],
            check["exceeding"],
            check["baseline_stale"],
        )

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
