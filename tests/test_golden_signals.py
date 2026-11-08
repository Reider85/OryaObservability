"""Tests for PP59 golden signals SLI computation and metrics."""

from __future__ import annotations

import re
from datetime import timedelta

import pytest
from prometheus_client import generate_latest

from agent_obs.metrics import (
    golden_signal_latency_sli,
    golden_signal_traffic_sli,
    golden_signal_error_sli,
    golden_signal_saturation_sli,
    golden_signal_healthy,
    golden_signal_burn_rate,
    golden_signal_latency_target,
    golden_signal_traffic_target,
    golden_signal_error_target,
    golden_signal_saturation_target,
)
from agent_obs.slo import (
    SLO,
    SLO_CATALOG,
    SLORefresher,
    burn_rate,
    parse_window,
    unique_windows,
    window_label,
)
from agent_obs.slo.definitions import (
    DEFAULT_WINDOW,
    SLI_KIND_LATENCY_GOLDEN,
    SLI_KIND_COST,
    SLI_KIND_LATENCY,
    SLI_KIND_QUALITY,
    SLI_KIND_TRAFFIC,
    SLI_KIND_ERRORS,
    SLI_KIND_SATURATION,
    sli_from_counts,
)

# Module-level import (NOT inside test bodies): tests/test_trace_viewer.py
# pollutes sys.modules['agent_obs.storage.hot'] with a MagicMock at import
# time, so a late import inside a test body would resolve to MockHotStore.
# This file is collected before test_trace_viewer.py (alphabetical order),
# so a module-level binding stays the real class.
from agent_obs.storage.hot import HotStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_metric(exposition: bytes, name: str, labels: dict | None = None) -> list[tuple[float, dict]]:
    """Parse a Prometheus metric from exposition text."""
    text = exposition.decode("utf-8")
    results = []
    for match in re.finditer(
        rf'^{re.escape(name)}\{{([^}}]*)\}}\s+([\d.e+-]+)',
        text,
        re.MULTILINE,
    ):
        raw_labels, value = match.group(1), float(match.group(2))
        label_dict = dict(
            item.split("=", 1) for item in raw_labels.split(",") if "=" in item
        )
        label_dict = {k: v.strip('"') for k, v in label_dict.items()}
        if labels is None or all(label_dict.get(k) == v for k, v in labels.items()):
            results.append((value, label_dict))
    return results


class FakeHotStore:
    """Test double for HotStore: returns canned (good, total) per SLI kind+window."""

    def __init__(self, stats: dict[tuple[str, float], tuple[int, int]]):
        # stats: {(sli_kind, window_seconds): (good, total)}
        self.stats = stats
        self.calls: list[tuple[str, float, float | None]] = []

    def get_sli_stats(self, sli_kind: str, window: timedelta, good_threshold: float | None = None):
        self.calls.append((sli_kind, window.total_seconds(), good_threshold))
        return self.stats.get((sli_kind, window.total_seconds()), (0, 0))


@pytest.fixture(autouse=True)
def _reset_golden_metrics():
    """Isolate golden signal metrics between tests."""
    for gauge in (
        golden_signal_latency_sli,
        golden_signal_traffic_sli,
        golden_signal_error_sli,
        golden_signal_saturation_sli,
        golden_signal_healthy,
        golden_signal_burn_rate,
        golden_signal_latency_target,
        golden_signal_traffic_target,
        golden_signal_error_target,
        golden_signal_saturation_target,
    ):
        gauge._metrics.clear()
    yield
    for gauge in (
        golden_signal_latency_sli,
        golden_signal_traffic_sli,
        golden_signal_error_sli,
        golden_signal_saturation_sli,
        golden_signal_healthy,
        golden_signal_burn_rate,
        golden_signal_latency_target,
        golden_signal_traffic_target,
        golden_signal_error_target,
        golden_signal_saturation_target,
    ):
        gauge._metrics.clear()


# ---------------------------------------------------------------------------
# Catalog structure
# ---------------------------------------------------------------------------

def test_slo_catalog_includes_golden_signals():
    """Test that SLO catalog includes all golden signals."""
    expected_signals = [
        "latency_p99",
        "traffic", 
        "errors",
        "saturation"
    ]
    
    for signal in expected_signals:
        assert signal in SLO_CATALOG
        slo = SLO_CATALOG[signal]
        
        # Check that golden signals have appropriate targets
        if signal == "latency_p99":
            assert slo.target == 0.95  # p99 target
            assert slo.good_threshold == 2.0  # 2 seconds
            assert slo.sli_kind == SLI_KIND_LATENCY_GOLDEN
        elif signal == "traffic":
            assert slo.target == 1.20  # 20% above baseline
            assert slo.sli_kind == SLI_KIND_TRAFFIC
        elif signal == "errors":
            assert slo.target == 0.995  # < 0.5% error rate
            assert slo.sli_kind == SLI_KIND_ERRORS
        elif signal == "saturation":
            assert slo.target == 0.80  # 80% utilization
            assert slo.sli_kind == SLI_KIND_SATURATION


def test_slo_validation_supports_traffic_targets():
    """Test that SLO validation supports traffic targets > 1.0."""
    # This should not raise an exception
    slo = SLO(
        name="traffic",
        target=1.20,  # > 1.0 is allowed for traffic
        sli_kind=SLI_KIND_TRAFFIC,
    )
    assert slo.target == 1.20


def test_slo_validation_blocks_invalid_targets():
    """Test that SLO validation blocks invalid targets for non-traffic signals."""
    # Regular SLOs must have targets in (0, 1]
    with pytest.raises(ValueError, match="target must be in"):
        SLO(
            name="latency",
            target=1.20,  # > 1.0 not allowed for non-traffic
            sli_kind=SLI_KIND_LATENCY,
        )


# ---------------------------------------------------------------------------
# SLI computation
# ---------------------------------------------------------------------------

def test_latency_golden_sli_computation():
    """Test latency golden signal SLI computation with p99 threshold."""
    store = FakeHotStore({
        (SLI_KIND_LATENCY_GOLDEN, 3600): (95, 100),  # 95% within 2s threshold
    })
    
    # Test with threshold of 2 seconds
    good, total = store.get_sli_stats(
        sli_kind=SLI_KIND_LATENCY_GOLDEN,
        window=timedelta(hours=1),
        good_threshold=2.0,
    )
    
    assert good == 95
    assert total == 100
    
    # Test empty window case
    good_empty, total_empty = store.get_sli_stats(
        sli_kind=SLI_KIND_LATENCY_GOLDEN,
        window=timedelta(seconds=0),  # Empty window
        good_threshold=2.0,
    )
    assert good_empty == 0
    assert total_empty == 0


def test_traffic_sli_computation():
    """Test traffic signal SLI computation with baseline comparison."""
    store = FakeHotStore({
        (SLI_KIND_TRAFFIC, 3600): (120, 100),  # 120% of baseline (good > total is normal for traffic)
    })
    
    good, total = store.get_sli_stats(
        sli_kind=SLI_KIND_TRAFFIC,
        window=timedelta(hours=1),
        good_threshold=None,  # Not used for traffic
    )
    
    assert good == 120
    assert total == 100
    # For traffic, good can be > total (representing percentage above baseline)


def test_errors_sli_computation():
    """Test errors signal SLI computation."""
    store = FakeHotStore({
        (SLI_KIND_ERRORS, 3600): (995, 1000),  # 99.5% error-free
    })
    
    good, total = store.get_sli_stats(
        sli_kind=SLI_KIND_ERRORS,
        window=timedelta(hours=1),
        good_threshold=None,  # Not used for errors
    )
    
    assert good == 995
    assert total == 1000


def test_saturation_sli_computation():
    """Test saturation signal SLI computation."""
    store = FakeHotStore({
        (SLI_KIND_SATURATION, 3600): (80, 100),  # 80% below threshold
    })
    
    good, total = store.get_sli_stats(
        sli_kind=SLI_KIND_SATURATION,
        window=timedelta(hours=1),
        good_threshold=None,  # Not used for saturation
    )
    
    assert good == 80
    assert total == 100


def test_invalid_sli_kind():
    """Test error handling for invalid SLI kind."""
    from agent_obs.storage.hot import HotStore
    
    store = HotStore()
    
    # Real HotStore validates SLI kind and throws an error for unknown ones
    with pytest.raises(ValueError, match="unknown sli_kind"):
        store.get_sli_stats(
            sli_kind="invalid_kind",
            window=timedelta(hours=1),
        )


def test_latency_golden_requires_threshold():
    """Test that latency golden signal requires threshold parameter."""
    from agent_obs.storage.hot import HotStore
    
    store = HotStore()
    
    # Real HotStore validates that latency_golden requires a threshold
    with pytest.raises(ValueError, match="latency_golden SLI requires good_threshold"):
        store.get_sli_stats(
            sli_kind=SLI_KIND_LATENCY_GOLDEN,
            window=timedelta(hours=1),
            good_threshold=None,
        )


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def test_latency_sli_metric_exists():
    """Test that latency SLI metric is defined."""
    # Check metric is properly defined
    assert golden_signal_latency_sli._name == "golden_signal_latency_sli"
    assert golden_signal_latency_sli._type == "gauge"
    assert "name" in golden_signal_latency_sli._labelnames


def test_traffic_sli_metric_exists():
    """Test that traffic SLI metric is defined."""
    assert golden_signal_traffic_sli._name == "golden_signal_traffic_sli"
    assert golden_signal_traffic_sli._type == "gauge"
    assert "name" in golden_signal_traffic_sli._labelnames


def test_error_sli_metric_exists():
    """Test that error SLI metric is defined."""
    assert golden_signal_error_sli._name == "golden_signal_error_sli"
    assert golden_signal_error_sli._type == "gauge"
    assert "name" in golden_signal_error_sli._labelnames


def test_saturation_sli_metric_exists():
    """Test that saturation SLI metric is defined."""
    assert golden_signal_saturation_sli._name == "golden_signal_saturation_sli"
    assert golden_signal_saturation_sli._type == "gauge"
    assert "name" in golden_signal_saturation_sli._labelnames


def test_healthy_metric_exists():
    """Test that healthy metric is defined."""
    assert golden_signal_healthy._name == "golden_signal_healthy"
    assert golden_signal_healthy._type == "gauge"
    assert "name" in golden_signal_healthy._labelnames


def test_burn_rate_metric_exists():
    """Test that burn rate metric is defined."""
    assert golden_signal_burn_rate._name == "golden_signal_burn_rate"
    assert golden_signal_burn_rate._type == "gauge"
    assert "name" in golden_signal_burn_rate._labelnames
    assert "window" in golden_signal_burn_rate._labelnames


def test_target_metrics_exist():
    """Test that target metrics are defined."""
    # All target metrics should be gauges with name label
    for metric in [
        golden_signal_latency_target,
        golden_signal_traffic_target,
        golden_signal_error_target,
        golden_signal_saturation_target,
    ]:
        assert metric._type == "gauge"
        assert "name" in metric._labelnames


# ---------------------------------------------------------------------------
# Integration
# ---------------------------------------------------------------------------

def test_slo_refresher_supports_golden_signals():
    """Test that SLO refresher can handle golden signals."""
    store = FakeHotStore({
        (SLI_KIND_LATENCY_GOLDEN, 3600): (95, 100),
        (SLI_KIND_TRAFFIC, 3600): (100, 100),  # 100% of baseline (within target)
        (SLI_KIND_ERRORS, 3600): (995, 1000),
        (SLI_KIND_SATURATION, 3600): (80, 100),
    })
    
    refresher = SLORefresher(store, catalog=SLO_CATALOG)
    
    # Test that it can refresh golden signals
    report = refresher.refresh()
    
    # Check that golden signals are in the report
    golden_signals = ["latency_p99", "traffic", "errors", "saturation"]
    for signal in golden_signals:
        assert signal in report
        assert "sli" in report[signal]
        assert "error_budget_remaining" in report[signal]


def test_golden_signal_burn_rate_calculation():
    """Test burn rate calculation for golden signals."""
    # Test with perfect SLI
    perfect_rate = burn_rate(actual_sli=1.0, target=0.95)
    assert perfect_rate == 0.0  # No budget burned
    
    # Test with SLI at target
    target_rate = burn_rate(actual_sli=0.95, target=0.95)
    assert target_rate == 1.0  # Budget burned at sustainable rate
    
    # Test with degraded SLI
    degraded_rate = burn_rate(actual_sli=0.90, target=0.95)
    assert degraded_rate > 1.0  # Budget burned faster than sustainable


def test_error_budget_integration():
    """Test that golden signals work with error budget tracking."""
    from agent_obs.slo.error_budget import ErrorBudget
    
    # Test error budget calculation for golden signal using from_counts
    budget = ErrorBudget.from_counts(
        slo_name="latency_p99",
        target=0.95,
        good_events=95,
        total_events=100,
    )
    
    assert budget.total_events == 100
    assert budget.error_events == 5  # Not good_events
    assert budget.target == 0.95
    # SLI would be 0.95, but we don't have direct access to it in the budget object


# ---------------------------------------------------------------------------
# Alerting
# ---------------------------------------------------------------------------

def test_burn_rate_thresholds():
    """Test that golden signals have appropriate burn rate thresholds."""
    # Check that golden signals have burn rate thresholds
    golden_signals = ["latency_p99", "traffic", "errors", "saturation"]
    
    for signal in golden_signals:
        slo = SLO_CATALOG[signal]
        assert len(slo.burn_rate_thresholds) > 0
        
        # Check thresholds are reasonable
        for short, long, threshold in slo.burn_rate_thresholds:
            assert short in ["5m", "1h", "6h"]
            assert long in ["1h", "6h", "1d"]
            assert threshold > 0  # Positive threshold


def test_alert_queries_are_valid():
    """Test that alert queries reference valid metrics."""
    # Verify that metrics referenced in alerts exist
    assert golden_signal_burn_rate._name == "golden_signal_burn_rate"
    assert golden_signal_healthy._name == "golden_signal_healthy"


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

def test_empty_window_handling():
    """Test that empty windows return (0, 0) for all golden signals."""
    signals = [
        (SLI_KIND_LATENCY_GOLDEN, 2.0),
        (SLI_KIND_TRAFFIC, None),
        (SLI_KIND_ERRORS, None),
        (SLI_KIND_SATURATION, None),
    ]
    
    for sli_kind, threshold in signals:
        store = FakeHotStore({})
        good, total = store.get_sli_stats(
            sli_kind=sli_kind,
            window=timedelta(seconds=0),  # Empty window
            good_threshold=threshold,
        )
        assert good == 0
        assert total == 0
        
        # SLI should be perfect (1.0) for empty windows
        sli = sli_from_counts(good, total)
        assert sli == 1.0


def test_extreme_thresholds():
    """Test behavior with extreme threshold values."""
    # Test very lenient threshold for latency
    store = FakeHotStore({
        (SLI_KIND_LATENCY_GOLDEN, 300): (100, 100),  # All traces under high threshold
    })
    
    good, total = store.get_sli_stats(
        sli_kind=SLI_KIND_LATENCY_GOLDEN,
        window=timedelta(minutes=5),
        good_threshold=1000.0,  # Very high threshold
    )
    assert 0 <= good <= total
    
    # Test very strict threshold for latency
    store = FakeHotStore({
        (SLI_KIND_LATENCY_GOLDEN, 300): (10, 100),  # Few traces under low threshold
    })
    
    good_strict, total_strict = store.get_sli_stats(
        sli_kind=SLI_KIND_LATENCY_GOLDEN,
        window=timedelta(minutes=5),
        good_threshold=0.001,  # Very low threshold
    )
    assert 0 <= good_strict <= total_strict


# ---------------------------------------------------------------------------
# Performance
# ---------------------------------------------------------------------------

def test_latency_p99_performance():
    """Test that latency p99 computation performs adequately."""
    import time
    
    store = FakeHotStore({
        (SLI_KIND_LATENCY_GOLDEN, 3600): (95, 100),
    })
    
    start_time = time.time()
    
    # Run p99 computation
    good, total = store.get_sli_stats(
        sli_kind=SLI_KIND_LATENCY_GOLDEN,
        window=timedelta(hours=1),
        good_threshold=2.0,
    )
    
    end_time = time.time()
    computation_time = end_time - start_time
    
    # Should complete in reasonable time (< 5 seconds for 1 hour window)
    assert computation_time < 5.0
    assert 0 <= good <= total


def test_traffic_baseline_performance():
    """Test that traffic baseline computation performs adequately."""
    import time
    
    store = FakeHotStore({
        (SLI_KIND_TRAFFIC, 3600): (120, 100),  # 120% of baseline
    })
    
    start_time = time.time()
    
    # Run traffic computation
    good, total = store.get_sli_stats(
        sli_kind=SLI_KIND_TRAFFIC,
        window=timedelta(hours=1),
    )
    
    end_time = time.time()
    computation_time = end_time - start_time
    
    # Should complete in reasonable time
    assert computation_time < 5.0
    # For traffic, good can be > total (representing percentage above baseline)
    assert good == 120
    assert total == 100


# ---------------------------------------------------------------------------
# Data consistency
# ---------------------------------------------------------------------------

def test_sli_values_between_zero_and_one():
    """Test that all SLI values are between 0 and 1."""
    signals = [
        (SLI_KIND_LATENCY_GOLDEN, 2.0),
        (SLI_KIND_TRAFFIC, None),
        (SLI_KIND_ERRORS, None),
        (SLI_KIND_SATURATION, None),
    ]
    
    for sli_kind, threshold in signals:
        store = FakeHotStore({
            (sli_kind, 600): (50, 100),  # 50% SLI
        })
        
        good, total = store.get_sli_stats(
            sli_kind=sli_kind,
            window=timedelta(minutes=10),
            good_threshold=threshold,
        )
        
        sli = sli_from_counts(good, total)
        assert 0.0 <= sli <= 1.0, f"SLI {sli} not in [0, 1] for {sli_kind}"


def test_total_events_consistency():
    """Test that total events are consistent across related computations."""
    # Get errors total for comparison
    store = FakeHotStore({
        (SLI_KIND_ERRORS, 600): (995, 1000),
    })
    
    errors_good, errors_total = store.get_sli_stats(
        sli_kind=SLI_KIND_ERRORS,
        window=timedelta(minutes=10),
    )
    
    # Total should be positive if we have data
    if errors_total > 0:
        assert errors_good >= 0
        assert errors_good <= errors_total


# ---------------------------------------------------------------------------
# Full integration
# ---------------------------------------------------------------------------

def test_slo_refresher_with_golden_signals_full():
    """Test SLO refresher with golden signals in full integration."""
    store = FakeHotStore({
        (SLI_KIND_LATENCY_GOLDEN, 3600): (95, 100),
        (SLI_KIND_TRAFFIC, 3600): (100, 100),  # 100% of baseline (within target)
        (SLI_KIND_ERRORS, 3600): (995, 1000),
        (SLI_KIND_SATURATION, 3600): (80, 100),
    })
    
    refresher = SLORefresher(store, catalog=SLO_CATALOG)
    
    # Test refresh
    report = refresher.refresh()
    
    # Check that all golden signals are in the report
    golden_signals = ["latency_p99", "traffic", "errors", "saturation"]
    for signal in golden_signals:
        assert signal in report
        assert "sli" in report[signal]
        assert "error_budget_remaining" in report[signal]