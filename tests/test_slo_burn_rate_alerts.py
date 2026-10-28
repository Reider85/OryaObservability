"""Tests for PP26 (T3.6.2) — multi-window multi-burn-rate alerting.

Validates:
- SLOBurnRate fires only when BOTH windows of a pair exceed the threshold
  (single-window breach must never alert — PP0 hard rule #5)
- is_burned window/threshold semantics per the SLO_CATALOG pairs
- infra/prometheus-rules.yml carries the 8 SLOBurnRateHigh/SlowBurn rules
  with thresholds/windows/labels matching the catalog exactly
- infra/alertmanager.yml routes severity=page -> pagerduty, ticket -> jira
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from agent_obs.metrics import slo_burn_rate
from agent_obs.slo import SLO_CATALOG, SLOBurnRate
from agent_obs.slo.burn_rate import gauge_burn_rate_provider

REPO_ROOT = Path(__file__).resolve().parent.parent
RULES_PATH = REPO_ROOT / "infra" / "prometheus-rules.yml"
ALERTMANAGER_PATH = REPO_ROOT / "infra" / "alertmanager.yml"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _provider(rates: dict[tuple[str, str], float]):
    """Canned burn-rate provider: (slo_name, window) -> rate."""
    return lambda slo_name, window: rates.get((slo_name, window), 0.0)


@pytest.fixture(autouse=True)
def _reset_burn_rate_gauge():
    slo_burn_rate._metrics.clear()
    yield
    slo_burn_rate._metrics.clear()


# ---------------------------------------------------------------------------
# SLOBurnRate.is_alert_fired — the core multi-window correlation contract
# ---------------------------------------------------------------------------

class TestIsAlertFired:
    def test_both_windows_over_threshold_fires(self):
        # availability pairs: (5m, 1h, 14.4), (1h, 6h, 6.0)
        br = SLOBurnRate(catalog={"availability": SLO_CATALOG["availability"]},
                         burn_rate_provider=_provider({
                             ("availability", "5m"): 20.0,
                             ("availability", "1h"): 15.0,
                             ("availability", "6h"): 1.0,
                         }))
        assert br.is_alert_fired("availability") is True

    def test_short_window_only_never_fires(self):
        # 5m is burning hard, 1h is healthy -> single-window, no alert
        br = SLOBurnRate(catalog={"availability": SLO_CATALOG["availability"]},
                         burn_rate_provider=_provider({
                             ("availability", "5m"): 100.0,
                             ("availability", "1h"): 1.0,
                             ("availability", "6h"): 1.0,
                         }))
        assert br.is_alert_fired("availability") is False

    def test_long_window_only_never_fires(self):
        # 1h over both pair thresholds, but 5m calm and 6h calm -> no alert.
        # 1h=20 exceeds the (1h,6h,6.0) threshold too, but its pair partner
        # 6h=1 does not, and the (5m,1h,14.4) partner 5m=1 does not either.
        br = SLOBurnRate(catalog={"availability": SLO_CATALOG["availability"]},
                         burn_rate_provider=_provider({
                             ("availability", "5m"): 1.0,
                             ("availability", "1h"): 20.0,
                             ("availability", "6h"): 1.0,
                         }))
        assert br.is_alert_fired("availability") is False

    def test_second_pair_fires_even_if_first_does_not(self):
        # slow burn: (1h, 6h) pair is over 6.0, fast (5m, 1h) pair is not
        br = SLOBurnRate(catalog={"availability": SLO_CATALOG["availability"]},
                         burn_rate_provider=_provider({
                             ("availability", "5m"): 1.0,
                             ("availability", "1h"): 7.0,
                             ("availability", "6h"): 10.0,
                         }))
        assert br.is_alert_fired("availability") is True
        assert br.fired_pairs("availability") == [("1h", "6h", 6.0)]

    def test_exactly_at_threshold_does_not_fire(self):
        # strict inequality: burning exactly AT the sustainable-adjusted
        # threshold is not an alert
        br = SLOBurnRate(catalog={"availability": SLO_CATALOG["availability"]},
                         burn_rate_provider=_provider({
                             ("availability", "5m"): 14.4,
                             ("availability", "1h"): 14.4,
                             ("availability", "6h"): 1.0,
                         }))
        assert br.is_alert_fired("availability") is False

    def test_quality_uses_slower_pairs(self):
        # quality page pair is (1h, 6h, 6.0) — a 5m spike is irrelevant
        br = SLOBurnRate(catalog={"quality": SLO_CATALOG["quality"]},
                         burn_rate_provider=_provider({
                             ("quality", "5m"): 100.0,
                             ("quality", "1h"): 7.0,
                             ("quality", "6h"): 7.0,
                             ("quality", "1d"): 1.0,
                         }))
        assert br.is_alert_fired("quality") is True

    def test_cost_ticket_pair(self):
        # cost_p99 ticket pair: (6h, 1d, 3.0)
        br = SLOBurnRate(catalog={"cost_p99": SLO_CATALOG["cost_p99"]},
                         burn_rate_provider=_provider({
                             ("cost_p99", "1h"): 1.0,
                             ("cost_p99", "6h"): 4.0,
                             ("cost_p99", "1d"): 3.5,
                         }))
        assert br.is_alert_fired("cost_p99") is True
        assert br.fired_pairs("cost_p99") == [("6h", "1d", 3.0)]

    def test_unknown_slo_raises(self):
        br = SLOBurnRate(burn_rate_provider=_provider({}))
        with pytest.raises(ValueError, match="unknown SLO"):
            br.is_alert_fired("nonexistent")

    def test_fired_pairs_empty_when_healthy(self):
        br = SLOBurnRate(burn_rate_provider=_provider({}))
        for name in SLO_CATALOG:
            assert br.fired_pairs(name) == []
            assert br.is_alert_fired(name) is False


# ---------------------------------------------------------------------------
# SLOBurnRate.is_burned — PP0 contract (window, slo) -> bool
# ---------------------------------------------------------------------------

class TestIsBurned:
    def test_window_over_pair_threshold(self):
        br = SLOBurnRate(catalog={"availability": SLO_CATALOG["availability"]},
                         burn_rate_provider=_provider({("availability", "5m"): 20.0}))
        assert br.is_burned("5m", "availability") is True

    def test_window_under_threshold(self):
        br = SLOBurnRate(catalog={"availability": SLO_CATALOG["availability"]},
                         burn_rate_provider=_provider({("availability", "5m"): 10.0}))
        assert br.is_burned("5m", "availability") is False

    def test_window_shared_between_pairs_uses_permissive_threshold(self):
        # "1h" participates in (5m,1h,14.4) and (1h,6h,6.0): burned means
        # exceeding EITHER pair's threshold, so the 6.0 threshold applies.
        br = SLOBurnRate(catalog={"availability": SLO_CATALOG["availability"]},
                         burn_rate_provider=_provider({("availability", "1h"): 7.0}))
        assert br.is_burned("1h", "availability") is True
        # 10.0 also exceeds 6.0 (and even 14.4 would only matter for the
        # stricter pair) — need to go below 6.0 to be un-burned
        br2 = SLOBurnRate(catalog={"availability": SLO_CATALOG["availability"]},
                          burn_rate_provider=_provider({("availability", "1h"): 5.0}))
        assert br2.is_burned("1h", "availability") is False

    def test_window_not_in_any_pair_raises(self):
        br = SLOBurnRate(catalog={"availability": SLO_CATALOG["availability"]},
                         burn_rate_provider=_provider({}))
        with pytest.raises(ValueError, match="not part of any burn-rate pair"):
            br.is_burned("30d", "availability")

    def test_unknown_slo_raises(self):
        br = SLOBurnRate(burn_rate_provider=_provider({}))
        with pytest.raises(ValueError, match="unknown SLO"):
            br.is_burned("5m", "nonexistent")


# ---------------------------------------------------------------------------
# Default provider reads the slo_burn_rate gauge (SLORefresher contract)
# ---------------------------------------------------------------------------

class TestGaugeProvider:
    def test_reads_gauge_value(self):
        slo_burn_rate.labels(name="availability", window="5m").set(16.0)
        assert gauge_burn_rate_provider("availability", "5m") == pytest.approx(16.0)

    def test_missing_series_is_zero(self):
        assert gauge_burn_rate_provider("availability", "5m") == 0.0

    def test_default_provider_wired_into_evaluator(self):
        slo_burn_rate.labels(name="availability", window="5m").set(20.0)
        slo_burn_rate.labels(name="availability", window="1h").set(15.0)
        slo_burn_rate.labels(name="availability", window="6h").set(1.0)
        br = SLOBurnRate(catalog={"availability": SLO_CATALOG["availability"]})
        assert br.is_alert_fired("availability") is True


# ---------------------------------------------------------------------------
# infra/prometheus-rules.yml — the 8 alert rules
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def rules():
    return yaml.safe_load(RULES_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def slo_rules(rules):
    groups = [g for g in rules["groups"] if g["name"] == "slo-burn-rate"]
    assert groups, "slo-burn-rate group missing from prometheus-rules.yml"
    return groups[0]["rules"]


class TestPrometheusRules:
    def test_eight_alerts_cover_all_slos_both_severities(self, slo_rules):
        combos = {(r["labels"]["slo"], r["labels"]["severity"]) for r in slo_rules}
        expected = {(slo, sev) for slo in SLO_CATALOG for sev in ("page", "ticket")}
        assert combos == expected

    def test_alert_names(self, slo_rules):
        for r in slo_rules:
            expected = "SLOBurnRateHigh" if r["labels"]["severity"] == "page" else "SLOBurnRateSlowBurn"
            assert r["alert"] == expected

    def test_all_for_2m(self, slo_rules):
        for r in slo_rules:
            assert r["for"] == "2m", f"{r['alert']}/{r['labels']['slo']} must hold for 2m"

    def test_multi_window_and_on_name(self, slo_rules):
        # every expr must correlate BOTH windows via `and on(name)` —
        # a single-window expression would violate PP0 hard rule #5
        for r in slo_rules:
            expr = r["expr"]
            assert "and on(name)" in expr, f"{r['alert']}/{r['labels']['slo']} is single-window"
            assert expr.count("slo_burn_rate") == 2

    def test_thresholds_and_windows_match_catalog(self, slo_rules):
        import re
        for r in slo_rules:
            slo = SLO_CATALOG[r["labels"]["slo"]]
            # find the catalog pair this rule implements
            pairs = [(s, l, t) for s, l, t in slo.burn_rate_thresholds]
            windows = re.findall(r'window="(\w+)"', r["expr"])
            thresholds = [float(t) for t in re.findall(r"> ([\d.]+)", r["expr"])]
            matching = [p for p in pairs if [p[0], p[1]] == windows]
            assert matching, (
                f"{r['alert']}/{r['labels']['slo']}: windows {windows} "
                f"not a catalog pair of {slo.name}"
            )
            assert thresholds == [matching[0][2], matching[0][2]], (
                f"{r['alert']}/{r['labels']['slo']}: thresholds {thresholds} "
                f"!= catalog {matching[0][2]}"
            )

    def test_page_rules_are_fast_pairs_ticket_slow_pairs(self, slo_rules):
        # the FIRST catalog pair is the fast (page) one, the second is slow (ticket)
        import re
        for r in slo_rules:
            slo = SLO_CATALOG[r["labels"]["slo"]]
            windows = re.findall(r'window="(\w+)"', r["expr"])
            pair = next(p for p in slo.burn_rate_thresholds if [p[0], p[1]] == windows)
            is_fast_pair = pair == slo.burn_rate_thresholds[0]
            if r["labels"]["severity"] == "page":
                assert is_fast_pair, f"page rule {r['labels']['slo']} must use the fast pair"
            else:
                assert not is_fast_pair, f"ticket rule {r['labels']['slo']} must use the slow pair"

    def test_annotations_present(self, slo_rules):
        for r in slo_rules:
            assert r["annotations"].get("summary")
            assert "docs/slo-alerts-runbook.md" in r["annotations"]["description"]

    def test_yaml_file_reparses(self, rules):
        assert rules["groups"]  # sanity: whole file is valid YAML


# ---------------------------------------------------------------------------
# infra/alertmanager.yml — page/ticket routing
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def am():
    return yaml.safe_load(ALERTMANAGER_PATH.read_text(encoding="utf-8"))


class TestAlertmanagerRouting:
    def test_page_route_goes_to_pagerduty(self, am):
        route = next(
            r for r in am["route"]["routes"]
            if any("severity" in m and "page" in m for m in r.get("matchers", []))
        )
        assert route["receiver"] == "pagerduty"
        assert "slo" in route.get("group_by", [])

    def test_ticket_route_goes_to_jira(self, am):
        route = next(
            r for r in am["route"]["routes"]
            if any("severity" in m and "ticket" in m for m in r.get("matchers", []))
        )
        assert route["receiver"] == "jira"
        assert "slo" in route.get("group_by", [])

    def test_pagerduty_receiver_exists(self, am):
        names = [r["name"] for r in am["receivers"]]
        assert "pagerduty" in names
        pd = next(r for r in am["receivers"] if r["name"] == "pagerduty")
        assert pd["pagerduty_configs"][0]["service_key"] == "${PAGERDUTY_SERVICE_KEY}"

    def test_jira_receiver_exists(self, am):
        names = [r["name"] for r in am["receivers"]]
        assert "jira" in names
        jira = next(r for r in am["receivers"] if r["name"] == "jira")
        assert jira["webhook_configs"][0]["url"] == "${JIRA_WEBHOOK_URL}"
