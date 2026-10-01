"""
Tests for compliance UI service (PC36).

Tests the FastAPI compliance dashboard, export functionality, and filtering capabilities.
"""

import pytest
import asyncio
from httpx import AsyncClient
from fastapi.testclient import TestClient
from unittest.mock import Mock, patch, AsyncMock
import tempfile
import os
from datetime import datetime, timedelta

from agent_obs.compliance.ui import app
from agent_obs.compliance.catalog import ComplianceRow
from agent_obs.compliance.gdpr_export import GDPRDataRow


class TestComplianceUI:
    """Test suite for compliance UI service"""

    def setup_method(self):
        """Setup test client"""
        self.client = TestClient(app)
        
        # Mock compliance data
        self.mock_compliance_rows = [
            ComplianceRow(
                agent_id="agent-1",
                tool="llm_call",
                field="user_message",
                pii_type="email",
                frequency=5
            ),
            ComplianceRow(
                agent_id="agent-2", 
                tool="tool_call",
                field="tool_input",
                pii_type="phone",
                frequency=3
            ),
            ComplianceRow(
                agent_id="agent-1",
                tool="llm_call", 
                field="llm_output",
                pii_type="inn",
                frequency=2
            )
        ]
        
        # Mock GDPR data
        self.mock_gdpr_rows = [
            GDPRDataRow(
                agent_id="agent-1",
                tool="llm_call",
                field="user_message", 
                pii_type="email",
                frequency=5,
                data_subject_category="employee",
                lawful_basis="legitimate_interest",
                retention_period="90d",
                recipient="internal"
            )
        ]

    @patch('agent_obs.compliance.ui.get_compliance_catalog')
    def test_health_check(self, mock_get_catalog):
        """Test health check endpoint"""
        response = self.client.get("/compliance/api/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        assert data["service"] == "compliance-ui"

    @patch('agent_obs.compliance.ui.get_compliance_catalog')
    @patch('agent_obs.compliance.ui.get_dashboard_summary')
    def test_dashboard_summary_api(self, mock_get_summary, mock_get_catalog):
        """Test dashboard summary API endpoint"""
        from agent_obs.compliance.ui import DashboardSummary
        
        # Mock catalog and summary
        mock_catalog = Mock()
        mock_get_catalog.return_value = mock_catalog
        mock_summary = DashboardSummary(
            total_pii_events=10,
            unique_agents=3,
            unique_tools=2,
            unique_pii_types=3,
            recent_events=self.mock_compliance_rows[:2],
            top_pii_types=[("email", 5), ("phone", 3)],
            top_agents=[("agent-1", 7), ("agent-2", 3)]
        )
        mock_get_summary.return_value = mock_summary
        
        # Test with basic auth
        response = self.client.get(
            "/compliance/api/summary",
            auth=("admin", "admin123")
        )
        assert response.status_code == 200
        data = response.json()
        assert data["total_pii_events"] == 10
        assert data["unique_agents"] == 3
        assert data["unique_tools"] == 2
        assert data["unique_pii_types"] == 3

    @patch('agent_obs.compliance.ui.get_compliance_catalog')
    def test_compliance_catalog_api(self, mock_get_catalog):
        """Test compliance catalog API endpoint with filtering"""
        # Mock catalog
        mock_catalog = Mock()
        mock_catalog.get_filtered_rows.return_value = self.mock_compliance_rows
        mock_get_catalog.return_value = mock_catalog
        
        # Test basic request
        response = self.client.get(
            "/compliance/api/catalog",
            auth=("admin", "admin123")
        )
        assert response.status_code == 200
        data = response.json()
        assert data["total_rows"] == 3
        assert len(data["rows"]) == 3
        # Check that filters are returned as empty by default
        assert all(v is None for v in data["filters"].values())
        
        # Test with filters
        response = self.client.get(
            "/compliance/api/catalog?agent_id=agent-1&pii_type=email",
            auth=("admin", "admin123")
        )
        assert response.status_code == 200
        data = response.json()
        assert data["filters"]["agent_id"] == "agent-1"
        assert data["filters"]["pii_type"] == "email"

    @patch('agent_obs.compliance.ui.get_compliance_catalog')
    @patch('agent_obs.compliance.ui.export_data_map')
    def test_export_csv(self, mock_export_data, mock_get_catalog):
        """Test CSV export functionality"""
        from io import StringIO
        
        # Mock catalog
        mock_catalog = Mock()
        mock_catalog.get_filtered_rows.return_value = self.mock_compliance_rows
        mock_get_catalog.return_value = mock_catalog
        
        # Mock export function
        mock_export_data.return_value = StringIO("agent_id,tool,field,pii_type,frequency\nagent-1,llm_call,user_message,email,5")
        
        # Test export
        response = self.client.get(
            "/compliance/export?fmt=csv",
            auth=("admin", "admin123")
        )
        assert response.status_code == 200
        assert response.headers["content-type"] == "text/csv"
        assert "attachment" in response.headers["content-disposition"]
        assert "gdpr_data_map" in response.headers["content-disposition"]

    @patch('agent_obs.compliance.ui.get_compliance_catalog')
    @patch('agent_obs.compliance.ui.export_data_map')
    def test_export_xlsx(self, mock_export_data, mock_get_catalog):
        """Test XLSX export functionality"""
        from io import BytesIO
        
        # Mock catalog
        mock_catalog = Mock()
        mock_catalog.get_filtered_rows.return_value = self.mock_compliance_rows
        mock_get_catalog.return_value = mock_catalog
        
        # Mock export function
        mock_export_data.return_value = BytesIO(b'PK\x03\x04')  # Mock XLSX content
        
        # Test export
        response = self.client.get(
            "/compliance/export?fmt=xlsx",
            auth=("admin", "admin123")
        )
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        assert "attachment" in response.headers["content-disposition"]

    @patch('agent_obs.compliance.ui.get_compliance_catalog')
    @patch('agent_obs.compliance.ui.export_data_map')
    def test_export_with_date_filter(self, mock_export_data, mock_get_catalog):
        """Test export with date filter"""
        from io import StringIO
        
        # Mock catalog
        mock_catalog = Mock()
        mock_catalog.get_filtered_rows.return_value = self.mock_compliance_rows
        mock_get_catalog.return_value = mock_catalog
        
        # Mock export function
        mock_export_data.return_value = StringIO("test data")
        
        # Test export with date filter
        response = self.client.get(
            "/compliance/export?fmt=csv&as_of=2024-01-15",
            auth=("admin", "admin123")
        )
        assert response.status_code == 200
        mock_export_data.assert_called_once()

    def test_export_invalid_date(self):
        """Test export with invalid date format"""
        response = self.client.get(
            "/compliance/export?fmt=csv&as_of=invalid-date",
            auth=("admin", "admin123")
        )
        assert response.status_code == 400
        assert "Invalid date format" in response.json()["detail"]

    def test_export_invalid_format(self):
        """Test export with invalid format"""
        response = self.client.get(
            "/compliance/export?fmt=json",
            auth=("admin", "admin123")
        )
        assert response.status_code == 422
        assert "pattern" in response.json()["detail"][0]["msg"]

    @patch('agent_obs.compliance.ui.get_compliance_catalog')
    @patch('agent_obs.compliance.ui.get_dashboard_summary')
    def test_dashboard_html_response(self, mock_get_summary, mock_get_catalog):
        """Test main dashboard HTML response"""
        from agent_obs.compliance.ui import DashboardSummary
        
        # Mock catalog and summary
        mock_catalog = Mock()
        mock_catalog.get_filtered_rows.return_value = self.mock_compliance_rows
        mock_get_catalog.return_value = mock_catalog
        
        mock_summary = DashboardSummary(
            total_pii_events=10,
            unique_agents=3,
            unique_tools=2,
            unique_pii_types=3,
            recent_events=self.mock_compliance_rows[:2],
            top_pii_types=[("email", 5), ("phone", 3)],
            top_agents=[("agent-1", 7), ("agent-2", 3)]
        )
        mock_get_summary.return_value = mock_summary
        
        # Test dashboard response
        response = self.client.get(
            "/",
            auth=("admin", "admin123")
        )
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]
        assert "Compliance Data Map" in response.text
        assert "Total PII Events" in response.text
        assert "agent-1" in response.text

    @patch('agent_obs.compliance.ui.get_compliance_catalog')
    def test_dashboard_with_filters(self, mock_get_catalog):
        """Test dashboard with applied filters"""
        # Mock catalog
        mock_catalog = Mock()
        mock_catalog.get_filtered_rows.return_value = [self.mock_compliance_rows[0]]  # Only first row
        mock_get_catalog.return_value = mock_catalog
        
        # Test with filters
        response = self.client.get(
            "/?agent_id=agent-1&tool=llm_call",
            auth=("admin", "admin123")
        )
        assert response.status_code == 200
        assert "agent-1" in response.text

    def test_invalid_auth(self):
        """Test authentication failure"""
        response = self.client.get(
            "/compliance/api/summary",
            auth=("wrong", "credentials")
        )
        assert response.status_code == 401

    def test_no_auth(self):
        """Test request without authentication"""
        response = self.client.get("/")
        assert response.status_code == 401

    @patch('agent_obs.compliance.ui.get_compliance_catalog')
    def test_empty_compliance_data(self, mock_get_catalog):
        """Test UI with empty compliance data"""
        # Mock empty catalog
        mock_catalog = Mock()
        mock_catalog.get_filtered_rows.return_value = []
        mock_get_catalog.return_value = mock_catalog
        
        response = self.client.get(
            "/",
            auth=("admin", "admin123")
        )
        assert response.status_code == 200
        assert "Total PII Events" in response.text
        # Should show 0 for empty data

    @patch('agent_obs.compliance.ui.get_compliance_catalog')
    def test_pagination(self, mock_get_catalog):
        """Test pagination functionality"""
        # Mock catalog with many rows
        mock_catalog = Mock()
        mock_catalog.get_filtered_rows.return_value = self.mock_compliance_rows * 10  # 30 rows
        mock_get_catalog.return_value = mock_catalog
        
        # Test page 1
        response = self.client.get(
            "/?page=1&page_size=10",
            auth=("admin", "admin123")
        )
        assert response.status_code == 200
        
        # Test page 2
        response = self.client.get(
            "/?page=2&page_size=10", 
            auth=("admin", "admin123")
        )
        assert response.status_code == 200

    @patch('agent_obs.compliance.ui.get_compliance_catalog')
    def test_dashboard_summary_helper(self, mock_get_catalog):
        """Test dashboard summary helper function"""
        from agent_obs.compliance.ui import get_dashboard_summary
        
        # Mock catalog
        mock_catalog = Mock()
        mock_catalog.get_filtered_rows.return_value = self.mock_compliance_rows
        mock_get_catalog.return_value = mock_catalog
        
        # Test summary generation
        summary = asyncio.run(get_dashboard_summary(mock_catalog))
        
        assert summary.total_pii_events == 3
        assert summary.unique_agents == 2
        assert summary.unique_tools == 2
        assert summary.unique_pii_types == 3
        assert len(summary.recent_events) <= 10
        assert len(summary.top_pii_types) <= 5
        assert len(summary.top_agents) <= 5


class TestComplianceUIIntegration:
    """Integration tests for compliance UI with real data"""

    def setup_method(self):
        """Setup for integration tests"""
        self.client = TestClient(app)
        
        # Mock compliance data
        self.mock_compliance_rows = [
            ComplianceRow(
                agent_id="agent-1",
                tool="llm_call",
                field="user_message",
                pii_type="email",
                frequency=5
            ),
            ComplianceRow(
                agent_id="agent-2", 
                tool="tool_call",
                field="tool_input",
                pii_type="phone",
                frequency=3
            ),
            ComplianceRow(
                agent_id="agent-1",
                tool="llm_call", 
                field="llm_output",
                pii_type="inn",
                frequency=2
            )
        ]

    @patch('agent_obs.compliance.ui.get_compliance_catalog')
    def test_end_to_end_workflow(self, mock_get_catalog):
        """Test complete workflow from dashboard to export"""
        from agent_obs.compliance.ui import DashboardSummary
        
        # Mock catalog and summary
        mock_catalog = Mock()
        mock_catalog.get_filtered_rows.return_value = self.mock_compliance_rows
        mock_get_catalog.return_value = mock_catalog
        
        mock_summary = DashboardSummary(
            total_pii_events=10,
            unique_agents=3,
            unique_tools=2,
            unique_pii_types=3,
            recent_events=self.mock_compliance_rows[:2],
            top_pii_types=[("email", 5), ("phone", 3)],
            top_agents=[("agent-1", 7), ("agent-2", 3)]
        )
        
        with patch('agent_obs.compliance.ui.get_dashboard_summary', return_value=mock_summary):
            with patch('agent_obs.compliance.ui.export_data_map') as mock_export:
                from io import StringIO
                mock_export.return_value = StringIO("agent_id,tool,field,pii_type,frequency\nagent-1,llm_call,user_message,email,5")
                
                # 1. View dashboard
                dashboard_response = self.client.get("/", auth=("admin", "admin123"))
                assert dashboard_response.status_code == 200
                
                # 2. Get filtered data via API
                api_response = self.client.get(
                    "/compliance/api/catalog?agent_id=agent-1",
                    auth=("admin", "admin123")
                )
                assert api_response.status_code == 200
                
                # 3. Export data
                export_response = self.client.get(
                    "/compliance/export?fmt=csv&agent_id=agent-1",
                    auth=("admin", "admin123")
                )
                assert export_response.status_code == 200


if __name__ == "__main__":
    pytest.main([__file__, "-v"])