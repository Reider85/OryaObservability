"""Tests for PC32 (T2.7.4) — sampler observability: alert rule + dashboard.

Validates that infra/prometheus-rules.yml carries the SamplerRateStuckAtCpuHigh
alert in its own group (the P21 tail_sampler recording rules must stay
untouched), that the PromQL semantics match the policy engine thresholds, and
that the provisioned Grafana dashboard exists and references the right metrics.

promtool unit tests live in tests/prometheus_rules_test.yml — run them with:

    docker run --rm -v "%cd%/infra:/infra" -v "%cd%/tests:/tests" \
        prom/prometheus promtool test rules /tests/prometheus_rules_test.yml
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
RULES_PATH = REPO_ROOT / "infra" / "prometheus-rules.yml"
DASHBOARD_PATH = (
    REPO_ROOT / "infra" / "grafana" / "dashboards" / "sampler_policy_history.json"
)
DATASOURCE_PATH = (
    REPO_ROOT / "infra" / "grafana" / "provisioning" / "datasources" / "prometheus.yml"
)
PROVIDER_PATH = (
    REPO_ROOT / "infra" / "grafana" / "provisioning" / "dashboards" / "dashboards.yml"
)

ALERT_NAME = "SamplerRateStuckAtCpuHigh"
CPU_HIGH_RATE = 0.05
FOR_MINUTES = 30.0


@pytest.fixture(scope="module")
def rules():
    assert RULES_PATH.exists(), f"missing {RULES_PATH}"
    return yaml.safe_load(RULES_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def sampler_group(rules):
    groups = [g for g in rules.get("groups", []) if g.get("name") == "sampler-monitoring"]
    assert len(groups) == 1, "sampler-monitoring group must exist exactly once"
    return groups[0]


@pytest.fixture(scope="module")
def alert_rule(sampler_group):
    matches = [r for r in sampler_group["rules"] if r.get("alert") == ALERT_NAME]
    assert len(matches) == 1, f"{ALERT_NAME} must appear exactly once"
    return matches[0]


# ---------------------------------------------------------------------------
# Rule file structure
# ---------------------------------------------------------------------------


class TestSamplerAlertStructure:
    def test_alert_lives_in_its_own_group(self, rules, sampler_group):
        """Must not be added to the P21 tail_sampler group.

        tests/test_cron_jobs.py::test_tail_sampler_recording_rules_preserved
        asserts that group contains exactly the two recording rules.
        """
        tail = next(g for g in rules["groups"] if g["name"] == "tail_sampler")
        alert_names = [r.get("alert") for r in tail["rules"]]
        assert ALERT_NAME not in alert_names
        assert sampler_group["name"] == "sampler-monitoring"

    def test_expr_targets_current_rate_gauge(self, alert_rule):
        expr = alert_rule["expr"]
        assert "agent_obs_tail_sampler_current_rate" in expr
        assert 'policy_reason="cpu_high"' in expr
        assert "0.05" in expr

    def test_for_clause_is_30_minutes(self, alert_rule):
        assert alert_rule["for"] == "30m"

    def test_severity_is_warning(self, alert_rule):
        assert alert_rule["labels"]["severity"] == "warning"
        assert alert_rule["labels"]["component"] == "sampler"

    def test_annotations_carry_actionable_context(self, alert_rule):
        annotations = alert_rule["annotations"]
        assert "pinned at 5%" in annotations["summary"]
        assert "Sampler Policy History" in annotations["description"]
        assert "/audit/sampler-rate-history" in annotations["description"]


# ---------------------------------------------------------------------------
# PromQL semantics, simulated in Python (mirrors the policy engine rules)
# ---------------------------------------------------------------------------


def alert_condition(rate_by_reason: dict[str, float]) -> bool:
    """agent_obs_tail_sampler_current_rate{policy_reason="cpu_high"} == 0.05"""
    return rate_by_reason.get("cpu_high") == pytest.approx(CPU_HIGH_RATE)


def alert_fires(condition_true_for_minutes: float) -> bool:
    """`for: 30m` — pending until the condition has held for the full window."""
    return condition_true_for_minutes >= FOR_MINUTES


class TestAlertSemantics:
    def test_cpu_high_at_five_percent_triggers_condition(self):
        assert alert_condition({"cpu_high": 0.05}) is True

    def test_cpu_high_at_other_rate_does_not_trigger(self):
        assert alert_condition({"cpu_high": 0.10}) is False
        assert alert_condition({"cpu_high": 0.01}) is False

    def test_other_reasons_do_not_trigger(self):
        assert alert_condition({"default": 0.05}) is False
        assert alert_condition({"error_high": 0.05}) is False

    def test_empty_metrics_do_not_trigger(self):
        assert alert_condition({}) is False

    def test_pending_before_window_elapses(self):
        assert alert_fires(29.0) is False

    def test_fires_once_window_elapses(self):
        assert alert_fires(30.0) is True
        assert alert_fires(45.0) is True

    def test_rate_recovery_clears_the_alert(self):
        """cpu_high held at 0.05 for 20m, then the policy moves the rate up."""
        held = 20.0
        assert alert_fires(held) is False
        # Rate left cpu_high / left 0.05: the condition is false again, so the
        # `for` clock resets even if the previous hold had been long.
        recovered = {"cpu_high": 0.10}
        assert alert_condition(recovered) is False


# ---------------------------------------------------------------------------
# Grafana dashboard + provisioning artifacts (PC32 item 3)
# ---------------------------------------------------------------------------


class TestGrafanaDashboard:
    def test_dashboard_json_is_valid(self):
        assert DASHBOARD_PATH.exists(), f"missing {DASHBOARD_PATH}"
        dashboard = json.loads(DASHBOARD_PATH.read_text(encoding="utf-8"))
        assert dashboard["uid"] == "sampler-policy-history"
        assert dashboard["title"] == "Sampler Policy History"

    def test_dashboard_plots_current_rate_with_reason_legend(self):
        dashboard = json.loads(DASHBOARD_PATH.read_text(encoding="utf-8"))
        rate_panels = [
            p
            for p in dashboard["panels"]
            if "agent_obs_tail_sampler_current_rate" in json.dumps(p)
        ]
        assert rate_panels, "a panel must plot agent_obs_tail_sampler_current_rate"
        panel = rate_panels[0]
        assert panel["title"] == "Sampler rate over time"
        target = panel["targets"][0]
        assert target["expr"] == "agent_obs_tail_sampler_current_rate"
        assert "{{policy_reason}}" in target["legendFormat"]

    def test_dashboard_annotates_rate_changes(self):
        dashboard = json.loads(DASHBOARD_PATH.read_text(encoding="utf-8"))
        exprs = [a.get("expr", "") for a in dashboard["annotations"]["list"]]
        assert any("agent_obs_tail_sampler_current_rate" in e for e in exprs)

    def test_datasource_provisioning_points_at_prometheus(self):
        assert DATASOURCE_PATH.exists(), f"missing {DATASOURCE_PATH}"
        data = yaml.safe_load(DATASOURCE_PATH.read_text(encoding="utf-8"))
        sources = data["datasources"]
        assert len(sources) == 1
        assert sources[0]["type"] == "prometheus"
        assert sources[0]["uid"] == "prometheus"
        assert "prometheus:9090" in sources[0]["url"]

    def test_dashboard_provider_reads_provisioned_path(self):
        assert PROVIDER_PATH.exists(), f"missing {PROVIDER_PATH}"
        data = yaml.safe_load(PROVIDER_PATH.read_text(encoding="utf-8"))
        providers = data["providers"]
        assert providers[0]["options"]["path"] == "/var/lib/grafana/dashboards"

    def test_compose_exposes_grafana_on_host_3001(self):
        """3000 is Langfuse; the dashboard must not fight it for the port."""
        compose = (REPO_ROOT / "infra" / "docker-compose.yml").read_text(
            encoding="utf-8"
        )
        assert "grafana/grafana" in compose
        assert "${GRAFANA_PORT:-3001}:3000" in compose
