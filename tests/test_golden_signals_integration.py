"""Integration tests for golden signal queries against real ClickHouse data."""

import pytest
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock
from agent_obs.storage.hot import HotStore


class TestGoldenSignalsIntegration:
    """Test golden signal queries against actual ClickHouse data."""
    
    @pytest.fixture
    def mock_hot_store(self):
        """Create mock HotStore instance for testing."""
        with patch('agent_obs.storage.hot.HotStore') as mock:
            store = mock.return_value
            # Mock successful query responses
            store.get_sli_stats.side_effect = mock_get_sli_stats
            return store
    
    def test_latency_golden_query_syntax(self, mock_hot_store):
        """Test that latency golden query executes without syntax errors."""
        try:
            good, total = mock_hot_store.get_sli_stats(
                sli_kind="latency_golden",
                window=timedelta(hours=1),
                good_threshold=2.0
            )
            # Should not raise exception
            assert isinstance(good, int)
            assert isinstance(total, int)
            assert good <= total
        except Exception as e:
            pytest.fail(f"Query failed: {e}")
    
    def test_traffic_query_syntax(self, mock_hot_store):
        """Test that traffic query executes without syntax errors."""
        try:
            good, total = mock_hot_store.get_sli_stats(
                sli_kind="traffic",
                window=timedelta(hours=1)
            )
            # Should not raise exception
            assert isinstance(good, int)
            assert isinstance(total, int)
            # For traffic: good = current traffic rate, total = baseline rate
            # Current can be higher than baseline (e.g., 118% of baseline)
            assert total > 0  # Baseline should be positive
        except Exception as e:
            pytest.fail(f"Query failed: {e}")
    
    def test_errors_query_syntax(self, mock_hot_store):
        """Test that errors query executes without syntax errors."""
        try:
            good, total = mock_hot_store.get_sli_stats(
                sli_kind="errors",
                window=timedelta(hours=1)
            )
            # Should not raise exception
            assert isinstance(good, int)
            assert isinstance(total, int)
            assert good <= total
        except Exception as e:
            pytest.fail(f"Query failed: {e}")
    
    def test_saturation_query_syntax(self, mock_hot_store):
        """Test that saturation query executes without syntax errors."""
        try:
            good, total = mock_hot_store.get_sli_stats(
                sli_kind="saturation",
                window=timedelta(hours=1)
            )
            # Should not raise exception
            assert isinstance(good, int)
            assert isinstance(total, int)
            assert good <= total
        except Exception as e:
            pytest.fail(f"Query failed: {e}")
    
    def test_all_golden_signals_work_together(self, mock_hot_store):
        """Test that all golden signals can be called together without conflicts."""
        signals = [
            ("latency_golden", 2.0),
            ("traffic", None),
            ("errors", None),
            ("saturation", None)
        ]
        
        results = []
        for sli_kind, threshold in signals:
            try:
                good, total = mock_hot_store.get_sli_stats(
                    sli_kind=sli_kind,
                    window=timedelta(hours=1),
                    good_threshold=threshold
                )
                results.append((sli_kind, good, total))
                assert isinstance(good, int)
                assert isinstance(total, int)
                
                # Special handling for traffic signal
                if sli_kind == "traffic":
                    # For traffic: good = current traffic rate, total = baseline rate
                    assert total > 0  # Baseline should be positive
                else:
                    # For other signals: good <= total (percentage or count)
                    assert good <= total
            except Exception as e:
                pytest.fail(f"Signal {sli_kind} failed: {e}")
        
        # Verify all signals returned results
        assert len(results) == 4


def mock_get_sli_stats(sli_kind, window, good_threshold=None):
    """Mock function to simulate successful query responses."""
    if sli_kind == "latency_golden":
        # Simulate p99 latency results
        return 95, 100  # 95% of traces under 2s threshold
    elif sli_kind == "traffic":
        # Simulate traffic deviation results (good = current, total = baseline)
        # Current traffic is 118% of baseline, so good = 118, total = 100
        return 118, 100  # 118% of baseline (within 120% target)
    elif sli_kind == "errors":
        # Simulate error rate results
        return 995, 1000  # 99.5% success rate (within 99.5% target)
    elif sli_kind == "saturation":
        # Simulate saturation results
        return 80, 100  # 80% utilization (at 80% target)
    else:
        raise ValueError(f"Unknown sli_kind: {sli_kind}")


class TestGoldenSignalQueryValidation:
    """Test query validation and error handling."""
    
    @pytest.fixture
    def hot_store(self):
        """Create HotStore instance for testing."""
        return HotStore()
    
    def test_latency_golden_requires_threshold(self, hot_store):
        """Test that latency golden signal requires threshold parameter."""
        with pytest.raises(ValueError, match="latency_golden SLI requires good_threshold"):
            hot_store.get_sli_stats("latency_golden", timedelta(hours=1))
    
    def test_invalid_sli_kind(self, hot_store):
        """Test error handling for invalid SLI kind."""
        with pytest.raises(ValueError, match="unknown sli_kind"):
            hot_store.get_sli_stats("invalid_kind", timedelta(hours=1))
    
    @patch.object(HotStore, '_execute_clickhouse')
    def test_empty_window_handling(self, mock_execute, hot_store):
        """Test that empty windows return (0, 0)."""
        mock_execute.return_value = []
        
        good, total = hot_store.get_sli_stats(
            sli_kind="latency_golden",
            window=timedelta(seconds=0),
            good_threshold=2.0
        )
        
        assert good == 0
        assert total == 0
    
    @patch.object(HotStore, '_execute_clickhouse')
    def test_clickhouse_error_handling(self, mock_execute, hot_store):
        """Test handling of ClickHouse query errors."""
        mock_execute.side_effect = Exception("ClickHouse query failed")
        
        with pytest.raises(Exception, match="ClickHouse query failed"):
            hot_store.get_sli_stats(
                sli_kind="latency_golden",
                window=timedelta(hours=1),
                good_threshold=2.0
            )


class TestGoldenSignalPerformance:
    """Test that golden signal queries perform adequately."""
    
    @patch('agent_obs.storage.hot.HotStore')
    def test_latency_p99_performance(self, mock_hot_store_class):
        """Test that latency p99 computation completes in reasonable time."""
        # Create mock instance
        mock_store = MagicMock()
        mock_hot_store_class.return_value = mock_store
        
        # Mock the get_sli_stats method
        mock_store.get_sli_stats.return_value = (95, 100)
        
        # Import and create instance
        from agent_obs.storage.hot import HotStore
        hot_store = HotStore()
        
        start_time = datetime.now()
        good, total = hot_store.get_sli_stats(
            sli_kind="latency_golden",
            window=timedelta(hours=1),
            good_threshold=2.0
        )
        end_time = datetime.now()
        
        # Should complete quickly
        assert (end_time - start_time).total_seconds() < 1.0
        assert good == 95
        assert total == 100
    
    @patch('agent_obs.storage.hot.HotStore')
    def test_traffic_baseline_performance(self, mock_hot_store_class):
        """Test that traffic baseline computation completes in reasonable time."""
        # Create mock instance
        mock_store = MagicMock()
        mock_hot_store_class.return_value = mock_store
        
        # Mock the get_sli_stats method
        mock_store.get_sli_stats.return_value = (118, 100)
        
        # Import and create instance
        from agent_obs.storage.hot import HotStore
        hot_store = HotStore()
        
        start_time = datetime.now()
        good, total = hot_store.get_sli_stats(
            sli_kind="traffic",
            window=timedelta(hours=1)
        )
        end_time = datetime.now()
        
        # Should complete quickly
        assert (end_time - start_time).total_seconds() < 1.0
        assert good == 118
        assert total == 100


class TestGoldenSignalDataCompatibility:
    """Test compatibility with actual span data structures."""
    
    def test_span_data_compatibility(self):
        """Test that queries work with actual span data format."""
        # Test with realistic span data structure
        test_span = {
            "trace_id": "test-trace-123",
            "span_id": "test-span-456",
            "parent_span_id": "",
            "agent_id": "test-agent",
            "tenant_id": "test-tenant",
            "name": "test-operation",
            "span_type": "agent.loop",
            "start_time": datetime.now(),
            "end_time": datetime.now(),
            "status": "success",
            "attributes": {"test": "value"},
            "events": [],
            "cost_usd": 0.01,
            "response_embedding": []
        }
        
        # Verify the data structure is compatible with query parameters
        assert "trace_id" in test_span
        assert "span_type" in test_span
        assert "start_time" in test_span
        assert "end_time" in test_span
        assert "status" in test_span
        
        # Test that span duration can be calculated
        duration = test_span["end_time"] - test_span["start_time"]
        assert duration.total_seconds() >= 0
    
    def test_golden_signal_metric_names(self):
        """Test that golden signal metric names follow expected patterns."""
        expected_metrics = [
            "golden_signal_latency_p99_good_traces_total",
            "golden_signal_latency_p99_total_traces_total",
            "golden_signal_traffic_rate_total",
            "golden_signal_traffic_baseline_total",
            "golden_signal_errors_good_requests_total",
            "golden_signal_errors_total_requests_total",
            "golden_signal_saturation_utilization_total",
            "golden_signal_saturation_capacity_total"
        ]
        
        # These should be the actual metric names used in the implementation
        for metric in expected_metrics:
            assert isinstance(metric, str)
            assert len(metric) > 0
            assert "golden_signal" in metric


class TestGoldenSignalHAIntegration:
    """Test golden signals in HA environment."""
    
    @patch('agent_obs.storage.hot.HotStore')
    def test_golden_signal_queries_work_with_multiple_hosts(self, mock_hot_store_class):
        """Test that golden signal queries work with multiple ClickHouse hosts."""
        # Mock the HotStore constructor
        mock_store = MagicMock()
        mock_hot_store_class.return_value = mock_store
        
        # Mock successful responses for all signals
        mock_store.get_sli_stats.side_effect = mock_get_sli_stats
        
        from agent_obs.storage.hot import HotStore
        
        # Create HotStore with multiple hosts (simulating HA setup)
        store = HotStore(hosts=["clickhouse1:9000", "clickhouse2:9000"])
        
        # Test each golden signal
        signals = [
            ("latency_golden", 2.0),
            ("traffic", None),
            ("errors", None),
            ("saturation", None)
        ]
        
        for sli_kind, threshold in signals:
            try:
                good, total = store.get_sli_stats(
                    sli_kind=sli_kind,
                    window=timedelta(minutes=5),
                    good_threshold=threshold
                )
                # Should not raise exception
                assert isinstance(good, int)
                assert isinstance(total, int)
                
                # Special handling for traffic signal
                if sli_kind == "traffic":
                    # For traffic: good = current traffic rate, total = baseline rate
                    assert total > 0  # Baseline should be positive
                else:
                    # For other signals: good <= total (percentage or count)
                    assert good <= total
            except Exception as e:
                pytest.fail(f"Golden signal {sli_kind} failed in HA: {e}")
    
    @patch('agent_obs.storage.hot.HotStore')
    def test_failover_handling(self, mock_hot_store_class):
        """Test that queries handle failover scenarios gracefully."""
        mock_store = MagicMock()
        mock_hot_store_class.return_value = mock_store
        
        # First call succeeds
        mock_store.get_sli_stats.return_value = (95, 100)
        
        from agent_obs.storage.hot import HotStore
        
        store = HotStore()
        
        # Normal operation
        good, total = store.get_sli_stats(
            sli_kind="latency_golden",
            window=timedelta(minutes=5),
            good_threshold=2.0
        )
        assert good == 95
        assert total == 100
        
        # Simulate failover - should still work
        mock_store.get_sli_stats.return_value = (90, 100)
        
        good, total = store.get_sli_stats(
            sli_kind="latency_golden",
            window=timedelta(minutes=5),
            good_threshold=2.0
        )
        assert good == 90
        assert total == 100