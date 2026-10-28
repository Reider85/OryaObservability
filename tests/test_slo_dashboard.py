"""Tests for PP28 SLO Dashboard (T3.6.4).

Covers the provisioned Grafana dashboard (infra/grafana/dashboards/slo.json),
its synchronization with the PP25 SLO catalog, the ClickHouse annotations it
reads (deploys / incidents), the drill-down links into the trace-viewer, and
the runbook README (docs/slo-dashboard.md).
"""

import json
import re
from pathlib import Path

import pytest
import yaml

from agent_obs.slo.definitions import SLO_CATALOG

REPO_ROOT = Path(__file__).resolve().parent.parent
DASHBOARD_PATH = REPO_ROOT / "infra" / "grafana" / "dashboards" / "slo.json"
CLICKHOUSE_DS_PATH = (
    REPO_ROOT / "infra" / "grafana" / "provisioning" / "datasources" / "clickhouse.yml"
)
PROMETHEUS_DS_PATH = (
    REPO_ROOT / "infra" / "grafana" / "provisioning" / "datasources" / "prometheus.yml"
)
README_PATH = REPO_ROOT / "docs" / "slo-dashboard.md"

CLICKHOUSE_DS_TYPE = "grafana-clickhouse-datasource"

#: Burn-rate windows the PP25 refresher publishes, per SLO, derived from the
#: catalog so this test never drifts from agent_obs/slo/definitions.py.
def _catalog_windows(slo_name: str) -> set[str]:
    slo = SLO_CATALOG[slo_name]
    return {spec for short, long, _threshold in slo.burn_rate_thresholds for spec in (short, long)}


@pytest.fixture
def dashboard_config():
    """Load the provisioned dashboard JSON."""
    return json.loads(DASHBOARD_PATH.read_text(encoding="utf-8"))


def _panels_flat(panels):
    """Flatten top-level panels plus panels nested in collapsed rows."""
    flat = list(panels)
    for panel in panels:
        flat.extend(panel.get("panels", []))
    return flat


def _panel(panels, panel_id):
    return next(p for p in _panels_flat(panels) if p["id"] == panel_id)


def _section(dashboard_config, slo_name):
    """Return (row_panel, nested_panels) for one SLO section."""
    row = next(
        p for p in dashboard_config["panels"]
        if p["type"] == "row" and p["title"] == f"SLO: {slo_name}"
    )
    return row, row["panels"]


def _section_panel(panels, kind, metric):
    """Find a panel of `kind` (gauge/timeseries/table) plotting `metric`."""
    for panel in panels:
        if panel["type"] != kind:
            continue
        for target in panel.get("targets", []):
            if metric in target.get("expr", target.get("rawSql", "")):
                return panel
    raise AssertionError(f"no {kind} panel plotting {metric!r} in section")


class TestSLODashboardStructure:
    """Test dashboard metadata and layout."""

    def test_dashboard_structure(self, dashboard_config):
        assert dashboard_config["uid"] == "slo"
        assert dashboard_config["title"] == "SLO"
        for pp in ("PP25", "PP28"):
            assert pp in dashboard_config["description"]
        assert dashboard_config["editable"] is False
        # SLO gauges recompute every 5 minutes; 1m refresh keeps them fresh
        # without hammering Prometheus.
        assert dashboard_config["refresh"] == "1m"

    def test_dashboard_has_correct_tags(self, dashboard_config):
        tags = dashboard_config["tags"]
        for tag in ("agent-obs", "slo", "pp25", "pp26", "pp27", "pp28"):
            assert tag in tags

    def test_dashboard_panels_have_descriptions(self, dashboard_config):
        # Row panels are layout-only containers, not visualizations.
        panels = [p for p in _panels_flat(dashboard_config["panels"]) if p["type"] != "row"]
        for panel in panels:
            assert "description" in panel, f"panel {panel['id']} has no description"
            assert len(panel["description"]) > 10

    def test_overall_health_gauge_exists(self, dashboard_config):
        panel = _panel(dashboard_config["panels"], 1)
        assert panel["type"] == "stat"
        assert panel["title"] == "SLO overall health"

    def test_overall_health_counts_exhausted_slos(self, dashboard_config):
        """The overview gauge counts SLOs at/below the PP27 soft threshold."""
        panel = _panel(dashboard_config["panels"], 1)
        expr = panel["targets"][0]["expr"]
        assert "slo_error_budget_remaining" in expr
        assert "count(" in expr
        # "Exhausted" = remaining <= 0.2 (the PP27 soft threshold).
        assert "<= 0.2" in expr

    def test_overall_health_green_yellow_red(self, dashboard_config):
        """green = all in budget, yellow = 1-2 at risk, red = more than 2."""
        panel = _panel(dashboard_config["panels"], 1)
        steps = panel["fieldConfig"]["defaults"]["thresholds"]["steps"]
        colors = [(step["color"], step["value"]) for step in steps]
        assert colors == [("green", None), ("yellow", 1), ("red", 3)]

        mappings = panel["fieldConfig"]["defaults"]["mappings"][0]["options"]
        assert mappings["0"]["color"] == "green"
        assert mappings["1"]["color"] == "yellow"
        assert mappings["2"]["color"] == "yellow"
        assert mappings["3"]["color"] == "red"
        assert mappings["4"]["color"] == "red"


class TestSLOSections:
    """One section per catalog SLO, synchronized with SLO_CATALOG."""

    @pytest.mark.parametrize("slo_name", sorted(SLO_CATALOG))
    def test_section_exists(self, dashboard_config, slo_name):
        row, panels = _section(dashboard_config, slo_name)
        assert row["type"] == "row"
        assert row["collapsed"] is True
        assert len(panels) == 4, "each SLO section has 4 panels"

    @pytest.mark.parametrize("slo_name", sorted(SLO_CATALOG))
    def test_section_panels(self, dashboard_config, slo_name):
        _, panels = _section(dashboard_config, slo_name)
        sli = _section_panel(panels, "gauge", "slo_actual_sli")
        burn = _section_panel(panels, "timeseries", "slo_burn_rate")
        budget = _section_panel(panels, "gauge", "slo_error_budget_remaining")
        # The degradation table is ClickHouse, not PromQL - find it directly.
        table = next(p for p in panels if p["type"] == "table")
        assert table["datasource"]["uid"] == "clickhouse"
        assert slo_name in sli["title"]
        assert slo_name in burn["title"]
        assert slo_name in budget["title"]

    @pytest.mark.parametrize("slo_name", sorted(SLO_CATALOG))
    def test_sli_gauge_threshold_matches_catalog_target(self, dashboard_config, slo_name):
        """The gauge turns green exactly at the catalog target (sync guard)."""
        _, panels = _section(dashboard_config, slo_name)
        sli = _section_panel(panels, "gauge", "slo_actual_sli")
        steps = sli["fieldConfig"]["defaults"]["thresholds"]["steps"]
        assert steps[0]["color"] == "red"
        assert steps[-1]["color"] == "green"
        assert steps[-1]["value"] == pytest.approx(SLO_CATALOG[slo_name].target)
        # The target itself is also queried, so the panel shows both series.
        exprs = " ".join(t["expr"] for t in sli["targets"])
        assert f'slo_target{{name="{slo_name}"}}' in exprs

    @pytest.mark.parametrize("slo_name", sorted(SLO_CATALOG))
    def test_budget_gauge_marks_soft_threshold(self, dashboard_config, slo_name):
        """The error-budget gauge goes red below the PP27 soft threshold."""
        _, panels = _section(dashboard_config, slo_name)
        budget = _section_panel(panels, "gauge", "slo_error_budget_remaining")
        steps = budget["fieldConfig"]["defaults"]["thresholds"]["steps"]
        assert steps[0]["color"] == "red"
        soft = next(s for s in steps if s["color"] == "orange")
        assert soft["value"] == pytest.approx(0.2)

    @pytest.mark.parametrize("slo_name", sorted(SLO_CATALOG))
    def test_burn_rate_windows_match_catalog(self, dashboard_config, slo_name):
        """Plot exactly the windows the refresher publishes - no short/long.

        PP25's `slo_burn_rate{name,window}` label holds the *duration*
        (5m/1h/6h/1d), not the short/long role; the roles map to durations in
        agent_obs.slo.burn_rate. A "short"/"long" label would never resolve.
        """
        _, panels = _section(dashboard_config, slo_name)
        burn = _section_panel(panels, "timeseries", "slo_burn_rate")
        windows = set()
        for target in burn["targets"]:
            expr = target["expr"]
            assert expr.startswith(f'slo_burn_rate{{name="{slo_name}",')
            match = re.search(r'window="([^"]+)"', expr)
            assert match, f"burn-rate expr has no window label: {expr}"
            windows.add(match.group(1))
        assert windows == _catalog_windows(slo_name)
        assert "short" not in windows and "long" not in windows

    @pytest.mark.parametrize("slo_name", sorted(SLO_CATALOG))
    def test_burn_rate_threshold_lines(self, dashboard_config, slo_name):
        """Threshold lines at the catalog's worst-case burn-rate thresholds."""
        _, panels = _section(dashboard_config, slo_name)
        burn = _section_panel(panels, "timeseries", "slo_burn_rate")
        steps = burn["fieldConfig"]["defaults"]["thresholds"]["steps"]
        values = [s["value"] for s in steps]
        worst = max(t for _s, _l, t in SLO_CATALOG[slo_name].burn_rate_thresholds)
        assert worst in values
        assert steps[-1]["color"] == "red"


class TestDegradationDrilldown:
    """Click on an SLO -> detail-view with the traces causing degradation."""

    @pytest.mark.parametrize("slo_name", sorted(SLO_CATALOG))
    def test_sli_gauge_links_to_degradation_table(self, dashboard_config, slo_name):
        _, panels = _section(dashboard_config, slo_name)
        table = next(p for p in panels if p["type"] == "table")
        for metric in ("slo_actual_sli", "slo_error_budget_remaining"):
            panel = _section_panel(panels, "gauge", metric)
            links = panel["fieldConfig"]["defaults"]["links"]
            assert links, f"{metric} gauge has no drill-down link"
            assert f"viewPanel={table['id']}" in links[0]["url"]

    @pytest.mark.parametrize("slo_name", sorted(SLO_CATALOG))
    def test_degradation_table_links_to_trace_viewer(self, dashboard_config, slo_name):
        _, panels = _section(dashboard_config, slo_name)
        table = next(p for p in panels if p["type"] == "table")
        overrides = table["fieldConfig"]["overrides"]
        trace_override = next(o for o in overrides if o["matcher"]["options"] == "trace_id")
        links = trace_override["properties"][0]["value"]
        assert "${var:trace_viewer_url}" in links[0]["url"]
        assert "trace_id=${__data.fields" in links[0]["url"]
        assert links[0]["targetBlank"] is True

    @pytest.mark.parametrize("slo_name", sorted(SLO_CATALOG))
    def test_degradation_table_respects_time_filter(self, dashboard_config, slo_name):
        _, panels = _section(dashboard_config, slo_name)
        table = next(p for p in panels if p["type"] == "table")
        raw_sql = table["targets"][0]["rawSql"]
        assert "$__timeFilter(" in raw_sql
        assert "LIMIT" in raw_sql

    def test_availability_table_queries_error_root_spans(self, dashboard_config):
        _, panels = _section(dashboard_config, "availability")
        table = next(p for p in panels if p["type"] == "table")
        raw_sql = table["targets"][0]["rawSql"]
        assert "spans_hot" in raw_sql
        assert "span_type = 'agent.loop'" in raw_sql
        assert "status = 'error'" in raw_sql

    def test_latency_table_queries_slow_root_spans(self, dashboard_config):
        _, panels = _section(dashboard_config, "latency_p95")
        table = next(p for p in panels if p["type"] == "table")
        raw_sql = table["targets"][0]["rawSql"]
        assert "spans_hot" in raw_sql
        assert "span_type = 'agent.loop'" in raw_sql
        # The SLI good threshold for latency_p95 is 3 seconds (SLO_CATALOG).
        assert "> 3.0" in raw_sql

    def test_quality_table_queries_eval_results(self, dashboard_config):
        _, panels = _section(dashboard_config, "quality")
        table = next(p for p in panels if p["type"] == "table")
        raw_sql = table["targets"][0]["rawSql"]
        assert "eval_results_hot" in raw_sql
        assert "faithfulness" in raw_sql
        # The SLI good threshold for quality is faithfulness > 0.7.
        assert "< 0.7" in raw_sql

    def test_cost_table_queries_trace_cost(self, dashboard_config):
        _, panels = _section(dashboard_config, "cost_p99")
        table = next(p for p in panels if p["type"] == "table")
        raw_sql = table["targets"][0]["rawSql"]
        assert "spans_hot" in raw_sql
        assert "sum(cost_usd)" in raw_sql
        # The SLI good threshold for cost_p99 is $0.05 per request.
        assert "> 0.05" in raw_sql


class TestDashboardDrilldown:
    """Trace-viewer wiring (same convention as the PP06 sampler dashboard)."""

    def test_dashboard_exposes_trace_viewer_variable(self, dashboard_config):
        variables = dashboard_config["templating"]["list"]
        trace_viewer = next(v for v in variables if v["name"] == "trace_viewer_url")
        assert trace_viewer["type"] == "textbox"
        assert trace_viewer["current"]["value"] == "http://localhost:3000"

    def test_dashboard_has_header_link_to_trace_viewer(self, dashboard_config):
        links = dashboard_config["links"]
        assert any("${var:trace_viewer_url}" in link["url"] for link in links)

    def test_burn_rate_panels_have_drilldown_links(self, dashboard_config):
        for slo_name in SLO_CATALOG:
            _, panels = _section(dashboard_config, slo_name)
            burn = _section_panel(panels, "timeseries", "slo_burn_rate")
            links = burn["fieldConfig"]["defaults"]["links"]
            assert links, f"{slo_name} burn-rate panel has no data link"
            assert "${var:trace_viewer_url}" in links[0]["url"]
            assert "${__value.time" in links[0]["url"]


class TestAnnotations:
    """Deploy markers and incident markers from the audit trail."""

    def _annotation(self, dashboard_config, name):
        return next(a for a in dashboard_config["annotations"]["list"] if a["name"] == name)

    def test_dashboard_has_three_annotations(self, dashboard_config):
        names = [a["name"] for a in dashboard_config["annotations"]["list"]]
        assert names == [
            "Deploy freezes",
            "Error budget state changes",
            "Incident mode (eval sampler)",
        ]

    @pytest.mark.parametrize(
        ("name", "action"),
        [
            ("Deploy freezes", "freeze_deploy"),
            ("Error budget state changes", "error_budget.state_change"),
            ("Incident mode (eval sampler)", "eval_sampler.mode_change"),
        ],
    )
    def test_annotation_reads_audit_trail(self, dashboard_config, name, action):
        annotation = self._annotation(dashboard_config, name)
        assert annotation["datasource"]["type"] == CLICKHOUSE_DS_TYPE
        assert annotation["datasource"]["uid"] == "clickhouse"
        raw_sql = annotation["target"]["rawSql"]
        assert "audit_events_hot" in raw_sql
        assert f"action = '{action}'" in raw_sql
        # The time picker must drive the annotation window.
        assert "$__timeFilter(timestamp)" in raw_sql
        # Grafana's standard annotation contract: `time` and `text` columns.
        assert "AS time" in raw_sql
        assert "AS text" in raw_sql

    def test_incident_annotation_filters_incident_mode(self, dashboard_config):
        """Only transitions INTO incident mode are incident markers."""
        annotation = self._annotation(dashboard_config, "Incident mode (eval sampler)")
        raw_sql = annotation["target"]["rawSql"]
        assert "JSONExtractString(resource, 'new_mode') = 'incident'" in raw_sql


class TestTimeRanges:
    """Ticket: time range picker 1h / 6h / 24h / 7d / 30d."""

    def test_dashboard_has_time_ranges(self, dashboard_config):
        assert dashboard_config["time"]["from"] == "now-24h"
        assert dashboard_config["time"]["to"] == "now"
        assert dashboard_config["timepicker"]["time_options"] == [
            "1h",
            "6h",
            "24h",
            "7d",
            "30d",
        ]


class TestDashboardInfrastructure:
    """Datasource provisioning the dashboard depends on."""

    def test_dashboard_datasource_uids_are_provisioned(self, dashboard_config):
        provisioned = set()
        for path in (CLICKHOUSE_DS_PATH, PROMETHEUS_DS_PATH):
            for ds in yaml.safe_load(path.read_text(encoding="utf-8"))["datasources"]:
                provisioned.add(ds["uid"])

        referenced = set()
        for annotation in dashboard_config["annotations"]["list"]:
            referenced.add(annotation["datasource"]["uid"])
        for panel in _panels_flat(dashboard_config["panels"]):
            for target in panel.get("targets", []):
                if "datasource" in target:
                    referenced.add(target["datasource"]["uid"])

        assert referenced <= provisioned, f"unprovisioned datasources: {referenced - provisioned}"

    def test_clickhouse_datasource_is_provisioned(self):
        datasources = yaml.safe_load(CLICKHOUSE_DS_PATH.read_text(encoding="utf-8"))["datasources"]
        ds = next(d for d in datasources if d["uid"] == "clickhouse")
        assert ds["type"] == CLICKHOUSE_DS_TYPE


class TestSLODashboardDocs:
    """Runbook README (DoD)."""

    def test_readme_exists(self):
        assert README_PATH.exists(), f"missing {README_PATH}"
        readme = README_PATH.read_text(encoding="utf-8")
        assert len(readme) > 500

    def test_readme_documents_all_slos(self):
        readme = README_PATH.read_text(encoding="utf-8")
        for slo_name in SLO_CATALOG:
            assert slo_name in readme

    def test_readme_documents_reading_guide(self):
        readme = README_PATH.read_text(encoding="utf-8")
        for term in ("burn rate", "error budget", "trace_viewer_url", "audit_events_hot"):
            assert term in readme

    def test_readme_documents_drilldown(self):
        readme = README_PATH.read_text(encoding="utf-8")
        assert "drill-down" in readme.lower() or "drilldown" in readme.lower()
        assert "$__timeFilter" in readme
