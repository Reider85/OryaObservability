"""
E2E Tests for Chaos Engineering System.
"""
import asyncio
import pytest
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from scripts.chaos.base import ChaosEngine, ChaosScenario, ChaosStatus, SLOMetric, SLOThreshold
from scripts.chaos.chaos_scenarios import (
    ClickHouseChaosScenario,
    PostgreSQLChaosScenario,
    create_target_scenarios
)
from scripts.chaos.config import ChaosConfig
from scripts.chaos.slo_validator import ChaosSLOValidator, create_default_slo_thresholds
from scripts.chaos.weekly_chaos import WeeklyChaosEngine


class MockChaosScenario(ChaosScenario):
    """Mock chaos scenario for testing."""
    
    def __init__(self, name: str, should_fail: bool = False):
        super().__init__(name=name, services=[name], duration_seconds=5)
        self.should_fail = should_fail
        self.pre_chaos_check_called = False
        self.execute_chaos_called = False
        self.restore_service_called = False
        self.validate_service_health_called = False
    
    async def execute_chaos(self) -> None:
        self.execute_chaos_called = True
        if self.should_fail:
            raise Exception("Mock chaos failure")
    
    async def restore_service(self) -> None:
        self.restore_service_called = True
    
    async def validate_service_health(self) -> bool:
        self.validate_service_health_called = True
        return True
    
    async def pre_chaos_check(self) -> bool:
        self.pre_chaos_check_called = True
        return True
    
    async def post_chaos_check(self) -> bool:
        return True


class TestChaosScenario:
    """Test ChaosScenario base class."""
    
    @pytest.mark.asyncio
    async def test_scenario_execution_success(self):
        """Test successful scenario execution."""
        scenario = MockChaosScenario("test_scenario")
        
        result = await scenario.execute_chaos()
        assert result is None
        assert scenario.execute_chaos_called
    
    @pytest.mark.asyncio
    async def test_scenario_execution_failure(self):
        """Test scenario execution failure."""
        scenario = MockChaosScenario("test_scenario", should_fail=True)
        
        with pytest.raises(Exception, match="Mock chaos failure"):
            await scenario.execute_chaos()
        
        assert scenario.execute_chaos_called
    
    @pytest.mark.asyncio
    async def test_service_restoration(self):
        """Test service restoration."""
        scenario = MockChaosScenario("test_scenario")
        
        await scenario.restore_service()
        assert scenario.restore_service_called
    
    @pytest.mark.asyncio
    async def test_health_validation(self):
        """Test health validation."""
        scenario = MockChaosScenario("test_scenario")
        
        result = await scenario.validate_service_health()
        assert result is True
        assert scenario.validate_service_health_called


class TestClickHouseChaosScenario:
    """Test ClickHouse chaos scenario."""
    
    @pytest.mark.asyncio
    async def test_clickhouse_scenario_creation(self):
        """Test ClickHouse scenario creation."""
        scenario = ClickHouseChaosScenario("clickhouse1", duration_seconds=30)
        
        assert scenario.name == "clickhouse_clickhouse1_chaos"
        assert scenario.services == ["clickhouse1"]
        assert scenario.duration_seconds == 30
        assert scenario.node_name == "clickhouse1"
    
    @pytest.mark.asyncio
    @patch('scripts.chaos.chaos_scenarios.docker')
    async def test_clickhouse_chaos_execution(self, mock_docker):
        """Test ClickHouse chaos execution with mocked Docker."""
        # Mock Docker client
        mock_container = AsyncMock()
        mock_container.stop = AsyncMock()
        mock_container.start = AsyncMock()
        mock_container.status = "running"
        
        mock_docker.from_env.return_value.containers.list.return_value = [mock_container]
        
        scenario = ClickHouseChaosScenario("clickhouse1", duration_seconds=5)
        
        # Execute chaos
        await scenario.execute_chaos()
        
        # Verify Docker calls
        mock_docker.from_env.assert_called_once()
        mock_container.stop.assert_called_once()
        
        # Restore service
        await scenario.restore_service()
        
        # Verify restoration calls
        mock_container.start.assert_called_once()


class TestPostgreSQLChaosScenario:
    """Test PostgreSQL chaos scenario."""
    
    @pytest.mark.asyncio
    @patch('scripts.chaos.chaos_scenarios.docker')
    async def test_postgres_scenario_creation(self, mock_docker):
        """Test PostgreSQL scenario creation."""
        scenario = PostgreSQLChaosScenario(duration_seconds=45)
        
        assert scenario.name == "postgresql_primary_chaos"
        assert scenario.services == ["pg-primary"]
        assert scenario.duration_seconds == 45


class TestChaosEngine:
    """Test ChaosEngine orchestrator."""
    
    @pytest.mark.asyncio
    async def test_engine_initialization(self):
        """Test chaos engine initialization."""
        scenarios = [MockChaosScenario("scenario1"), MockChaosScenario("scenario2")]
        slo_validator = MagicMock()
        slo_validator.check_slo_violations.return_value = {}
        
        engine = ChaosEngine(scenarios, slo_validator)
        
        assert len(engine.scenarios) == 2
        assert engine.slo_validator == slo_validator
        assert len(engine.execution_history) == 0
    
    @pytest.mark.asyncio
    async def test_run_scenario_success(self):
        """Test successful scenario execution."""
        scenario = MockChaosScenario("test_scenario")
        slo_validator = MagicMock()
        slo_validator.check_slo_violations.return_value = {}
        
        engine = ChaosEngine([scenario], slo_validator)
        
        result = await engine.run_scenario("test_scenario")
        
        assert result.status == ChaosStatus.COMPLETED
        assert result.scenario_name == "test_scenario"
        assert result.start_time is not None
        assert result.end_time is not None
        assert len(engine.execution_history) == 1
    
    @pytest.mark.asyncio
    async def test_run_scenario_failure(self):
        """Test failed scenario execution."""
        scenario = MockChaosScenario("test_scenario", should_fail=True)
        slo_validator = MagicMock()
        slo_validator.check_slo_violations.return_value = {}
        
        engine = ChaosEngine([scenario], slo_validator)
        
        result = await engine.run_scenario("test_scenario")
        
        assert result.status == ChaosStatus.FAILED
        assert result.error_message is not None
        assert len(engine.execution_history) == 1
    
    @pytest.mark.asyncio
    async def test_run_all_scenarios(self):
        """Test running all scenarios."""
        scenarios = [
            MockChaosScenario("scenario1"),
            MockChaosScenario("scenario2", should_fail=True),
            MockChaosScenario("scenario3")
        ]
        slo_validator = MagicMock()
        slo_validator.check_slo_violations.return_value = {}
        
        engine = ChaosEngine(scenarios, slo_validator)
        
        results = await engine.run_all_scenarios()
        
        assert len(results) == 3
        assert len(engine.execution_history) == 3
        
        # Check success/failure counts
        successful = sum(1 for r in results if r.status == ChaosStatus.COMPLETED)
        failed = sum(1 for r in results if r.status == ChaosStatus.FAILED)
        
        assert successful == 2
        assert failed == 1
    
    @pytest.mark.asyncio
    async def test_get_execution_summary(self):
        """Test execution summary generation."""
        scenarios = [MockChaosScenario("scenario1")]
        slo_validator = MagicMock()
        slo_validator.check_slo_violations.return_value = {}
        
        engine = ChaosEngine(scenarios, slo_validator)
        
        # Run a scenario
        await engine.run_scenario("scenario1")
        
        summary = engine.get_execution_summary()
        
        assert summary["total_scenarios"] == 1
        assert summary["successful"] == 1
        assert summary["failed"] == 0
        assert summary["success_rate"] == 1.0
        assert "average_recovery_time" in summary


class TestSLOValidator:
    """Test SLO Validator."""
    
    @pytest.mark.asyncio
    async def test_slo_validator_initialization(self):
        """Test SLO validator initialization."""
        thresholds = create_default_slo_thresholds()
        validator = ChaosSLOValidator(thresholds)
        
        assert len(validator.thresholds) == 3
        assert validator.baseline_metrics == {}
        assert validator.current_metrics == {}
    
    @pytest.mark.asyncio
    async def test_check_slo_violations(self):
        """Test SLO violation checking."""
        thresholds = [
            SLOThreshold(SLOMetric.AVAILABILITY, 99.5, 99.0),
            SLOThreshold(SLOMetric.LATENCY_P95, 5000, 10000)
        ]
        validator = ChaosSLOValidator(thresholds)
        
        # Mock current metrics
        validator.get_current_metrics = AsyncMock(return_value={
            "availability": 99.0,  # Below warning threshold
            "latency_p95": 6000    # Above warning threshold
        })
        
        violations = await validator.check_slo_violations()
        
        assert "availability" in violations
        assert "latency_p95" in violations
        assert violations["availability"] == 99.0
        assert violations["latency_p95"] == 6000
    
    @pytest.mark.asyncio
    async def test_wait_for_recovery(self):
        """Test recovery wait functionality."""
        thresholds = [SLOThreshold(SLOMetric.AVAILABILITY, 99.5, 99.0)]
        validator = ChaosSLOValidator(thresholds)
        
        # Mock no violations after some time
        validator.check_slo_violations = AsyncMock(return_value={})
        validator._sleep = AsyncMock()
        
        result = await validator.wait_for_recovery(timeout_seconds=10)
        
        assert result is True
        validator.check_slo_violations.assert_called()
        validator._sleep.assert_called()


class TestWeeklyChaosEngine:
    """Test Weekly Chaos Engine."""
    
    @pytest.mark.asyncio
    async def test_weekly_engine_initialization(self):
        """Test weekly chaos engine initialization."""
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            
            engine = WeeklyChaosEngine(output_dir)
            
            assert engine.output_dir == output_dir
            assert engine.reporter is not None
            assert engine.notifier is not None
            assert len(engine.scenarios) > 0
    
    @pytest.mark.asyncio
    async def test_run_chaos_session(self):
        """Test running a chaos session."""
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            
            engine = WeeklyChaosEngine(output_dir)
            
            # Use mock scenarios to avoid Docker dependencies
            engine.chaos_engine.scenarios = [
                MockChaosScenario("scenario1"),
                MockChaosScenario("scenario2")
            ]
            
            result = await engine.run_chaos_session()
            
            assert "summary" in result
            assert "results" in result
            assert "report_file" in result
            assert "summary_file" in result
            
            # Check that report files were created
            assert Path(result["report_file"]).exists()
            assert Path(result["summary_file"]).exists()
    
    @pytest.mark.asyncio
    async def test_run_single_scenario(self):
        """Test running a single scenario."""
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            
            engine = WeeklyChaosEngine(output_dir)
            
            # Use mock scenario
            engine.chaos_engine.scenarios = [MockChaosScenario("scenario1")]
            
            result = await engine.run_single_scenario("scenario1")
            
            assert "summary" in result
            assert "result" in result
            assert "report_file" in result
            assert "summary_file" in result


class TestChaosConfig:
    """Test Chaos Configuration."""
    
    def test_config_from_env(self):
        """Test loading configuration from environment."""
        with patch.dict('os.environ', {
            'CHAOS_ENGINEERING_ENABLED': 'true',
            'CHAOS_DEFAULT_DURATION_SECONDS': '120',
            'CHAOS_LOG_LEVEL': 'DEBUG'
        }):
            config = ChaosConfig.from_env()
            
            assert config.enabled is True
            assert config.default_duration_seconds == 120
            assert config.log_level == 'DEBUG'
    
    def test_config_validation(self):
        """Test configuration validation."""
        # Valid configuration
        config = ChaosConfig()
        errors = config.validate()
        assert len(errors) == 0
        
        # Invalid configuration
        config.default_duration_seconds = -1
        errors = config.validate()
        assert len(errors) > 0
        assert any("must be positive" in error for error in errors)


class TestIntegration:
    """Integration tests for the chaos engineering system."""
    
    @pytest.mark.asyncio
    async def test_full_chaos_workflow(self):
        """Test full chaos engineering workflow."""
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            
            # Create scenarios
            scenarios = [
                MockChaosScenario("integration_test_1"),
                MockChaosScenario("integration_test_2")
            ]
            
            # Create SLO validator
            slo_validator = ChaosSLOValidator(create_default_slo_thresholds())
            slo_validator.check_slo_violations = AsyncMock(return_value={})
            
            # Create chaos engine
            chaos_engine = ChaosEngine(scenarios, slo_validator)
            
            # Run scenarios
            results = await chaos_engine.run_all_scenarios()
            
            # Verify results
            assert len(results) == 2
            assert all(r.status == ChaosStatus.COMPLETED for r in results)
            
            # Create weekly chaos engine
            weekly_engine = WeeklyChaosEngine(output_dir)
            weekly_engine.chaos_engine = chaos_engine
            
            # Run chaos session
            session_result = await weekly_engine.run_chaos_session()
            
            # Verify session result
            assert "summary" in session_result
            assert "results" in session_result
            assert len(session_result["results"]) == 2
            
            # Verify reports were generated
            assert Path(session_result["report_file"]).exists()
            assert Path(session_result["summary_file"]).exists()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])