"""PC34: Tests for compliance catalog aggregation cron job."""

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Add repo root to Python path for imports
REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from agent_obs.compliance.aggregate import (
    ComplianceAggregationResult,
    aggregate_compliance_catalog,
    compliance_catalog_new_rows_24h,
    compliance_catalog_rows_cleaned_total,
    compliance_catalog_rows_total,
)
from agent_obs.storage.warm import WarmStore


class TestComplianceAggregation:
    """Test compliance catalog aggregation logic."""

    @pytest.fixture
    def mock_warm_store(self):
        """Create a mock warm store."""
        store = MagicMock(spec=WarmStore)
        store.get_all_compliance_catalog = AsyncMock()
        store.delete_compliance_catalog_stale = AsyncMock()
        store.refresh_compliance_views = AsyncMock()
        return store

    @pytest.mark.asyncio
    async def test_aggregate_compliance_catalog_basic(self, mock_warm_store):
        """Test basic aggregation with no stale rows."""
        # Mock data
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone.utc)
        mock_rows = [
            {"id": 1, "agent_id": "agent1", "tool": "tool1", "field": "field1", "pii_type": "email", "frequency": 5, "first_seen": now - timedelta(hours=12), "last_seen": now - timedelta(hours=12)},
            {"id": 2, "agent_id": "agent1", "tool": "tool1", "field": "field2", "pii_type": "phone", "frequency": 3, "first_seen": now - timedelta(hours=6), "last_seen": now - timedelta(hours=6)},
        ]
        mock_warm_store.get_all_compliance_catalog.return_value = mock_rows
        
        # Run aggregation
        result = await aggregate_compliance_catalog(mock_warm_store, dry_run=True)
        
        # Verify results
        assert result.affected == 2
        assert result.errors == 0
        assert result.new_rows_24h == 2  # Both rows are recent
        
        # Verify calls
        mock_warm_store.get_all_compliance_catalog.assert_called_once()
        mock_warm_store.delete_compliance_catalog_stale.assert_not_called()
        mock_warm_store.refresh_compliance_views.assert_not_called()

    @pytest.mark.asyncio
    async def test_aggregate_compliance_catalog_clean_stale(self, mock_warm_store):
        """Test cleaning stale rows."""
        from datetime import datetime, timedelta, timezone
        
        # Mock data with stale rows (older than 90 days)
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(days=90)
        recent_cutoff = now - timedelta(days=1)
        
        mock_rows = [
            {"id": 1, "agent_id": "agent1", "tool": "tool1", "field": "field1", "pii_type": "email", "frequency": 5, "first_seen": now - timedelta(days=100), "last_seen": cutoff - timedelta(days=1)},  # Stale
            {"id": 2, "agent_id": "agent1", "tool": "tool1", "field": "field2", "pii_type": "phone", "frequency": 3, "first_seen": now - timedelta(hours=12), "last_seen": now - timedelta(hours=12)},  # Recent
        ]
        mock_warm_store.get_all_compliance_catalog.return_value = mock_rows
        
        # Run aggregation (not dry run)
        result = await aggregate_compliance_catalog(mock_warm_store, dry_run=False)
        
        # Verify results
        assert result.affected == 2
        assert result.errors == 1  # One stale row cleaned
        assert result.new_rows_24h == 1  # Only one row is recent
        
        # Verify calls
        mock_warm_store.get_all_compliance_catalog.assert_called_once()
        mock_warm_store.delete_compliance_catalog_stale.assert_called_once_with([mock_rows[0]])
        mock_warm_store.refresh_compliance_views.assert_called_once()

    @pytest.mark.asyncio
    async def test_aggregate_compliance_catalog_dry_run(self, mock_warm_store):
        """Test dry run mode doesn't modify database."""
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone.utc)
        mock_rows = [
            {"id": 1, "agent_id": "agent1", "tool": "tool1", "field": "field1", "pii_type": "email", "frequency": 5, "first_seen": now - timedelta(days=10), "last_seen": now - timedelta(days=10)},
        ]
        mock_warm_store.get_all_compliance_catalog.return_value = mock_rows
        
        # Run aggregation in dry run mode
        result = await aggregate_compliance_catalog(mock_warm_store, dry_run=True)
        
        # Verify results
        assert result.affected == 1
        assert result.errors == 0  # No cleaning in dry run
        assert result.new_rows_24h == 0  # Old row, not recent
        
        # Verify no database modifications
        mock_warm_store.delete_compliance_catalog_stale.assert_not_called()
        mock_warm_store.refresh_compliance_views.assert_not_called()

    @pytest.mark.asyncio
    async def test_aggregate_compliance_catalog_metrics(self, mock_warm_store):
        """Test metrics are updated correctly."""
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone.utc)
        mock_rows = [
            {"id": 1, "agent_id": "agent1", "tool": "tool1", "field": "field1", "pii_type": "email", "frequency": 5, "first_seen": now - timedelta(hours=12), "last_seen": now - timedelta(hours=12)},
        ]
        mock_warm_store.get_all_compliance_catalog.return_value = mock_rows
        
        # Reset metrics before test
        compliance_catalog_rows_total._value._value = 0
        compliance_catalog_rows_cleaned_total._value._value = 0
        compliance_catalog_new_rows_24h._value._value = 0
        
        # Run aggregation
        await aggregate_compliance_catalog(mock_warm_store, dry_run=False)
        
        # Verify metrics
        assert compliance_catalog_rows_total._value._value == 1
        assert compliance_catalog_rows_cleaned_total._value._value == 0
        assert compliance_catalog_new_rows_24h._value._value == 1

    @pytest.mark.asyncio
    async def test_aggregate_compliance_catalog_empty_catalog(self, mock_warm_store):
        """Test with empty compliance catalog."""
        mock_warm_store.get_all_compliance_catalog.return_value = []
        
        result = await aggregate_compliance_catalog(mock_warm_store, dry_run=True)
        
        assert result.affected == 0
        assert result.errors == 0
        assert result.new_rows_24h == 0
        
        # Views should not be refreshed in dry run mode
        mock_warm_store.refresh_compliance_views.assert_not_called()


class TestComplianceAggregationResult:
    """Test ComplianceAggregationResult dataclass."""

    def test_result_creation(self):
        """Test result dataclass creation."""
        result = ComplianceAggregationResult(
            affected=10,
            errors=2,
            new_rows_24h=8
        )
        
        assert result.affected == 10
        assert result.errors == 2
        assert result.new_rows_24h == 8


class TestComplianceAggregationIntegration:
    """Integration tests with scheduler job wrapper."""

    @pytest.mark.asyncio
    async def test_compliance_aggregation_job_wrapper(self):
        """Test the job wrapper function."""
        with patch('agent_obs.compliance.aggregate.aggregate_compliance_catalog') as mock_agg, \
             patch('agent_obs.storage.maintenance.build_warm_store') as mock_build:
            
            # Setup mocks
            mock_store = MagicMock()
            mock_store.close = AsyncMock()
            mock_build.return_value = mock_store
            mock_result = ComplianceAggregationResult(affected=5, errors=1, new_rows_24h=4)
            mock_agg.return_value = mock_result
            
            # Import and run the job wrapper
            from scripts.cron.scheduler import _compliance_aggregation_job
            result = await _compliance_aggregation_job()
            
            # Verify that aggregate_compliance_catalog was called with correct args
            mock_agg.assert_called_once()
            mock_build.assert_called_once()
            
            # The test passes if no exception is raised
            assert True


class TestComplianceAggressionInfra:
    """Test infrastructure configuration."""

    def test_env_vars_in_example(self):
        """Test that required env vars are in .env.example."""
        env_file = REPO_ROOT / "infra" / ".env.example"
        content = env_file.read_text(encoding='utf-8')
        
        assert "AGENT_OBS_CRON_COMPLIANCE_SPEC=" in content
        assert "AGENT_OBS_CRON_COMPLIANCE_RETENTION_DAYS=" in content

    def test_docker_compose_env_vars(self):
        """Test that required env vars are in docker-compose.yml."""
        compose_file = REPO_ROOT / "infra" / "docker-compose.yml"
        content = compose_file.read_text()
        
        assert "AGENT_OBS_CRON_COMPLIANCE_SPEC:" in content
        assert "AGENT_OBS_CRON_COMPLIANCE_RETENTION_DAYS:" in content

    def test_prometheus_rules_include_compliance_alerts(self):
        """Test that compliance alerts are in prometheus rules."""
        rules_file = REPO_ROOT / "infra" / "prometheus-rules.yml"
        content = rules_file.read_text()
        
        assert "ComplianceCatalogAggregationStale" in content
        assert "ComplianceCatalogZeroActivity" in content
        assert "aggregate_compliance_catalog" in content
        assert "agent_obs_compliance_catalog_new_rows_24h" in content


class TestComplianceAggregationScheduler:
    """Test scheduler integration."""

    def test_job_registration(self):
        """Test that compliance job is registered in scheduler."""
        # This test verifies the job is in build_jobs()
        # Import here to avoid import issues during test discovery
        from scripts.cron.scheduler import build_jobs
        
        jobs = build_jobs()
        job_names = [job.name for job in jobs]
        
        assert "aggregate_compliance_catalog" in job_names
        
        # Find the compliance job
        compliance_job = next(job for job in jobs if job.name == "aggregate_compliance_catalog")
        assert compliance_job.interval_seconds == 86400  # 24 hours
        assert compliance_job.spec == "0 2 * * *"  # 02:00 UTC

    def test_daily_job_count_updated(self):
        """Test that daily job count is correct after adding compliance job."""
        from scripts.cron.scheduler import build_jobs
        
        jobs = build_jobs()
        daily_specs = []
        
        for job in jobs:
            if job.interval_seconds == 86400:  # Daily jobs
                daily_specs.append(job.spec)
        
        # Should have 4 daily jobs now (was 3 before adding compliance)
        assert len(daily_specs) == 4
        
        # Check that compliance spec is unique and doesn't conflict
        assert "0 2 * * *" in daily_specs
        assert len(set(daily_specs)) == len(daily_specs)  # All specs unique

    def test_compliance_spec_is_unique(self):
        """Test that compliance cron spec doesn't conflict with existing daily jobs."""
        from scripts.cron.scheduler import build_jobs
        
        jobs = build_jobs()
        daily_specs = []
        
        for job in jobs:
            if job.interval_seconds == 86400:  # Daily jobs
                daily_specs.append(job.spec)
        
        # Compliance spec should be different from existing daily specs
        existing_daily_specs = ["17 3 * * *", "47 3 * * *", "0 3 * * *"]  # From research
        compliance_spec = "0 2 * * *"
        
        assert compliance_spec not in existing_daily_specs
        assert compliance_spec in daily_specs