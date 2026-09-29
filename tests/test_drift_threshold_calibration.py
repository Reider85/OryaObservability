"""Tests for PC26 — drift threshold calibration (p99 of 30d KL history).

Covers three layers:

* ``agent_obs.drift.threshold`` — the YAML store the detector reads.
* ``DriftDetector`` — picks up a calibrated value without a restart.
* ``scripts/cron/calibrate_drift_threshold.py`` — the monthly job itself.

Every ClickHouse interaction is mocked; no test here needs a live backend.
"""

import importlib
import os
import sys
import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest
import yaml

from agent_obs.drift.detector import DriftDetector
from agent_obs.drift.threshold import (
    DEFAULT_KL_THRESHOLD,
    MIN_SAMPLES_FOR_CALIBRATION,
    ThresholdStore,
    get_calibrated_at,
    get_threshold,
    is_calibrated,
    load_thresholds,
    resolve_path,
    save_thresholds,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

calibration = importlib.import_module("scripts.cron.calibrate_drift_threshold")


@pytest.fixture
def thresholds_file(tmp_path):
    """Path to a throwaway threshold YAML, isolated from the repo config."""
    return str(tmp_path / "drift_thresholds.yaml")


def _write_yaml(path, document):
    with open(path, "w", encoding="utf-8") as handle:
        yaml.safe_dump(document, handle, sort_keys=False)
    return path


def _make_hotstore(history=None, good_runs=None, agents=None):
    """Mock HotStore whose ``_execute_clickhouse`` dispatches on the query text.

    Mirrors the real method's contract: the calibration module passes params as
    a dict, and rows come back as tuples.
    """
    history = history or {}
    good_runs = good_runs or {}
    agents = agents or []

    async def _execute(query, params=None):
        if "DISTINCT agent_id" in query:
            return [(agent,) for agent in agents]
        if "is_drift_detected = 0" in query:
            return [(score,) for score in good_runs.get(params["agent_id"], [])]
        return history.get(params["agent_id"], [])

    hotstore = MagicMock()
    hotstore._execute_clickhouse = AsyncMock(side_effect=_execute)
    return hotstore


def _history_rows(scores, days=30, end=None):
    """Build ``[(kl_score, eval_timestamp), ...]`` spread over ``days``."""
    end = end or time.time()
    start = end - (days * 86400)
    step = (end - start) / max(1, len(scores) - 1) if len(scores) > 1 else 0
    return [(score, start + (i * step)) for i, score in enumerate(scores)]


# ---------------------------------------------------------------------------
# Threshold store
# ---------------------------------------------------------------------------


class TestThresholdFile:
    """Loading, saving and defaulting the calibration file."""

    def test_load_valid_file(self, thresholds_file):
        _write_yaml(
            thresholds_file,
            {
                "version": 1,
                "default_kl_threshold": 0.1,
                "thresholds": {
                    "agent_a": {
                        "kl_threshold": 0.42,
                        "calibrated_at": "2026-09-21T05:23:00+00:00",
                        "sample_size": 2880,
                    }
                },
            },
        )

        result = load_thresholds(thresholds_file)

        assert result["agent_a"]["kl_threshold"] == 0.42
        assert result["agent_a"]["calibrated_at"] == "2026-09-21T05:23:00+00:00"
        assert result["agent_a"]["sample_size"] == 2880

    def test_load_missing_file_returns_empty(self, thresholds_file):
        """A missing file must degrade to defaults, not raise."""
        assert load_thresholds(thresholds_file) == {}

    def test_load_corrupt_file_returns_empty(self, thresholds_file):
        with open(thresholds_file, "w", encoding="utf-8") as handle:
            handle.write("thresholds: [this is not: a mapping\n  - broken")

        assert load_thresholds(thresholds_file) == {}

    def test_load_file_without_thresholds_key(self, thresholds_file):
        _write_yaml(thresholds_file, {"version": 1, "default_kl_threshold": 0.1})
        assert load_thresholds(thresholds_file) == {}

    def test_load_rejects_non_mapping_document(self, thresholds_file):
        _write_yaml(thresholds_file, ["not", "a", "mapping"])
        assert load_thresholds(thresholds_file) == {}

    def test_load_coerces_bad_threshold_to_default(self, thresholds_file):
        """A hand-edited non-numeric threshold degrades to 0.1, not an exception."""
        _write_yaml(
            thresholds_file,
            {"thresholds": {"agent_a": {"kl_threshold": "not-a-number"}}},
        )

        result = load_thresholds(thresholds_file)

        assert result["agent_a"]["kl_threshold"] == DEFAULT_KL_THRESHOLD

    def test_load_coerces_nan_threshold_to_default(self, thresholds_file):
        _write_yaml(
            thresholds_file,
            {"thresholds": {"agent_a": {"kl_threshold": float("nan")}}},
        )

        result = load_thresholds(thresholds_file)

        assert result["agent_a"]["kl_threshold"] == DEFAULT_KL_THRESHOLD

    def test_load_ignores_malformed_entries(self, thresholds_file):
        _write_yaml(
            thresholds_file,
            {"thresholds": {"good": {"kl_threshold": 0.3}, "bad": "scalar"}},
        )

        result = load_thresholds(thresholds_file)

        assert result["good"]["kl_threshold"] == 0.3
        assert result["bad"]["kl_threshold"] == DEFAULT_KL_THRESHOLD

    def test_get_threshold_known_agent(self, thresholds_file):
        _write_yaml(
            thresholds_file,
            {"thresholds": {"agent_a": {"kl_threshold": 0.25, "sample_size": 500}}},
        )

        thresholds = load_thresholds(thresholds_file)

        assert get_threshold("agent_a", thresholds) == 0.25

    def test_get_threshold_unknown_agent_defaults(self, thresholds_file):
        _write_yaml(
            thresholds_file, {"thresholds": {"agent_a": {"kl_threshold": 0.25}}}
        )

        thresholds = load_thresholds(thresholds_file)

        assert get_threshold("never_configured", thresholds) == DEFAULT_KL_THRESHOLD

    def test_get_threshold_empty_store_defaults(self):
        assert get_threshold("anything", {}) == DEFAULT_KL_THRESHOLD

    def test_get_calibrated_at(self, thresholds_file):
        _write_yaml(
            thresholds_file,
            {
                "thresholds": {
                    "agent_a": {
                        "kl_threshold": 0.25,
                        "calibrated_at": "2026-09-21T05:23:00+00:00",
                    }
                }
            },
        )

        thresholds = load_thresholds(thresholds_file)

        assert get_calibrated_at("agent_a", thresholds) == "2026-09-21T05:23:00+00:00"
        assert get_calibrated_at("agent_b", thresholds) is None

    def test_is_calibrated(self):
        assert is_calibrated(
            {"calibrated_at": "2026-09-21T05:23:00+00:00", "sample_size": 100}
        )
        # Seeded default: no timestamp, no samples.
        assert not is_calibrated({"calibrated_at": None, "sample_size": 0})
        assert not is_calibrated(None)

    def test_save_thresholds_roundtrip(self, thresholds_file):
        save_thresholds(
            {
                "agent_a": {
                    "kl_threshold": 0.31,
                    "calibrated_at": "2026-09-21T05:23:00+00:00",
                    "sample_size": 2900,
                }
            },
            thresholds_file,
        )

        reloaded = load_thresholds(thresholds_file)

        assert reloaded["agent_a"]["kl_threshold"] == 0.31
        assert reloaded["agent_a"]["sample_size"] == 2900

    def test_save_thresholds_creates_missing_directory(self, tmp_path):
        nested = str(tmp_path / "does" / "not" / "exist" / "drift.yaml")

        save_thresholds({"agent_a": {"kl_threshold": 0.2}}, nested)

        assert os.path.exists(nested)

    def test_save_thresholds_preserves_config_keys(self, thresholds_file):
        """Calibration must not silently reset the documented knobs."""
        _write_yaml(
            thresholds_file,
            {
                "version": 1,
                "default_kl_threshold": 0.1,
                "history_window_days": 30,
                "calibration_percentile": 99,
                "thresholds": {},
            },
        )

        save_thresholds({"agent_a": {"kl_threshold": 0.4}}, thresholds_file)

        with open(thresholds_file, "r", encoding="utf-8") as handle:
            document = yaml.safe_load(handle)

        assert document["default_kl_threshold"] == 0.1
        assert document["history_window_days"] == 30
        assert document["calibration_percentile"] == 99

    def test_save_thresholds_is_atomic(self, thresholds_file, monkeypatch):
        """The detector may read concurrently; no temp files may be left behind."""
        directory = os.path.dirname(thresholds_file)
        save_thresholds({"agent_a": {"kl_threshold": 0.2}}, thresholds_file)

        leftovers = [n for n in os.listdir(directory) if n.startswith(".drift_thresholds.")]

        assert leftovers == []

    def test_resolve_path_absolute_unchanged(self):
        absolute = os.path.abspath(os.path.join("configs", "drift_thresholds.yaml"))
        assert resolve_path(absolute) == absolute

    def test_resolve_path_anchors_relative_to_repo_root(self, tmp_path, monkeypatch):
        """A relative path from an unrelated CWD must still find the repo config."""
        monkeypatch.chdir(tmp_path)
        resolved = resolve_path(os.path.join("configs", "drift_thresholds.yaml"))
        assert os.path.isabs(resolved)
        assert os.path.exists(resolved)


class TestThresholdStore:
    """The mtime-cached view the detector uses."""

    def test_store_reads_calibrated_value(self, thresholds_file):
        _write_yaml(
            thresholds_file,
            {"thresholds": {"agent_a": {"kl_threshold": 0.37, "sample_size": 900}}},
        )

        store = ThresholdStore(thresholds_file)

        assert store.threshold("agent_a") == 0.37

    def test_store_defaults_unknown_agent(self, thresholds_file):
        _write_yaml(
            thresholds_file, {"thresholds": {"agent_a": {"kl_threshold": 0.37}}}
        )

        store = ThresholdStore(thresholds_file)

        assert store.threshold("agent_b") == DEFAULT_KL_THRESHOLD

    def test_store_picks_up_external_write(self, thresholds_file):
        """PC26 DoD 2: a calibration lands without restarting the detector."""
        _write_yaml(
            thresholds_file, {"thresholds": {"agent_a": {"kl_threshold": 0.1}}}
        )

        store = ThresholdStore(thresholds_file)
        assert store.threshold("agent_a") == 0.1

        # Simulate the monthly cron rewriting the file, including an mtime bump.
        _write_yaml(
            thresholds_file,
            {
                "thresholds": {
                    "agent_a": {
                        "kl_threshold": 0.55,
                        "calibrated_at": "2026-10-01T05:23:00+00:00",
                        "sample_size": 2880,
                    }
                }
            },
        )
        os.utime(thresholds_file, (time.time() + 10, time.time() + 10))

        assert store.threshold("agent_a") == 0.55

    def test_store_reload_forces_reread(self, thresholds_file):
        _write_yaml(
            thresholds_file, {"thresholds": {"agent_a": {"kl_threshold": 0.1}}}
        )

        store = ThresholdStore(thresholds_file)
        assert store.threshold("agent_a") == 0.1

        _write_yaml(
            thresholds_file, {"thresholds": {"agent_a": {"kl_threshold": 0.9}}}
        )

        # Same-second mtime means the cache would otherwise be considered fresh.
        store.reload()

        assert store.threshold("agent_a") == 0.9

    def test_store_entry_fills_defaults(self, thresholds_file):
        _write_yaml(thresholds_file, {"thresholds": {}})

        store = ThresholdStore(thresholds_file)
        entry = store.entry("missing")

        assert entry["kl_threshold"] == DEFAULT_KL_THRESHOLD
        assert entry["calibrated_at"] is None
        assert entry["sample_size"] == 0

    def test_store_missing_file_uses_default(self, thresholds_file):
        store = ThresholdStore(thresholds_file)
        assert store.threshold("agent_a") == DEFAULT_KL_THRESHOLD

    def test_store_len(self, thresholds_file):
        _write_yaml(
            thresholds_file,
            {"thresholds": {"a": {}, "b": {}, "c": {}}},
        )
        assert len(ThresholdStore(thresholds_file)) == 3


# ---------------------------------------------------------------------------
# Detector integration
# ---------------------------------------------------------------------------


class TestDetectorUsesCalibratedThreshold:
    """DriftDetector must read the calibrated value, not a hardcoded 0.1."""

    @pytest.fixture
    def mock_hotstore(self):
        hotstore = MagicMock()
        hotstore._execute_clickhouse = AsyncMock()
        return hotstore

    def _detector(self, hotstore, thresholds_path, kl_threshold=None):
        return DriftDetector(
            sdk=None,
            hotstore=hotstore,
            kl_threshold=kl_threshold,
            thresholds_path=thresholds_path,
        )

    def test_effective_threshold_from_file(self, thresholds_file, mock_hotstore):
        _write_yaml(
            thresholds_file, {"thresholds": {"agent_a": {"kl_threshold": 0.42}}}
        )

        detector = self._detector(mock_hotstore, thresholds_file)

        assert detector.effective_threshold("agent_a") == 0.42

    def test_effective_threshold_defaults_when_uncalibrated(
        self, thresholds_file, mock_hotstore
    ):
        _write_yaml(
            thresholds_file,
            {"thresholds": {"agent_a": {"kl_threshold": 0.42, "calibrated_at": None}}},
        )

        detector = self._detector(mock_hotstore, thresholds_file)

        # Seeded-but-never-calibrated agents fall back to the documented 0.1.
        assert detector.effective_threshold("agent_a") == 0.42
        assert detector.effective_threshold("brand_new_agent") == DEFAULT_KL_THRESHOLD

    def test_explicit_threshold_overrides_file(self, thresholds_file, mock_hotstore):
        _write_yaml(
            thresholds_file, {"thresholds": {"agent_a": {"kl_threshold": 0.42}}}
        )

        detector = self._detector(mock_hotstore, thresholds_file, kl_threshold=0.05)

        assert detector.effective_threshold("agent_a") == 0.05

    def test_per_agent_thresholds_are_independent(
        self, thresholds_file, mock_hotstore
    ):
        _write_yaml(
            thresholds_file,
            {
                "thresholds": {
                    "noisy_agent": {"kl_threshold": 0.8},
                    "stable_agent": {"kl_threshold": 0.02},
                }
            },
        )

        detector = self._detector(mock_hotstore, thresholds_file)

        assert detector.effective_threshold("noisy_agent") == 0.8
        assert detector.effective_threshold("stable_agent") == 0.02

    def test_reload_threshold_returns_new_value(self, thresholds_file, mock_hotstore):
        _write_yaml(
            thresholds_file, {"thresholds": {"agent_a": {"kl_threshold": 0.1}}}
        )

        detector = self._detector(mock_hotstore, thresholds_file)
        _write_yaml(
            thresholds_file, {"thresholds": {"agent_a": {"kl_threshold": 0.6}}}
        )

        assert detector.reload_threshold("agent_a") == 0.6

    @pytest.mark.asyncio
    async def test_run_once_reports_calibrated_threshold(
        self, thresholds_file, mock_hotstore
    ):
        """The threshold recorded on the report is the calibrated one."""
        _write_yaml(
            thresholds_file, {"thresholds": {"agent_a": {"kl_threshold": 0.9}}}
        )

        baseline = [[0.1, 0.2, 0.15, 0.3]] * 200
        current = [[0.1, 0.2, 0.15, 0.3]] * 150
        mock_hotstore._execute_clickhouse.side_effect = [
            [(e,) for e in baseline],
            [(e,) for e in current],
        ]

        detector = self._detector(mock_hotstore, thresholds_file)
        report = await detector.run_once("agent_a")

        assert report.threshold == 0.9
        assert report.is_drift_detected is False

    @pytest.mark.asyncio
    async def test_run_once_drift_uses_calibrated_threshold(
        self, thresholds_file, mock_hotstore
    ):
        """A raised threshold suppresses an alert the default 0.1 would have raised."""
        baseline = [[0.0, 0.1, 0.05]] * 200
        current = [[0.5, 0.6, 0.55]] * 150
        mock_hotstore._execute_clickhouse.side_effect = [
            [(e,) for e in baseline],
            [(e,) for e in current],
        ]

        strict = self._detector(mock_hotstore, thresholds_file, kl_threshold=0.1)
        strict_report = await strict.run_once("agent_a")
        assert strict_report.is_drift_detected is True

        mock_hotstore._execute_clickhouse.side_effect = [
            [(e,) for e in baseline],
            [(e,) for e in current],
        ]
        lenient_path = thresholds_file + ".lenient"
        _write_yaml(
            lenient_path, {"thresholds": {"agent_a": {"kl_threshold": 100.0}}}
        )
        lenient = self._detector(mock_hotstore, lenient_path)
        lenient_report = await lenient.run_once("agent_a")

        assert lenient_report.threshold == 100.0
        assert lenient_report.is_drift_detected is False

    def test_severity_with_zero_threshold_does_not_divide_by_zero(
        self, thresholds_file, mock_hotstore
    ):
        """A calibrated p99 can round to 0; severity must not raise."""
        detector = self._detector(mock_hotstore, thresholds_file, kl_threshold=0.0)

        assert detector._compute_severity(0.0, 0.0) == "info"
        assert detector._compute_severity(0.01, 0.0) == "critical"


# ---------------------------------------------------------------------------
# Calibration job
# ---------------------------------------------------------------------------


class TestComputeP99:
    """The percentile statistic itself."""

    def test_empty_returns_zero(self):
        assert calibration.compute_p99([]) == 0.0

    def test_matches_numpy_percentile(self):
        scores = [float(v) for v in np.random.default_rng(7).normal(0.1, 0.05, 500)]
        assert calibration.compute_p99(scores, 99) == pytest.approx(
            float(np.percentile(scores, 99))
        )

    def test_ignores_occasional_spike(self):
        """p99 must not be dragged up by a single outlier window."""
        steady = [0.10 + (i % 5) * 0.001 for i in range(500)]
        with_spike = steady + [5.0]

        assert calibration.compute_p99(steady, 99) == pytest.approx(0.104, abs=1e-3)
        assert calibration.compute_p99(with_spike, 99) == pytest.approx(0.104, abs=1e-3)

    def test_single_sample(self):
        assert calibration.compute_p99([0.42], 99) == pytest.approx(0.42)


class TestCalibrateAgent:
    """Per-agent calibration against mocked ClickHouse history."""

    @pytest.mark.asyncio
    async def test_calibrates_from_full_30_day_history(self):
        scores = [0.10 + (i % 10) * 0.005 for i in range(2880)]
        hotstore = _make_hotstore({"agent_a": _history_rows(scores, days=30)})

        result = await calibration.calibrate_agent(hotstore, "agent_a", current={})

        assert result.calibrated is True
        assert result.outcome == calibration.OUTCOME_CALIBRATED
        assert result.threshold == pytest.approx(
            float(np.percentile(scores, 99)), abs=1e-6
        )
        assert result.sample_size == 2880
        assert result.history_days == pytest.approx(30.0, abs=0.5)
        assert result.calibrated_at is not None

    @pytest.mark.asyncio
    async def test_raises_threshold_over_previous(self):
        scores = [0.30] * 500
        hotstore = _make_hotstore({"agent_a": _history_rows(scores, days=30)})

        result = await calibration.calibrate_agent(
            hotstore,
            "agent_a",
            current={"agent_a": {"kl_threshold": 0.1, "sample_size": 100}},
        )

        assert result.previous_threshold == 0.1
        assert result.threshold == pytest.approx(0.30)
        assert result.threshold > result.previous_threshold

    @pytest.mark.asyncio
    async def test_lowers_threshold_over_previous(self):
        """Calibration is not one-way: a settled agent's threshold comes down."""
        scores = [0.02] * 500
        hotstore = _make_hotstore({"agent_a": _history_rows(scores, days=30)})

        result = await calibration.calibrate_agent(
            hotstore,
            "agent_a",
            current={"agent_a": {"kl_threshold": 2.5, "sample_size": 100}},
        )

        assert result.previous_threshold == 2.5
        assert result.threshold == pytest.approx(0.02)

    @pytest.mark.asyncio
    async def test_falls_back_to_default_when_no_history(self):
        """PC26 DoD 4: under 30 days of history, keep 0.1."""
        hotstore = _make_hotstore({})

        result = await calibration.calibrate_agent(
            hotstore,
            "agent_a",
            current={"agent_a": {"kl_threshold": 0.1, "sample_size": 0}},
        )

        assert result.calibrated is False
        assert result.outcome == calibration.OUTCOME_NO_DATA
        assert result.threshold == DEFAULT_KL_THRESHOLD

    @pytest.mark.asyncio
    async def test_refuses_partial_window(self):
        """5 days of data is not a 30-day p99 — refuse rather than mislead."""
        scores = [0.20] * 500
        hotstore = _make_hotstore({"agent_a": _history_rows(scores, days=5)})

        result = await calibration.calibrate_agent(
            hotstore,
            "agent_a",
            current={"agent_a": {"kl_threshold": 0.1}},
        )

        assert result.calibrated is False
        assert result.outcome == calibration.OUTCOME_INSUFFICIENT_HISTORY
        assert result.threshold == DEFAULT_KL_THRESHOLD
        assert "5.0d" in result.detail

    @pytest.mark.asyncio
    async def test_refuses_too_few_samples(self):
        scores = [0.20] * 42
        hotstore = _make_hotstore({"agent_a": _history_rows(scores, days=30)})

        result = await calibration.calibrate_agent(
            hotstore, "agent_a", current={}
        )

        assert result.calibrated is False
        assert result.outcome == calibration.OUTCOME_INSUFFICIENT_HISTORY
        assert result.threshold == DEFAULT_KL_THRESHOLD
        assert "42" in result.detail

    @pytest.mark.asyncio
    async def test_refuses_at_exactly_min_samples_minus_one(self):
        hotstore = _make_hotstore(
            {"agent_a": _history_rows([0.2] * (MIN_SAMPLES_FOR_CALIBRATION - 1), days=30)}
        )

        result = await calibration.calibrate_agent(hotstore, "agent_a", current={})

        assert result.calibrated is False

    @pytest.mark.asyncio
    async def test_calibrates_at_exactly_min_samples(self):
        hotstore = _make_hotstore(
            {"agent_a": _history_rows([0.2] * MIN_SAMPLES_FOR_CALIBRATION, days=30)}
        )

        result = await calibration.calibrate_agent(hotstore, "agent_a", current={})

        assert result.calibrated is True

    @pytest.mark.asyncio
    async def test_partial_window_allowed_when_explicitly_opted_in(
        self,
    ):
        scores = [0.20] * 500
        hotstore = _make_hotstore({"agent_a": _history_rows(scores, days=5)})

        result = await calibration.calibrate_agent(
            hotstore, "agent_a", current={}, require_full_window=False
        )

        assert result.calibrated is True
        assert result.threshold == pytest.approx(0.20)

    @pytest.mark.asyncio
    async def test_query_failure_is_contained(self):
        hotstore = MagicMock()
        hotstore._execute_clickhouse = AsyncMock(side_effect=RuntimeError("clickhouse down"))

        result = await calibration.calibrate_agent(hotstore, "agent_a", current={})

        assert result.calibrated is False
        assert result.outcome == calibration.OUTCOME_ERROR
        assert result.threshold == DEFAULT_KL_THRESHOLD

    @pytest.mark.asyncio
    async def test_is_idempotent(self):
        """Re-running on unchanged history must yield the same threshold."""
        scores = [0.10 + (i % 7) * 0.004 for i in range(1000)]
        hotstore = _make_hotstore({"agent_a": _history_rows(scores, days=30)})

        first = await calibration.calibrate_agent(hotstore, "agent_a", current={})
        second = await calibration.calibrate_agent(
            hotstore,
            "agent_a",
            current={"agent_a": {"kl_threshold": first.threshold}},
        )

        assert first.threshold == pytest.approx(second.threshold)

    @pytest.mark.asyncio
    async def test_custom_percentile(self):
        scores = [float(v) for v in np.linspace(0.01, 1.0, 500)]
        hotstore = _make_hotstore({"agent_a": _history_rows(scores, days=30)})

        result = await calibration.calibrate_agent(
            hotstore, "agent_a", current={}, percentile=95
        )

        assert result.threshold == pytest.approx(
            float(np.percentile(scores, 95)), abs=1e-6
        )


class TestSanityCheck:
    """Quarterly 'has the baseline itself drifted' check."""

    @pytest.mark.asyncio
    async def test_healthy_baseline(self):
        hotstore = _make_hotstore(good_runs={"agent_a": [0.05] * 100})

        result = await calibration.sanity_check_baseline(
            hotstore, "agent_a", threshold=0.2
        )

        assert result.checked == 100
        assert result.exceeding == 0
        assert result.baseline_stale is False

    @pytest.mark.asyncio
    async def test_stale_baseline_detected(self):
        """If most known-good runs now exceed the threshold, the baseline is stale."""
        hotstore = _make_hotstore(good_runs={"agent_a": [0.9] * 100})

        result = await calibration.sanity_check_baseline(
            hotstore, "agent_a", threshold=0.2
        )

        assert result.exceeding == 100
        assert result.baseline_stale is True
        assert result.exceed_ratio == pytest.approx(1.0)

    @pytest.mark.asyncio
    async def test_stale_within_tolerance_is_ok(self):
        """A couple of noisy good runs must not trip the check."""
        scores = [0.05] * 95 + [0.9] * 5
        hotstore = _make_hotstore(good_runs={"agent_a": scores})

        result = await calibration.sanity_check_baseline(
            hotstore, "agent_a", threshold=0.2
        )

        assert result.exceeding == 5
        assert result.baseline_stale is False

    @pytest.mark.asyncio
    async def test_no_good_runs_is_not_stale(self):
        hotstore = _make_hotstore(good_runs={})

        result = await calibration.sanity_check_baseline(
            hotstore, "agent_a", threshold=0.2
        )

        assert result.checked == 0
        assert result.baseline_stale is False

    @pytest.mark.asyncio
    async def test_query_failure_is_contained(self):
        hotstore = MagicMock()
        hotstore._execute_clickhouse = AsyncMock(side_effect=RuntimeError("boom"))

        result = await calibration.sanity_check_baseline(
            hotstore, "agent_a", threshold=0.2
        )

        assert result.baseline_stale is False
        assert "boom" in result.detail


class TestCalibrateAll:
    """End-to-end job behaviour, including the persisted file."""

    @pytest.mark.asyncio
    async def test_writes_calibrated_values(self, thresholds_file):
        _write_yaml(
            thresholds_file, {"thresholds": {"agent_a": {"kl_threshold": 0.1}}}
        )
        hotstore = _make_hotstore(
            {
                "agent_a": _history_rows([0.25] * 500, days=30),
                "agent_b": _history_rows([0.60] * 500, days=30),
            }
        )

        summary = await calibration.calibrate_all(
            hotstore, ["agent_a", "agent_b"], thresholds_path=thresholds_file
        )

        assert summary["calibrated_count"] == 2
        assert summary["written"] is True

        saved = load_thresholds(thresholds_file)
        assert saved["agent_a"]["kl_threshold"] == pytest.approx(0.25)
        assert saved["agent_b"]["kl_threshold"] == pytest.approx(0.60)
        assert saved["agent_a"]["calibrated_at"] is not None
        assert saved["agent_a"]["sample_size"] == 500

    @pytest.mark.asyncio
    async def test_dry_run_does_not_write(self, thresholds_file):
        _write_yaml(
            thresholds_file, {"thresholds": {"agent_a": {"kl_threshold": 0.1}}}
        )
        hotstore = _make_hotstore(
            {"agent_a": _history_rows([0.25] * 500, days=30)}
        )

        summary = await calibration.calibrate_all(
            hotstore, ["agent_a"], thresholds_path=thresholds_file, dry_run=True
        )

        assert summary["written"] is False
        assert summary["thresholds"]["agent_a"] == pytest.approx(0.25)
        # File on disk is untouched.
        assert load_thresholds(thresholds_file)["agent_a"]["kl_threshold"] == 0.1

    @pytest.mark.asyncio
    async def test_uncalibrated_agent_keeps_default_and_records_reason(
        self, thresholds_file
    ):
        _write_yaml(
            thresholds_file, {"thresholds": {"agent_a": {"kl_threshold": 0.1}}}
        )
        hotstore = _make_hotstore({})  # no history at all

        summary = await calibration.calibrate_all(
            hotstore, ["agent_a"], thresholds_path=thresholds_file
        )

        assert summary["calibrated_count"] == 0

        saved = load_thresholds(thresholds_file)
        assert saved["agent_a"]["kl_threshold"] == DEFAULT_KL_THRESHOLD
        assert saved["agent_a"]["calibrated_at"] is None
        assert "calibration_skipped_reason" in saved["agent_a"]

    @pytest.mark.asyncio
    async def test_preserves_untouched_agents(self, thresholds_file):
        _write_yaml(
            thresholds_file,
            {
                "thresholds": {
                    "agent_a": {"kl_threshold": 0.33, "calibrated_at": "2026-08-01T00:00:00+00:00"},
                    "agent_b": {"kl_threshold": 0.44, "calibrated_at": "2026-08-01T00:00:00+00:00"},
                }
            },
        )
        hotstore = _make_hotstore(
            {"agent_a": _history_rows([0.25] * 500, days=30)}
        )

        await calibration.calibrate_all(
            hotstore, ["agent_a"], thresholds_path=thresholds_file
        )

        saved = load_thresholds(thresholds_file)
        assert saved["agent_a"]["kl_threshold"] == pytest.approx(0.25)
        assert saved["agent_b"]["kl_threshold"] == 0.44

    @pytest.mark.asyncio
    async def test_discovers_agents_when_none_specified(self, thresholds_file):
        hotstore = _make_hotstore(
            {"agent_x": _history_rows([0.25] * 500, days=30)},
            agents=["agent_x"],
        )

        summary = await calibration.calibrate_all(
            hotstore, None, thresholds_path=thresholds_file
        )

        assert summary["calibrated_count"] == 1
        assert "agent_x" in summary["thresholds"]

    @pytest.mark.asyncio
    async def test_stale_baseline_reported(self, thresholds_file):
        _write_yaml(thresholds_file, {"thresholds": {}})
        hotstore = _make_hotstore(
            {"agent_a": _history_rows([0.25] * 500, days=30)},
            good_runs={"agent_a": [2.0] * 100},
        )

        summary = await calibration.calibrate_all(
            hotstore, ["agent_a"], thresholds_path=thresholds_file
        )

        assert summary["stale_count"] == 1
        assert summary["sanity_checks"][0]["baseline_stale"] is True

    @pytest.mark.asyncio
    async def test_sanity_check_can_be_skipped(self, thresholds_file):
        _write_yaml(thresholds_file, {"thresholds": {}})
        hotstore = _make_hotstore(
            {"agent_a": _history_rows([0.25] * 500, days=30)},
            good_runs={"agent_a": [2.0] * 100},
        )

        summary = await calibration.calibrate_all(
            hotstore, ["agent_a"], thresholds_path=thresholds_file, run_sanity_check=False
        )

        assert summary["sanity_checks"] == []
        assert summary["stale_count"] == 0

    @pytest.mark.asyncio
    async def test_one_failing_agent_does_not_block_others(self, thresholds_file):
        """A single ClickHouse error must not cost us the other agents' calibration."""
        _write_yaml(thresholds_file, {"thresholds": {}})
        scores = _history_rows([0.25] * 500, days=30)

        async def _execute(query, params=None):
            if params and params.get("agent_id") == "broken":
                raise RuntimeError("agent partition missing")
            if "DISTINCT agent_id" in query:
                return []
            return [(s, ts) for s, ts in scores]

        hotstore = MagicMock()
        hotstore._execute_clickhouse = AsyncMock(side_effect=_execute)

        summary = await calibration.calibrate_all(
            hotstore, ["broken", "agent_a"], thresholds_path=thresholds_file
        )

        outcomes = {e["agent_id"]: e["outcome"] for e in summary["agents"]}
        assert outcomes["broken"] == calibration.OUTCOME_ERROR
        assert outcomes["agent_a"] == calibration.OUTCOME_CALIBRATED


class TestCalibrationCli:
    """Argument parsing for the cron entry point."""

    def test_defaults(self):
        args = calibration.parse_args([])

        assert args.window_days == 30
        assert args.percentile == 99.0
        assert args.min_samples == MIN_SAMPLES_FOR_CALIBRATION
        assert args.dry_run is False
        assert args.sanity_check is True

    def test_agents_parsed(self):
        args = calibration.parse_args(["--agents", "a, b ,c"])
        assert args.agents == "a, b ,c"

    def test_dry_run_flag(self):
        assert calibration.parse_args(["--dry-run"]).dry_run is True

    def test_no_sanity_check_flag(self):
        assert calibration.parse_args(["--no-sanity-check"]).sanity_check is False

    def test_allow_partial_window_flag(self):
        assert calibration.parse_args(["--allow-partial-window"]).allow_partial_window is True


class TestRepoConfigFile:
    """The checked-in configs/drift_thresholds.yaml must stay loadable."""

    def test_repo_config_parses(self):
        thresholds = load_thresholds(
            os.path.join(REPO_ROOT, "configs", "drift_thresholds.yaml")
        )

        assert thresholds
        for agent_id, entry in thresholds.items():
            assert entry["kl_threshold"] > 0, agent_id

    def test_repo_config_matches_shipped_defaults(self):
        thresholds = load_thresholds(
            os.path.join(REPO_ROOT, "configs", "drift_thresholds.yaml")
        )

        for entry in thresholds.values():
            assert entry["kl_threshold"] == DEFAULT_KL_THRESHOLD
            assert entry["calibrated_at"] is None
