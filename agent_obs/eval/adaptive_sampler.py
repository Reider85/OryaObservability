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
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Awaitable

import redis

from agent_obs.metrics import eval_sampler_incident_evals_total, eval_sampler_mode

if TYPE_CHECKING:
    from agent_obs.observability import Span

logger = logging.getLogger(__name__)


@dataclass
class BudgetEnforcer:
    """Budget enforcer for eval pipeline (PP15).
    
    Enforces daily budget with soft (80% → 5% rate) and hard (100% → paused) limits.
    Reads cost from Redis and updates metrics.

    PP27: reads the error-budget policy state published by the SLO policy
    (Redis key ``agent_obs:error_budget:policy_state``) and halves the
    effective daily budget while the state is soft/hard — "eval cost-budget
    сокращается до 50%" to free spend while an SLO budget is nearly gone.
    """
    redis_url: str = "redis://localhost:6379/1"
    budget_usd: float = 20.0
    cache_ttl: float = 15.0  # seconds between Redis reads
    
    def __init__(self, redis_url: str | None = None, budget_usd: float | None = None, cache_ttl: float | None = None):
        self.redis_url = redis_url or self.redis_url
        self.budget_usd = budget_usd or self.budget_usd
        self.cache_ttl = cache_ttl or self.cache_ttl
        self._client: redis.Redis | None = None
        self._cache_time: float = 0.0
        self._cache_value: dict[str, float] = {}  # {mode: cost}
        self._state: int = 0  # 0=ok, 1=soft, 2=paused
        self._state_lock = asyncio.Lock()
        # PP27: 1.0 normally; 0.5 while the SLO error-budget policy is in
        # soft/hard state (eval spend is cut to help the budget recover).
        self.budget_multiplier: float = 1.0

    @property
    def effective_budget_usd(self) -> float:
        """Daily budget after the PP27 error-budget multiplier."""
        return self.budget_usd * self.budget_multiplier

    def set_budget_multiplier(self, multiplier: float) -> bool:
        """Apply the PP27 error-budget multiplier. Returns True on change.

        Values outside [0, 1] are rejected (the policy only ever *reduces*
        eval spend — raising it would defeat the purpose).
        """
        if not 0.0 <= multiplier <= 1.0:
            raise ValueError(f"budget multiplier must be in [0, 1], got {multiplier}")
        if multiplier == self.budget_multiplier:
            return False
        prev = self.budget_multiplier
        self.budget_multiplier = multiplier
        logger.info(
            "eval budget multiplier changed: %.2f -> %.2f (effective budget $%.2f -> $%.2f)",
            prev, multiplier,
            self.budget_usd * prev, self.effective_budget_usd,
        )
        return True
    
    @property
    def client(self) -> redis.Redis:
        """Lazy Redis client."""
        if self._client is None:
            self._client = redis.from_url(self.redis_url)
        return self._client
    
    async def _read_daily_cost(self) -> dict[str, float]:
        """Read daily cost from Redis, with caching.
        
        Returns dict of {mode: cost} for day/night/incident.
        Returns empty dict if Redis unavailable.
        """
        now = time.time()
        if self._cache_value and (now - self._cache_time) < self.cache_ttl:
            return self._cache_value
        
        try:
            from datetime import datetime
            date_str = datetime.now(UTC).strftime("%Y-%m-%d")
            cost_key = f"agent_obs:eval:cost:{date_str}"
            
            # Read all mode costs
            cost_data = await asyncio.to_thread(
                self.client.hgetall, cost_key
            )
            
            # Parse and sum costs per mode
            result = {}
            for mode, cost_bytes in cost_data.items():
                if isinstance(cost_bytes, (bytes, str)):
                    try:
                        cost = float(cost_bytes)
                        if cost > 0:
                            result[mode] = cost
                    except (ValueError, TypeError):
                        continue
            
            self._cache_value = result
            self._cache_time = now
            return result
            
        except Exception as e:
            logger.warning("Failed to read daily cost from Redis: %s, using cached", e)
            return self._cache_value if self._cache_value else {}
    
    async def _update_gauges(self, costs: dict[str, float]) -> None:
        """Update Prometheus gauges with current costs and ratios."""
        from agent_obs.metrics import (
            budget_utilization_ratio,
            eval_budget_state,
            eval_cost_today_usd,
        )
        
        # Update cost gauges per mode
        for mode, cost in costs.items():
            eval_cost_today_usd.labels(mode=mode).set(cost)
        
        # Update budget utilization ratio (against the PP27-adjusted budget)
        total_cost = sum(costs.values())
        effective = self.effective_budget_usd
        ratio = total_cost / effective if effective > 0 else 0.0
        budget_utilization_ratio.set(ratio)
        
        # Update state gauge
        eval_budget_state.set(self._state)
    
    async def _determine_state(self, costs: dict[str, float]) -> int:
        """Determine budget state based on total cost.
        
        Returns: 0=ok, 1=soft, 2=paused
        """
        total_cost = sum(costs.values())
        effective = self.effective_budget_usd
        ratio = total_cost / effective if effective > 0 else 0.0
        
        if ratio >= 1.0:
            return 2  # paused
        elif ratio >= 0.8:
            return 1  # soft
        else:
            return 0  # ok

    async def _read_error_budget_policy_state(self) -> str:
        """Read the PP27 error-budget policy state from Redis.

        Returns "ok" when the key is missing or Redis is unreachable — an
        unknown SLO budget must not change eval behaviour (fail-open, same as
        the rest of the eval budget path).
        """
        try:
            from agent_obs.slo.error_budget import POLICY_STATE_REDIS_KEY

            raw = await asyncio.to_thread(self.client.get, POLICY_STATE_REDIS_KEY)
        except Exception as exc:  # noqa: BLE001 - fail-open: unknown state must not change evals
            logger.warning("failed to read error-budget policy state: %s", exc)
            return "ok"
        if not raw:
            return "ok"
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        try:
            import json

            return str(json.loads(raw).get("state", "ok"))
        except Exception:  # noqa: BLE001 - malformed value degrades to "ok", never raises
            # A raw non-JSON value is still a usable signal.
            return str(raw)
    
    async def _check_state_transition(self, old_state: int, new_state: int) -> bool:
        """Check if state transition requires audit and logging.
        
        Returns True if transition occurred.
        """
        if old_state == new_state:
            return False
        
        # Log transition
        state_names = {0: "ok", 1: "soft", 2: "paused"}
        logger.info(
            "Budget state changed from %s to %s (cost=%.2f, budget=%.2f)",
            state_names[old_state],
            state_names[new_state],
            sum(self._cache_value.values()),
            self.budget_usd,
        )
        
        # Record audit event (never raise)
        if self._client is not None:
            try:
                await asyncio.to_thread(
                    self._record_state_change_audit,
                    old_state,
                    new_state
                )
            except Exception as exc:
                logger.error("Failed to write budget state change audit: %s", exc)
        
        return True
    
    def _record_state_change_audit(self, old_state: int, new_state: int) -> None:
        """Write budget state change audit event to ClickHouse.
        
        Never raises: audit failure should not block budget decisions.
        """
        from agent_obs.guardrail.audit import EvalBudgetStateChangeAuditEvent
        
        state_names = {0: "ok", 1: "soft", 2: "paused"}
        event = EvalBudgetStateChangeAuditEvent.for_budget_state_change(
            old_state=state_names[old_state],
            new_state=state_names[new_state],
            current_cost=sum(self._cache_value.values()),
            budget=self.budget_usd,
            actor_id="adaptive-eval-sampler",
        )
        
        try:
            if self._client is not None:
                self._client.write_audit_event(event)
        except Exception as exc:
            logger.error("Failed to write budget state change audit event: %s", exc)
            # Do not raise - audit failure should not block budget decisions
    
    async def refresh_budget(self) -> int:
        """Refresh budget state from Redis and return current state.
        
        Returns: 0=ok, 1=soft, 2=paused
        """
        try:
            # PP27: halve the effective budget while the SLO error-budget
            # policy is in soft/hard — evaluated before the state check so a
            # cut budget correctly pushes the enforcer into its own soft/paused.
            policy_state = await self._read_error_budget_policy_state()
            self.set_budget_multiplier(
                0.5 if policy_state in ("soft", "hard") else 1.0
            )
            costs = await self._read_daily_cost()
            new_state = await self._determine_state(costs)
            
            async with self._state_lock:
                if await self._check_state_transition(self._state, new_state):
                    self._state = new_state
                    await self._update_gauges(costs)
            
            return self._state
            
        except Exception as e:
            logger.error("Error refreshing budget: %s, keeping current state %d", e, self._state)
            return self._state  # Fail-open: keep current state


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
        hot_store: Any | None = None,
        enable_audit: bool = True,
        redis_url: str | None = None,
        incident_rate_limit: int | None = None,
        budget_usd: float | None = None,
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
        
        # Budget enforcer (PP15)
        self._budget_enforcer = BudgetEnforcer(
            redis_url=redis_url,
            budget_usd=budget_usd
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
        return dt.astimezone(UTC).hour
    
    async def detect_mode(self, now: datetime | None = None) -> str:
        """Detect current sampling mode.
        
        Returns one of: "day", "night", "incident"
        """
        if now is None:
            now = datetime.now(UTC)
        
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
        
        PP15 Budget enforcement logic:
        1. Check budget state: hard-pause (100%) blocks everything, including incident evals
        2. PP14: Always evaluate security incident and prompt injection traces (unless budget paused)
        3. PP12 Logic for non-incident-traces, with soft budget (80%) reducing day/night rates to 5%
        
        States: 0=ok, 1=soft (80% budget → day/night use 5%), 2=paused (100% → skip all)
        """
        # PP15: Check budget state first (hard pause blocks everything)
        budget_state = await self._budget_enforcer.refresh_budget()
        
        if budget_state == 2:  # paused
            return (False, "budget_paused")
        
        # PP14: Always evaluate security incident and prompt injection traces
        # (unless budget is paused - already handled above)
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
            # PP15: Apply budget soft limit (80% → reduce rate to 5%)
            if budget_state == 1:  # soft
                if trace_hash < 5:
                    return (True, "night_5pct_soft")
                else:
                    return (False, "budget_soft")
            else:  # ok
                if trace_hash < 50:
                    return (True, "night_50pct")
                else:
                    return (False, "skip")
        
        # mode == "day"
        if budget_state == 1:  # soft
            if trace_hash < 5:
                return (True, "day_5pct_soft")
            else:
                return (False, "budget_soft")
        else:  # ok
            if trace_hash < 10:
                return (True, "day_10pct")
            else:
                return (False, "skip")
        
        # Increment eval count metric (only for positive decisions)
        from agent_obs.metrics import eval_count_by_mode
        eval_count_by_mode.labels(mode=mode).inc()
    
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