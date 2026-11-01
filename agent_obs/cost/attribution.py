"""Tenant-aware cost attribution + budget enforcement for the LLM proxy (PP42).

Every proxied request is attributed to ``tenant + agent + user`` via the
``X-OryaObs-*`` headers, so reports like "how much did tenant X spend on
agent Y last week" become a simple group-by.

Design notes
------------
* Hybrid aggregation: :class:`CostAttributionTracker.record` keeps an
  in-memory per-``(tenant, agent, day)`` accumulator (O(1), sync, never
  blocks) so budget decisions are instant; the background flush cycle
  (default 60s) additionally re-asks ClickHouse for the authoritative
  ``sum(cost_usd)`` of today and takes ``max(memory, clickhouse)`` — the
  CH snapshot is the source of truth, memory only wins on rows written
  after the snapshot.
* Flushed totals are UPSERTed into the warm-tier ``tenant_cost_daily``
  table (``ON CONFLICT ... DO UPDATE ... GREATEST``), exported as the
  ``cost_per_tenant_usd{tenant_id, agent_id}`` gauge, and each *changed*
  row is logged to the audit trail (``action="cost_attribution"``).
* Budget enforcement (:class:`BudgetEnforcer`): per-tenant daily budget
  from env ``TENANT_BUDGET_USD_DEFAULT`` (default 10.0 USD), overridable
  per tenant via the warm-tier ``tenant_config`` table. ``<= 0`` disables
  enforcement for that tenant. At >= 80% of the budget the proxy answers
  429 ("budget approaching"); at >= 100% it answers 403 ("budget
  exhausted") — see ``LLMProxyServer.chat_completions``.
* Fail-open everywhere: a ClickHouse/Postgres/audit failure is logged and
  skipped, never propagated — observability never blocks business logic
  (architectural invariant).
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Mapping
from datetime import UTC, date, datetime
from typing import Any

from agent_obs.metrics import cost_per_tenant_usd

logger = logging.getLogger(__name__)

_DEFAULT_FLUSH_INTERVAL = 60.0
_DEFAULT_TENANT_BUDGET_USD = 10.0

#: Ratio of spend/budget at which the proxy starts rate-limiting (429).
BUDGET_APPROACHING_RATIO = 0.8

#: Budget check outcomes.
BUDGET_OK = "ok"
BUDGET_APPROACHING = "approaching"
BUDGET_EXHAUSTED = "exhausted"


def _utc_today() -> date:
    return datetime.now(UTC).date()


def _start_of_utc_day(day: date) -> float:
    return datetime.combine(day, datetime.min.time(), tzinfo=UTC).timestamp()


def _parse_float(raw: Any, default: float, *, name: str) -> float:
    try:
        return float(raw)
    except (TypeError, ValueError):
        if raw is not None and str(raw).strip() != "":
            logger.warning("Invalid %s=%r, using default %s", name, raw, default)
        return default


def attribution_from_headers(headers: Mapping[str, str]) -> tuple[str, str, str]:
    """Resolve ``(tenant_id, agent_id, user_id)`` from proxy headers (PP42 §1).

    Case-insensitive header lookup (Starlette's ``Headers`` is already
    case-insensitive; plain dicts get a manual scan so unit tests can pass
    either casing).  Missing/blank values fall back to the spec defaults:
    ``default`` / ``unknown`` / ``anonymous``.
    """

    def _get(name: str) -> str:
        lowered = name.lower()
        for key, value in headers.items():
            if key.lower() == lowered:
                return str(value).strip()
        return ""

    tenant_id = _get("X-OryaObs-Tenant-Id") or "default"
    agent_id = _get("X-OryaObs-Agent-Id") or "unknown"
    user_id = _get("X-OryaObs-User-Id") or "anonymous"
    return tenant_id, agent_id, user_id


class CostAttributionTracker:
    """In-memory per-(tenant, agent, day) spend accumulator + flush job (PP42).

    ``record()`` is synchronous and O(1).  Persistence / gauge / audit happen
    on a background ``asyncio`` task every ``flush_interval`` seconds
    (default 60, env ``AGENT_OBS_COST_FLUSH_INTERVAL``), mirroring the
    PC33 ComplianceCatalog worker pattern.  Stores are optional: when not
    injected they are created lazily on the first flush and every failure
    degrades to a warning.
    """

    def __init__(
        self,
        *,
        hot_store: Any | None = None,
        warm_store: Any | None = None,
        flush_interval: float | None = None,
    ) -> None:
        if flush_interval is None:
            flush_interval = _parse_float(
                os.environ.get("AGENT_OBS_COST_FLUSH_INTERVAL"),
                _DEFAULT_FLUSH_INTERVAL,
                name="AGENT_OBS_COST_FLUSH_INTERVAL",
            )
        self._hot_store = hot_store
        self._warm_store = warm_store
        self._flush_interval = flush_interval
        # (tenant_id, agent_id, day) -> USD spent
        self._spend: dict[tuple[str, str, date], float] = {}
        # Last flushed value per key — used to emit audit only on change.
        self._last_flushed: dict[tuple[str, str, date], float] = {}
        # Per-tenant budget overrides, refreshed from tenant_config on flush.
        self._overrides: dict[str, float] = {}
        self._task: asyncio.Task[None] | None = None
        self._shutdown = asyncio.Event()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def overrides(self) -> dict[str, float]:
        """Per-tenant budget overrides (USD/day), shared with BudgetEnforcer."""
        return self._overrides

    @property
    def flush_interval(self) -> float:
        return self._flush_interval

    def record(self, tenant_id: str, agent_id: str, cost_usd: float) -> None:
        """Attribute *cost_usd* to (tenant, agent, today).  Never blocks."""
        if not cost_usd:
            return
        key = (tenant_id, agent_id, _utc_today())
        self._spend[key] = self._spend.get(key, 0.0) + float(cost_usd)

    def spend_today(self, tenant_id: str) -> float:
        """Total USD spent by *tenant_id* today (all agents, in-memory view)."""
        today = _utc_today()
        return sum(
            cost
            for (tenant, _agent, day), cost in self._spend.items()
            if tenant == tenant_id and day == today
        )

    def start(self) -> None:
        """Start the background flush task (requires a running event loop)."""
        self._ensure_worker()

    async def stop(self) -> None:
        """Stop the worker and run one final flush to drain pending spend."""
        self._shutdown.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except (TimeoutError, asyncio.CancelledError, Exception):  # noqa: BLE001 - teardown must never raise
                self._task.cancel()
                try:
                    await self._task
                except (asyncio.CancelledError, Exception):
                    logger.debug("cost attribution worker teardown", exc_info=True)
            self._task = None
        try:
            await self.flush_once()
        except Exception:
            logger.warning("cost attribution final flush failed", exc_info=True)

    async def flush_once(self) -> None:
        """One aggregation cycle: CH recompute → warm upsert → gauge → audit

        Best-effort per step: a failure in one store never aborts the others
        and never raises to the caller's business path.

        Hermetic no-op when nothing was recorded and no stores were injected
        — ``stop()``/unit tests must never open ClickHouse/Postgres sockets
        just because the flush timer fired.
        """
        if not self._spend and self._hot_store is None and self._warm_store is None:
            return

        today = _utc_today()
        source = "memory"

        # 1. Authoritative recompute from ClickHouse (hybrid design). The lazy
        #    store is only created once there is local spend to reconcile —
        #    an empty, non-injected tracker never dials the network.
        hot = self._hot_store
        if hot is None and self._spend:
            hot = self._resolve_hot_store()
        if hot is not None:
            try:
                ch_rows = hot.get_tenant_cost_daily(_start_of_utc_day(today))
            except Exception:
                logger.warning(
                    "cost attribution: ClickHouse recompute failed; "
                    "using in-memory totals",
                    exc_info=True,
                )
            else:
                source = "clickhouse"
                for tenant, agent, total in ch_rows:
                    key = (tenant, agent, today)
                    self._spend[key] = max(self._spend.get(key, 0.0), float(total))

        # 2. Snapshot today's rows and upsert into the warm tier.
        rows: list[dict[str, Any]] = [
            {
                "tenant_id": tenant,
                "agent_id": agent,
                "day": day,
                "cost_usd_total": round(cost, 8),
            }
            for (tenant, agent, day), cost in self._spend.items()
            if day == today and cost > 0
        ]
        if rows:
            warm = self._warm_store
            if warm is None:
                warm = self._resolve_warm_store()
            if warm is not None:
                try:
                    await warm.upsert_tenant_cost_daily(rows)
                except Exception:
                    logger.warning(
                        "cost attribution: warm upsert failed (%d rows)",
                        len(rows),
                        exc_info=True,
                    )

        # 3. Gauges — updated even when the warm write failed.
        for row in rows:
            cost_per_tenant_usd.labels(
                tenant_id=row["tenant_id"], agent_id=row["agent_id"]
            ).set(row["cost_usd_total"])

        # 4. Audit trail: one event per *changed* row (never per unchanged tick).
        for row in rows:
            key = (row["tenant_id"], row["agent_id"], row["day"])
            prev = self._last_flushed.get(key)
            if prev is not None and abs(prev - row["cost_usd_total"]) < 1e-12:
                continue
            self._last_flushed[key] = row["cost_usd_total"]
            self._write_audit(row, source)

        # 5. Refresh per-tenant budget overrides — only against an already
        #    available warm store (injected, or resolved by an upsert above);
        #    never lazily dial Postgres just to read config.
        if self._warm_store is not None:
            try:
                fresh = await self._warm_store.get_tenant_budget_overrides()
            except Exception:
                logger.debug(
                    "cost attribution: budget override refresh failed", exc_info=True
                )
            else:
                self._overrides.clear()
                self._overrides.update(fresh)

    # ------------------------------------------------------------------
    # Store resolution (lazy, fail-open)
    # ------------------------------------------------------------------

    def _resolve_hot_store(self) -> Any | None:
        if self._hot_store is not None:
            return self._hot_store
        try:
            from agent_obs.storage.hot import HotStore

            self._hot_store = HotStore()
        except Exception:
            logger.debug("cost attribution: HotStore unavailable", exc_info=True)
            return None
        return self._hot_store

    def _resolve_warm_store(self) -> Any | None:
        if self._warm_store is not None:
            return self._warm_store
        try:
            from agent_obs.storage.warm import WarmStore

            self._warm_store = WarmStore()
        except Exception:
            logger.debug("cost attribution: WarmStore unavailable", exc_info=True)
            return None
        return self._warm_store

    def _write_audit(self, row: dict[str, Any], source: str) -> None:
        """Write one cost-attribution audit event; never raises (PP42 §4)."""
        from agent_obs.guardrail.audit import CostAttributionAuditEvent

        event = CostAttributionAuditEvent.for_cost_update(
            tenant_id=row["tenant_id"],
            agent_id=row["agent_id"],
            day=row["day"],
            cost_usd_total=row["cost_usd_total"],
            source=source,
        )
        hot = self._resolve_hot_store()
        if hot is None:
            return
        try:
            hot.write_audit_event(event)
        except Exception:
            logger.debug("cost attribution audit write failed", exc_info=True)

    # ------------------------------------------------------------------
    # Worker (PC33 pattern)
    # ------------------------------------------------------------------

    def _ensure_worker(self) -> None:
        if self._task is not None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._shutdown.clear()
        self._task = loop.create_task(self._flush_worker())

    async def _flush_worker(self) -> None:
        while not self._shutdown.is_set():
            try:
                await asyncio.wait_for(
                    self._shutdown.wait(), timeout=self._flush_interval
                )
                break
            except TimeoutError:
                pass
            try:
                await self.flush_once()
            except Exception:
                logger.warning("cost attribution flush failed", exc_info=True)


class BudgetEnforcer:
    """Tenant-level daily budget check (PP42 §3).

    Budget for a tenant = ``tracker.overrides[tenant]`` (warm ``tenant_config``)
    when present, else env ``TENANT_BUDGET_USD_DEFAULT`` (default 10.0 USD).
    A budget ``<= 0`` disables enforcement for that tenant.  Spend is read
    from the tracker's in-memory accumulator so the check is synchronous
    and never touches the network.
    """

    def __init__(
        self,
        tracker: CostAttributionTracker,
        *,
        default_budget_usd: float | None = None,
    ) -> None:
        self._tracker = tracker
        if default_budget_usd is None:
            default_budget_usd = _parse_float(
                os.environ.get("TENANT_BUDGET_USD_DEFAULT"),
                _DEFAULT_TENANT_BUDGET_USD,
                name="TENANT_BUDGET_USD_DEFAULT",
            )
        self.default_budget_usd = default_budget_usd

    def budget_for(self, tenant_id: str) -> float:
        """Effective daily budget in USD for *tenant_id*."""
        return float(self._tracker.overrides.get(tenant_id, self.default_budget_usd))

    def check(self, tenant_id: str) -> str:
        """Return ``ok`` | ``approaching`` | ``exhausted`` for *tenant_id*."""
        budget = self.budget_for(tenant_id)
        if budget <= 0:
            return BUDGET_OK
        spend = self._tracker.spend_today(tenant_id)
        ratio = spend / budget
        if ratio >= 1.0:
            return BUDGET_EXHAUSTED
        if ratio >= BUDGET_APPROACHING_RATIO:
            return BUDGET_APPROACHING
        return BUDGET_OK


__all__ = [
    "BUDGET_APPROACHING",
    "BUDGET_APPROACHING_RATIO",
    "BUDGET_EXHAUSTED",
    "BUDGET_OK",
    "BudgetEnforcer",
    "CostAttributionTracker",
    "attribution_from_headers",
]
