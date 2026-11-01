"""High-availability primitives for the observability SDK (PP35–PP38)."""

from agent_obs.ha.health_checks import (
    DEPENDENCIES,
    CircuitBreaker,
    CircuitOpenError,
    CircuitState,
    Health,
    HealthChecker,
    circuit_breaker_for,
    reset_circuit_breakers,
)

__all__ = [
    "DEPENDENCIES",
    "CircuitBreaker",
    "CircuitOpenError",
    "CircuitState",
    "Health",
    "HealthChecker",
    "circuit_breaker_for",
    "reset_circuit_breakers",
]
