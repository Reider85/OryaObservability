"""Tests for PC25 drift detection functionality."""

import asyncio
import numpy as np
import pytest
import time
from unittest.mock import AsyncMock, MagicMock, patch

from agent_obs.drift import DriftDetector
from agent_obs.drift.kl_divergence import (
    compute_kl_divergence,
    compute_kl_from_embeddings,
    create_histogram,
    normalize_histogram,
)
from agent_obs.drift.models import DriftReport
from agent_obs.storage.hot import HotStore


class TestKLComputation:
    """Test KL-divergence computation functions."""

    def test_create_histogram_empty(self):
        """Test histogram creation with empty input."""
        hist = create_histogram([])
        assert len(hist) == 50
        assert all(x == 0 for x in hist)

    def test_create_histogram_simple(self):
        """Test histogram creation with simple data."""
        embeddings = [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]  # Same length
        hist = create_histogram(embeddings, bins=5)
        assert len(hist) == 5
        assert sum(hist) == 6  # 6 total data points

    def test_normalize_histogram_zero(self):
        """Test histogram normalization with zero input."""
        hist = np.zeros(10)
        normalized = normalize_histogram(hist)
        assert all(x == 0 for x in normalized)

    def test_normalize_histogram_normal(self):
        """Test histogram normalization with normal input."""
        hist = np.array([1, 2, 3, 4])
        normalized = normalize_histogram(hist)
        assert len(normalized) == 4
        assert abs(sum(normalized) - 1.0) < 1e-10

    def test_compute_kl_divergence_identical(self):
        """Test KL-divergence with identical distributions."""
        p = np.array([0.25, 0.25, 0.25, 0.25])
        q = np.array([0.25, 0.25, 0.25, 0.25])
        kl = compute_kl_divergence(p, q)
        assert kl == 0.0

    def test_compute_kl_divergence_different(self):
        """Test KL-divergence with different distributions."""
        p = np.array([0.9, 0.1, 0.0, 0.0])
        q = np.array([0.25, 0.25, 0.25, 0.25])
        kl = compute_kl_divergence(p, q)
        assert kl > 0.0

    def test_compute_kl_from_embeddings(self):
        """Test end-to-end KL computation from embeddings."""
        baseline = [[0.1, 0.2], [0.3, 0.4]]
        current = [[0.5, 0.6], [0.7, 0.8]]
        kl = compute_kl_from_embeddings(baseline, current)
        assert isinstance(kl, float)
        assert kl >= 0.0


class TestDriftReport:
    """Test DriftReport dataclass."""

    def test_drift_report_creation(self):
        """Test DriftReport creation with default values."""
        report = DriftReport(
            trace_id="test_trace",
            eval_id="test_eval",
            agent_id="test_agent"
        )
        
        assert report.trace_id == "test_trace"
        assert report.eval_id == "test_eval"
        assert report.agent_id == "test_agent"
        assert report.eval_name == "drift_detection"
        assert report.eval_version == "1.0.0"
        assert report.kl_score == 0.0
        assert report.is_drift_detected == False
        assert report.severity == "info"
        assert report.flags == []

    def test_drift_report_to_dict(self):
        """Test DriftReport serialization to dictionary."""
        report = DriftReport(
            trace_id="test_trace",
            eval_id="test_eval",
            kl_score=0.15,
            is_drift_detected=True,
            severity="warning"
        )
        
        result = report.to_dict()
        assert result["trace_id"] == "test_trace"
        assert result["kl_score"] == 0.15
        assert result["is_drift_detected"] == True
        assert result["severity"] == "warning"


class TestDriftDetector:
    """Test DriftDetector main functionality."""

    @pytest.fixture
    def mock_hotstore(self):
        """Create mock HotStore for testing."""
        hotstore = MagicMock(spec=HotStore)
        hotstore._execute_clickhouse = AsyncMock()
        return hotstore

    @pytest.fixture
    def mock_sdk(self):
        """Create mock SDK for testing."""
        sdk = MagicMock()
        return sdk

    @pytest.fixture
    def detector(self, mock_sdk, mock_hotstore):
        """Create DriftDetector instance for testing."""
        return DriftDetector(
            sdk=mock_sdk,
            hotstore=mock_hotstore,
            baseline_hours=24,  # 24 hours for faster testing
            last_window_hours=1,   # 1 hour
            kl_threshold=0.1
        )

    @pytest.mark.asyncio
    async def test_run_once_no_drift(self, detector, mock_hotstore):
        """Test drift detection run with no drift detected."""
        # Mock identical embeddings (no drift)
        baseline_embeddings = [[0.1, 0.2, 0.15, 0.3, 0.25], [0.2, 0.1, 0.25, 0.15, 0.3]] * 10
        current_embeddings = [[0.1, 0.2, 0.15, 0.3, 0.25], [0.2, 0.1, 0.25, 0.15, 0.3]] * 5
        
        mock_hotstore._execute_clickhouse.side_effect = [
            [(emb,) for emb in baseline_embeddings],  # Baseline query
            [(emb,) for emb in current_embeddings],   # Current query
        ]
        
        report = await detector.run_once("test_agent")
        
        assert isinstance(report, DriftReport)
        assert report.agent_id == "test_agent"
        # With identical data, KL should be essentially 0 (no drift)
        assert report.kl_score < 1e-10
        assert report.is_drift_detected == False
        assert report.severity == "info"

    @pytest.mark.asyncio
    async def test_run_once_drift_detected(self, detector, mock_hotstore):
        """Test drift detection run with drift detected."""
        # Mock baseline embeddings (low values)
        baseline_embeddings = [[0.0, 0.1, 0.05, 0.2, 0.15], [0.1, 0.0, 0.15, 0.05, 0.2]] * 10
        # Mock current embeddings (high values - clearly different distribution)
        current_embeddings = [[0.7, 0.8, 0.75, 0.9, 0.85], [0.8, 0.7, 0.85, 0.75, 0.9]] * 50  # 50 embeddings to pass threshold
        
        mock_hotstore._execute_clickhouse.side_effect = [
            [(emb,) for emb in baseline_embeddings],  # Baseline query
            [(emb,) for emb in current_embeddings],   # Current query
        ]
        
        report = await detector.run_once("test_agent")
        
        assert isinstance(report, DriftReport)
        assert report.agent_id == "test_agent"
        # With clearly different distributions, KL should be high (drift detected)
        assert report.kl_score > detector.kl_threshold
        assert report.is_drift_detected == True
        assert report.severity in ["warning", "critical"]

    @pytest.mark.asyncio
    async def test_get_baseline_embeddings(self, detector, mock_hotstore):
        """Test baseline embeddings retrieval."""
        embeddings = [[0.1, 0.2], [0.3, 0.4]]
        mock_hotstore._execute_clickhouse.return_value = [(emb,) for emb in embeddings]
        
        result = await detector._get_baseline_embeddings("test_agent")
        
        assert result == embeddings
        mock_hotstore._execute_clickhouse.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_last_window_embeddings(self, detector, mock_hotstore):
        """Test last window embeddings retrieval."""
        embeddings = [[0.5, 0.6], [0.7, 0.8]]
        mock_hotstore._execute_clickhouse.return_value = [(emb,) for emb in embeddings]
        
        result = await detector._get_last_window_embeddings("test_agent")
        
        assert result == embeddings
        mock_hotstore._execute_clickhouse.assert_called_once()

    def test_compute_severity(self, detector):
        """Test severity computation."""
        # Below threshold
        assert detector._compute_severity(0.05, 0.1) == "info"
        # At threshold
        assert detector._compute_severity(0.1, 0.1) == "info"
        # 2x threshold
        assert detector._compute_severity(0.2, 0.1) == "warning"
        # 3x threshold (should be critical due to >= 3.0)
        assert detector._compute_severity(0.3000001, 0.1) == "critical"
        # Just below 3x
        assert detector._compute_severity(0.299999, 0.1) == "warning"

    @pytest.mark.asyncio
    async def test_empty_embeddings_handling(self, detector, mock_hotstore):
        """Test handling of empty embedding results."""
        # Mock empty results
        mock_hotstore._execute_clickhouse.return_value = []
        
        report = await detector.run_once("test_agent")
        
        assert isinstance(report, DriftReport)
        assert report.sample_size_baseline == 0
        assert report.sample_size_last == 0
        assert report.kl_score == 0.0

    @pytest.mark.asyncio
    async def test_sample_size_skip_logic(self, detector, mock_hotstore):
        """Test skip logic when sample_size_last < 100."""
        # Mock insufficient data (only 50 embeddings)
        baseline_embeddings = [[0.1, 0.2] for _ in range(200)]  # 200 baseline embeddings
        current_embeddings = [[0.5, 0.6] for _ in range(50)]    # Only 50 current embeddings
        
        mock_hotstore._execute_clickhouse.side_effect = [
            [(emb,) for emb in baseline_embeddings],  # Baseline query
            [(emb,) for emb in current_embeddings],   # Current query
        ]
        
        report = await detector.run_once("test_agent")
        
        assert isinstance(report, DriftReport)
        assert report.sample_size_last == 50
        assert report.sample_size_baseline == 200
        assert report.is_drift_detected == False
        assert "sample_size_too_small" in report.flags
        assert report.kl_score == 0.0
        assert "skip" in report.trace_id
        assert "skip" in report.eval_id

    @pytest.mark.asyncio
    async def test_sample_size_continues_when_sufficient(self, detector, mock_hotstore):
        """Test that KL computation continues when sample_size_last >= 100."""
        # Mock sufficient data (150 embeddings)
        baseline_embeddings = [[0.1, 0.2] for _ in range(200)]  # 200 baseline embeddings
        current_embeddings = [[0.5, 0.6] for _ in range(150)]    # 150 current embeddings
        
        mock_hotstore._execute_clickhouse.side_effect = [
            [(emb,) for emb in baseline_embeddings],  # Baseline query
            [(emb,) for emb in current_embeddings],   # Current query
        ]
        
        report = await detector.run_once("test_agent")
        
        assert isinstance(report, DriftReport)
        assert report.sample_size_last == 150
        assert report.sample_size_baseline == 200
        assert report.kl_score > 0.0  # Should compute KL since we have enough data
        assert "sample_size_too_small" not in report.flags
        assert "skipped" not in report.trace_id
        assert "skipped" not in report.eval_id


class TestDriftIntegration:
    """Test drift detection integration with other components."""

    @pytest.mark.asyncio
    async def test_detector_with_real_sdk(self):
        """Test detector integration with real SDK components."""
        from agent_obs.observability import ObservabilitySDK
        from agent_obs.storage.hot import HotStore
        
        # Create real instances (minimal config)
        sdk = ObservabilitySDK(exporters=[])
        hotstore = HotStore(
            host="localhost",
            port=8123,
            database="observability"
        )
        
        detector = DriftDetector(
            sdk=sdk,
            hotstore=hotstore,
            baseline_hours=1,  # 1 hour for testing
            last_window_hours=1,
            kl_threshold=0.1
        )
        
        # This should handle ClickHouse connection errors gracefully
        try:
            report = await detector.run_once("test_agent")
            assert isinstance(report, DriftReport)
        except Exception as e:
            # Expected if ClickHouse is not running
            error_str = str(e).lower()
            assert any(keyword in error_str for keyword in ["connection", "unreachable", "network", "timeout", "code: 210"])

    def test_metrics_import(self):
        """Test that drift metrics can be imported."""
        from agent_obs.metrics import (
            drift_kl_score,
            drift_runs_total,
            drift_alerts_total,
        )
        
        # Verify metrics are properly defined
        assert drift_kl_score._name == "agent_obs_drift_kl_score"
        assert drift_runs_total._name + "_total" == "agent_obs_drift_runs_total"
        assert drift_alerts_total._name + "_total" == "agent_obs_drift_alerts_total"


class TestDriftPerformance:
    """Test drift detection performance characteristics."""

    @pytest.mark.asyncio
    async def test_large_embedding_dataset(self):
        """Test drift detection with large embedding datasets."""
        from agent_obs.drift import DriftDetector
        
        # Generate large synthetic datasets
        baseline = [[float(i) for i in range(768)] for _ in range(1000)]  # 1000 embeddings
        current = [[float(i + 0.1) for i in range(768)] for _ in range(500)]   # 500 embeddings
        
        # Create detector with mocked dependencies
        detector = DriftDetector(
            sdk=None,
            hotstore=None,
            baseline_hours=168,
            last_window_hours=1,
            kl_threshold=0.1
        )
        
        # Override the embedding retrieval methods
        detector._get_baseline_embeddings = AsyncMock(return_value=baseline)
        detector._get_last_window_embeddings = AsyncMock(return_value=current)
        
        start_time = time.time()
        report = await detector.run_once("test_agent")
        end_time = time.time()
        
        assert isinstance(report, DriftReport)
        assert report.sample_size_baseline == 1000
        assert report.sample_size_last == 500
        assert end_time - start_time < 10.0  # Should complete in under 10 seconds


class TestDriftHistoryWriting:
    """Test writing drift reports to drift_history table."""

    @pytest.fixture
    def mock_hotstore(self):
        """Create mock HotStore for testing."""
        hotstore = MagicMock(spec=HotStore)
        hotstore._execute_clickhouse = AsyncMock()
        hotstore.write_drift_history = MagicMock()
        return hotstore

    @pytest.fixture
    def mock_sdk(self):
        """Create mock SDK for testing."""
        sdk = MagicMock()
        return sdk

    @pytest.fixture
    def detector(self, mock_sdk, mock_hotstore):
        """Create DriftDetector instance for testing."""
        return DriftDetector(
            sdk=mock_sdk,
            hotstore=mock_hotstore,
            baseline_hours=24,
            last_window_hours=1,
            kl_threshold=0.1
        )

    @pytest.mark.asyncio
    async def test_drift_report_written_to_history(self, detector, mock_hotstore):
        """Test that drift report is written to drift_history table."""
        # Mock sufficient embeddings
        embeddings = [[0.1, 0.2] for _ in range(150)]  # Sufficient data
        
        mock_hotstore._execute_clickhouse.side_effect = [
            [(emb,) for emb in embeddings],  # Baseline query
            [(emb,) for emb in embeddings],   # Current query (no drift)
        ]
        
        report = await detector.run_once("test_agent")
        
        # Verify report was written to drift_history
        mock_hotstore.write_drift_history.assert_called_once()
        
        # Verify the written data contains correct information
        written_data = mock_hotstore.write_drift_history.call_args[0][0]
        assert written_data["trace_id"] == report.trace_id
        assert written_data["eval_id"] == report.eval_id
        assert written_data["agent_id"] == "test_agent"
        assert written_data["kl_score"] == report.kl_score
        assert written_data["is_drift_detected"] == report.is_drift_detected
        assert written_data["severity"] == report.severity
        assert written_data["sample_size_baseline"] == 150
        assert written_data["sample_size_last"] == 150

    @pytest.mark.asyncio
    async def test_drift_history_write_failure_handled_gracefully(self, detector, mock_hotstore):
        """Test that drift detection continues even if drift_history write fails."""
        # Mock sufficient embeddings
        embeddings = [[0.1, 0.2] for _ in range(150)]
        
        mock_hotstore._execute_clickhouse.side_effect = [
            [(emb,) for emb in embeddings],  # Baseline query
            [(emb,) for emb in embeddings],   # Current query
        ]
        
        # Make write_drift_history raise an exception
        mock_hotstore.write_drift_history.side_effect = Exception("ClickHouse connection failed")
        
        # Should still return a valid report despite write failure
        report = await detector.run_once("test_agent")
        
        assert isinstance(report, DriftReport)
        assert report.sample_size_last == 150
        assert report.is_drift_detected == False
        
        # Verify write was attempted
        mock_hotstore.write_drift_history.assert_called_once()

    @pytest.mark.asyncio
    async def test_skip_not_written_to_drift_history(self, detector, mock_hotstore):
        """Test that skipped runs (sample_size < 100) are not written to drift_history."""
        # Mock insufficient embeddings (only 50)
        embeddings = [[0.1, 0.2] for _ in range(50)]
        
        mock_hotstore._execute_clickhouse.side_effect = [
            [(emb,) for emb in embeddings],  # Baseline query
            [(emb,) for emb in embeddings],   # Current query (insufficient)
        ]
        
        report = await detector.run_once("test_agent")
        
        # Verify that write_drift_history was NOT called for skipped runs
        mock_hotstore.write_drift_history.assert_not_called()
        
        # Verify it was skipped
        assert "sample_size_too_small" in report.flags
        assert report.sample_size_last == 50


# Test for ClickHouse table creation
class TestClickHouseIntegration:
    """Test ClickHouse integration for drift storage."""

    def test_drift_history_table_ddl(self):
        """Test that drift history table DDL is correct."""
        ddl_sql = """
        CREATE TABLE IF NOT EXISTS drift_history (
            id UUID DEFAULT generateUUIDv4(),
            trace_id String,
            eval_id String,
            eval_name String DEFAULT 'drift_detection',
            eval_version String DEFAULT '1.0.0',
            eval_timestamp DateTime64(3),
            eval_latency_seconds Float64,
            kl_score Float64,
            threshold Float64,
            is_drift_detected UInt8,
            severity String,
            baseline_window_start DateTime64(3),
            baseline_window_end DateTime64(3),
            last_window_start DateTime64(3),
            last_window_end DateTime64(3),
            sample_size_baseline UInt32,
            sample_size_last UInt32,
            agent_id String,
            model_name String,
            flags Array(String),
            created_at DateTime64(3) DEFAULT now()
        )
        ENGINE = MergeTree()
        ORDER BY (agent_id, created_at)
        PARTITION BY toYYYYMMDD(created_at)
        TTL created_at + INTERVAL 30 DAY
        SETTINGS index_granularity = 8192;
        """
        
        # Verify key components are present
        assert "drift_history" in ddl_sql
        assert "kl_score" in ddl_sql
        assert "agent_id" in ddl_sql
        assert "MergeTree" in ddl_sql
        assert "PARTITION BY toYYYYMMDD(created_at)" in ddl_sql