"""Adaptive evaluation sampling for eval pipeline (PP12).

Implements day/night/incident modes for eval frequency:
- Day (08:00-22:00 UTC): 10% of traces
- Night (22:00-08:00 UTC): 50% of traces  
- Incident: 100% of security-incident traces, 0% otherwise

PP12: T3.3.1 — Adaptive eval sampler with day/night/incident modes.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone, time as dt_time
from typing import TYPE_CHECKING, Any, Callable, Optional

import redis
from prometheus_client import REGISTRY

from agent_obs.guardrail.audit import AuditWriteError
from agent_obs.metrics import eval_sampler_mode, eval_sampler_incident_evals_total

if TYPE_CHECKING:
    from agent_obs.observability import Span

logger = logging.getLogger(__name__)


@dataclass
class IncidentRateLimiter:
    """Rate limiter for incident eval jobs.
    
    PP14: Limits incident evals to 100 per minute using Redis.
    """
    redis_url: str = "redis://localhost:6379/1"
    limit: int = 100
    window_seconds: int = 60
    
    def __init__(self, redis_url: str | None = None, limit: int | None = None):
        self.redis_url = redis_url or self.redis_url
        self.limit = limit or self.limit
        self._client: redis.Redis | None = None
    
    @property
    def client(self) -> redis.Redis:
        """Lazy Redis client."""
        if self._client is None:
            self._client = redis.from_url(self.redis_url)
        return self._client
    
    async def acquire(self) -> bool:
        """Try to acquire an incident eval slot.
        
        Returns True if allowed, False if rate limited.
        Fail-open: on Redis error, return True (allow eval).
        """
        try:
            current_minute = int(time.time() // self.window_seconds)
            key = f"agent_obs:eval:incident_rate:{current_minute}"
            
            # Increment in executor thread to avoid blocking
            result = await asyncio.to_thread(
                self.client.incr, key
            )
            
            if result == 1:
                # Set expiration on first increment
                await asyncio.to_thread(
                    self.client.expire, key, self.window_seconds * 2
                )
            
            return result <= self.limit
        except Exception as e:
            logger.warning("Rate limiter Redis error: %s, allowing eval", e)
            return True


@dataclass
class Trace:
    """Lightweight trace information for eval sampling decisions.
    
    PP12: Minimal trace contract for should_eval().
    """
    trace_id: str
    attributes: dict[str, Any]
    
    @property
    def has_security_incident(self) -> bool:
        """Check if trace contains a security incident."""
        return self.attributes.get("security.incident") == "true"
    
    @property
    def has_prompt_injection(self) -> bool:
        """Check if trace contains a prompt injection."""
        return self.attributes.get("error.type") == "prompt_injection"
    
    @classmethod
    def from_spans(cls, spans: list[Any]) -> "Trace":
        """Create Trace from list of spans.
        
        PP14: Extracts security incident and prompt injection attributes
        from span attributes.
        """
        if not spans:
            return cls("unknown", {})
        
        trace_id = spans[0].context.trace_id if hasattr(spans[0], 'context') else "unknown"
        attributes = {}
        
        # Collect all span attributes
        for span in spans:
            if hasattr(span, 'attributes') and span.attributes:
                attributes.update(span.attributes)
        
        return cls(trace_id, attributes)
    
    @classmethod
    def from_spans(cls, spans: list[Span]) -> Trace:
        """Create Trace from list of spans (all share same trace_id)."""
        if not spans:
            raise ValueError("Cannot create Trace from empty span list")
        
        # All spans in a trace have the same trace_id
        trace_id = spans[0].context.trace_id
        # Merge attributes from all spans
        attributes = {}
        for span in spans:
            attributes.update(span.attributes)
        
        return cls(trace_id=trace_id, attributes=attributes)


class AdaptiveEvalSampler:
    """PP12: Adaptive eval sampler with day/night/incident modes.
    
    Adjusts eval frequency based on time of day and incident status:
    - Day (08:00-22:00 UTC): 10% of traces
    - Night (22:00-08:00 UTC): 50% of traces
    - Incident: 100% of security-incident traces, 0% otherwise
    """
    
    def __init__(
        self,
        prometheus_url: str = "http://localhost:9090",
        cache_ttl: float = 30.0,
        hot_store: Optional[Any] = None,
        enable_audit: bool = True,
        redis_url: str | None = None,
        incident_rate_limit: int | None = None,
    ):
        self.prometheus_url = prometheus_url
        self.cache_ttl = cache_ttl
        self.hot_store = hot_store
        self.enable_audit = enable_audit
        
        # Rate limiter for incident evals
        self._rate_limiter = IncidentRateLimiter(
            redis_url=redis_url,
            limit=incident_rate_limit
        )
        
        # Cache for active incident status
        self._incident_cache_time: float = 0.0
        self._incident_cache_value: bool = False
        self._incident_lock = asyncio.Lock()
        
        # Current mode state
        self._current_mode: str = "day"  # Start with day mode
        self._mode_lock = asyncio.Lock()
        
        # Injectable dependency for testing
        self._fetch_active_incident: Callable[[], Awaitable[bool]] = self._fetch_active_incident_impl
    
    async def _fetch_active_incident_impl(self) -> bool:
        """Fetch active incident status from Prometheus.
        
        Reads alert_active metric with caching.
        """
        import aiohttp
        
        now = time.time()
        if self._incident_cache_value and (now - self._incident_cache_time) < self.cache_ttl:
            return self._incident_cache_value
        
        async with self._incident_lock:
            # Double-check after acquiring lock
            if self._incident_cache_value and (now - self._incident_cache_time) < self.cache_ttl:
                return self._incident_cache_value
            
            try:
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5.0)) as session:
                    params = {"query": "alert_active"}
                    async with session.get(f"{self.prometheus_url}/api/v1/query", params=params) as response:
                        if response.status == 200:
                            data = await response.json()
                            if data["status"] == "success" and data["data"]["result"]:
                                # Extract value from Prometheus response
                                value = float(data["data"]["result"][0]["value"][1])
                                self._incident_cache_value = value > 0.0
                                self._incident_cache_time = now
                                logger.debug(f"Updated incident cache: {self._incident_cache_value}")
                                return self._incident_cache_value
                            else:
                                logger.warning(f"Prometheus query failed: {data}")
                        else:
                            logger.warning(f"Prometheus query failed: {response.status}")
            except Exception as e:
                logger.error(f"Error fetching incident status: {e}")
            
            # Return last known value or default (no incident)
            return self._incident_cache_value if self._incident_cache_value is not None else False
    
    def _get_hour_from_time(self, dt: datetime) -> int:
        """Get hour from datetime in UTC."""
        return dt.astimezone(timezone.utc).hour
    
    async def detect_mode(self, now: Optional[datetime] = None) -> str:
        """Detect current sampling mode.
        
        Returns one of: "day", "night", "incident"
        """
        if now is None:
            now = datetime.now(timezone.utc)
        
        # Check for active incident first (overrides day/night)
        # Check cache first before calling _fetch_active_incident
        now_time = time.time()
        if self._incident_cache_value and (now_time - self._incident_cache_time) < self.cache_ttl:
            active_incident = self._incident_cache_value
        else:
            try:
                active_incident = await self._fetch_active_incident()
            except Exception:
                # Fallback: no incident if fetch fails
                active_incident = False
        
        if active_incident:
            new_mode = "incident"
        else:
            # Determine day/night based on UTC time
            hour = self._get_hour_from_time(now)
            if 8 <= hour < 22:  # 08:00-22:00
                new_mode = "day"
            else:  # 22:00-08:00
                new_mode = "night"
        
        # Update metric and audit if mode changed
        async with self._mode_lock:
            if new_mode != self._current_mode:
                await self._report_mode_change(self._current_mode, new_mode)
                self._current_mode = new_mode
        
        return new_mode
    
    async def should_eval(self, trace: Trace) -> tuple[bool, str]:
        """Decide if a trace should be evaluated.
        
        Returns tuple of (should_eval, reason).
        
        PP14 Logic (all modes):
        - security.incident="true" -> (True, "incident_security") [rate-limited]
        - error.type="prompt_injection" -> (True, "incident_injection") [rate-limited]
        - PP12 Logic for remaining traces:
          - incident mode + non-security -> (False, "skip")
          - night mode + hash(trace_id) % 100 < 50 -> (True, "night_50pct")
          - day mode + hash(trace_id) % 100 < 10 -> (True, "day_10pct")
          - otherwise -> (False, "skip")
        """
        # PP14: Always evaluate security incident and prompt injection traces
        reason_base = None
        if trace.has_security_incident:
            reason_base = "incident_security"
        elif trace.has_prompt_injection:
            reason_base = "incident_injection"
        
        if reason_base is not None:
            # Rate limit for incident evals
            if not await self._rate_limiter.acquire():
                return (False, "rate_limited")
            
            # Record incident eval metric
            reason_label = self._get_incident_reason_label(reason_base)
            eval_sampler_incident_evals_total.labels(reason=reason_label).inc()
            
            # Record audit event
            if self.enable_audit and self.hot_store is not None:
                await self._record_incident_eval_audit(
                    trace.trace_id, reason_base, reason_label
                )
            
            return (True, reason_base)
        
        # PP12 Logic for non-incident-traces
        mode = await self.detect_mode()
        
        if mode == "incident":
            # Non-security traces in incident mode are skipped
            return (False, "skip")
        
        # Use deterministic bucketing (sha256) instead of builtin hash
        # for cross-process determinism (content_sampling invariant)
        trace_hash = int(hashlib.sha256(trace.trace_id.encode("utf-8")).hexdigest(), 16) % 100
        
        if mode == "night":
            if trace_hash < 50:
                return (True, "night_50pct")
            else:
                return (False, "skip")
        
        # mode == "day"
        if trace_hash < 10:
            return (True, "day_10pct")
        else:
            return (False, "skip")
    
    def _get_incident_reason_label(self, reason: str) -> str:
        """Map incident reason to metric label."""
        mapping = {
            "incident_security": "security",
            "incident_injection": "injection",
            "incident_cost": "cost",
        }
        return mapping.get(reason, "other")
    
    async def _record_incident_eval_audit(
        self, trace_id: str, reason: str, reason_label: str
    ) -> None:
        """Record incident eval audit event.
        
        Never raises: audit failure should not block eval decisions.
        """
        from agent_obs.guardrail.audit import EvalIncidentEvalAuditEvent
        
        mode = self._current_mode
        trigger = "security.incident" if reason == "incident_security" else "error.type=prompt_injection"
        
        event = EvalIncidentEvalAuditEvent.for_incident_eval(
            trace_id=trace_id,
            reason=reason,
            reason_label=reason_label,
            mode=mode,
            trigger=trigger,
            actor_id="adaptive-eval-sampler",
        )
        
        try:
            await asyncio.to_thread(self.hot_store.write_audit_event, event)
        except Exception as exc:
            logger.error(
                "Failed to write incident eval audit event: %s", exc
            )
            # Do not raise - audit failure should not block eval decisions
    
    async def _report_mode_change(self, old_mode: str, new_mode: str) -> None:
        """Report mode change to metrics and audit trail."""
        # Update metrics: current mode = 1.0, others = 0.0
        for mode in ["day", "night", "incident"]:
            eval_sampler_mode.labels(mode=mode).set(1.0 if mode == new_mode else 0.0)
        
        # Log to audit trail
        logger.info(f"Eval sampler mode changed from {old_mode} to {new_mode}")
        
        # Schedule audit write (off-thread to avoid blocking)
        if self.enable_audit and self.hot_store is not None:
            try:
                await asyncio.to_thread(self._record_mode_change_audit, old_mode, new_mode)
            except Exception as exc:
                logger.error("Failed to write mode change audit event: %s", exc)
    
    def _record_mode_change_audit(self, old_mode: str, new_mode: str) -> None:
        """Write mode change audit event to ClickHouse.
        
        Never raises: audit failure should not block mode detection.
        """
        from agent_obs.guardrail.audit import EvalSamplerModeChangeAuditEvent
        
        event = EvalSamplerModeChangeAuditEvent.for_mode_change(
            old_mode=old_mode,
            new_mode=new_mode,
            actor_id="adaptive-eval-sampler",
        )
        
        try:
            self.hot_store.write_audit_event(event)
        except Exception as exc:
            logger.error("Failed to write eval sampler mode-change audit event: %s", exc)
            # Do not raise - audit failure should not block mode changes