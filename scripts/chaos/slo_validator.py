"""
SLO Validator for chaos engineering scenarios.
"""
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import aiohttp
from pydantic import BaseModel, Field

from .base import SLOMetric, SLOThreshold, SLOValidator


class MetricData(BaseModel):
    """Metric data point."""
    timestamp: datetime
    value: float
    metric: SLOMetric
    service: str
    tags: Dict[str, str] = Field(default_factory=dict)


class SLOMetricsCollector:
    """Collects SLO metrics from various sources."""
    
    def __init__(self):
        self.session: Optional[aiohttp.ClientSession] = None
        self.base_urls = {
            "clickhouse": "http://localhost:8123",
            "postgres": "http://localhost:5432",
            "redis": "http://localhost:6379",
            "vault": "http://localhost:8200",
            "minio": "http://localhost:9000",
            "otel": "http://localhost:18888",
            "langfuse": "http://localhost:3000"
        }
    
    async def __aenter__(self):
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.session:
            await self.session.close()
    
    async def get_clickhouse_metrics(self) -> Dict[str, float]:
        """Get ClickHouse metrics."""
        metrics = {}
        
        try:
            # Query for availability
            query = """
            SELECT 
                avg(status) as availability,
                quantile(0.95)(duration_ms) as p95_latency
            FROM system.metrics
            WHERE metric IN ['Query', 'SelectQuery']
            """
            
            # This would be implemented with actual ClickHouse client
            # For now, simulate metrics
            metrics["clickhouse_availability"] = 99.9
            metrics["clickhouse_latency_p95"] = 10.5
            
        except Exception as e:
            logging.error(f"Failed to get ClickHouse metrics: {e}")
            metrics["clickhouse_availability"] = 0.0
            metrics["clickhouse_latency_p95"] = 1000.0
        
        return metrics
    
    async def get_postgres_metrics(self) -> Dict[str, float]:
        """Get PostgreSQL metrics."""
        metrics = {}
        
        try:
            # Query for PostgreSQL availability and latency
            # This would be implemented with actual PostgreSQL monitoring
            # For now, simulate metrics
            metrics["postgres_availability"] = 99.8
            metrics["postgres_latency_p95"] = 25.3
            
        except Exception as e:
            logging.error(f"Failed to get PostgreSQL metrics: {e}")
            metrics["postgres_availability"] = 0.0
            metrics["postgres_latency_p95"] = 5000.0
        
        return metrics
    
    async def get_redis_metrics(self) -> Dict[str, float]:
        """Get Redis metrics."""
        metrics = {}
        
        try:
            # Query Redis for availability and performance
            # This would be implemented with actual Redis monitoring
            # For now, simulate metrics
            metrics["redis_availability"] = 99.9
            metrics["redis_latency_p95"] = 5.2
            
        except Exception as e:
            logging.error(f"Failed to get Redis metrics: {e}")
            metrics["redis_availability"] = 0.0
            metrics["redis_latency_p95"] = 1000.0
        
        return metrics
    
    async def get_vault_metrics(self) -> Dict[str, float]:
        """Get Vault metrics."""
        metrics = {}
        
        try:
            # Query Vault API for health status
            # This would be implemented with actual Vault client
            # For now, simulate metrics
            metrics["vault_availability"] = 99.7
            metrics["vault_latency_p95"] = 15.8
            
        except Exception as e:
            logging.error(f"Failed to get Vault metrics: {e}")
            metrics["vault_availability"] = 0.0
            metrics["vault_latency_p95"] = 2000.0
        
        return metrics
    
    async def get_minio_metrics(self) -> Dict[str, float]:
        """Get MinIO metrics."""
        metrics = {}
        
        try:
            # Query MinIO for availability and performance
            # This would be implemented with actual MinIO monitoring
            # For now, simulate metrics
            metrics["minio_availability"] = 99.9
            metrics["minio_latency_p95"] = 8.7
            
        except Exception as e:
            logging.error(f"Failed to get MinIO metrics: {e}")
            metrics["minio_availability"] = 0.0
            metrics["minio_latency_p95"] = 1500.0
        
        return metrics
    
    async def get_otel_metrics(self) -> Dict[str, float]:
        """Get OTel Collector metrics."""
        metrics = {}
        
        try:
            # Query OTel Collector health endpoint
            # This would be implemented with actual OTel monitoring
            # For now, simulate metrics
            metrics["otel_availability"] = 99.8
            metrics["otel_latency_p95"] = 12.3
            
        except Exception as e:
            logging.error(f"Failed to get OTel metrics: {e}")
            metrics["otel_availability"] = 0.0
            metrics["otel_latency_p95"] = 2000.0
        
        return metrics
    
    async def get_langfuse_metrics(self) -> Dict[str, float]:
        """Get Langfuse metrics."""
        metrics = {}
        
        try:
            # Query Langfuse API for health status
            # This would be implemented with actual Langfuse client
            # For now, simulate metrics
            metrics["langfuse_availability"] = 99.9
            metrics["langfuse_latency_p95"] = 20.1
            
        except Exception as e:
            logging.error(f"Failed to get Langfuse metrics: {e}")
            metrics["langfuse_availability"] = 0.0
            metrics["langfuse_latency_p95"] = 3000.0
        
        return metrics
    
    async def get_all_metrics(self) -> Dict[str, float]:
        """Get metrics from all services."""
        all_metrics = {}
        
        # Get metrics from all services concurrently
        tasks = [
            self.get_clickhouse_metrics(),
            self.get_postgres_metrics(),
            self.get_redis_metrics(),
            self.get_vault_metrics(),
            self.get_minio_metrics(),
            self.get_otel_metrics(),
            self.get_langfuse_metrics()
        ]
        
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        for result in results:
            if isinstance(result, Exception):
                logging.error(f"Error collecting metrics: {result}")
            else:
                all_metrics.update(result)
        
        return all_metrics


class ChaosSLOValidator(SLOValidator):
    """SLO Validator specifically for chaos scenarios."""
    
    def __init__(self, thresholds: List[SLOThreshold]):
        super().__init__(thresholds)
        self.collector = SLOMetricsCollector()
        self.metric_history: List[MetricData] = []
    
    async def get_current_metrics(self) -> Dict[str, float]:
        """Get current SLO metrics."""
        try:
            async with self.collector as collector:
                metrics = await collector.get_all_metrics()
                
                # Store metric history
                for metric_name, value in metrics.items():
                    metric_enum = self._map_metric_name(metric_name)
                    if metric_enum:
                        metric_data = MetricData(
                            timestamp=datetime.now(),
                            value=value,
                            metric=metric_enum,
                            service=metric_name.split("_")[0],
                            tags={"chaos_testing": "true"}
                        )
                        self.metric_history.append(metric_data)
                
                return metrics
                
        except Exception as e:
            logging.error(f"Failed to get current metrics: {e}")
            return {}
    
    def _map_metric_name(self, metric_name: str) -> Optional[SLOMetric]:
        """Map metric name to SLOMetric enum."""
        if "availability" in metric_name:
            return SLOMetric.AVAILABILITY
        elif "latency_p95" in metric_name:
            return SLOMetric.LATENCY_P95
        elif "error_budget" in metric_name:
            return SLOMetric.ERROR_BUDGET
        return None
    
    def get_metric_history(self, service: str, metric: SLOMetric, 
                          hours: int = 1) -> List[MetricData]:
        """Get metric history for a specific service and metric."""
        cutoff_time = datetime.now() - timedelta(hours=hours)
        
        return [
            data for data in self.metric_history
            if data.service == service and data.metric == metric and data.timestamp >= cutoff_time
        ]
    
    def calculate_slo_compliance(self, service: str) -> Dict[str, float]:
        """Calculate SLO compliance for a service."""
        compliance = {}
        
        for threshold in self.thresholds:
            metric_name = threshold.metric.value
            service_metric = f"{service}_{metric_name}"
            
            # Get recent metric values
            history = self.get_metric_history(service, threshold.metric, hours=1)
            
            if history:
                # Calculate compliance percentage
                values = [data.value for data in history]
                compliant_values = [v for v in values if v <= threshold.warning_threshold]
                compliance[metric_name] = len(compliant_values) / len(values) * 100
            else:
                compliance[metric_name] = 0.0
        
        return compliance
    
    def generate_slo_report(self) -> Dict[str, any]:
        """Generate SLO compliance report."""
        report = {
            "timestamp": datetime.now().isoformat(),
            "services": {},
            "overall_compliance": 0.0,
            "total_services": 0,
            "compliant_services": 0
        }
        
        total_compliance = 0.0
        compliant_count = 0
        
        # Get all unique services from metric history
        services = list(set(data.service for data in self.metric_history))
        
        for service in services:
            service_report = {
                "name": service,
                "compliance": self.calculate_slo_compliance(service),
                "metrics": {}
            }
            
            # Add detailed metric information
            for threshold in self.thresholds:
                metric_name = threshold.metric.value
                history = self.get_metric_history(service, threshold.metric, hours=1)
                
                if history:
                    values = [data.value for data in history]
                    service_report["metrics"][metric_name] = {
                        "current_value": values[-1] if values else 0.0,
                        "min_value": min(values),
                        "max_value": max(values),
                        "avg_value": sum(values) / len(values),
                        "threshold_warning": threshold.warning_threshold,
                        "threshold_critical": threshold.critical_threshold,
                        "compliant": values[-1] <= threshold.warning_threshold if values else False
                    }
                    
                    # Update overall compliance
                    if service_report["compliance"][metric_name] >= 95.0:
                        compliant_count += 1
                
                total_compliance += service_report["compliance"][metric_name]
            
            report["services"][service] = service_report
            report["total_services"] += 1
        
        report["compliant_services"] = compliant_count
        report["overall_compliance"] = total_compliance / (len(services) * len(self.thresholds)) if services and self.thresholds else 0.0
        
        return report


def create_default_slo_thresholds() -> List[SLOThreshold]:
    """Create default SLO thresholds for chaos testing."""
    return [
        SLOThreshold(
            metric=SLOMetric.AVAILABILITY,
            warning_threshold=99.5,
            critical_threshold=99.0,
            unit="%"
        ),
        SLOThreshold(
            metric=SLOMetric.LATENCY_P95,
            warning_threshold=5000.0,  # 5 seconds
            critical_threshold=10000.0,  # 10 seconds
            unit="ms"
        ),
        SLOThreshold(
            metric=SLOMetric.ERROR_BUDGET,
            warning_threshold=20.0,  # 20% error budget
            critical_threshold=50.0,  # 50% error budget
            unit="%"
        )
    ]