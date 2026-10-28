"""Tests for PP25 (T3.6.1) — SLO catalog, SLI definitions, burn rate, gauges.

Validates:
- SLO_CATALOG contains exactly the 4 SLOs from the PP25 spec
- burn_rate formula and window parsing helpers
- SLORefresher computes SLIs correctly from (good, total) pairs and exports
  the 4 per-SLO Prometheus gauges
- ClickHouse failures never raise out of refresh() (observability must not
  block business logic)
"""

from __future__ import annotations

import re
from datetime import timedelta

import pytest
from prometheus_client import generate_latest

from agent_obs.metrics import (
    slo_actual_sli,
    slo_burn_rate,
    slo_error_budget_remaining,
    slo_target,
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
    SLI_KIND_AVAILABILITY,
    SLI_KIND_COST,
    SLI_KIND_LATENCY,
    SLI_KIND_QUALITY,
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
    """Parse a Prometheus metric from exposition text (same as test_cost_metrics)."""
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
def _reset_slo_metrics():
    """Isolate SLO gauges between tests."""
    for gauge in (slo_target, slo_actual_sli, slo_burn_rate, slo_error_budget_remaining):
        gauge._metrics.clear()
    yield
    for gauge in (slo_target, slo_actual_sli, slo_burn_rate, slo_error_budget_remaining):
        gauge._metrics.clear()


# ---------------------------------------------------------------------------
# Catalog structure
# ---------------------------------------------------------------------------

class TestSLOCatalog:
    def test_catalog_has_exactly_four_slos(self):
        assert set(SLO_CATALOG) == {"availability", "latency_p95", "quality", "cost_p99"}

    def test_availability_target_and_windows(self):
        slo = SLO_CATALOG["availability"]
        assert slo.target == 0.999
        assert slo.window == timedelta(days=30)
        assert slo.sli_kind == SLI_KIND_AVAILABILITY
        assert slo.good_threshold is None
        assert ("5m", "1h", 14.4) in slo.burn_rate_thresholds
        assert ("1h", "6h", 6.0) in slo.burn_rate_thresholds

    def test_latency_target_and_threshold(self):
        slo = SLO_CATALOG["latency_p95"]
        assert slo.target == 0.95
        assert slo.window == timedelta(days=30)
        assert slo.sli_kind == SLI_KIND_LATENCY
        assert slo.good_threshold == 3.0  # seconds

    def test_quality_target_and_threshold(self):
        slo = SLO_CATALOG["quality"]
        assert slo.target == 0.90
        assert slo.sli_kind == SLI_KIND_QUALITY
        assert slo.good_threshold == 0.7  # faithfulness score
        assert ("1h", "6h", 6.0) in slo.burn_rate_thresholds
        assert ("6h", "1d", 3.0) in slo.burn_rate_thresholds

    def test_cost_target_and_threshold(self):
        slo = SLO_CATALOG["cost_p99"]
        assert slo.target == 0.95
        assert slo.sli_kind == SLI_KIND_COST
        assert slo.good_threshold == 0.05  # USD per request

    def test_invalid_target_rejected(self):
        with pytest.raises(ValueError):
            SLO(name="bad", target=0.0)
        with pytest.raises(ValueError):
            SLO(name="bad", target=1.5)


# ---------------------------------------------------------------------------
# Window parsing helpers
# ---------------------------------------------------------------------------

class TestWindowHelpers:
    @pytest.mark.parametrize("spec,expected", [
        ("5m", timedelta(minutes=5)),
        ("1h", timedelta(hours=1)),
        ("6h", timedelta(hours=6)),
        ("1d", timedelta(days=1)),
        ("30s", timedelta(seconds=30)),
    ])
    def test_parse_window(self, spec, expected):
        assert parse_window(spec) == expected

    @pytest.mark.parametrize("spec", ["", "m", "5x", "-5m", "abc", "5"])
    def test_parse_window_invalid(self, spec):
        with pytest.raises(ValueError):
            parse_window(spec)

    def test_window_label_roundtrip(self):
        for spec in ("5m", "1h", "6h", "1d"):
            assert window_label(parse_window(spec)) == spec

    def test_unique_windows_covers_all_thresholds(self):
        windows = unique_windows()
        # every window referenced by any burn-rate pair, plus the 30d SLO window
        assert timedelta(minutes=5) in windows
        assert timedelta(hours=1) in windows
        assert timedelta(hours=6) in windows
        assert timedelta(days=1) in windows
        assert DEFAULT_WINDOW in windows

    def test_unique_windows_sorted(self):
        windows = unique_windows()
        assert windows == sorted(windows)


# ---------------------------------------------------------------------------
# Burn rate formula
# ---------------------------------------------------------------------------

class TestBurnRate:
    def test_one_when_burning_at_target_rate(self):
        # burn_rate == 1.0 means budget is consumed exactly at the target rate
        assert burn_rate(0.999, 0.999) == pytest.approx(1.0)

    def test_one_when_sli_is_half_budget(self):
        # target 0.9 => budget 0.1; sli 0.95 => consumed 0.05 => burn rate 0.5
        assert burn_rate(0.95, 0.9) == pytest.approx(0.5)

    def test_full_burn_when_sli_is_zero(self):
        assert burn_rate(0.0, 0.999) == pytest.approx(1000.0)

    def test_no_burn_when_sli_perfect(self):
        assert burn_rate(1.0, 0.999) == pytest.approx(0.0)

    def test_14_4x_burn_rate_formula(self):
        # spec example: availability target 0.999, budget 0.001;
        # sli = 1 - 14.4*0.001 = 0.9856 => burn rate 14.4
        assert burn_rate(0.9856, 0.999) == pytest.approx(14.4)

    def test_target_of_one_undefined(self):
        with pytest.raises(ValueError):
            burn_rate(1.0, 1.0)

    def test_invalid_sli_rejected(self):
        with pytest.raises(ValueError):
            burn_rate(-0.1, 0.999)
        with pytest.raises(ValueError):
            burn_rate(1.1, 0.999)


class TestSliFromCounts:
    def test_normal(self):
        assert sli_from_counts(999, 1000) == pytest.approx(0.999)

    def test_empty_window_is_perfect(self):
        assert sli_from_counts(0, 0) == 1.0


# ---------------------------------------------------------------------------
# SLORefresher
# ---------------------------------------------------------------------------

class TestSLORefresher:
    def _availability_store(self, good: int, total: int, short_good: int, short_total: int):
        """Fake store: 30d window gets (good, total), 5m/1h/6h windows get the short stats."""
        thirty_d = DEFAULT_WINDOW.total_seconds()
        hour = 3600.0
        stats = {
            ("availability", thirty_d): (good, total),
            ("availability", 300.0): (short_good, short_total),
            ("availability", hour): (short_good, short_total),
            ("availability", 6 * hour): (short_good, short_total),
        }
        return FakeHotStore(stats)

    def test_availability_sli_and_gauges(self):
        store = self._availability_store(good=999, total=1000, short_good=999, short_total=1000)
        refresher = SLORefresher(store, catalog={"availability": SLO_CATALOG["availability"]})
        report = refresher.refresh()

        assert report["availability"]["sli"] == pytest.approx(0.999)
        expo = generate_latest()
        assert _parse_metric(expo, "slo_target", {"name": "availability"})[0][0] == pytest.approx(0.999)
        assert _parse_metric(expo, "slo_actual_sli", {"name": "availability"})[0][0] == pytest.approx(0.999)
        # burn rate = (1 - 0.999) / (1 - 0.999) = 1.0
        for window in ("5m", "1h", "6h"):
            value = _parse_metric(expo, "slo_burn_rate", {"name": "availability", "window": window})
            assert value, f"missing slo_burn_rate for window={window}"
            assert value[0][0] == pytest.approx(1.0)

    def test_short_window_burn_differs_from_long(self):
        # healthy long window, burning budget fast in the last 5 minutes
        thirty_d = DEFAULT_WINDOW.total_seconds()
        store = FakeHotStore({
            ("availability", thirty_d): (9995, 10000),      # sli=0.9995, burn=0.5
            ("availability", 300.0): (90, 100),             # sli=0.90, burn=100
            ("availability", 3600.0): (990, 1000),          # sli=0.99, burn=10
            ("availability", 6 * 3600.0): (9995, 10000),
        })
        refresher = SLORefresher(store, catalog={"availability": SLO_CATALOG["availability"]})
        refresher.refresh()

        expo = generate_latest()
        short = _parse_metric(expo, "slo_burn_rate", {"name": "availability", "window": "5m"})[0][0]
        long_ = _parse_metric(expo, "slo_burn_rate", {"name": "availability", "window": "1h"})[0][0]
        assert short == pytest.approx(100.0)
        assert long_ == pytest.approx(10.0)
        # 30d SLI unaffected by the short-window spike
        assert _parse_metric(expo, "slo_actual_sli", {"name": "availability"})[0][0] == pytest.approx(0.9995)

    def test_quality_sli_counts_faithful_answers(self):
        thirty_d = DEFAULT_WINDOW.total_seconds()
        hour = 3600.0
        six_h = 6 * hour
        # catalog pairs for quality: (1h, 6h) and (6h, 1d) -> needs 1h, 6h, 1d windows
        stats = {
            ("quality", thirty_d): (90, 100),
            ("quality", hour): (8, 10),
            ("quality", six_h): (45, 50),
            ("quality", 86400.0): (9, 10),
        }
        refresher = SLORefresher(FakeHotStore(stats), catalog={"quality": SLO_CATALOG["quality"]})
        report = refresher.refresh()

        assert report["quality"]["sli"] == pytest.approx(0.9)
        expo = generate_latest()
        # 1h window: sli=0.8 -> burn=(1-0.8)/(1-0.9)=2.0; 6h window: sli=0.9 -> burn=1.0
        assert _parse_metric(expo, "slo_burn_rate", {"name": "quality", "window": "1h"})[0][0] == pytest.approx(2.0)
        assert _parse_metric(expo, "slo_burn_rate", {"name": "quality", "window": "6h"})[0][0] == pytest.approx(1.0)

    def test_latency_and_cost_thresholds_passed_to_store(self):
        thirty_d = DEFAULT_WINDOW.total_seconds()
        stats = {
            ("latency", thirty_d): (95, 100),
            ("latency", 300.0): (9, 10),
            ("latency", 3600.0): (90, 100),
            ("latency", 6 * 3600.0): (95, 100),
            ("cost", thirty_d): (95, 100),
            ("cost", 3600.0): (9, 10),
            ("cost", 6 * 3600.0): (90, 100),
            ("cost", 86400.0): (95, 100),
        }
        store = FakeHotStore(stats)
        catalog = {"latency_p95": SLO_CATALOG["latency_p95"], "cost_p99": SLO_CATALOG["cost_p99"]}
        SLORefresher(store, catalog=catalog).refresh()

        latency_calls = [c for c in store.calls if c[0] == "latency"]
        cost_calls = [c for c in store.calls if c[0] == "cost"]
        assert all(c[2] == 3.0 for c in latency_calls)
        assert all(c[2] == 0.05 for c in cost_calls)

    def test_error_budget_remaining_gauge(self):
        thirty_d = DEFAULT_WINDOW.total_seconds()
        # burn rate 0.5 => 50% budget consumed => 0.5 remaining
        stats = {
            ("availability", thirty_d): (9995, 10000),
            ("availability", 300.0): (9995, 10000),
            ("availability", 3600.0): (9995, 10000),
            ("availability", 6 * 3600.0): (9995, 10000),
        }
        SLORefresher(
            FakeHotStore(stats), catalog={"availability": SLO_CATALOG["availability"]}
        ).refresh()
        expo = generate_latest()
        value = _parse_metric(expo, "slo_error_budget_remaining", {"name": "availability"})[0][0]
        assert value == pytest.approx(0.5)

    def test_error_budget_clamped_at_zero(self):
        thirty_d = DEFAULT_WINDOW.total_seconds()
        stats = {
            ("availability", thirty_d): (0, 100),
            ("availability", 300.0): (0, 100),
            ("availability", 3600.0): (0, 100),
            ("availability", 6 * 3600.0): (0, 100),
        }
        SLORefresher(
            FakeHotStore(stats), catalog={"availability": SLO_CATALOG["availability"]}
        ).refresh()
        expo = generate_latest()
        value = _parse_metric(expo, "slo_error_budget_remaining", {"name": "availability"})[0][0]
        assert value == 0.0

    def test_hotstore_failure_does_not_raise(self):
        class ExplodingHotStore:
            def get_sli_stats(self, *a, **kw):
                raise ConnectionError("clickhouse down")

        refresher = SLORefresher(
            ExplodingHotStore(), catalog={"availability": SLO_CATALOG["availability"]}
        )
        report = refresher.refresh()  # must not raise
        assert report == {}

    def test_full_catalog_refresh(self):
        refresher = SLORefresher(FakeHotStore({}))
        report = refresher.refresh()
        # every SLO reported; empty windows are perfect SLIs (1.0)
        assert set(report) == set(SLO_CATALOG)
        expo = generate_latest()
        for name in SLO_CATALOG:
            assert _parse_metric(expo, "slo_target", {"name": name})
            assert _parse_metric(expo, "slo_actual_sli", {"name": name})

    def test_interval_defaults_to_300(self):
        refresher = SLORefresher(FakeHotStore({}))
        assert refresher.interval_seconds == 300

    def test_interval_env_override(self, monkeypatch):
        monkeypatch.setenv("AGENT_OBS_SLO_REFRESH_SECONDS", "60")
        refresher = SLORefresher(FakeHotStore({}))
        assert refresher.interval_seconds == 60


# ---------------------------------------------------------------------------
# HotStore.get_sli_stats SQL-level tests (mocked ClickHouse)
# ---------------------------------------------------------------------------

class TestHotStoreSliStats:
    @staticmethod
    def _store_with_execute(fake_execute) -> HotStore:
        store = HotStore.__new__(HotStore)
        store._execute_clickhouse = fake_execute  # type: ignore[method-assign]
        return store

    def test_availability_query(self):
        captured = {}

        def fake_execute(query, params=None):
            captured["query"] = query
            captured["params"] = params
            return [(1000, 999)]

        store = self._store_with_execute(fake_execute)
        good, total = store.get_sli_stats("availability", timedelta(days=30))

        assert (good, total) == (999, 1000)
        assert "spans_hot" in captured["query"]
        assert "agent.loop" in captured["query"]
        assert "status != 'error'" in captured["query"]
        assert "cutoff" in captured["params"]

    def test_latency_threshold_in_query(self):
        captured = {}

        def fake_execute(query, params=None):
            captured["query"] = query
            captured["params"] = params
            return [(100, 95)]

        store = self._store_with_execute(fake_execute)
        good, total = store.get_sli_stats("latency", timedelta(hours=1), good_threshold=3.0)

        assert (good, total) == (95, 100)
        assert "threshold" in captured["params"]
        assert captured["params"]["threshold"] == 3.0

    def test_latency_requires_threshold(self):
        store = self._store_with_execute(lambda query, params=None: [])
        with pytest.raises(ValueError):
            store.get_sli_stats("latency", timedelta(hours=1))

    def test_cost_groups_by_trace(self):
        captured = {}

        def fake_execute(query, params=None):
            captured["query"] = query
            return [(100, 96)]

        store = self._store_with_execute(fake_execute)
        good, total = store.get_sli_stats("cost", timedelta(days=30), good_threshold=0.05)

        assert (good, total) == (96, 100)
        assert "sum(cost_usd)" in captured["query"]
        assert "GROUP BY trace_id" in captured["query"]

    def test_quality_queries_eval_table(self):
        captured = {}

        def fake_execute(query, params=None):
            captured["query"] = query
            return [(100, 92)]

        store = self._store_with_execute(fake_execute)
        good, total = store.get_sli_stats("quality", timedelta(days=30), good_threshold=0.7)

        assert (good, total) == (92, 100)
        assert "eval_results_hot" in captured["query"]
        assert "faithfulness" in captured["query"]

    def test_unknown_kind_rejected(self):
        store = self._store_with_execute(lambda query, params=None: [])
        with pytest.raises(ValueError):
            store.get_sli_stats("throughput", timedelta(days=30))

    def test_empty_result_returns_zero_pair(self):
        store = self._store_with_execute(lambda query, params=None: [])
        assert store.get_sli_stats("availability", timedelta(days=30)) == (0, 0)
