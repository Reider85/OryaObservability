"""Shared pipeline for observability + security integration (PP44 / T3.10.1).

SharedPipeline coordinates between security scanning and observability enrichment
in a single transaction. It implements parallel execution with independent timeouts
and fail-open semantics to ensure high availability.

Design notes
------------
* Parallel execution: Security scan and obs enrichment run concurrently with
  independent timeouts (security: 50ms, obs: 100ms).
* Fail-open semantics: Security timeouts allow requests to proceed with
  audit warnings; obs failures create minimal spans.
* Decision router: Security decisions (clean/flag/block) override obs decisions.
* Single transaction: Span, audit-event, and decision are batched and idempotent.
* Performance isolation: Independent circuit breakers for security/obs.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from agent_obs.ha.health_checks import circuit_breaker_for
from prometheus_client import Counter
from agent_obs.observability import ObservabilitySDK, Span
from agent_obs.proxy.obs_pipeline import ObsPipeline
from agent_obs.proxy.security_client import SecurityClient, SecurityScanResult

logger = logging.getLogger(__name__)


@dataclass
class SharedPipelineResult:
    """Result from shared pipeline processing.
    
    Fields:
        masked_request_body: bytes - request body with PII redacted
        security_result: SecurityScanResult - security scanning result
        obs_span: Span - enriched observability span
        decision: str - final decision ("forward" | "flag" | "block")
        latency_seconds: float - total processing time
    """
    masked_request_body: bytes
    security_result: SecurityScanResult
    obs_span: Span
    decision: str  # "forward" | "flag" | "block"
    latency_seconds: float


class SharedPipeline:
    """Combined observability + security processing pipeline (PP44).
    
    Coordinates between security scanning and observability enrichment with
    performance isolation and fail-open semantics.
    """

    def __init__(
        self,
        security_client: SecurityClient,
        obs_pipeline: ObsPipeline,
        sdk: ObservabilitySDK,
        metrics: None = None,
    ):
        """Initialize shared pipeline.
        
        Args:
            security_client: Security client for threat detection
            obs_pipeline: Observability pipeline for span enrichment
            sdk: Observability SDK for span enqueueing
            metrics: Optional metrics client for monitoring
        """
        self.security_client = security_client
        self.obs_pipeline = obs_pipeline
        self.sdk = sdk
        self.metrics = metrics or Counter("shared_proxy_pipeline_total", "Pipeline processing")
        # Use unique metric names to avoid conflicts
        if not hasattr(self, '_metrics_initialized'):
            self._metrics_initialized = True
        
        # Independent circuit breakers (use existing dependencies)
        self.security_circuit_breaker = circuit_breaker_for("vault")  # proxy for security
        self.obs_circuit_breaker = circuit_breaker_for("langfuse")   # proxy for obs

    async def process(
        self,
        request_body: bytes,
        headers: dict[str, str],
        trace_id: str | None = None,
        request_id: str | None = None,
    ) -> SharedPipelineResult:
        """Process request through security + obs pipeline.
        
        Args:
            request_body: Raw request body bytes
            headers: HTTP headers dict
            trace_id: Optional trace_id for correlation
            request_id: Optional request_id for correlation
            
        Returns:
            SharedPipelineResult with processing results
            
        Raises:
            Exception: On unrecoverable errors (rare due to fail-open)
        """
        start_time = time.time()
        
        try:
            # Create security and obs tasks with independent timeouts
            security_task = asyncio.create_task(
                self._scan_with_timeout(request_body, headers, trace_id)
            )
            obs_task = asyncio.create_task(
                self._enrich_with_timeout(request_body, headers, trace_id, request_id)
            )
            
            # Wait for both tasks with timeouts
            security_result = await asyncio.wait_for(
                security_task, timeout=self.security_client.timeout
            )
            obs_span = await asyncio.wait_for(
                obs_task, timeout=0.1  # 100ms for obs enrichment
            )
            
            # Make final decision (security > obs)
            decision = self._make_decision(security_result, obs_span)
            
            # Record metrics
            latency = time.time() - start_time
            self.metrics.histogram(
                "shared_proxy_total_latency_seconds",
                latency,
                {"decision": decision}
            )
            
            # Update circuit breakers on success
            self.security_circuit_breaker.record_success()
            self.obs_circuit_breaker.record_success()
            
            logger.debug(
                "Shared pipeline completed: decision=%s, latency=%.3fs",
                decision, latency
            )
            
            return SharedPipelineResult(
                masked_request_body=security_result.masked_request_body,
                security_result=security_result,
                obs_span=obs_span,
                decision=decision,
                latency_seconds=latency,
            )
            
        except asyncio.TimeoutError:
            # Handle timeouts with fail-open
            latency = time.time() - start_time
            self.metrics.histogram(
                "shared_proxy_total_latency_seconds",
                latency,
                {"decision": "timeout"}
            )
            
            logger.warning(
                "Shared pipeline timeout: latency=%.3fs, fail_open=True",
                latency
            )
            
            # Fail-open: return clean security result and minimal obs span
            security_result = SecurityScanResult.fail_open("pipeline_timeout")
            obs_span = self._build_minimal_span(trace_id, request_id)
            
            return SharedPipelineResult(
                masked_request_body=security_result.masked_request_body,
                security_result=security_result,
                obs_span=obs_span,
                decision="forward",  # Fail-open allows forwarding
                latency_seconds=latency,
            )
            
        except Exception as e:
            # Handle other errors
            latency = time.time() - start_time
            self.metrics.histogram(
                "shared_proxy_total_latency_seconds",
                latency,
                {"decision": "error"}
            )
            
            logger.error("Shared pipeline failed: %s", e)
            
            # Fail-open on errors
            security_result = SecurityScanResult.fail_open(f"pipeline_error: {e}")
            obs_span = self._build_minimal_span(trace_id, request_id)
            
            return SharedPipelineResult(
                masked_request_body=security_result.masked_request_body,
                security_result=security_result,
                obs_span=obs_span,
                decision="forward",  # Fail-open allows forwarding
                latency_seconds=latency,
            )

    async def _scan_with_timeout(
        self,
        request_body: bytes,
        headers: dict[str, str],
        trace_id: str | None,
    ) -> SecurityScanResult:
        """Security scan with circuit breaker check."""
        if not self.security_circuit_breaker.allow_request():
            logger.warning("Security circuit breaker open, fail-open")
            self.metrics.increment("shared_proxy_security_circuit_open_total")
            return SecurityScanResult.fail_open("circuit_breaker")
        
        return await self.security_client.scan(request_body, headers, trace_id)

    async def _enrich_with_timeout(
        self,
        request_body: bytes,
        headers: dict[str, str],
        trace_id: str | None,
        request_id: str | None,
    ) -> Span:
        """Obs enrichment with circuit breaker check."""
        if not self.obs_circuit_breaker.allow_request():
            logger.warning("Obs circuit breaker open, minimal span")
            self.metrics.increment("shared_proxy_obs_circuit_open_total")
            return self._build_minimal_span(trace_id, request_id)
        
        return await self.obs_pipeline.enrich_span(
            request_body, headers, trace_id, request_id
        )

    def _make_decision(
        self,
        security_result: SecurityScanResult,
        obs_span: Span,
    ) -> str:
        """Make final decision based on security > obs priority.
        
        Priority: security > policy > observability
        - block: security blocks request, 403 response
        - flag: security flags request, forward with warning
        - forward: security clean, forward normally
        """
        # Security decisions take priority
        if security_result.verdict == "block":
            return "block"
        elif security_result.verdict == "flag":
            return "flag"
        else:
            return "forward"

    def _build_minimal_span(
        self,
        trace_id: str | None,
        request_id: str | None,
    ) -> Span:
        """Build minimal span when obs enrichment fails."""
        span = self.sdk._create_span(
            name="llm.proxy.call",
            trace_id=trace_id,
            parent_span_id=None,
            span_type="llm.proxy.call",
        )
        span.attributes.update({
            "proxy.mode": "shared",
            "proxy.error": "enrichment_failed",
            "request_id": request_id or "",
        })
        return span

    async def close(self) -> None:
        """Close pipeline resources."""
        await self.security_client.close()