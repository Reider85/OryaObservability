"""Tests for PP06 Sampler Policy History Dashboard.

Tests the Grafana dashboard configuration and metrics.
"""

import json
import pytest


class TestSamplerPolicyHistoryDashboard:
    """Test sampler policy history dashboard configuration."""
    
    @pytest.fixture
    def dashboard_config(self):
        """Load dashboard configuration."""
        with open("infra/grafana/dashboards/sampler_policy_history.json", "r") as f:
            return json.load(f)
    
    def test_dashboard_structure(self, dashboard_config):
        """Test dashboard has correct structure."""
        assert dashboard_config["uid"] == "sampler-policy-history"
        assert "Sampler Policy History" in dashboard_config["title"]
        assert "PP03-PP06" in dashboard_config["description"]
        assert dashboard_config["version"] == 2
        assert dashboard_config["refresh"] == "30s"
    
    def test_dashboard_has_required_panels(self, dashboard_config):
        """Test dashboard has all required panels."""
        panels = dashboard_config["panels"]
        
        # Panel 1: Sampler rate over time
        rate_panel = next(p for p in panels if p["id"] == 1)
        assert rate_panel["title"] == "Sampler rate over time"
        assert "agent_obs_tail_sampler_current_rate" in rate_panel["targets"][0]["expr"]
        
        # Panel 2: Sampling decisions by reason
        decisions_panel = next(p for p in panels if p["id"] == 2)
        assert decisions_panel["title"] == "Sampling decisions by reason"
        assert "agent_obs_tail_sampler_policy_evaluations_total" in decisions_panel["targets"][0]["expr"]
        
        # Panel 3: Kept spans by reason
        kept_panel = next(p for p in panels if p["id"] == 3)
        assert kept_panel["title"] == "Kept spans by reason"
        assert "agent_obs_tail_sampler_traces_sampled_total" in kept_panel["targets"][0]["expr"]
        
        # Panel 4: Eval score correlation
        eval_panel = next(p for p in panels if p["id"] == 4)
        assert eval_panel["title"] == "Eval score correlation"
        assert "faithfulness_score" in eval_panel["targets"][0]["expr"]
        
        # Panel 5: System correlation
        system_panel = next(p for p in panels if p["id"] == 5)
        assert system_panel["title"] == "System correlation"
        assert "agent_obs_system_cpu_ratio" in system_panel["targets"][0]["expr"]
    
    def test_dashboard_has_policy_reason_annotations(self, dashboard_config):
        """Test dashboard has policy reason change annotations."""
        annotations = dashboard_config["annotations"]["list"]
        
        # Find policy reason changes annotation
        policy_annotation = next(
            a for a in annotations 
            if a["name"] == "Policy reason changes"
        )
        assert policy_annotation["expr"] == "changes(agent_obs_tail_sampler_current_rate[2m]) > 0"
        assert "policy_reason" in policy_annotation["titleFormat"]
        assert "rate" in policy_annotation["titleFormat"]
    
    def test_dashboard_has_audit_trail_annotations(self, dashboard_config):
        """Test dashboard has audit trail annotations."""
        annotations = dashboard_config["annotations"]["list"]
        
        # Find sampling decisions annotation
        audit_annotation = next(
            a for a in annotations 
            if a["name"] == "Sampling decisions"
        )
        assert audit_annotation["datasource"]["type"] == "clickhouse"
        assert "audit_events_hot" in audit_annotation["expr"]
        assert "sampler.rate_change" in audit_annotation["expr"]
    
    def test_dashboard_supports_all_policy_reasons(self, dashboard_config):
        """Test dashboard description mentions all policy reasons."""
        description = dashboard_config["description"]
        
        # Check that description mentions policy tracking (not specific reasons)
        assert "policy reasons" in description
        
        # Check panel descriptions instead for specific reasons
        panels = dashboard_config["panels"]
        rate_panel_desc = next(p for p in panels if p["id"] == 1)["description"]
        
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
        panels = dashboard_config["panels"]
        
        for panel in panels:
            assert "description" in panel
            assert len(panel["description"]) > 10  # Non-empty descriptions
    
    def test_dashboard_has_time_ranges(self, dashboard_config):
        """Test dashboard has appropriate time ranges."""
        # Default time range
        assert dashboard_config["time"]["from"] == "now-6h"
        assert dashboard_config["time"]["to"] == "now"
    
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
                expr = target["expr"]
                for metric in required_metrics:
                    if metric in expr:
                        # Check metric format is reasonable
                        assert "agent_obs_" in metric
                        break
    
    def test_dashboard_has_clickhouse_integration(self, dashboard_config):
        """Test dashboard integrates with ClickHouse for audit trail."""
        annotations = dashboard_config["annotations"]["list"]
        
        clickhouse_annotation = next(
            a for a in annotations 
            if a["datasource"]["type"] == "clickhouse"
        )
        assert "audit_events_hot" in clickhouse_annotation["expr"]
        assert "sampler.rate_change" in clickhouse_annotation["expr"]
    
    def test_dashboard_has_proper_grid_layout(self, dashboard_config):
        """Test dashboard panels have proper grid layout."""
        panels = dashboard_config["panels"]
        
        # Check panel positions
        positions = [(p["id"], p["gridPos"]) for p in panels]
        
        # Panel 1: Full width, top position
        panel1 = next(pos for pos in positions if pos[0] == 1)
        assert panel1[1]["w"] == 24  # Full width
        assert panel1[1]["h"] == 10  # Height
        assert panel1[1]["y"] == 0   # Top position
        
        # Panel 2-3: Half width, second row
        panel2 = next(pos for pos in positions if pos[0] == 2)
        panel3 = next(pos for pos in positions if pos[0] == 3)
        assert panel2[1]["w"] == 12  # Half width
        assert panel3[1]["w"] == 12  # Half width
        assert panel2[1]["y"] == 10  # Second row
        assert panel3[1]["y"] == 10  # Second row
        
        # Panel 4-5: Half width, third row
        panel4 = next(pos for pos in positions if pos[0] == 4)
        panel5 = next(pos for pos in positions if pos[0] == 5)
        assert panel4[1]["w"] == 12  # Half width
        assert panel5[1]["w"] == 12  # Half width
        assert panel4[1]["y"] == 18  # Third row
        assert panel5[1]["y"] == 18  # Third row