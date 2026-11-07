"""
Base classes for chaos engineering scenarios.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Dict, List, Optional, Protocol

from pydantic import BaseModel, Field


class ChaosStatus(Enum):
    """Status of a chaos scenario execution."""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    ABORTED = "aborted"


class SLOMetric(Enum):
    """SLO metrics to monitor during chaos."""
    AVAILABILITY = "availability"
    LATENCY_P95 = "latency_p95"
    ERROR_BUDGET = "error_budget"


@dataclass
class SLOThreshold:
    """SLO threshold configuration."""
    metric: SLOMetric
    warning_threshold: float
    critical_threshold: float
    unit: str = "%"


@dataclass
class ChaosResult:
    """Result of a chaos scenario execution."""
    scenario_name: str
    start_time: datetime
    end_time: Optional[datetime] = None
    status: ChaosStatus = ChaosStatus.PENDING
    services_affected: List[str] = field(default_factory=list)
    slo_violations: Dict[str, float] = field(default_factory=dict)
    error_message: Optional[str] = None
    recovery_time_seconds: Optional[float] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


class ChaosScenario(ABC):
    """Base class for chaos scenarios."""
    
    def __init__(self, name: str, services: List[str], duration_seconds: int = 60):
        self.name = name
        self.services = services
        self.duration_seconds = duration_seconds
        self.result = ChaosResult(
            scenario_name=name,
            start_time=datetime.now()
        )
    
    @abstractmethod
    async def execute_chaos(self) -> None:
        """Execute the chaos scenario."""
        pass
    
    @abstractmethod
    async def restore_service(self) -> None:
        """Restore the service to normal state."""
        pass
    
    @abstractmethod
    async def validate_service_health(self) -> bool:
        """Validate that service is healthy after chaos."""
        pass
    
    async def pre_chaos_check(self) -> bool:
        """Check if services are healthy before chaos."""
        return True
    
    async def post_chaos_check(self) -> bool:
        """Check if services are healthy after chaos."""
        return True


class SLOValidator:
    """Validates SLOs during chaos testing."""
    
    def __init__(self, thresholds: List[SLOThreshold]):
        self.thresholds = thresholds
        self.baseline_metrics: Dict[str, float] = {}
        self.current_metrics: Dict[str, float] = {}
    
    async def get_current_metrics(self) -> Dict[str, float]:
        """Get current SLO metrics."""
        # This would be implemented with actual metric collection
        return {}
    
    async def check_slo_violations(self) -> Dict[str, float]:
        """Check for SLO violations."""
        violations = {}
        current_metrics = await self.get_current_metrics()
        
        for threshold in self.thresholds:
            metric_value = current_metrics.get(threshold.metric.value, 0.0)
            if metric_value > threshold.critical_threshold:
                violations[threshold.metric.value] = metric_value
        
        return violations
    
    async def wait_for_recovery(self, timeout_seconds: int = 300) -> bool:
        """Wait for services to recover within SLO thresholds."""
        start_time = datetime.now()
        
        while (datetime.now() - start_time).total_seconds() < timeout_seconds:
            violations = await self.check_slo_violations()
            if not violations:
                return True
            await self._sleep(5)
        
        return False
    
    @staticmethod
    async def _sleep(seconds: int):
        """Sleep for specified seconds."""
        import asyncio
        await asyncio.sleep(seconds)


class ChaosEngine:
    """Main chaos engineering orchestrator."""
    
    def __init__(self, scenarios: List[ChaosScenario], slo_validator: SLOValidator):
        self.scenarios = scenarios
        self.slo_validator = slo_validator
        self.execution_history: List[ChaosResult] = []
        self.current_scenario: Optional[ChaosScenario] = None
    
    async def run_scenario(self, scenario_name: str) -> ChaosResult:
        """Run a specific chaos scenario."""
        scenario = next((s for s in self.scenarios if s.name == scenario_name), None)
        if not scenario:
            raise ValueError(f"Scenario {scenario_name} not found")
        
        self.current_scenario = scenario
        scenario.result.status = ChaosStatus.RUNNING
        scenario.result.start_time = datetime.now()
        
        try:
            # Pre-chaos validation
            if not await scenario.pre_chaos_check():
                scenario.result.status = ChaosStatus.FAILED
                scenario.result.error_message = "Pre-chaos health check failed"
                return scenario.result
            
            # Execute chaos
            await scenario.execute_chaos()
            
            # Monitor SLOs during chaos
            slo_violations = await self.slo_validator.check_slo_violations()
            scenario.result.slo_violations = slo_violations
            
            # Post-chaos validation
            if not await scenario.post_chaos_check():
                scenario.result.status = ChaosStatus.FAILED
                scenario.result.error_message = "Post-chaos health check failed"
                return scenario.result
            
            # Restore service
            await scenario.restore_service()
            
            # Wait for recovery
            recovery_start = datetime.now()
            recovered = await self.slo_validator.wait_for_recovery()
            recovery_end = datetime.now()
            
            if recovered:
                scenario.result.status = ChaosStatus.COMPLETED
                scenario.result.recovery_time_seconds = (recovery_end - recovery_start).total_seconds()
            else:
                scenario.result.status = ChaosStatus.FAILED
                scenario.result.error_message = "Service recovery timeout"
            
        except Exception as e:
            scenario.result.status = ChaosStatus.FAILED
            scenario.result.error_message = str(e)
            try:
                await scenario.restore_service()
            except Exception:
                pass
        
        finally:
            scenario.result.end_time = datetime.now()
            self.execution_history.append(scenario.result)
        
        return scenario.result
    
    async def run_all_scenarios(self) -> List[ChaosResult]:
        """Run all chaos scenarios."""
        results = []
        for scenario in self.scenarios:
            result = await self.run_scenario(scenario.name)
            results.append(result)
        return results
    
    def get_execution_summary(self) -> Dict[str, Any]:
        """Get summary of all executions."""
        total_scenarios = len(self.execution_history)
        successful = len([r for r in self.execution_history if r.status == ChaosStatus.COMPLETED])
        failed = len([r for r in self.execution_history if r.status == ChaosStatus.FAILED])
        
        return {
            "total_scenarios": total_scenarios,
            "successful": successful,
            "failed": failed,
            "success_rate": successful / total_scenarios if total_scenarios > 0 else 0,
            "average_recovery_time": sum(r.recovery_time_seconds or 0 for r in self.execution_history) / total_scenarios if total_scenarios > 0 else 0
        }