"""PC26 (T2.5.3) — drift threshold store.

The alert threshold for an agent is the p99 of its KL-divergence scores over a
trailing 30-day window of ``drift_history``. This module owns the YAML file that
holds those values; ``scripts/cron/calibrate_drift_threshold.py`` writes it and
``DriftDetector`` reads it.

Why a file and not an env var: the detector runs in a long-lived cron process
and the calibration job runs monthly in another one. A file is the only channel
where the write (calibration) and the read (detection) are decoupled without
either side restarting the other. The detector re-reads the file on every run
and caches by mtime, so a calibration lands within one 15-minute tick.

The write is atomic (temp file + ``os.replace``) because the detector may read
concurrently; a torn YAML would take drift detection down until the next
monthly run.
"""

from __future__ import annotations

import logging
import os
import tempfile
import time
from typing import Any, Optional

import yaml

logger = logging.getLogger(__name__)

DEFAULT_THRESHOLDS_PATH = "configs/drift_thresholds.yaml"
DEFAULT_KL_THRESHOLD = 0.1
DEFAULT_HISTORY_WINDOW_DAYS = 30
DEFAULT_PERCENTILE = 99.0

#: Repository root (agent_obs/drift/threshold.py -> parents[2]).
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

#: Minimum KL samples required before a calibrated value replaces the default.
#: Mirrors DriftDetector's own sample-size guard (100) so we never calibrate a
#: p99 from a handful of runs.
MIN_SAMPLES_FOR_CALIBRATION = 100

_KNOWN_KEYS = frozenset(
    {
        "version",
        "default_kl_threshold",
        "history_window_days",
        "calibration_percentile",
        "thresholds",
    }
)


def resolve_path(path: str = DEFAULT_THRESHOLDS_PATH) -> str:
    """Anchor a relative threshold path to the repo root.

    The cron container and the agent process do not necessarily share a working
    directory, so a bare ``configs/drift_thresholds.yaml`` would resolve
    differently for each. Anchoring to the package location keeps the detector
    and the calibration job pointed at the same file.
    """
    if os.path.isabs(path):
        return path
    if os.path.exists(path):
        return path
    return os.path.join(PROJECT_ROOT, path)


def _coerce_float(value: Any, fallback: float) -> float:
    """Best-effort float conversion; anything unusable falls back."""
    try:
        result = float(value)
    except (TypeError, ValueError):
        return fallback
    if result != result or result in (float("inf"), float("-inf")):  # NaN / inf
        return fallback
    return result


def _normalise_entry(entry: Any) -> dict[str, Any]:
    """Coerce one agent entry into the canonical shape.

    Unknown keys are dropped and bad values fall back to the default rather than
    propagating, so a hand-edited or partially-written file degrades to "use
    0.1" instead of raising inside the detector's hot loop.
    """
    if not isinstance(entry, dict):
        return {
            "kl_threshold": DEFAULT_KL_THRESHOLD,
            "calibrated_at": None,
            "sample_size": 0,
        }

    calibrated_at = entry.get("calibrated_at")
    if calibrated_at is not None and not isinstance(calibrated_at, str):
        calibrated_at = str(calibrated_at)

    try:
        sample_size = int(entry.get("sample_size") or 0)
    except (TypeError, ValueError):
        sample_size = 0

    normalised = {
        "kl_threshold": _coerce_float(
            entry.get("kl_threshold"), DEFAULT_KL_THRESHOLD
        ),
        "calibrated_at": calibrated_at or None,
        "sample_size": max(0, sample_size),
    }

    # Preserved verbatim: the calibration job writes the reason an agent was
    # skipped, and dropping it here would make the file lie about its own state.
    skipped = entry.get("calibration_skipped_reason")
    if skipped:
        normalised["calibration_skipped_reason"] = str(skipped)

    return normalised


def load_thresholds(
    path: str = DEFAULT_THRESHOLDS_PATH,
) -> dict[str, dict[str, Any]]:
    """Read the threshold file.

    Returns a mapping of ``agent_id -> {kl_threshold, calibrated_at, sample_size}``.
    A missing or unparsable file yields ``{}`` — every agent then falls back to
    ``DEFAULT_KL_THRESHOLD``, which is the documented behaviour for an agent
    that has not been calibrated yet.
    """
    path = resolve_path(path)
    if not os.path.exists(path):
        logger.warning(
            "drift thresholds file %s not found, using default %.2f for all agents",
            path,
            DEFAULT_KL_THRESHOLD,
        )
        return {}

    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle)
    except (OSError, yaml.YAMLError) as exc:
        logger.error("failed to read drift thresholds from %s: %s", path, exc)
        return {}

    if not isinstance(raw, dict):
        logger.error("drift thresholds file %s is not a mapping, ignoring", path)
        return {}

    entries = raw.get("thresholds")
    if not isinstance(entries, dict):
        logger.warning("no 'thresholds' mapping in %s, using defaults", path)
        return {}
    result: dict[str, dict[str, Any]] = {}
    for agent_id, entry in entries.items():
        if not isinstance(agent_id, str) or not agent_id:
            continue
        result[agent_id] = _normalise_entry(entry)

    logger.debug("loaded %d drift thresholds from %s", len(result), path)
    return result


def save_thresholds(
    thresholds: dict[str, dict[str, Any]],
    path: str = DEFAULT_THRESHOLDS_PATH,
) -> None:
    """Write the threshold file atomically.

    Preserves the file-level keys (default, window, percentile) so calibration
    does not silently reset the configuration knobs the file documents.
    """
    path = resolve_path(path)
    document: dict[str, Any] = {
        "version": 1,
        "default_kl_threshold": DEFAULT_KL_THRESHOLD,
        "history_window_days": DEFAULT_HISTORY_WINDOW_DAYS,
        "calibration_percentile": DEFAULT_PERCENTILE,
    }

    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                existing = yaml.safe_load(handle)
            if isinstance(existing, dict):
                for key in ("default_kl_threshold", "history_window_days", "calibration_percentile"):
                    if key in existing:
                        document[key] = existing[key]
        except (OSError, yaml.YAMLError):
            logger.debug("could not merge existing drift threshold config at %s", path)

    document["thresholds"] = {
        agent_id: _normalise_entry(entry)
        for agent_id, entry in sorted(thresholds.items())
    }

    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)

    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=directory,
        prefix=".drift_thresholds.",
        suffix=".tmp",
        delete=False,
    )
    try:
        with handle:
            handle.write("# Managed by scripts/cron/calibrate_drift_threshold.py (PC26).\n")
            handle.write("# Do not hand-edit kl_threshold: rewritten on every monthly run.\n")
            yaml.safe_dump(document, handle, sort_keys=False, default_flow_style=False)
        os.replace(handle.name, path)
    except Exception:
        # Never leave a stray temp file behind on failure.
        try:
            os.unlink(handle.name)
        except OSError:
            pass
        raise

    logger.info("wrote %d drift thresholds to %s", len(document["thresholds"]), path)


def get_threshold(agent_id: str, thresholds: dict[str, dict[str, Any]]) -> float:
    """Return the effective threshold for ``agent_id``."""
    entry = thresholds.get(agent_id)
    if not entry:
        return DEFAULT_KL_THRESHOLD
    return _coerce_float(entry.get("kl_threshold"), DEFAULT_KL_THRESHOLD)


def get_calibrated_at(agent_id: str, thresholds: dict[str, dict[str, Any]]) -> Optional[str]:
    """Return the ISO timestamp of the last calibration, or ``None``."""
    entry = thresholds.get(agent_id)
    if not entry:
        return None
    return entry.get("calibrated_at")


def is_calibrated(entry: dict[str, Any] | None) -> bool:
    """True when the entry carries a real calibration rather than a seeded default."""
    if not entry:
        return False
    return bool(entry.get("calibrated_at")) and int(entry.get("sample_size") or 0) > 0


def file_mtime(path: str = DEFAULT_THRESHOLDS_PATH) -> float:
    """Return the config mtime, or 0.0 when the file is absent.

    Used as a cache key so the detector re-reads the YAML only when the
    calibration job has actually rewritten it.
    """
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


class ThresholdStore:
    """mtime-cached view over the threshold file.

    ``DriftDetector`` holds one of these and calls :meth:`threshold` per run.
    The cache is invalidated by comparing mtime, which makes dynamic reloads
    (PC26 requirement 2: no SDK restart) free — a stat() per run instead of a
    YAML parse every 15 minutes.
    """

    def __init__(self, path: str = DEFAULT_THRESHOLDS_PATH) -> None:
        self.path = resolve_path(path)
        self._mtime: float = -1.0
        self._thresholds: dict[str, dict[str, Any]] = {}
        self._loaded_at: float = 0.0

    def _ensure_loaded(self) -> dict[str, dict[str, Any]]:
        current = file_mtime(self.path)
        if current != self._mtime or not self._thresholds:
            self._thresholds = load_thresholds(self.path)
            self._mtime = current
            self._loaded_at = time.time()
        return self._thresholds

    def threshold(self, agent_id: str) -> float:
        """Effective threshold for ``agent_id``, reloading if the file changed."""
        return get_threshold(agent_id, self._ensure_loaded())

    def entry(self, agent_id: str) -> dict[str, Any]:
        """Full threshold record for ``agent_id`` (defaults filled in)."""
        entries = self._ensure_loaded()
        return dict(entries.get(agent_id) or _normalise_entry(None))

    def calibrated_at(self, agent_id: str) -> Optional[str]:
        return get_calibrated_at(agent_id, self._ensure_loaded())

    def reload(self) -> dict[str, dict[str, Any]]:
        """Force a re-read regardless of mtime."""
        self._mtime = -1.0
        self._thresholds = {}
        return self._ensure_loaded()

    @property
    def loaded_at(self) -> float:
        return self._loaded_at

    def __len__(self) -> int:
        return len(self._ensure_loaded())


__all__ = [
    "DEFAULT_HISTORY_WINDOW_DAYS",
    "DEFAULT_KL_THRESHOLD",
    "DEFAULT_PERCENTILE",
    "DEFAULT_THRESHOLDS_PATH",
    "MIN_SAMPLES_FOR_CALIBRATION",
    "PROJECT_ROOT",
    "ThresholdStore",
    "file_mtime",
    "get_calibrated_at",
    "get_threshold",
    "is_calibrated",
    "load_thresholds",
    "resolve_path",
    "save_thresholds",
]
