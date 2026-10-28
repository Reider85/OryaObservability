"""Multi-window multi-burn-rate alerting (PP26, T3.6.2).

Google SRE Workbook style: an alert only fires when BOTH windows of a
(short, long, threshold) pair from :data:`SLO_CATALOG` exceed their
threshold simultaneously. A single hot window (e.g. a 5-minute traffic
spike) never fires — that is the whole point of multi-window alerting
(PP0 hard rule #5: single-window alerts are forbidden, too many false
positives).

The class fulfils the PP0 ``SLOBurnRate`` contract::

    class SLOBurnRate:                          # E3.6 — multi-window burn-rate
        def is_burned(self, window: str, slo: str) -> bool: ...
        def is_alert_fired(self, slo_name) -> bool: ...

Burn-rate values come from a provider callable ``(slo_name, window) ->
float``. By default the provider reads the ``slo_burn_rate{name,window}``
gauge published by :class:`agent_obs.slo.refresher.SLORefresher` (5-minute
cadence) — the refresher is the single source of truth, so alerting logic
never re-queries ClickHouse itself.

The Prometheus side of this feature lives in
``infra/prometheus-rules.yml`` (group ``slo-burn-rate``) with page/ticket
routing in ``infra/alertmanager.yml``. This module is the in-process
counterpart: same math, same thresholds, usable from code (e.g. PP27
error-budget policy, PP28 dashboard backend) and unit-testable without a
Prometheus server.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from agent_obs.metrics import slo_burn_rate
from agent_obs.slo.definitions import SLO, SLO_CATALOG

logger = logging.getLogger(__name__)

#: Provider signature: (slo_name, window_spec) -> burn rate value.
BurnRateProvider = Callable[[str, str], float]


def gauge_burn_rate_provider(slo_name: str, window: str) -> float:
    """Default provider: read the ``slo_burn_rate`` gauge published by SLORefresher.

    Returns 0.0 when the refresher has not published that series yet (a
    missing series burns no budget — same convention as an empty ClickHouse
    window being a perfect SLI).
    """
    child = slo_burn_rate._metrics.get((slo_name, window))
    if child is None:
        return 0.0
    return child._value.get()  # type: ignore[attr-defined]


class SLOBurnRate:
    """Multi-window burn-rate alert evaluator for the PP25 SLO catalog.

    Args:
        catalog: SLO catalog to evaluate. Defaults to ``SLO_CATALOG``.
        burn_rate_provider: ``(slo_name, window_spec) -> burn_rate``. Defaults
            to reading the ``slo_burn_rate`` gauge (values maintained by
            ``SLORefresher``). Tests inject a canned provider here.
    """

    def __init__(
        self,
        catalog: dict[str, SLO] | None = None,
        burn_rate_provider: BurnRateProvider | None = None,
    ) -> None:
        self.catalog = SLO_CATALOG if catalog is None else catalog
        self._provider: BurnRateProvider = (
            gauge_burn_rate_provider if burn_rate_provider is None else burn_rate_provider
        )

    def burn_rate(self, window: str, slo: str) -> float:
        """Current burn rate of ``window`` for ``slo`` via the provider."""
        return self._provider(slo, window)

    def _threshold_for_window(self, slo: SLO, window: str) -> float | None:
        """Threshold of the pair(s) this window participates in.

        A window can appear in several pairs (e.g. "1h" is the long side of
        the (5m, 1h, 14.4) pair and the short side of (1h, 6h, 6.0)). A
        window counts as burned when it exceeds ANY pair threshold it
        participates in, so the most permissive threshold wins.
        """
        thresholds = [t for short, long, t in slo.burn_rate_thresholds if window in (short, long)]
        return min(thresholds) if thresholds else None

    def is_burned(self, window: str, slo: str) -> bool:
        """True when ``window``'s burn rate exceeds its pair threshold.

        PP0 contract: windows are "5m", "1h", "6h", "1d". Raises ValueError
        for unknown SLO names or windows not referenced by the SLO's
        burn-rate pairs (misconfiguration must fail loud, not silently
        return False).
        """
        try:
            slo_def = self.catalog[slo]
        except KeyError:
            raise ValueError(f"unknown SLO: {slo!r}") from None
        threshold = self._threshold_for_window(slo_def, window)
        if threshold is None:
            raise ValueError(
                f"window {window!r} is not part of any burn-rate pair of SLO {slo!r}"
            )
        rate = self.burn_rate(window, slo)
        burned = rate > threshold
        logger.debug(
            "is_burned(slo=%s, window=%s): rate=%.3f threshold=%.3f -> %s",
            slo, window, rate, threshold, burned,
        )
        return burned

    def is_alert_fired(self, slo_name: str) -> bool:
        """True when ANY (short, long) pair of ``slo_name`` has BOTH sides over threshold.

        Single-window breaches (only short or only long) return False —
        multi-window correlation is what suppresses false positives.
        """
        try:
            slo_def = self.catalog[slo_name]
        except KeyError:
            raise ValueError(f"unknown SLO: {slo_name!r}") from None
        for short, long, threshold in slo_def.burn_rate_thresholds:
            short_rate = self.burn_rate(short, slo_name)
            long_rate = self.burn_rate(long, slo_name)
            if short_rate > threshold and long_rate > threshold:
                logger.info(
                    "burn-rate alert fired for SLO %s: pair (%s=%0.3f, %s=%0.3f) "
                    "exceeds threshold %.3f",
                    slo_name, short, short_rate, long, long_rate, threshold,
                )
                return True
        return False

    def fired_pairs(self, slo_name: str) -> list[tuple[str, str, float]]:
        """All pairs of ``slo_name`` currently over threshold on BOTH windows.

        Useful for diagnostics/runbooks: shows exactly which correlation
        windows triggered. (Used by PP27 policy actions and test assertions.)
        """
        slo_def = self.catalog[slo_name]
        fired: list[tuple[str, str, float]] = []
        for short, long, threshold in slo_def.burn_rate_thresholds:
            if (
                self.burn_rate(short, slo_name) > threshold
                and self.burn_rate(long, slo_name) > threshold
            ):
                fired.append((short, long, threshold))
        return fired
