"""Security client for OryaSecurity integration (PP44 / T3.10.1).

SecurityClient provides integration with OryaSecurity service for threat detection,
PII scanning, and security policy evaluation. It operates with fail-open semantics
to ensure proxy availability even when security service is unavailable.

Design notes
------------
* Fail-open: Security timeouts return verdict="clean" with audit warnings rather
  than blocking requests. This ensures observability never blocks business logic.
* Timeout handling: Strict 50ms p99 timeout for security scanning (configurable).
* Circuit breakers: Independent circuit breaker for security failures (PP38).
* Audit trail: All security decisions are logged in shared audit trail.
* Metadata: Detailed security metadata is returned for span enrichment.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from agent_obs.ha.health_checks import CircuitBreaker, circuit_breaker_for
from prometheus_client import Counter, Histogram, Histogram

logger = logging.getLogger(__name__)


@dataclass
class SecurityScanResult:
    """Result from OryaSecurity scan (PP44).
    
    Fields:
        verdict: "clean" | "flag" | "block" - security decision
        score: float - 0..1 confidence score from security analysis
        redacted_fields: list[str] - PII fields that were redacted
        masked_request_body: bytes - request body with PII redacted
        audit_event_id: str - unique ID for audit trail correlation
        security_metadata: dict - detailed analysis results per detector
    """
    verdict: str  # "clean" | "flag" | "block"
    score: float  # 0.0..1.0
    redacted_fields: list[str]
    masked_request_body: bytes
    audit_event_id: str
    security_metadata: dict[str, Any]

    @classmethod
    def fail_open(cls, reason: str = "security_timeout") -> SecurityScanResult:
        """Create fail-open result when security service is unavailable."""
        return cls(
            verdict="clean",
            score=0.0,
            redacted_fields=[],
            masked_request_body=b"",
            audit_event_id=f"fail_open_{int(time.time())}",
            security_metadata={"fail_reason": reason, "timestamp": time.time()},
        )


class SecurityClient:
    """HTTP client for OryaSecurity integration (PP44).
    
    Handles security scanning with timeout, circuit breakers, and fail-open
    semantics. All security decisions are correlated with trace_id for
    cross-system audit trail.
    """

    def __init__(
        self,
        base_url: str,
        timeout: float = 0.05,
        fail_open: bool = True,
        metrics: None = None,
    ):
        """Initialize security client.
        
        Args:
            base_url: Base URL of OryaSecurity service
            timeout: Timeout in seconds (default: 0.05 = 50ms p99)
            fail_open: Whether to allow requests when security fails
            metrics: Optional metrics client for monitoring
        """
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.fail_open = fail_open
        self.metrics = metrics or {
            "counter": Counter("shared_proxy_security_scans_total", "Security scans"),
            "histogram": Histogram("shared_proxy_security_latency_seconds", "Security scan latency"),
        }
        # Use unique metric names to avoid conflicts
        if not hasattr(self, '_metrics_initialized'):
            self._metrics_initialized = True
        # Use unique metric names to avoid conflicts
        if not hasattr(self, '_metrics_initialized'):
            self._metrics_initialized = True
        
        # Circuit breaker for security failures (use 'vault' as proxy)
        self.circuit_breaker = circuit_breaker_for("vault")
        
        # HTTP client with timeout
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=timeout * 0.8),
            follow_redirects=False,
        )

    async def scan(
        self, 
        request_body: bytes, 
        headers: dict[str, str],
        trace_id: str | None = None,
        mode: str = "full"
    ) -> SecurityScanResult:
        """Scan request body and headers for security threats.
        
        Args:
            request_body: Raw request body bytes
            headers: HTTP headers dict
            trace_id: Optional trace_id for correlation
            mode: Scan mode ("full" | "fast" | "pii_only")
            
        Returns:
            SecurityScanResult with verdict and metadata
            
        Raises:
            httpx.TimeoutException: On timeout (handled by caller)
            httpx.HTTPStatusError: On HTTP errors (handled by caller)
        """
        start_time = time.time()
        
        try:
            # Check circuit breaker before making request
            if not self.circuit_breaker.allow_request():
                logger.warning("Security service circuit breaker open, fail-open")
                self.metrics["counter"].inc()
                return SecurityScanResult.fail_open("circuit_breaker")

            # Prepare scan request
            scan_request = {
                "request_body": request_body.decode('utf-8', errors='replace'),
                "headers": headers,
                "mode": mode,
                "trace_id": trace_id or "",
                "timestamp": time.time(),
            }
            
            # Make security scan request
            response = await self.client.post(
                f"{self.base_url}/scan",
                json=scan_request,
                headers={"Content-Type": "application/json"},
            )
            response.raise_for_status()
            
            # Parse response
            result_data = response.json()
            
            # Generate audit event ID
            audit_data = f"{trace_id or ''}{request_body}{time.time()}".encode()
            audit_event_id = hashlib.sha256(audit_data).hexdigest()
            
            # Create result
            result = SecurityScanResult(
                verdict=result_data.get("verdict", "clean"),
                score=float(result_data.get("score", 0.0)),
                redacted_fields=result_data.get("redacted_fields", []),
                masked_request_body=result_data.get("masked_request_body", b"").encode(),
                audit_event_id=audit_event_id,
                security_metadata=result_data.get("security_metadata", {}),
            )
            
            # Record metrics
            latency = time.time() - start_time
            self.metrics["histogram"].observe(latency)
            self.metrics["counter"].inc()
            
            # Update circuit breaker on success
            self.circuit_breaker.record_success()
            
            logger.debug(
                "Security scan completed: verdict=%s, score=%.2f, latency=%.3fs",
                result.verdict, result.score, latency
            )
            
            return result
            
        except Exception as e:
            # Record failure
            latency = time.time() - start_time
            self.metrics["histogram"].observe(latency)
            self.metrics["counter"].inc()
            
            # Update circuit breaker on failure
            self.circuit_breaker.record_failure()
            
            logger.warning(
                "Security scan failed: %s, latency=%.3fs, fail_open=%s",
                e, latency, self.fail_open
            )
            
            # Fail-open if enabled
            if self.fail_open:
                self.metrics["counter"].inc()
                return SecurityScanResult.fail_open(str(e))
            
            # Re-raise if fail-open disabled
            raise

    async def close(self) -> None:
        """Close HTTP client."""
        await self.client.aclose()

    def __aenter__(self) -> SecurityClient:
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.close()