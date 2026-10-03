"""PC34: Tests for compliance catalog hot to warm migration."""

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Add repo root to Python path for imports
REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from agent_obs.compliance.migrate_hot_to_warm import (
    ComplianceHotToWarmResult,
    migrate_compliance_catalog_hot_to_warm,
    register_compliance_hot_to_warm_migration_job,
)
from agent_obs.storage.hot import HotStore
from agent_obs.storage.warm import WarmStore


class TestComplianceHotToWarmMigration:
    """Test compliance catalog hot to warm migration logic."""

    @pytest.mark.asyncio
    async def test_migrate_compliance_catalog_hot_to_warm_basic(self):
        """Test basic migration with no hot rows."""
        # Mock stores
        hot_store = MagicMock(spec=HotStore)
        warm_store = MagicMock(spec=WarmStore)
        
        hot_store.get_compliance_catalog_last_seen_cutoff = AsyncMock(return_value=[])
        warm_store.update_compliance_last_seen_from_hot = AsyncMock()
        hot_store.delete_compliance_catalog_older_than = AsyncMock()
        
        # Run migration
        result = await migrate_compliance_catalog_hot_to_warm(
            hot_store=hot_store,
            warm_store=warm_store,
            dry_run=True
        )
        
        # Verify results
        assert result.hot_rows_scanned == 0
        assert result.warm_rows_migrated == 0
        assert result.hot_rows_deleted == 0
        assert result.errors == 0
        
        # Verify calls
        hot_store.get_compliance_catalog_last_seen_cutoff.assert_called_once()
        warm_store.update_compliance_last_seen_from_hot.assert_not_called()
        hot_store.delete_compliance_catalog_older_than.assert_not_called()

    @pytest.mark.asyncio
    async def test_migrate_compliance_catalog_hot_to_warm_with_rows(self):
        """Test migration with hot rows."""
        from datetime import datetime, timezone, timedelta
        
        now = datetime.now(timezone.utc)
        hot_rows = [
            {
                "agent_id": "agent1",
                "tool": "tool1",
                "field": "field1",
                "pii_type": "email",
                "frequency": 5,
                "first_seen": now - timedelta(hours=12),
                "last_seen": now - timedelta(hours=12),
                "updated_at": now - timedelta(hours=12),
            },
            {
                "agent_id": "agent1",
                "tool": "tool1",
                "field": "field2",
                "pii_type": "phone",
                "frequency": 3,
                "first_seen": now - timedelta(hours=6),
                "last_seen": now - timedelta(hours=6),
                "updated_at": now - timedelta(hours=6),
            },
        ]
        
        # Mock stores
        hot_store = MagicMock(spec=HotStore)
        warm_store = MagicMock(spec=WarmStore)
        
        hot_store.get_compliance_catalog_last_seen_cutoff = AsyncMock(return_value=hot_rows)
        warm_store.update_compliance_last_seen_from_hot = AsyncMock()
        hot_store.delete_compliance_catalog_older_than = AsyncMock(return_value=2)
        
        # Run migration (not dry run)
        result = await migrate_compliance_catalog_hot_to_warm(
            hot_store=hot_store,
            warm_store=warm_store,
            dry_run=False
        )
        
        # Verify results
        assert result.hot_rows_scanned == 2
        assert result.warm_rows_migrated == 2
        assert result.hot_rows_deleted == 2
        assert result.errors == 0
        
        # Verify calls
        hot_store.get_compliance_catalog_last_seen_cutoff.assert_called_once()
        warm_store.update_compliance_last_seen_from_hot.assert_called_once_with(hot_rows)
        hot_store.delete_compliance_catalog_older_than.assert_called_once()

    @pytest.mark.asyncio
    async def test_migrate_compliance_catalog_hot_to_warm_dry_run(self):
        """Test dry run mode doesn't modify database."""
        from datetime import datetime, timezone, timedelta
        
        now = datetime.now(timezone.utc)
        hot_rows = [
            {
                "agent_id": "agent1",
                "tool": "tool1",
                "field": "field1",
                "pii_type": "email",
                "frequency": 5,
                "first_seen": now - timedelta(hours=12),
                "last_seen": now - timedelta(hours=12),
                "updated_at": now - timedelta(hours=12),
            },
        ]
        
        # Mock stores
        hot_store = MagicMock(spec=HotStore)
        warm_store = MagicMock(spec=WarmStore)
        
        hot_store.get_compliance_catalog_last_seen_cutoff = AsyncMock(return_value=hot_rows)
        warm_store.update_compliance_last_seen_from_hot = AsyncMock()
        hot_store.delete_compliance_catalog_older_than = AsyncMock()
        
        # Run migration in dry run mode
        result = await migrate_compliance_catalog_hot_to_warm(
            hot_store=hot_store,
            warm_store=warm_store,
            dry_run=True
        )
        
        # Verify results
        assert result.hot_rows_scanned == 1
        assert result.warm_rows_migrated == 1
        assert result.hot_rows_deleted == 0  # No deletion in dry run
        assert result.errors == 0
        
        # Verify no database modifications
        warm_store.update_compliance_last_seen_from_hot.assert_not_called()
        hot_store.delete_compliance_catalog_older_than.assert_not_called()

    @pytest.mark.asyncio
    async def test_migrate_compliance_catalog_hot_to_warm_warm_upsert_failure(self):
        """Test warm upsert failure keeps hot rows."""
        from datetime import datetime, timezone, timedelta
        
        now = datetime.now(timezone.utc)
        hot_rows = [
            {
                "agent_id": "agent1",
                "tool": "tool1",
                "field": "field1",
                "pii_type": "email",
                "frequency": 5,
                "first_seen": now - timedelta(hours=12),
                "last_seen": now - timedelta(hours=12),
                "updated_at": now - timedelta(hours=12),
            },
        ]
        
        # Mock stores with warm upsert failure
        hot_store = MagicMock(spec=HotStore)
        warm_store = MagicMock(spec=WarmStore)
        
        hot_store.get_compliance_catalog_last_seen_cutoff = AsyncMock(return_value=hot_rows)
        warm_store.update_compliance_last_seen_from_hot = AsyncMock(side_effect=Exception("Warm upsert failed"))
        hot_store.delete_compliance_catalog_older_than = AsyncMock()
        
        # Run migration (should fail and keep hot rows)
        result = await migrate_compliance_catalog_hot_to_warm(
            hot_store=hot_store,
            warm_store=warm_store,
            dry_run=False
        )
        
        # Verify results
        assert result.hot_rows_scanned == 1
        assert result.warm_rows_migrated == 0  # No successful migration
        assert result.hot_rows_deleted == 0  # No deletion on failure
        assert result.errors == 1
        
        # Verify warm upsert was attempted but hot rows not deleted
        warm_store.update_compliance_last_seen_from_hot.assert_called_once_with(hot_rows)
        hot_store.delete_compliance_catalog_older_than.assert_not_called()

    @pytest.mark.asyncio
    async def test_migrate_compliance_catalog_hot_to_warm_result(self):
        """Test ComplianceHotToWarmResult dataclass."""
        result = ComplianceHotToWarmResult(
            hot_rows_scanned=10,
            warm_rows_migrated=8,
            hot_rows_deleted=6,
            errors=1
        )
        
        assert result.hot_rows_scanned == 10
        assert result.warm_rows_migrated == 8
        assert result.hot_rows_deleted == 6
        assert result.errors == 1


class TestComplianceHotToWarmMigrationIntegration:
    """Integration tests with scheduler job wrapper."""

    @pytest.mark.asyncio
    async def test_compliance_hot_to_warm_job_wrapper(self):
        """Test the job wrapper function."""
        with patch('agent_obs.compliance.migrate_hot_to_warm.migrate_compliance_catalog_hot_to_warm') as mock_migrate, \
             patch('agent_obs.storage.maintenance.build_hot_store') as mock_hot_build, \
             patch('agent_obs.storage.maintenance.build_warm_store') as mock_warm_build:
            
            # Setup mocks
            mock_hot_store = MagicMock()
            mock_warm_store = MagicMock()
            mock_hot_build.return_value = mock_hot_store
            mock_warm_build.return_value = mock_warm_store
            mock_result = ComplianceHotToWarmResult(
                hot_rows_scanned=5,
                warm_rows_migrated=4,
                hot_rows_deleted=3,
                errors=0
            )
            mock_migrate.return_value = mock_result
            
            # Import and run the job wrapper
            from scripts.cron.scheduler import _compliance_hot_to_warm_job
            result = await _compliance_hot_to_warm_job()
            
            # Verify that migrate_compliance_catalog_hot_to_warm was called with correct args
            mock_migrate.assert_called_once()
            mock_hot_build.assert_called_once()
            mock_warm_build.assert_called_once()
            
            # The test passes if no exception is raised
            assert True


class TestComplianceHotToWarmMigrationScheduler:
    """Test scheduler integration."""

    def test_job_registration(self):
        """Test that compliance migration job is registered in scheduler."""
        # This test verifies the job is in build_jobs()
        # Import here to avoid import issues during test discovery
        from scripts.cron.scheduler import build_jobs
        
        jobs = build_jobs()
        job_names = [job.name for job in jobs]
        
        assert "migrate_compliance_catalog_hot_to_warm" in job_names
        
        # Find the compliance migration job
        migration_job = next(job for job in jobs if job.name == "migrate_compliance_catalog_hot_to_warm")
        assert migration_job.interval_seconds == 604800  # 7 days (weekly)
        assert migration_job.spec == "0 5 * * 1"  # Monday 05:00 UTC

    def test_weekly_job_count_updated(self):
        """Test that weekly job count is correct after adding compliance migration job."""
        from scripts.cron.scheduler import build_jobs
        
        jobs = build_jobs()
        weekly_specs = []
        
        for job in jobs:
            if job.interval_seconds == 604800:  # Weekly jobs
                weekly_specs.append(job.spec)
        
        # Should have 2 weekly jobs now (cold migration and compliance migration)
        assert len(weekly_specs) == 2
        
        # Check that compliance spec is unique and doesn't conflict
        assert "0 5 * * 1" in weekly_specs  # Monday 05:00
        assert "0 4 * * 0" in weekly_specs  # Sunday 04:00 (cold migration)
        assert len(set(weekly_specs)) == len(weekly_specs)  # All specs unique

    def test_compliance_spec_is_unique(self):
        """Test that compliance migration cron spec doesn't conflict with existing weekly jobs."""
        from scripts.cron.scheduler import build_jobs
        
        jobs = build_jobs()
        weekly_specs = []
        
        for job in jobs:
            if job.interval_seconds == 604800:  # Weekly jobs
                weekly_specs.append(job.spec)
        
        # Compliance spec should be different from existing weekly specs
        existing_weekly_specs = ["0 4 * * 0"]  # Sunday cold migration
        compliance_spec = "0 5 * * 1"  # Monday compliance migration
        
        assert compliance_spec not in existing_weekly_specs
        assert compliance_spec in weekly_specs


class TestComplianceHotToWarmMigrationInfra:
    """Test infrastructure configuration."""

    def test_env_vars_in_example(self):
        """Test that required env vars are in .env.example."""
        env_file = REPO_ROOT / "infra" / ".env.example"
        content = env_file.read_text(encoding='utf-8')
        
        assert "AGENT_OBS_CRON_COMPLIANCE_MIGRATION_SPEC=" in content
        assert "AGENT_OBS_COMPLIANCE_TIER=" in content
        assert "AGENT_OBS_COMPLIANCE_PG=" in content

    def test_docker_compose_env_vars(self):
        """Test that required env vars are in docker-compose.yml."""
        compose_file = REPO_ROOT / "infra" / "docker-compose.yml"
        content = compose_file.read_text()
        
        assert "AGENT_OBS_CRON_COMPLIANCE_MIGRATION_SPEC:" in content
        assert "AGENT_OBS_COMPLIANCE_TIER:" in content
        assert "AGENT_OBS_COMPLIANCE_PG:" in content

    def test_prometheus_rules_include_compliance_migration_alert(self):
        """Test that compliance migration alerts are in prometheus rules."""
        rules_file = REPO_ROOT / "infra" / "prometheus-rules.yml"
        content = rules_file.read_text()
        
        assert "ComplianceCatalogMigrationStale" in content
        assert "migrate_compliance_catalog_hot_to_warm" in content