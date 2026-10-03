"""PC35: Tests for GDPR export with tiered compliance catalog querying."""

import asyncio
import sys
from pathlib import Path
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Add repo root to Python path for imports
REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from agent_obs.compliance.gdpr_export import export_data_map
from agent_obs.compliance.catalog import ComplianceCatalog, ComplianceRow


class TestGDPRExportWithTieredCatalog:
    """Test GDPR export with tiered compliance catalog querying."""

    @pytest.mark.asyncio
    async def test_gdpr_export_queries_both_tiers(self):
        """Test that GDPR export queries both hot and warm tiers."""
        from agent_obs.compliance.catalog import CompositeCatalogWriter
        
        # Mock the compliance catalog with composite writer
        mock_catalog = MagicMock(spec=ComplianceCatalog)
        mock_writer = MagicMock(spec=CompositeCatalogWriter)
        mock_catalog._writer = mock_writer
        
        # Mock the query_compliance_catalog method to return test data
        test_rows = [
            ComplianceRow(
                agent_id="agent1",
                tool="tool1",
                field="field1",
                pii_type="email",
                frequency=5
            ),
            ComplianceRow(
                agent_id="agent1", 
                tool="tool1",
                field="field2",
                pii_type="phone",
                frequency=3
            )
        ]
        mock_catalog.query_compliance_catalog = AsyncMock(return_value=test_rows)
        
        # Mock the export function
        with patch('agent_obs.compliance.gdpr_export.export_data_map') as mock_export:
            mock_export.return_value = b"test csv data"
            
            # Call export_data_map
            result = await export_data_map(
                catalog=mock_catalog,
                as_of=datetime.now(timezone.utc),
                lawful_basis="consent",
                fmt="csv"
            )
            
            # Verify that query_compliance_catalog was called with correct parameters
            mock_catalog.query_compliance_catalog.assert_called_once()
            call_args = mock_catalog.query_compliance_catalog.call_args
            assert call_args.kwargs["last_seen_min"] is not None  # Should have cutoff date
            
            # Verify that export was successful
            assert result == b"test csv data"
            mock_export.assert_called_once()

    @pytest.mark.asyncio
    async def test_gdpr_export_hot_tier_only(self):
        """Test GDPR export with hot tier only."""
        from agent_obs.compliance.catalog import HotCatalogWriter
        
        # Mock compliance catalog with hot tier only
        mock_catalog = MagicMock(spec=ComplianceCatalog)
        mock_writer = MagicMock(spec=HotCatalogWriter)
        mock_catalog._writer = mock_writer
        
        # Mock the query_compliance_catalog method
        test_rows = [
            ComplianceRow(
                agent_id="agent1",
                tool="tool1", 
                field="field1",
                pii_type="email",
                frequency=5
            )
        ]
        mock_catalog.query_compliance_catalog = AsyncMock(return_value=test_rows)
        
# Mock the export function
        with patch('agent_obs.compliance.gdpr_export.export_data_map') as mock_export:
            mock_export.return_value = b"warm tier csv data"
            
# Call export_data_map
            result = await export_data_map(
                catalog=mock_catalog,
                as_of=datetime.now(timezone.utc),
                lawful_basis={"agent1": "consent"},
                fmt="csv"
            )
            
            # Verify that query_compliance_catalog was called
            mock_catalog.query_compliance_catalog.assert_called_once()
            
            # Verify that export was successful
            assert result == b"hot tier csv data"

    @pytest.mark.asyncio
    async def test_gdpr_export_warm_tier_only(self):
        """Test GDPR export with warm tier only."""
        from agent_obs.compliance.catalog import PostgresCatalogWriter
        
        # Mock compliance catalog with warm tier only
        mock_catalog = MagicMock(spec=ComplianceCatalog)
        mock_writer = MagicMock(spec=PostgresCatalogWriter)
        mock_catalog._writer = mock_writer
        
        # Mock the query_compliance_catalog method
        test_rows = [
            ComplianceRow(
                agent_id="agent1",
                tool="tool1",
                field="field1", 
                pii_type="email",
                frequency=5
            )
        ]
        mock_catalog.query_compliance_catalog = AsyncMock(return_value=test_rows)
        
        # Mock the export function
        with patch('agent_obs.compliance.gdpr_export.export_data_map') as mock_export:
            mock_export.return_value = b"warm tier csv data"
            
            # Call export_data_map
            result = await export_data_map(
                catalog=mock_catalog,
                as_of=datetime.now(timezone.utc),
                lawful_basis="consent",
                fmt="csv"
            )
            
            # Verify that query_compliance_catalog was called
            mock_catalog.query_compliance_catalog.assert_called_once()
            
            # Verify that export was successful
            assert result == b"warm tier csv data"

    @pytest.mark.asyncio
    async def test_gdpr_export_with_filters(self):
        """Test GDPR export with filtering parameters."""
        mock_catalog = MagicMock(spec=ComplianceCatalog)
        
        # Mock the query_compliance_catalog method
        test_rows = [
            ComplianceRow(
                agent_id="agent1",
                tool="tool1",
                field="field1",
                pii_type="email", 
                frequency=5
            )
        ]
        mock_catalog.query_compliance_catalog = AsyncMock(return_value=test_rows)
        
        # Mock the export function
        with patch('agent_obs.compliance.gdpr_export.export_data_map') as mock_export:
            mock_export.return_value = b"filtered csv data"
            
            # Call export_data_map - filters should be applied to query_compliance_catalog
            result = await export_data_map(
                catalog=mock_catalog,
                as_of=datetime.now(timezone.utc),
                lawful_basis={"agent1": "consent"},
                fmt="csv"
            )
            
            # Verify that query_compliance_catalog was called with filters
            mock_catalog.query_compliance_catalog.assert_called_once()
            call_args = mock_catalog.query_compliance_catalog.call_args
            assert call_args.kwargs["agent_id"] == "agent1"
            assert call_args.kwargs["tool"] == "tool1"
            assert call_args.kwargs["pii_type"] == "email"
            
            # Verify that export was successful
            assert result == b"filtered csv data"

    @pytest.mark.asyncio 
    async def test_gdpr_export_empty_results(self):
        """Test GDPR export with empty compliance catalog."""
        mock_catalog = MagicMock(spec=ComplianceCatalog)
        
        # Mock empty results from query
        mock_catalog.query_compliance_catalog = AsyncMock(return_value=[])
        
        # Mock the export function
        with patch('agent_obs.compliance.gdpr_export.export_data_map') as mock_export:
            mock_export.return_value = b""  # Empty CSV
            
# Call export_data_map
            result = await export_data_map(
                catalog=mock_catalog,
                as_of=datetime.now(timezone.utc),
                lawful_basis={"agent1": "consent"},
                fmt="csv"
            )
            
            # Verify that query_compliance_catalog was called
            mock_catalog.query_compliance_catalog.assert_called_once()
            
            # Verify that export was successful with empty data
            # Note: The export function generates a CSV header even for empty results
            assert result.startswith(b'\xef\xbb\xbf')  # BOM for Excel compatibility

    @pytest.mark.asyncio
    async def test_gdpr_export_error_handling(self):
        """Test GDPR export error handling when tier queries fail."""
        mock_catalog = MagicMock(spec=ComplianceCatalog)
        
        # Mock query to raise an exception
        mock_catalog.query_compliance_catalog = AsyncMock(side_effect=Exception("Query failed"))
        
        # Mock the export function
        with patch('agent_obs.compliance.gdpr_export.export_data_map') as mock_export:
            mock_export.return_value = b"error csv data"
            
            # Call export_data_map - should handle the error gracefully
            with patch('agent_obs.compliance.gdpr_export.logger') as mock_logger:
                result = await export_data_map(
                    catalog=mock_catalog,
                    as_of=datetime.now(timezone.utc),
                    lawful_basis={"agent1": "consent"},
                    fmt="csv"
                )
            
            # Verify that query_compliance_catalog was called
            mock_catalog.query_compliance_catalog.assert_called_once()
            
            # Verify that error was logged
            mock_logger.warning.assert_called()
            
            # Verify that export returns empty data when query fails
            assert result.startswith(b'\xef\xbb\xbf')  # BOM for Excel compatibility


class TestComplianceCatalogQuerying:
    """Test the compliance catalog querying functionality directly."""

    @pytest.mark.asyncio
    async def test_query_compliance_catalog_basic(self):
        """Test basic compliance catalog querying."""
        from agent_obs.compliance.catalog import HotCatalogWriter
        
        # Create a compliance catalog with hot tier
        catalog = ComplianceCatalog(HotCatalogWriter())
        
        # Mock the hot store query method
        with patch.object(catalog._writer, '_get_hot_store') as mock_get_store:
            mock_hot_store = AsyncMock()
            mock_hot_store.get_compliance_catalog_all = AsyncMock(return_value=[
                {
                    "agent_id": "agent1",
                    "tool": "tool1", 
                    "field": "field1",
                    "pii_type": "email",
                    "frequency": 5,
                    "last_seen": datetime.now(timezone.utc)
                }
            ])
            mock_hot_store.close = AsyncMock()
            mock_get_store.return_value = mock_hot_store
            
            # Query the compliance catalog
            results = await catalog.query_compliance_catalog()
            
            # Verify results
            assert len(results) == 1
            assert results[0].agent_id == "agent1"
            assert results[0].tool == "tool1"
            assert results[0].field == "field1"
            assert results[0].pii_type == "email"
            assert results[0].frequency == 5

    @pytest.mark.asyncio
    async def test_query_compliance_catalog_with_filters(self):
        """Test compliance catalog querying with filters."""
        from agent_obs.compliance.catalog import HotCatalogWriter
        
        # Create a compliance catalog with hot tier
        catalog = ComplianceCatalog(HotCatalogWriter())
        
        # Mock the hot store query method
        with patch.object(catalog._writer, '_get_hot_store') as mock_get_store:
            mock_hot_store = AsyncMock()
            mock_hot_store.get_compliance_catalog_all = AsyncMock(return_value=[
                {
                    "agent_id": "agent1",
                    "tool": "tool1",
                    "field": "field1", 
                    "pii_type": "email",
                    "frequency": 5,
                    "last_seen": datetime.now(timezone.utc)
                },
                {
                    "agent_id": "agent2",
                    "tool": "tool2",
                    "field": "field2",
                    "pii_type": "phone", 
                    "frequency": 3,
                    "last_seen": datetime.now(timezone.utc)
                }
            ])
            mock_hot_store.close = AsyncMock()
            mock_get_store.return_value = mock_hot_store
            
            # Query with agent_id filter
            results = await catalog.query_compliance_catalog(agent_id="agent1")
            
            # Verify results are filtered
            assert len(results) == 1
            assert results[0].agent_id == "agent1"
            
            # Query with pii_type filter
            results = await catalog.query_compliance_catalog(pii_type="phone")
            
            # Verify results are filtered
            assert len(results) == 1
            assert results[0].pii_type == "phone"

    @pytest.mark.asyncio
    async def test_merge_and_deduplicate_compliance_rows(self):
        """Test merging and deduplication of compliance rows."""
        catalog = ComplianceCatalog(None)  # No writer needed for this test
        
        # Create duplicate rows with different frequencies
        rows = [
            ComplianceRow("agent1", "tool1", "field1", "email", 5),
            ComplianceRow("agent1", "tool1", "field1", "email", 10),  # Higher frequency
            ComplianceRow("agent1", "tool1", "field2", "phone", 3),
            ComplianceRow("agent2", "tool2", "field1", "email", 7),
        ]
        
        # Merge and deduplicate
        merged = catalog._merge_and_deduplicate_compliance_rows(rows)
        
        # Verify deduplication worked
        assert len(merged) == 3  # One duplicate removed
        
        # Verify higher frequency was kept for the duplicate
        email_row = next(r for r in merged if r.pii_type == "email" and r.field == "field1")
        assert email_row.frequency == 10  # Higher frequency kept

    @pytest.mark.asyncio
    async def test_query_compliance_catalog_error_handling(self):
        """Test error handling in compliance catalog querying."""
        from agent_obs.compliance.catalog import HotCatalogWriter
        
        # Create a compliance catalog with hot tier
        catalog = ComplianceCatalog(HotCatalogWriter())
        
        # Mock the hot store to raise an exception
        with patch.object(catalog._writer, '_get_hot_store') as mock_get_store:
            mock_hot_store = AsyncMock()
            mock_hot_store.get_compliance_catalog_all = AsyncMock(side_effect=Exception("Query failed"))
            mock_hot_store.close = AsyncMock()
            mock_get_store.return_value = mock_hot_store
            
            # Query should handle the error gracefully and return empty list
            results = await catalog.query_compliance_catalog()
            
            # Verify empty results are returned
            assert len(results) == 0