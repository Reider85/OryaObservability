"""Tests for tail-sampler Prometheus recording rules (P21, T1.3.3).

Validates that infra/prometheus-rules.yml is well-formed and that the
kept-ratio formula computes correctly on a fixed set of traces.
"""

from pathlib import Path

import pytest
import yaml

RULES_PATH = Path(__file__).resolve().parent.parent / "infra" / "prometheus-rules.yml"

REASONS = ["error-keep", "cost-over-budget-keep", "security-incident-keep", "normal-sample"]


@pytest.fixture(scope="module")
def rules():
    assert RULES_PATH.exists(), f"missing {RULES_PATH}"
    return yaml.safe_load(RULES_PATH.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Helpers — simulate the PromQL expressions in Python.
# ---------------------------------------------------------------------------

def kept_ratio(counters: dict[str, int]) -> float:
    """kept / (kept + dropped) from decision-labelled counters."""
    kept = counters.get("sampled", 0)
    dropped = counters.get("dropped", 0)
    total = kept + dropped
    if total == 0:
        return 0.0
    return kept / total


def kept_by_reason(reason_counters: dict[str, int]) -> dict[str, float]:
    """Share of each keep-reason among all kept traces."""
    total_kept = sum(reason_counters.values())
    if total_kept == 0:
        return {reason: 0.0 for reason in reason_counters}
    return {reason: value / total_kept for reason, value in reason_counters.items()}


# ---------------------------------------------------------------------------
# Rule file structure
# ---------------------------------------------------------------------------

class TestRulesFileStructure:
    def test_has_single_group(self, rules):
        groups = rules.get("groups", [])
        assert len(groups) == 1
        assert groups[0]["name"] == "tail_sampler"

    def test_kept_ratio_rule_exists(self, rules):
        names = [r["record"] for r in rules["groups"][0]["rules"]]
        assert "job:tail_sampler_kept_ratio:ratio" in names

    def test_kept_by_reason_rule_exists(self, rules):
        names = [r["record"] for r in rules["groups"][0]["rules"]]
        assert "job:tail_sampler_kept_by_reason:ratio" in names

    def test_kept_ratio_expr_references_native_counter(self, rules):
        rule = next(
            r for r in rules["groups"][0]["rules"]
            if r["record"] == "job:tail_sampler_kept_ratio:ratio"
        )
        expr = rule["expr"]
        assert "otelcol_processor_tail_sampling_count_traces_sampled" in expr
        assert 'decision="sampled"' in expr
        assert 'decision="dropped"' in expr

    def test_kept_by_reason_expr_groups_by_reason(self, rules):
        rule = next(
            r for r in rules["groups"][0]["rules"]
            if r["record"] == "job:tail_sampler_kept_by_reason:ratio"
        )
        expr = rule["expr"]
        assert "sum by (reason)" in expr


# ---------------------------------------------------------------------------
# Ratio calculation on a fixed set of traces
# ---------------------------------------------------------------------------

class TestKeptRatio:
    def test_fixed_set_sampled_100_dropped_400(self):
        assert kept_ratio({"sampled": 100, "dropped": 400}) == pytest.approx(0.2)

    def test_all_kept(self):
        assert kept_ratio({"sampled": 50, "dropped": 0}) == pytest.approx(1.0)

    def test_all_dropped(self):
        assert kept_ratio({"sampled": 0, "dropped": 50}) == pytest.approx(0.0)

    def test_no_data_returns_zero(self):
        assert kept_ratio({}) == 0.0

    def test_half_half(self):
        assert kept_ratio({"sampled": 7, "dropped": 7}) == pytest.approx(0.5)

    def test_percent_normal_rate_10(self):
        # 10% keep: 10 sampled, 90 dropped
        assert kept_ratio({"sampled": 10, "dropped": 90}) == pytest.approx(0.1)


class TestKeptByReason:
    @pytest.fixture
    def fixed_reasons(self):
        return {
            "error-keep": 30,
            "cost-over-budget-keep": 20,
            "security-incident-keep": 10,
            "normal-sample": 40,
        }

    def test_reasons_are_distinguishable(self, fixed_reasons):
        ratios = kept_by_reason(fixed_reasons)
        assert set(ratios.keys()) == set(REASONS)

    def test_reason_ratios_sum_to_one(self, fixed_reasons):
        ratios = kept_by_reason(fixed_reasons)
        assert sum(ratios.values()) == pytest.approx(1.0)

    def test_error_keep_ratio(self, fixed_reasons):
        ratios = kept_by_reason(fixed_reasons)
        assert ratios["error-keep"] == pytest.approx(0.3)

    def test_cost_keep_ratio(self, fixed_reasons):
        ratios = kept_by_reason(fixed_reasons)
        assert ratios["cost-over-budget-keep"] == pytest.approx(0.2)

    def test_security_keep_ratio(self, fixed_reasons):
        ratios = kept_by_reason(fixed_reasons)
        assert ratios["security-incident-keep"] == pytest.approx(0.1)

    def test_normal_sample_ratio(self, fixed_reasons):
        ratios = kept_by_reason(fixed_reasons)
        assert ratios["normal-sample"] == pytest.approx(0.4)

    def test_no_kept_returns_zero_for_each(self):
        reasons = {reason: 0 for reason in REASONS}
        ratios = kept_by_reason(reasons)
        assert all(v == 0.0 for v in ratios.values())

    def test_single_reason_is_full_share(self):
        ratios = kept_by_reason({"error-keep": 5})
        assert ratios["error-keep"] == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Keep reasons must match the sampler policy names in the Collector config
# ---------------------------------------------------------------------------

class TestReasonsMatchCollectorPolicies:
    def test_policy_names_in_collector_config(self):
        config_path = (
            Path(__file__).resolve().parent.parent
            / "infra" / "otel-collector" / "config.yaml"
        )
        config_text = config_path.read_text(encoding="utf-8")
        for reason in REASONS:
            assert f"name: {reason}" in config_text, (
                f"policy '{reason}' not found in Collector config — "
                "prometheus-rules reason label would not match"
            )
