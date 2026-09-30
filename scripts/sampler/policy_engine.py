#!/usr/bin/env python3
"""Adaptive tail-sampling policy engine (PC30, ticket T2.7.2).

Chooses the keep-rate for *normal* traces from two signals the SDK already
exports (PC29): ``agent_obs_system_cpu_ratio`` and
``agent_obs_agent_error_rate_5m``.  The rule set is fixed by the CRITICAL spec::

    if system_cpu_ratio > 0.8:      rate = 0.05
    elif agent_error_rate_5m > 0.05: rate = 0.30
    else:                            rate = 0.10

``evaluate_rate`` is a pure function so the rules can be tested without any I/O.

Signals are read from Prometheus by default.  When the engine runs inside the
sampler proxy the pilot-agent gauges are not in the same process, so Prometheus
is the only shared channel; a scrape failure is *not* an error condition — it
degrades to the default rate and bumps a failure counter, because losing the
metrics endpoint must not wedge the sampler.

Every transition is reported to the caller-supplied ``rate_callback`` and
written to the ClickHouse audit trail as a ``SamplerRateChangeAuditEvent``
(PC30 requires the reason to survive the process so a dropped trace can be
explained weeks later; PC31 adds the query API over those rows).
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

import httpx

from agent_obs.guardrail.audit import (
    CPU_HIGH_THRESHOLD,
    ERROR_RATE_THRESHOLD,
    SAMPLER_RATES,
    SamplerRateChangeAuditEvent,
)
from agent_obs.metrics import (
    tail_sampler_current_rate,
    tail_sampler_policy_evaluations_total,
    tail_sampler_rate_changes_total,
)

logger = logging.getLogger(__name__)

# Prometheus instant-query names for the two PC29 gauges.
CPU_QUERY = "agent_obs_system_cpu_ratio"
ERROR_RATE_QUERY = "agent_obs_agent_error_rate_5m"

# When several agents report an error rate, the policy reacts to the worst one:
# one broken agent is exactly the case where we most want its traces kept.
_ERROR_RATE_AGGREGATION = "max"

DEFAULT_POLL_INTERVAL = 30.0
DEFAULT_PROMETHEUS_URL = "http://localhost:9090"
_SCRAPE_TIMEOUT = 5.0


def evaluate_rate(
    cpu_ratio: float,
    error_rate_5m: float,
    cpu_threshold: float = CPU_HIGH_THRESHOLD,
    error_threshold: float = ERROR_RATE_THRESHOLD,
) -> tuple[float, str]:
    """Pick the normal-trace keep-rate from the two load/error signals.

    Pure and side-effect free.  Returns ``(rate, reason)`` where ``reason`` is
    one of ``cpu_high`` | ``error_high`` | ``default``.

    The cpu check is evaluated first: a saturated host endangers the export
    path itself, so protecting it takes precedence over retaining the extra
    error traces.  Thresholds are strict ``>`` — a ratio sitting exactly on the
    threshold is not yet a reason to move.
    """
    if cpu_ratio > cpu_threshold:
        return SAMPLER_RATES["cpu_high"], "cpu_high"
    if error_rate_5m > error_threshold:
        return SAMPLER_RATES["error_high"], "error_high"
    return SAMPLER_RATES["default"], "default"


@dataclass
class PolicyDecision:
    """Outcome of one policy evaluation.

    Carries the inputs alongside the result so the audit event and the log line
    can quote the numbers that produced the rate without re-deriving them.
    """

    rate: float
    reason: str
    prev_rate: float
    prev_reason: str
    cpu_ratio: float
    error_rate_5m: float
    changed: bool

    @property
    def reason_text(self) -> str:
        """Human-readable reason, matching the audit trail wording."""
        if self.reason == "cpu_high":
            return (
                f"cpu_high (system_cpu_ratio={self.cpu_ratio:.2f} > "
                f"{CPU_HIGH_THRESHOLD:.2f})"
            )
        if self.reason == "error_high":
            return (
                f"error_high (agent_error_rate_5m={self.error_rate_5m:.3f} > "
                f"{ERROR_RATE_THRESHOLD:.3f})"
            )
        return (
            f"default (system_cpu_ratio={self.cpu_ratio:.2f}, "
            f"agent_error_rate_5m={self.error_rate_5m:.3f})"
        )


class PolicyEngine:
    """Evaluates the adaptive rate on a fixed tick and publishes changes.

    Parameters
    ----------
    prometheus_url:
        Base URL of the Prometheus instance that scrapes the pilot-agent.
    poll_interval:
        Seconds between evaluations.  PC30 specifies 30s.
    cpu_threshold / error_threshold:
        Overridable rule thresholds; defaults are the CRITICAL spec values.
    hot_store:
        Optional :class:`~agent_obs.storage.hot.HotStore` for audit writes.
        When omitted, rate changes are still logged and counted but not
        persisted — that keeps the engine unit-testable without ClickHouse.
    enable_audit:
        Set False to suppress ClickHouse audit writes.
    """

    def __init__(
        self,
        prometheus_url: str = DEFAULT_PROMETHEUS_URL,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        cpu_threshold: float = CPU_HIGH_THRESHOLD,
        error_threshold: float = ERROR_RATE_THRESHOLD,
        hot_store=None,
        enable_audit: bool = True,
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self.prometheus_url = prometheus_url.rstrip("/")
        self.poll_interval = poll_interval
        self.cpu_threshold = cpu_threshold
        self.error_threshold = error_threshold
        self.hot_store = hot_store
        self.enable_audit = enable_audit
        self._client = client

        self.current_rate: float = SAMPLER_RATES["default"]
        self.current_reason: str = "default"
        # Seed the gauge so /metrics is meaningful before the first tick.
        tail_sampler_current_rate.labels(policy_reason=self.current_reason).set(
            self.current_rate
        )

    # --- signal acquisition ------------------------------------------------

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=_SCRAPE_TIMEOUT)
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _query(self, query: str) -> list[float]:
        """Run one Prometheus instant query, returning the sample values.

        An empty list means "no series" — a pilot-agent that has not reported
        yet.  Transport and parse errors propagate to the caller, which treats
        them the same as a missing series.
        """
        client = await self._get_client()
        response = await client.get(
            f"{self.prometheus_url}/api/v1/query", params={"query": query}
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("status") != "success":
            raise RuntimeError(
                f"prometheus query {query!r} failed: {payload.get('error')}"
            )
        values: list[float] = []
        for series in payload.get("data", {}).get("result", []):
            raw = series.get("value", [None, None])[1]
            if raw is None:
                continue
            try:
                values.append(float(raw))
            except (TypeError, ValueError):
                continue
        return values

    async def read_signals(self) -> tuple[float, float]:
        """Fetch ``(cpu_ratio, error_rate_5m)`` from Prometheus.

        Missing series resolve to 0.0, which routes the policy to its default
        rate.  A scrape failure is logged and also treated as 0.0 — fail-safe,
        because guessing a *high* rate from absent data would silently triple
        storage spend, while guessing low would hide errors.
        """
        try:
            cpu_values = await self._query(CPU_QUERY)
        except Exception as exc:
            logger.warning("Prometheus CPU scrape failed: %s", exc)
            cpu_values = []
        try:
            error_values = await self._query(ERROR_RATE_QUERY)
        except Exception as exc:
            logger.warning("Prometheus error-rate scrape failed: %s", exc)
            error_values = []

        cpu_ratio = max(cpu_values) if cpu_values else 0.0
        # max over agents: one unhealthy agent must be able to lift the rate.
        error_rate = max(error_values) if error_values else 0.0
        return cpu_ratio, error_rate

    # --- evaluation --------------------------------------------------------

    def evaluate(
        self, cpu_ratio: float, error_rate_5m: float
    ) -> PolicyDecision:
        """Evaluate the rules and, on change, update state, metrics and audit.

        Synchronous and idempotent: calling it twice with the same inputs
        produces a change the second time only if the first actually moved the
        rate.  The audit write is dispatched to a thread because
        ``HotStore`` is blocking and this runs on the sampler proxy's event
        loop.
        """
        rate, reason = evaluate_rate(
            cpu_ratio, error_rate_5m, self.cpu_threshold, self.error_threshold
        )
        prev_rate, prev_reason = self.current_rate, self.current_reason
        changed = rate != prev_rate or reason != prev_reason

        decision = PolicyDecision(
            rate=rate,
            reason=reason,
            prev_rate=prev_rate,
            prev_reason=prev_reason,
            cpu_ratio=cpu_ratio,
            error_rate_5m=error_rate_5m,
            changed=changed,
        )

        if not changed:
            tail_sampler_policy_evaluations_total.labels(outcome="unchanged").inc()
            return decision

        self.current_rate = rate
        self.current_reason = reason
        tail_sampler_current_rate.labels(policy_reason=reason).set(rate)
        tail_sampler_rate_changes_total.labels(
            from_reason=prev_reason, to_reason=reason
        ).inc()
        tail_sampler_policy_evaluations_total.labels(outcome="changed").inc()

        logger.info(
            "Sampling rate changed: %.2f -> %.2f (%s)",
            prev_rate,
            rate,
            decision.reason_text,
        )
        return decision

    def build_audit_event(self, decision: PolicyDecision) -> SamplerRateChangeAuditEvent:
        """Build the audit row for a rate change (separated for testability)."""
        return SamplerRateChangeAuditEvent.for_rate_change(
            prev_rate=decision.prev_rate,
            new_rate=decision.rate,
            prev_reason=decision.prev_reason,
            new_reason=decision.reason,
            system_cpu_ratio=decision.cpu_ratio,
            agent_error_rate_5m=decision.error_rate_5m,
        )

    def record_audit(self, decision: PolicyDecision) -> None:
        """Write a rate-change audit event to ClickHouse, if configured.

        Never raises: a failing audit write must not stop the sampler from
        changing rate, so the failure is logged and counted instead.
        """
        if not self.enable_audit or self.hot_store is None:
            return
        event = self.build_audit_event(decision)
        try:
            self.hot_store.write_audit_event(event)
        except Exception as exc:
            logger.error("Failed to write sampler rate-change audit event: %s", exc)
            tail_sampler_policy_evaluations_total.labels(
                outcome="audit_failed"
            ).inc()

    # --- main loop ---------------------------------------------------------

    async def run_once(
        self, rate_callback: Optional[Callable[[PolicyDecision], Awaitable[None]]] = None
    ) -> Optional[PolicyDecision]:
        """One tick: read signals, evaluate, publish, audit.

        Returns the decision when the rate moved, else None.
        """
        cpu_ratio, error_rate = await self.read_signals()
        decision = self.evaluate(cpu_ratio, error_rate)
        if not decision.changed:
            return None
        # Off-loop write: HotStore is a blocking clickhouse-driver client.
        await asyncio.to_thread(self.record_audit, decision)
        if rate_callback is not None:
            await rate_callback(decision)
        return decision

    async def run(
        self,
        rate_callback: Optional[Callable[[PolicyDecision], Awaitable[None]]] = None,
        stop_event: Optional[asyncio.Event] = None,
    ) -> None:
        """Tick every ``poll_interval`` seconds until ``stop_event`` is set.

        A failing tick is logged and the loop continues — a transient
        Prometheus outage must not permanently freeze the sampling rate.
        """
        stop = stop_event or asyncio.Event()
        logger.info(
            "Policy engine started: prometheus=%s interval=%.0fs initial_rate=%.2f",
            self.prometheus_url,
            self.poll_interval,
            self.current_rate,
        )
        while not stop.is_set():
            try:
                await self.run_once(rate_callback)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("Policy engine tick failed: %s", exc)
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.poll_interval)
            except asyncio.TimeoutError:
                continue
        logger.info("Policy engine stopped at rate=%.2f", self.current_rate)


def build_policy_engine_from_env(**overrides) -> PolicyEngine:
    """Construct a PolicyEngine from environment variables.

    Used by the sampler proxy entrypoint and the standalone cron mode.
    """
    poll = float(os.environ.get("AGENT_OBS_SAMPLER_POLL_INTERVAL", "30"))
    prom_url = os.environ.get("PROMETHEUS_URL", DEFAULT_PROMETHEUS_URL)
    params = {
        "prometheus_url": prom_url,
        "poll_interval": poll,
        "cpu_threshold": float(
            os.environ.get("AGENT_OBS_SAMPLER_CPU_THRESHOLD", CPU_HIGH_THRESHOLD)
        ),
        "error_threshold": float(
            os.environ.get("AGENT_OBS_SAMPLER_ERROR_THRESHOLD", ERROR_RATE_THRESHOLD)
        ),
    }
    params.update(overrides)
    return PolicyEngine(**params)
