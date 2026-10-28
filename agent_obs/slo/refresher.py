"""SLO refresher: queries ClickHouse and exports per-SLO gauges (PP25).

Periodically computes each catalog SLO's SLI over its rolling window and
burn rates over the alerting windows, then publishes them as Prometheus
gauges (declared in ``agent_obs.metrics``):

- ``slo_target{name}`` — gauge, the SLO target (0..1)
- ``slo_actual_sli{name}`` — gauge, rolling SLI (updated every 5 minutes)
- ``slo_burn_rate{name,window}`` — gauge, burn rate per alerting window.
  The ``window`` label is the *duration* (``5m``/``1h``/``6h``/``1d``), not
  the short/long role — PP26's ``SLOBurnRate`` maps roles to durations.
- ``slo_error_budget_remaining{name}`` — gauge 0..1, budget left on the
  rolling window, computed by PP27's :class:`~agent_obs.slo.error_budget.ErrorBudget`
  from the same (good, total) counts as the SLI.
- ``slo_error_budget_actions_taken_total{name,action}`` — counter of policy
  transitions fired by an attached :class:`~agent_obs.slo.error_budget.ErrorBudgetPolicy`.

When an ``ErrorBudgetPolicy`` is attached, every refresh evaluates each SLO's
budget against it. The policy is transition-triggered (actions fire once per
state change, not per tick), so a 5-minute cadence is safe.

Never blocks business logic (SDK invariant): ClickHouse failures are logged
and the gauges simply keep their previous values until the next refresh.
"""

from __future__ import annotations

import logging
import os
import threading
from datetime import timedelta

from agent_obs.metrics import (
    slo_actual_sli,
    slo_burn_rate,
    slo_error_budget_remaining,
    slo_target,
)
from agent_obs.slo.definitions import (
    SLO,
    SLO_CATALOG,
    burn_rate,
    parse_window,
    sli_from_counts,
    unique_windows,
    window_label,
)
from agent_obs.slo.error_budget import ErrorBudget, ErrorBudgetPolicy

logger = logging.getLogger(__name__)

#: Default refresh cadence (PP25 spec: "обновляется раз в 5 минут").
DEFAULT_REFRESH_SECONDS = 300


class SLORefresher:
    """Computes SLIs from ClickHouse hot tier and exports SLO gauges.

    Args:
        hotstore: ``agent_obs.storage.hot.HotStore`` (or a test double exposing
            ``get_sli_stats``). Querying is delegated to HotStore so the SQL
            lives next to the rest of the ClickHouse access code.
        catalog: SLO catalog to evaluate. Defaults to ``SLO_CATALOG``.
        interval_seconds: Refresh cadence. Defaults to
            ``AGENT_OBS_SLO_REFRESH_SECONDS`` env var, falling back to 300s.
        policy: Optional PP27 :class:`~agent_obs.slo.error_budget.ErrorBudgetPolicy`.
            When present, every refresh evaluates the rolling-window budget of
            each SLO against it (soft/hard actions fire on transitions only).
    """

    def __init__(
        self,
        hotstore: object,
        catalog: dict[str, SLO] | None = None,
        interval_seconds: float | None = None,
        policy: ErrorBudgetPolicy | None = None,
    ) -> None:
        self.hotstore = hotstore
        self.catalog = SLO_CATALOG if catalog is None else catalog
        self.policy = policy
        if interval_seconds is None:
            interval_seconds = float(
                os.environ.get("AGENT_OBS_SLO_REFRESH_SECONDS", str(DEFAULT_REFRESH_SECONDS))
            )
        self.interval_seconds = interval_seconds
        # Rolling-window SLI is the slowest signal; queried once per SLO kind.
        self._windows = unique_windows(self.catalog)

    def refresh(self) -> dict[str, dict[str, float]]:
        """Run one refresh cycle for every SLO in the catalog.

        Returns a per-SLO report ``{"sli": ..., "burn_rates": {...},
        "error_budget_remaining": ...}`` for logging/tests. Individual SLO
        failures are logged and skipped — one broken query must not starve
        the other gauges.
        """
        report: dict[str, dict[str, float]] = {}
        for name, slo in self.catalog.items():
            try:
                report[name] = self._refresh_one(slo)
            except Exception:
                logger.exception("SLO refresh failed for %s; gauges unchanged", name)
        return report

    def _refresh_one(self, slo: SLO) -> dict[str, float]:
        # 1. Rolling-window SLI (the SLO's own 30d window).
        good, total = self._fetch_counts(slo, slo.window)
        sli = sli_from_counts(good, total)
        slo_target.labels(name=slo.name).set(slo.target)
        slo_actual_sli.labels(name=slo.name).set(sli)

        # 2. Burn rate per alerting window (deduplicated across pairs).
        burn_rates: dict[str, float] = {}
        for short, long, _threshold in slo.burn_rate_thresholds:
            for spec in (short, long):
                if spec in burn_rates:
                    continue
                window = parse_window(spec)
                if window == slo.window:
                    window_sli = sli
                else:
                    window_sli = sli_from_counts(*self._fetch_counts(slo, window))
                rate = burn_rate(window_sli, slo.target)
                burn_rates[spec] = rate
                slo_burn_rate.labels(name=slo.name, window=spec).set(rate)

        # 3. Error budget remaining (PP27): budget = (1 - target) * total
        # events on the rolling window; remaining = unspent share.
        budget = ErrorBudget.from_counts(slo.name, slo.target, good, total)
        remaining = budget.clamped_remaining
        slo_error_budget_remaining.labels(name=slo.name).set(remaining)

        # 4. Policy actions (soft/hard/recover) fire only on state transitions.
        if self.policy is not None:
            self.policy.evaluate(slo.name, budget)

        logger.info(
            "SLO %s: sli=%.4f target=%.4f budget_remaining=%.2f burn_rates=%s",
            slo.name, sli, slo.target, remaining, burn_rates,
        )
        return {"sli": sli, "error_budget_remaining": remaining, **{
            f"burn_{w}": r for w, r in burn_rates.items()
        }}

    def _fetch_counts(self, slo: SLO, window: timedelta) -> tuple[int, int]:
        return self.hotstore.get_sli_stats(  # type: ignore[attr-defined]
            slo.sli_kind, window, slo.good_threshold
        )

    def run_forever(self, stop_event: threading.Event | None = None) -> None:
        """Blocking refresh loop for embedding in a daemon thread.

        Stops when ``stop_event`` is set (or immediately if already set).
        Sleeps in slices so shutdown is responsive.
        """
        stop_event = stop_event or threading.Event()
        logger.info(
            "SLO refresher started (interval=%.0fs, windows=%s)",
            self.interval_seconds,
            [window_label(w) for w in self._windows],
        )
        while not stop_event.is_set():
            self.refresh()
            stop_event.wait(self.interval_seconds)
        logger.info("SLO refresher stopped")

    def refresh_async(self) -> None:
        """Kick off a refresh in a background thread (fire-and-forget)."""
        threading.Thread(target=self.refresh, name="slo-refresh", daemon=True).start()
