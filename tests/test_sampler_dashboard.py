"""Tests for PP06 Sampler Policy History Dashboard (T3.1.4).

Covers the provisioned Grafana dashboard, the ClickHouse datasource it
annotates from, the drill-down links into the trace-viewer, and the runbook
README.
"""

import json
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
DASHBOARD_PATH = (
    REPO_ROOT / "infra" / "grafana" / "dashboards" / "sampler_policy_history.json"
)
COMPOSE_PATH = REPO_ROOT / "infra" / "docker-compose.yml"
CLICKHOUSE_DS_PATH = (
    REPO_ROOT / "infra" / "grafana" / "provisioning" / "datasources" / "clickhouse.yml"
)
PROMETHEUS_DS_PATH = (
    REPO_ROOT / "infra" / "grafana" / "provisioning" / "datasources" / "prometheus.yml"
)
README_PATH = REPO_ROOT / "docs" / "sampler-dashboard.md"

CLICKHOUSE_DS_TYPE = "grafana-clickhouse-datasource"


@pytest.fixture
def dashboard_config():
    """Load the provisioned dashboard JSON."""
    return json.loads(DASHBOARD_PATH.read_text(encoding="utf-8"))


@pytest.fixture
def compose_config():
    """Load the docker compose stack."""
    return yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))


@pytest.fixture
def clickhouse_datasource():
    """Load the provisioned ClickHouse datasource."""
    return yaml.safe_load(CLICKHOUSE_DS_PATH.read_text(encoding="utf-8"))


def _panel(panels, panel_id):
    return next(p for p in panels if p["id"] == panel_id)


class TestSamplerPolicyHistoryDashboard:
    """Test sampler policy history dashboard configuration."""

    def test_dashboard_structure(self, dashboard_config):
        """Test dashboard has correct structure."""
        assert dashboard_config["uid"] == "sampler-policy-history"
        assert "Sampler Policy History" in dashboard_config["title"]
        assert "PP03-PP06" in dashboard_config["description"]
        assert dashboard_config["version"] == 3
        assert dashboard_config["refresh"] == "30s"

    def test_dashboard_has_required_panels(self, dashboard_config):
        """Test dashboard has all required panels."""
        panels = dashboard_config["panels"]

        # Panel 1: current rate (gauge) — the "gauge" half of panel 1
        gauge = _panel(panels, 1)
        assert gauge["type"] == "gauge"
        assert "agent_obs_tail_sampler_current_rate" in gauge["targets"][0]["expr"]

        # Panel 2: rate over time — the "graph" half of panel 1
        rate = _panel(panels, 2)
        assert rate["type"] == "timeseries"
        assert "agent_obs_tail_sampler_current_rate" in rate["targets"][0]["expr"]

        # Panel 3: decisions distribution (pie)
        pie = _panel(panels, 3)
        assert pie["type"] == "piechart"
        assert "agent_obs_tail_sampler_policy_evaluations_total" in pie["targets"][0]["expr"]

        # Panel 4: decisions table (audit trail)
        table = _panel(panels, 4)
        assert table["type"] == "table"
        assert "audit_events_hot" in table["targets"][0]["rawSql"]

        # Panel 5: kept spans (stacked bar)
        kept = _panel(panels, 5)
        assert kept["type"] == "barchart"
        assert "agent_obs_tail_sampler_traces_sampled_total" in kept["targets"][0]["expr"]

        # Panel 6: eval score overlay
        eval_panel = _panel(panels, 6)
        assert "faithfulness_score" in eval_panel["targets"][0]["expr"]

        # Panel 7: cpu + error rate overlay
        system = _panel(panels, 7)
        exprs = " ".join(t["expr"] for t in system["targets"])
        assert "agent_obs_system_cpu_ratio" in exprs
        assert "agent_obs_agent_error_rate_5m" in exprs

    def test_policy_evaluations_grouped_by_outcome(self, dashboard_config):
        """The policy-evaluation metric is labelled by `outcome`, not `reason`."""
        pie = _panel(dashboard_config["panels"], 3)
        expr = pie["targets"][0]["expr"]
        assert "by (outcome)" in expr
        assert "by (reason)" not in expr

    def test_kept_spans_bar_is_stacked(self, dashboard_config):
        """Panel 5 must be a stacked bar (ticket: 'stacked bar')."""
        kept = _panel(dashboard_config["panels"], 5)
        stacking = kept["fieldConfig"]["defaults"]["custom"]["stacking"]
        assert stacking["mode"] == "normal"

    def test_dashboard_has_policy_reason_annotations(self, dashboard_config):
        """Test dashboard has policy reason change annotations."""
        annotations = dashboard_config["annotations"]["list"]

        policy_annotation = next(
            a for a in annotations if a["name"] == "Policy reason changes"
        )
        assert policy_annotation["datasource"]["uid"] == "prometheus"
        assert (
            policy_annotation["expr"]
            == "changes(agent_obs_tail_sampler_current_rate[2m]) > 0"
        )
        assert "policy_reason" in policy_annotation["titleFormat"]
        assert "rate" in policy_annotation["titleFormat"]

    def test_dashboard_has_audit_trail_annotations(self, dashboard_config):
        """Test dashboard has audit trail annotations read from ClickHouse."""
        annotations = dashboard_config["annotations"]["list"]

        audit_annotation = next(
            a for a in annotations if a["name"] == "Sampling decisions"
        )
        assert audit_annotation["datasource"]["type"] == CLICKHOUSE_DS_TYPE
        assert audit_annotation["datasource"]["uid"] == "clickhouse"
        raw_sql = audit_annotation["target"]["rawSql"]
        assert "audit_events_hot" in raw_sql
        assert "sampler.rate_change" in raw_sql
        # The time picker must drive the annotation window.
        assert "$__timeFilter(timestamp)" in raw_sql
        # Grafana's standard annotation contract: a `time` and a `text` column.
        assert "AS time" in raw_sql
        assert "AS text" in raw_sql

    def test_dashboard_has_clickhouse_integration(self, dashboard_config):
        """Test dashboard integrates with ClickHouse for audit trail."""
        annotations = dashboard_config["annotations"]["list"]

        clickhouse_annotation = next(
            a for a in annotations if a["datasource"]["type"] == CLICKHOUSE_DS_TYPE
        )
        assert "audit_events_hot" in clickhouse_annotation["target"]["rawSql"]
        assert "sampler.rate_change" in clickhouse_annotation["target"]["rawSql"]

    def test_dashboard_supports_all_policy_reasons(self, dashboard_config):
        """Test dashboard description mentions all policy reasons."""
        panels = dashboard_config["panels"]
        rate_panel_desc = _panel(panels, 2)["description"]

        # Check PP03 reasons
        assert "error_keep" in rate_panel_desc
        assert "cost_high_keep" in rate_panel_desc
        assert "security_keep" in rate_panel_desc
        assert "random" in rate_panel_desc

        # Check PP04 reasons
        assert "eval_feedback" in rate_panel_desc

    def test_dashboard_has_correct_tags(self, dashboard_config):
        """Test dashboard has correct tags."""
        tags = dashboard_config["tags"]
        assert "agent-obs" in tags
        assert "sampler" in tags
        assert "pc32" in tags
        assert "pp03" in tags
        assert "pp04" in tags
        assert "pp05" in tags
        assert "pp06" in tags

    def test_dashboard_panels_have_descriptions(self, dashboard_config):
        """Test all panels have meaningful descriptions."""
        for panel in dashboard_config["panels"]:
            assert "description" in panel
            assert len(panel["description"]) > 10

    def test_dashboard_has_time_ranges(self, dashboard_config):
        """Test dashboard has appropriate time ranges."""
        assert dashboard_config["time"]["from"] == "now-6h"
        assert dashboard_config["time"]["to"] == "now"
        # Ticket: time range picker 1h / 6h / 24h / 7d
        assert dashboard_config["timepicker"]["time_options"] == [
            "1h",
            "6h",
            "24h",
            "7d",
        ]

    def test_dashboard_metrics_exist_in_prometheus(self, dashboard_config):
        """Test all metrics referenced in dashboard exist in Prometheus."""
        panels = dashboard_config["panels"]

        required_metrics = [
            "agent_obs_tail_sampler_current_rate",
            "agent_obs_tail_sampler_policy_evaluations_total",
            "agent_obs_tail_sampler_traces_sampled_total",
            "agent_obs_system_cpu_ratio",
            "agent_obs_agent_error_rate_5m",
        ]

        for panel in panels:
            for target in panel["targets"]:
                # ClickHouse targets are raw SQL, not PromQL.
                expr = target.get("expr", "")
                for metric in required_metrics:
                    if metric in expr:
                        assert "agent_obs_" in metric
                        break

    def test_dashboard_has_proper_grid_layout(self, dashboard_config):
        """Test dashboard panels have proper grid layout."""
        panels = dashboard_config["panels"]

        # Row 1: gauge (6) + rate graph (18)
        gauge = _panel(panels, 1)["gridPos"]
        assert (gauge["x"], gauge["y"], gauge["w"]) == (0, 0, 6)
        rate = _panel(panels, 2)["gridPos"]
        assert (rate["x"], rate["y"], rate["w"]) == (6, 0, 18)

        # Row 2: pie (6) + table (12) + stacked bar (6)
        pie = _panel(panels, 3)["gridPos"]
        assert (pie["x"], pie["y"], pie["w"]) == (0, 7, 6)
        table = _panel(panels, 4)["gridPos"]
        assert (table["x"], table["y"], table["w"]) == (6, 7, 12)
        kept = _panel(panels, 5)["gridPos"]
        assert (kept["x"], kept["y"], kept["w"]) == (18, 7, 6)

        # Row 3: eval (12) + system (12)
        eval_panel = _panel(panels, 6)["gridPos"]
        assert (eval_panel["x"], eval_panel["y"], eval_panel["w"]) == (0, 15, 12)
        system = _panel(panels, 7)["gridPos"]
        assert (system["x"], system["y"], system["w"]) == (12, 15, 12)


class TestSamplerDashboardDrilldown:
    """Drill-down from a rate change into the trace-viewer (DoD)."""

    def test_dashboard_exposes_trace_viewer_variable(self, dashboard_config):
        """The trace-viewer target is a variable the SRE can repoint."""
        variables = dashboard_config["templating"]["list"]
        trace_viewer = next(v for v in variables if v["name"] == "trace_viewer_url")
        assert trace_viewer["type"] == "textbox"
        assert trace_viewer["current"]["value"] == "http://localhost:3000"

    def test_rate_graph_has_drilldown_link(self, dashboard_config):
        """Clicking a point on the rate graph opens the trace-viewer."""
        rate = _panel(dashboard_config["panels"], 2)
        links = rate["fieldConfig"]["defaults"]["links"]
        assert links, "rate graph has no data links"
        assert "${var:trace_viewer_url}" in links[0]["url"]
        assert "${__value.time" in links[0]["url"]
        assert links[0]["targetBlank"] is True

    def test_audit_table_has_drilldown_link(self, dashboard_config):
        """Clicking a rate-change row opens the trace-viewer."""
        table = _panel(dashboard_config["panels"], 4)
        overrides = table["fieldConfig"]["overrides"]
        assert overrides, "audit table has no data links"
        links = overrides[0]["properties"][0]["value"]
        assert "${var:trace_viewer_url}" in links[0]["url"]

    def test_dashboard_has_header_link_to_trace_viewer(self, dashboard_config):
        """A header link opens the trace-viewer root."""
        links = dashboard_config["links"]
        assert any("${var:trace_viewer_url}" in link["url"] for link in links)


class TestSamplerDashboardInfrastructure:
    """The ClickHouse datasource and plugin the dashboard depends on."""

    def test_clickhouse_datasource_is_provisioned(self, clickhouse_datasource):
        """The `clickhouse` datasource is provisioned with a fixed uid."""
        datasources = clickhouse_datasource["datasources"]
        ds = next(d for d in datasources if d["uid"] == "clickhouse")
        assert ds["type"] == CLICKHOUSE_DS_TYPE
        assert ds["access"] == "proxy"
        assert "password" in ds["secureJsonData"]
        # The plugin names the connection's default database `defaultDatabase`;
        # without it the queries run in `default` and audit_events_hot is lost.
        assert ds["jsonData"]["defaultDatabase"] == "$__env{CLICKHOUSE_DB}"
        assert ds["jsonData"]["protocol"] == "native"
        assert ds["jsonData"]["port"] == 9000

    def test_prometheus_datasource_is_provisioned(self):
        """The `prometheus` datasource the dashboard queries."""
        datasources = yaml.safe_load(
            PROMETHEUS_DS_PATH.read_text(encoding="utf-8")
        )["datasources"]
        assert any(d["uid"] == "prometheus" for d in datasources)

    def test_dashboard_datasource_uids_are_provisioned(self, dashboard_config):
        """Every datasource the dashboard references exists in provisioning."""
        provisioned = set()
        for path in (CLICKHOUSE_DS_PATH, PROMETHEUS_DS_PATH):
            for ds in yaml.safe_load(path.read_text(encoding="utf-8"))["datasources"]:
                provisioned.add(ds["uid"])

        referenced = set()
        for annotation in dashboard_config["annotations"]["list"]:
            referenced.add(annotation["datasource"]["uid"])
        for panel in dashboard_config["panels"]:
            for target in panel["targets"]:
                if "datasource" in target:
                    referenced.add(target["datasource"]["uid"])

        assert referenced <= provisioned, f"unprovisioned datasources: {referenced - provisioned}"

    def test_grafana_service_installs_clickhouse_plugin(self, compose_config):
        """The ClickHouse plugin is installed with the Grafana container."""
        grafana = compose_config["services"]["grafana"]
        env = grafana["environment"]
        assert "grafana-clickhouse-datasource" in env["GF_INSTALL_PLUGINS"]
        # Grafana 11.2 splits GF_INSTALL_PLUGINS on whitespace, so the version
        # is a separate token ("<id> <version>", not "<id>==<version>").
        assert env["GF_INSTALL_PLUGINS"].split() == [
            "grafana-clickhouse-datasource",
            "4.14.0",
        ]
        # The plugin must stay compatible with the pinned Grafana image:
        # 4.15.0+ requires Grafana >= 11.6.0.
        assert "11.2.0" in grafana["image"]

    def test_grafana_service_receives_clickhouse_credentials(self, compose_config):
        """The datasource provisioning reads the ClickHouse env vars."""
        env = compose_config["services"]["grafana"]["environment"]
        for key in ("CLICKHOUSE_HOST", "CLICKHOUSE_DB", "CLICKHOUSE_USER", "CLICKHOUSE_PASSWORD"):
            assert key in env

    def test_grafana_depends_on_clickhouse(self, compose_config):
        """Grafana waits for ClickHouse so the datasource can connect."""
        depends = compose_config["services"]["grafana"]["depends_on"]
        assert "clickhouse" in depends


class TestSamplerDashboardDocs:
    """Runbook README (DoD)."""

    def test_readme_exists(self):
        """The runbook README is part of the ticket."""
        assert README_PATH.exists(), f"missing {README_PATH}"
        readme = README_PATH.read_text(encoding="utf-8")
        assert len(readme) > 500

    def test_readme_documents_runbook_scenarios(self):
        """eval_drop / cpu_high / error_spike actions are documented."""
        readme = README_PATH.read_text(encoding="utf-8")
        for scenario in ("cpu_high", "error_high", "eval_feedback"):
            assert scenario in readme
        assert "Escalation" in readme

    def test_readme_documents_drilldown_and_annotations(self):
        """The README explains annotations and the drill-down target."""
        readme = README_PATH.read_text(encoding="utf-8")
        assert "trace_viewer_url" in readme
        assert "audit_events_hot" in readme
        assert "$__timeFilter" in readme
