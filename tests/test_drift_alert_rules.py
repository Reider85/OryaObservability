"""Tests for PC27 — drift-detection alert rule and Alertmanager integration.

Validates that:
- infra/prometheus-rules.yml contains a DriftDetected alert rule
- infra/alertmanager.yml routes drift alerts with 30-minute suppression
- The rule expression references the correct metric names
- Annotations include agent_id, KL score, and Phoenix UMAP link
"""

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
RULES_PATH = REPO_ROOT / "infra" / "prometheus-rules.yml"
ALERTMANAGER_PATH = REPO_ROOT / "infra" / "alertmanager.yml"


@pytest.fixture(scope="module")
def rules():
    assert RULES_PATH.exists(), f"missing {RULES_PATH}"
    return yaml.safe_load(RULES_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def alertmanager():
    assert ALERTMANAGER_PATH.exists(), f"missing {ALERTMANAGER_PATH}"
    return yaml.safe_load(ALERTMANAGER_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def drift_group(rules):
    groups = rules.get("groups", [])
    for g in groups:
        if g["name"] == "drift-detection":
            return g
    pytest.fail("drift-detection group not found in prometheus-rules.yml")


@pytest.fixture(scope="module")
def drift_detected_rule(drift_group):
    for r in drift_group.get("rules", []):
        if r.get("alert") == "DriftDetected":
            return r
    pytest.fail("DriftDetected alert rule not found in drift-detection group")


@pytest.fixture(scope="module")
def drift_route(alertmanager):
    routes = alertmanager.get("route", {}).get("routes", [])
    for r in routes:
        matchers = r.get("matchers", [])
        if any("component" in m and "drift-detector" in m for m in matchers):
            return r
    pytest.fail("drift-detector route not found in alertmanager.yml")


# ---------------------------------------------------------------------------
# Prometheus rule structure
# ---------------------------------------------------------------------------

class TestDriftDetectedRule:
    def test_drift_detection_group_exists(self, drift_group):
        """drift-detection group is present in prometheus-rules.yml."""
        assert drift_group["name"] == "drift-detection"

    def test_drift_detected_alert_exists(self, drift_detected_rule):
        """DriftDetected alert rule is present."""
        assert drift_detected_rule["alert"] == "DriftDetected"

    def test_expr_references_kl_score_metric(self, drift_detected_rule):
        """Expression must compare agent_obs_drift_kl_score."""
        assert "agent_obs_drift_kl_score" in drift_detected_rule["expr"]

    def test_expr_references_threshold_metric(self, drift_detected_rule):
        """Expression must compare against agent_obs_drift_threshold_value."""
        assert "agent_obs_drift_threshold_value" in drift_detected_rule["expr"]

    def test_expr_joins_on_agent_id(self, drift_detected_rule):
        """Expression must join KL score and threshold on agent_id label."""
        assert "on(agent_id)" in drift_detected_rule["expr"]

    def test_for_duration_is_5m(self, drift_detected_rule):
        """Alert must fire for 5 minutes before Alertmanager sees it."""
        assert drift_detected_rule["for"] == "5m"

    def test_severity_label_is_warning(self, drift_detected_rule):
        """Default severity for drift alerts is warning."""
        labels = drift_detected_rule.get("labels", {})
        assert labels.get("severity") == "warning"

    def test_component_label(self, drift_detected_rule):
        """Component label must be drift-detector for route matching."""
        labels = drift_detected_rule.get("labels", {})
        assert labels.get("component") == "drift-detector"

    def test_annotations_contain_summary(self, drift_detected_rule):
        """Summary annotation must reference agent_id."""
        annotations = drift_detected_rule.get("annotations", {})
        assert "summary" in annotations
        assert "agent_id" in annotations["summary"]

    def test_annotations_contain_description(self, drift_detected_rule):
        """Description must include KL value and agent_id."""
        annotations = drift_detected_rule.get("annotations", {})
        assert "description" in annotations
        desc = annotations["description"]
        assert "KL" in desc
        assert "agent_id" in desc

    def test_annotations_contain_baseline_window(self, drift_detected_rule):
        """Description must include baseline window start and end."""
        annotations = drift_detected_rule.get("annotations", {})
        desc = annotations.get("description", "")
        assert "baseline window" in desc.lower()
        assert "baseline_window_start" in desc
        assert "baseline_window_end" in desc

    def test_annotations_contain_phoenix_link(self, drift_detected_rule):
        """Description must include a Phoenix UMAP link for root-cause analysis."""
        annotations = drift_detected_rule.get("annotations", {})
        desc = annotations.get("description", "")
        assert "phoenix" in desc.lower()


# ---------------------------------------------------------------------------
# Alertmanager routing
# ---------------------------------------------------------------------------

class TestAlertmanagerDriftRoute:
    def test_drift_route_exists(self, drift_route):
        """A route matching component=drift-detector exists."""
        assert drift_route is not None

    def test_route_groups_by_agent_id(self, drift_route):
        """Drift alerts are grouped per agent_id to avoid cross-agent noise."""
        group_by = drift_route.get("group_by", [])
        assert "agent_id" in group_by

    def test_route_groups_by_alertname(self, drift_route):
        """Drift alerts also group by alertname."""
        group_by = drift_route.get("group_by", [])
        assert "alertname" in group_by

    def test_suppression_window_is_30m(self, drift_route):
        """group_interval=30m ensures 30-minute suppression of duplicate alerts.

        This is the PC27 requirement: if an alert already fired within
        30 minutes, Alertmanager should not send a duplicate.
        """
        assert drift_route.get("group_interval") == "30m"

    def test_group_wait_is_30s(self, drift_route):
        """First alert fires within 30 seconds of being seen by Alertmanager."""
        assert drift_route.get("group_wait") == "30s"

    def test_repeat_interval_is_2h(self, drift_route):
        """If unresolved, the alert repeats every 2 hours."""
        assert drift_route.get("repeat_interval") == "2h"

    def test_receiver_is_not_null(self, drift_route):
        """Drift route uses a real receiver (not the null sink)."""
        assert drift_route.get("receiver") != "null"

    def test_receiver_is_slack(self, drift_route):
        """Drift route uses the Slack receiver."""
        assert drift_route.get("receiver") == "slack"

    def test_matcher_uses_component_label(self, drift_route):
        """Route matches on component=drift-detector label from Prometheus rule."""
        matchers = drift_route.get("matchers", [])
        component_matchers = [m for m in matchers if "component" in m]
        assert len(component_matchers) >= 1
        assert any("drift-detector" in m for m in component_matchers)


# ---------------------------------------------------------------------------
# Cross-file consistency
# ---------------------------------------------------------------------------

class TestCrossFileConsistency:
    def test_prometheus_routes_to_alertmanager(self, alertmanager):
        """Alertmanager is reachable from the route configuration."""
        route = alertmanager.get("route", {})
        assert route.get("receiver") is not None

    def test_alertmanager_has_default_receiver(self, alertmanager):
        """Default receiver is configured (even if it's a placeholder)."""
        receivers = alertmanager.get("receivers", [])
        names = [r["name"] for r in receivers]
        assert "default" in names

    def test_alertmanager_has_slack_receiver(self, alertmanager):
        """Alertmanager has a Slack receiver configured."""
        receivers = alertmanager.get("receivers", [])
        names = [r["name"] for r in receivers]
        assert "slack" in names
        # Check that Slack receiver has slack_configs
        slack_receiver = next((r for r in receivers if r["name"] == "slack"), None)
        assert slack_receiver is not None
        assert "slack_configs" in slack_receiver
        assert len(slack_receiver["slack_configs"]) > 0

    def test_rules_file_is_valid_yaml(self, rules):
        """prometheus-rules.yml parses as valid YAML."""
        assert "groups" in rules
        assert isinstance(rules["groups"], list)

    def test_alertmanager_file_is_valid_yaml(self, alertmanager):
        """alertmanager.yml parses as valid YAML."""
        assert "route" in alertmanager
        assert "receivers" in alertmanager

    def test_drift_kl_score_has_baseline_labels(self):
        """The drift_kl_score gauge includes baseline window labels."""
        from agent_obs.metrics import drift_kl_score
        assert "baseline_window_start" in drift_kl_score._labelnames
        assert "baseline_window_end" in drift_kl_score._labelnames
        assert len(drift_kl_score._labelnames) == 3


# ---------------------------------------------------------------------------
# Metrics emitted by drift detector (PC25)
# ---------------------------------------------------------------------------

class TestDriftDetectorMetrics:
    def test_kl_score_gauge_exists(self):
        """The drift_kl_score gauge metric is defined."""
        from agent_obs.metrics import drift_kl_score
        assert drift_kl_score._name == "agent_obs_drift_kl_score"

    def test_threshold_value_gauge_exists(self):
        """The drift_threshold_value gauge metric is defined."""
        from agent_obs.metrics import drift_threshold_value
        assert drift_threshold_value._name == "agent_obs_drift_threshold_value"

    def test_alerts_counter_exists(self):
        """The drift_alerts_total counter metric is defined."""
        from agent_obs.metrics import drift_alerts_total
        # prometheus_client Counter stores _name without _total suffix
        assert drift_alerts_total._name == "agent_obs_drift_alerts"

    def test_runs_counter_exists(self):
        """The drift_runs_total counter metric is defined."""
        from agent_obs.metrics import drift_runs_total
        # prometheus_client Counter stores _name without _total suffix
        assert drift_runs_total._name == "agent_obs_drift_runs"
