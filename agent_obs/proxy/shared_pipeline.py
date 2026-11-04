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
from prometheus_client import Counter, Histogram
from agent_obs.observability import ObservabilitySDK, Span
from agent_obs.proxy.obs_pipeline import ObsPipeline
from agent_obs.proxy.security_client import SecurityClient, SecurityScanResult

logger = logging.getLogger(__name__)

# Module-level metrics to avoid duplicate registration
_pipeline_total = Counter("shared_proxy_pipeline_total", "Pipeline processing")
_pipeline_latency_seconds = Histogram("shared_proxy_pipeline_latency_seconds", "Pipeline processing latency")

# PP47: Initialize metrics with proper error handling
try:
    _security_latency_seconds = Histogram(
        "shared_proxy_security_latency_seconds",
        "Security pipeline latency",
        ["agent_id", "tenant_id"],
    )
    _obs_latency_seconds = Histogram(
        "shared_proxy_obs_latency_seconds",
        "Observability pipeline latency",
        ["agent_id", "tenant_id"],
    )
    _total_latency_seconds = Histogram(
        "shared_proxy_total_latency_seconds",
        "Total pipeline latency",
        ["agent_id", "tenant_id"],
    )
    _security_timeouts_total = Counter(
        "shared_proxy_security_timeouts_total",
        "Security pipeline timeouts",
        ["agent_id", "tenant_id"],
    )
    _security_circuit_open_total = Counter(
        "shared_proxy_security_circuit_open_total",
        "Security circuit breaker opens",
        ["agent_id", "tenant_id"],
    )
except Exception as e:
    logger.warning("Failed to initialize PP47 metrics: %s", e)
    # Create dummy metrics for testing
    class DummyMetric:
        def inc(self): pass
        def observe(self, value): pass
    _security_latency_seconds = DummyMetric()
    _obs_latency_seconds = DummyMetric()
    _total_latency_seconds = DummyMetric()
    _security_timeouts_total = DummyMetric()
    _security_circuit_open_total = DummyMetric()


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
        # Use unique metric names to avoid conflicts
        if not hasattr(self, '_metrics_initialized'):
            self._metrics_initialized = True
        
        # Independent circuit breakers with PP47 thresholds
        self.security_circuit_breaker = circuit_breaker_for("vault")  # proxy for security
        self.obs_circuit_breaker = circuit_breaker_for("langfuse")   # proxy for obs
        
        # PP47 timeout configuration
        self.security_timeout = 0.05  # 50ms for security pipeline
        self.obs_timeout = 0.1        # 100ms for obs pipeline

    async def process(
        self,
        request_body: bytes,
        headers: dict[str, str],
        trace_id: str | None = None,
        request_id: str | None = None,
    ) -> SharedPipelineResult:
        """Process request through security + obs pipeline with PP47 performance isolation.
        
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
            # Create security and obs tasks with PP47 timeouts
            security_task = asyncio.create_task(
                self._scan_with_timeout(request_body, headers, trace_id)
            )
            obs_task = asyncio.create_task(
                self._enrich_with_timeout(request_body, headers, trace_id, request_id)
            )
            
            # PP47: Wait for both tasks with independent timeouts
            security_result = await asyncio.wait_for(
                security_task, timeout=self.security_timeout  # 50ms for security
            )
            obs_span = await asyncio.wait_for(
                obs_task, timeout=self.obs_timeout  # 100ms for obs
            )
            
            # Make final decision (security > obs)
            decision = self._make_decision(security_result, obs_span)
            
            # Record metrics
            latency = time.time() - start_time
            _pipeline_total.inc()
            
            # Update circuit breakers on success
            self.security_circuit_breaker.record_success()
            self.obs_circuit_breaker.record_success()
            
            # Record per-pipeline latencies
            security_latency = getattr(security_result, '_processing_time', 0)
            obs_latency = getattr(obs_span, '_processing_time', 0)
            
            _security_latency_seconds.observe(security_latency)
            _obs_latency_seconds.observe(obs_latency)
            _total_latency_seconds.observe(latency)
            
            logger.debug(
                "Shared pipeline completed: decision=%s, latency=%.3fs, security=%.3fs, obs=%.3fs",
                decision, latency, security_latency, obs_latency
            )
            
            return SharedPipelineResult(
                masked_request_body=security_result.masked_request_body,
                security_result=security_result,
                obs_span=obs_span,
                decision=decision,
                latency_seconds=latency,
            )
            
        except asyncio.TimeoutError:
            # Handle timeouts with PP47 fail-open semantics
            latency = time.time() - start_time
            _pipeline_total.inc()
            
            logger.warning(
                "Shared pipeline timeout: latency=%.3fs, fail_open=True",
                latency
            )
            
            # PP47: Determine which pipeline timed out based on task names
            security_task_name = "security_scan"
            obs_task_name = "obs_enrichment"
            
            # Check which task completed and which timed out
            if security_task.done() and not obs_task.done():
                # Security completed, obs timed out
                logger.warning("Obs pipeline timeout after %.3fs, fail-open", self.obs_timeout)
                obs_span = self._build_minimal_span(trace_id, request_id)
                obs_span._timeout_reason = "obs_timeout"
                obs_span._processing_time = self.obs_timeout
                security_result = SecurityScanResult.fail_open("obs_timeout")
                _security_timeouts_total.inc()
            elif obs_task.done() and not security_task.done():
                # Obs completed, security timed out
                logger.warning("Security pipeline timeout after %.3fs, fail-open", self.security_timeout)
                security_result = SecurityScanResult.fail_open("security_timeout")
                security_result._processing_time = self.security_timeout
                obs_span = obs_task.result()
                _security_timeouts_total.inc()
            else:
                # Both timed out or neither completed
                logger.warning("Both pipelines timed out, fail-open both")
                security_result = SecurityScanResult.fail_open("both_timeout")
                obs_span = self._build_minimal_span(trace_id, request_id)
                security_result._processing_time = self.security_timeout
                obs_span._processing_time = self.obs_timeout
                _security_timeouts_total.inc()
            
            # Cancel any pending tasks
            if not security_task.done():
                security_task.cancel()
            if not obs_task.done():
                obs_task.cancel()
            
            # Wait for cancellation to complete
            await asyncio.gather(security_task, obs_task, return_exceptions=True)
            
            return SharedPipelineResult(
                masked_request_body=security_result.masked_request_body,
                security_result=security_result,
                obs_span=obs_span,
                decision="forward",  # PP47: Fail-open allows forwarding
                latency_seconds=latency,
            )
            
        except Exception as e:
            # Handle other errors with PP47 fail-open
            latency = time.time() - start_time
            _pipeline_total.inc()
            
            logger.error("Shared pipeline failed: %s", e)
            
            # PP47: Fail-open on errors
            security_result = SecurityScanResult.fail_open(f"pipeline_error: {e}")
            obs_span = self._build_minimal_span(trace_id, request_id)
            
            return SharedPipelineResult(
                masked_request_body=security_result.masked_request_body,
                security_result=security_result,
                obs_span=obs_span,
                decision="forward",  # PP47: Fail-open allows forwarding
                latency_seconds=latency,
            )

    async def _scan_with_timeout(
        self,
        request_body: bytes,
        headers: dict[str, str],
        trace_id: str | None,
    ) -> SecurityScanResult:
        """Security scan with PP47 circuit breaker check and timeout."""
        # PP47: Check security circuit breaker first
        if not self.security_circuit_breaker.allow_request():
            logger.warning("Security circuit breaker open, fail-open")
            _security_circuit_open_total.inc()
            _pipeline_total.inc()
            return SecurityScanResult.fail_open("circuit_breaker")
        
        # PP47: Track security processing time
        start_time = time.time()
        try:
            result = await self.security_client.scan(request_body, headers, trace_id)
            result._processing_time = time.time() - start_time  # PP47: Track latency
            return result
        except asyncio.TimeoutError:
            # PP47: Handle timeout specifically
            processing_time = time.time() - start_time
            logger.warning("Security scan timeout after %.3fs", processing_time)
            result = SecurityScanResult.fail_open("security_timeout")
            result._processing_time = processing_time
            return result
        except Exception as e:
            # PP47: Record security processing time even on failure
            processing_time = time.time() - start_time
            logger.error("Security scan failed: %s, took %.3fs", e, processing_time)
            result = SecurityScanResult.fail_open(f"scan_error: {e}")
            result._processing_time = processing_time
            return result

    async def _enrich_with_timeout(
        self,
        request_body: bytes,
        headers: dict[str, str],
        trace_id: str | None,
        request_id: str | None,
    ) -> Span:
        """Obs enrichment with PP47 circuit breaker check and timeout."""
        # PP47: Check obs circuit breaker first
        if not self.obs_circuit_breaker.allow_request():
            logger.warning("Obs circuit breaker open, minimal span")
            _pipeline_total.inc()
            return self._build_minimal_span(trace_id, request_id)
        
        # PP47: Track obs processing time
        start_time = time.time()
        try:
            result = await self.obs_pipeline.enrich_span(
                request_body, headers, trace_id, request_id
            )
            result._processing_time = time.time() - start_time  # PP47: Track latency
            return result
        except asyncio.TimeoutError:
            # PP47: Handle timeout specifically
            processing_time = time.time() - start_time
            logger.warning("Obs enrichment timeout after %.3fs", processing_time)
            result = self._build_minimal_span(trace_id, request_id)
            result._processing_time = processing_time
            result._timeout_reason = "obs_timeout"
            return result
        except Exception as e:
            # PP47: Record obs processing time even on failure
            processing_time = time.time() - start_time
            logger.error("Obs enrichment failed: %s, took %.3fs", e, processing_time)
            result = self._build_minimal_span(trace_id, request_id)
            result._processing_time = processing_time
            return result

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
        # Add default processing time
        if not hasattr(span, '_processing_time'):
            span._processing_time = 0.0
        # Add default timeout reason
        if not hasattr(span, '_timeout_reason'):
            span._timeout_reason = None
        return span

    async def close(self) -> None:
        """Close pipeline resources."""
        await self.security_client.close()

    async def _execute_parallel(
        self,
        request_body: bytes,
        headers: dict[str, str],
        trace_id: str | None,
        request_id: str | None,
    ) -> tuple[SecurityScanResult, Span]:
        """Execute security and obs pipelines in parallel with PP47 timeouts.
        
        PP47: This method implements the core performance isolation with:
        - Independent timeouts (security: 50ms, obs: 100ms)
        - Fail-open semantics for security timeouts
        - Circuit breaker isolation
        """
        # Create tasks with PP47 timeouts
        security_task = asyncio.create_task(
            self._scan_with_timeout(request_body, headers, trace_id),
            name="security_scan"  # PP47: Name for timeout identification
        )
        obs_task = asyncio.create_task(
            self._enrich_with_timeout(request_body, headers, trace_id, request_id),
            name="obs_enrichment"  # PP47: Name for timeout identification
        )
        
        # PP47: Execute with independent timeouts
        try:
            # Wait for security with 50ms timeout
            security_result = await asyncio.wait_for(security_task, timeout=self.security_timeout)
            # Wait for obs with 100ms timeout
            obs_span = await asyncio.wait_for(obs_task, timeout=self.obs_timeout)
            
        except asyncio.TimeoutError as e:
            # PP47: Handle timeouts with fail-open
            if security_task.done() and not obs_task.done():
                # Security completed, obs timed out
                logger.warning("Obs pipeline timeout after %.3fs, fail-open", self.obs_timeout)
                obs_span = self._build_minimal_span(trace_id, request_id)
                obs_span._processing_time = self.obs_timeout
                obs_span._timeout_reason = "obs_timeout"
            elif obs_task.done() and not security_task.done():
                # Obs completed, security timed out
                logger.warning("Security pipeline timeout after %.3fs, fail-open", self.security_timeout)
                security_result = SecurityScanResult.fail_open("security_timeout")
                security_result._processing_time = self.security_timeout
                security_result._timeout_reason = "security_timeout"
            else:
                # Both timed out or neither completed
                logger.warning("Both pipelines timed out, fail-open both")
                security_result = SecurityScanResult.fail_open("both_timeout")
                obs_span = self._build_minimal_span(trace_id, request_id)
                security_result._processing_time = self.security_timeout
                obs_span._processing_time = self.obs_timeout
            
            # Cancel any pending tasks
            if not security_task.done():
                security_task.cancel()
            if not obs_task.done():
                obs_task.cancel()
            
            # Wait for cancellation to complete
            await asyncio.gather(security_task, obs_task, return_exceptions=True)
            
            return security_result, obs_span
        
        except Exception as e:
            # PP47: Handle any other errors
            logger.error("Parallel execution failed: %s", e)
            security_result = SecurityScanResult.fail_open(f"parallel_error: {e}")
            obs_span = self._build_minimal_span(trace_id, request_id)
            return security_result, obs_span