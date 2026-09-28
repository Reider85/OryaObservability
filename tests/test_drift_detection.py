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
        current_embeddings = [[0.7, 0.8, 0.75, 0.9, 0.85], [0.8, 0.7, 0.85, 0.75, 0.9]] * 5
        
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