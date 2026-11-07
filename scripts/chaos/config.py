"""
Configuration for chaos engineering scenarios.
"""
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .base import SLOMetric, SLOThreshold


@dataclass
class ChaosConfig:
    """Configuration for chaos engineering."""
    
    # General settings
    enabled: bool = True
    output_dir: str = "docs/chaos-reports"
    log_level: str = "INFO"
    
    # SLO settings
    slo_thresholds: List[SLOThreshold] = field(default_factory=lambda: [
        SLOThreshold(
            metric=SLOMetric.AVAILABILITY,
            warning_threshold=99.5,
            critical_threshold=99.0,
            unit="%"
        ),
        SLOThreshold(
            metric=SLOMetric.LATENCY_P95,
            warning_threshold=5000.0,
            critical_threshold=10000.0,
            unit="ms"
        ),
        SLOThreshold(
            metric=SLOMetric.ERROR_BUDGET,
            warning_threshold=20.0,
            critical_threshold=50.0,
            unit="%"
        )
    ])
    
    # Chaos scenario settings
    default_duration_seconds: int = 60
    max_concurrent_scenarios: int = 1
    recovery_timeout_seconds: int = 300
    
    # Notification settings
    webhook_url: Optional[str] = None
    slack_webhook_url: Optional[str] = None
    telegram_bot_token: Optional[str] = None
    telegram_chat_id: Optional[str] = None
    
    # Service-specific settings
    service_health_check_timeout: int = 30
    service_recovery_wait_seconds: int = 10
    
    # Docker settings
    docker_timeout_seconds: int = 120
    
    # PP52 specific targets
    pp52_targets: List[str] = field(default_factory=lambda: [
        "clickhouse1",
        "clickhouse1",  # ch-1 (same as clickhouse1)
        "pg-primary",
        "vault-1",
        "redis-master",
        "minio1",
        "otel-collector-1",
        "langfuse-1"
    ])
    
    @classmethod
    def from_env(cls) -> "ChaosConfig":
        """Load configuration from environment variables."""
        config = cls()
        
        # General settings
        config.enabled = os.getenv("CHAOS_ENGINEERING_ENABLED", "true").lower() == "true"
        config.output_dir = os.getenv("CHAOS_OUTPUT_DIR", config.output_dir)
        config.log_level = os.getenv("CHAOS_LOG_LEVEL", config.log_level)
        
        # SLO settings
        if os.getenv("CHAOS_SLO_AVAILABILITY_WARNING"):
            config.slo_thresholds[0].warning_threshold = float(os.getenv("CHAOS_SLO_AVAILABILITY_WARNING"))
        if os.getenv("CHAOS_SLO_AVAILABILITY_CRITICAL"):
            config.slo_thresholds[0].critical_threshold = float(os.getenv("CHAOS_SLO_AVAILABILITY_CRITICAL"))
        
        if os.getenv("CHAOS_SLO_LATENCY_WARNING"):
            config.slo_thresholds[1].warning_threshold = float(os.getenv("CHAOS_SLO_LATENCY_WARNING"))
        if os.getenv("CHAOS_SLO_LATENCY_CRITICAL"):
            config.slo_thresholds[1].critical_threshold = float(os.getenv("CHAOS_SLO_LATENCY_CRITICAL"))
        
        if os.getenv("CHAOS_SLO_ERROR_BUDGET_WARNING"):
            config.slo_thresholds[2].warning_threshold = float(os.getenv("CHAOS_SLO_ERROR_BUDGET_WARNING"))
        if os.getenv("CHAOS_SLO_ERROR_BUDGET_CRITICAL"):
            config.slo_thresholds[2].critical_threshold = float(os.getenv("CHAOS_SLO_ERROR_BUDGET_CRITICAL"))
        
        # Chaos scenario settings
        config.default_duration_seconds = int(os.getenv("CHAOS_DEFAULT_DURATION_SECONDS", str(config.default_duration_seconds)))
        config.max_concurrent_scenarios = int(os.getenv("CHAOS_MAX_CONCURRENT_SCENARIOS", str(config.max_concurrent_scenarios)))
        config.recovery_timeout_seconds = int(os.getenv("CHAOS_RECOVERY_TIMEOUT_SECONDS", str(config.recovery_timeout_seconds)))
        
        # Notification settings
        config.webhook_url = os.getenv("CHAOS_WEBHOOK_URL")
        config.slack_webhook_url = os.getenv("CHAOS_SLACK_WEBHOOK_URL")
        config.telegram_bot_token = os.getenv("CHAOS_TELEGRAM_BOT_TOKEN")
        config.telegram_chat_id = os.getenv("CHAOS_TELEGRAM_CHAT_ID")
        
        # Service-specific settings
        config.service_health_check_timeout = int(os.getenv("CHAOS_HEALTH_CHECK_TIMEOUT", str(config.service_health_check_timeout)))
        config.service_recovery_wait_seconds = int(os.getenv("CHAOS_RECOVERY_WAIT_SECONDS", str(config.service_recovery_wait_seconds)))
        
        # Docker settings
        config.docker_timeout_seconds = int(os.getenv("CHAOS_DOCKER_TIMEOUT_SECONDS", str(config.docker_timeout_seconds)))
        
        return config
    
    def get_scenario_config(self, scenario_name: str) -> Dict:
        """Get configuration for a specific scenario."""
        # Service-specific configurations can be added here
        return {
            "duration_seconds": self.default_duration_seconds,
            "health_check_timeout": self.service_health_check_timeout,
            "recovery_wait_seconds": self.service_recovery_wait_seconds,
            "docker_timeout": self.docker_timeout_seconds
        }
    
    def validate(self) -> List[str]:
        """Validate configuration and return list of errors."""
        errors = []
        
        if not self.enabled:
            return ["Chaos engineering is disabled"]
        
        if self.default_duration_seconds <= 0:
            errors.append("CHAOS_DEFAULT_DURATION_SECONDS must be positive")
        
        if self.max_concurrent_scenarios <= 0:
            errors.append("CHAOS_MAX_CONCURRENT_SCENARIOS must be positive")
        
        if self.recovery_timeout_seconds <= 0:
            errors.append("CHAOS_RECOVERY_TIMEOUT_SECONDS must be positive")
        
        if self.service_health_check_timeout <= 0:
            errors.append("CHAOS_HEALTH_CHECK_TIMEOUT must be positive")
        
        if self.service_recovery_wait_seconds <= 0:
            errors.append("CHAOS_RECOVERY_WAIT_SECONDS must be positive")
        
        if self.docker_timeout_seconds <= 0:
            errors.append("CHAOS_DOCKER_TIMEOUT_SECONDS must be positive")
        
        # Validate SLO thresholds
        for threshold in self.slo_thresholds:
            if threshold.warning_threshold <= 0:
                errors.append(f"SLO warning threshold must be positive for {threshold.metric}")
            if threshold.critical_threshold <= 0:
                errors.append(f"SLO critical threshold must be positive for {threshold.metric}")
            if threshold.warning_threshold >= threshold.critical_threshold:
                errors.append(f"SLO warning threshold must be less than critical threshold for {threshold.metric}")
        
        # Validate notification settings
        if self.webhook_url and not self.webhook_url.startswith(("http://", "https://")):
            errors.append("CHAOS_WEBHOOK_URL must be a valid HTTP/HTTPS URL")
        
        if self.slack_webhook_url and not self.slack_webhook_url.startswith(("http://", "https://")):
            errors.append("CHAOS_SLACK_WEBHOOK_URL must be a valid HTTP/HTTPS URL")
        
        if self.telegram_bot_token and not self.telegram_bot_token.startswith("bot"):
            errors.append("CHAOS_TELEGRAM_BOT_TOKEN must start with 'bot'")
        
        if self.telegram_chat_id and not self.telegram_chat_id.isdigit():
            errors.append("CHAOS_TELEGRAM_CHAT_ID must be a numeric ID")
        
        return errors


@dataclass
class ChaosScheduleConfig:
    """Configuration for chaos engineering schedule."""
    
    # Cron schedule for weekly chaos
    weekly_cron: str = "0 2 * * 0"  # Sunday at 2 AM
    
    # Schedule settings
    enabled: bool = True
    timezone: str = "UTC"
    
    # Chaos session settings
    random_order: bool = True
    include_all_services: bool = True
    exclude_services: List[str] = field(default_factory=list)
    
    @classmethod
    def from_env(cls) -> "ChaosScheduleConfig":
        """Load schedule configuration from environment variables."""
        config = cls()
        
        config.enabled = os.getenv("CHAOS_SCHEDULE_ENABLED", "true").lower() == "true"
        config.weekly_cron = os.getenv("CHAOS_WEEKLY_CRON", config.weekly_cron)
        config.timezone = os.getenv("CHAOS_TIMEZONE", config.timezone)
        config.random_order = os.getenv("CHAOS_RANDOM_ORDER", "true").lower() == "true"
        config.include_all_services = os.getenv("CHAOS_INCLUDE_ALL_SERVICES", "true").lower() == "true"
        
        exclude_services = os.getenv("CHAOS_EXCLUDE_SERVICES", "")
        if exclude_services:
            config.exclude_services = [s.strip() for s in exclude_services.split(",")]
        
        return config
    
    def get_target_scenarios(self, all_scenarios: List[str]) -> List[str]:
        """Get list of scenarios to run based on schedule configuration."""
        if self.include_all_services:
            scenarios = all_scenarios.copy()
        else:
            scenarios = []
        
        # Exclude specified services
        scenarios = [s for s in scenarios if not any(exclude in s for exclude in self.exclude_services)]
        
        return scenarios


# Global configuration instance
config = ChaosConfig.from_env()
schedule_config = ChaosScheduleConfig.from_env()

# Validate configuration
validation_errors = config.validate()
if validation_errors:
    print("Chaos engineering configuration errors:")
    for error in validation_errors:
        print(f"  - {error}")
    exit(1)