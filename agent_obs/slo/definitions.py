"""SLO catalog: availability, latency, quality, cost (PP25, T3.6.1).

Four SLOs with target + rolling window + multi-window burn-rate thresholds
(Google SRE Workbook style). Single source of truth for PP26 (burn-rate
alerting), PP27 (error budget) and PP28 (Grafana dashboard).

PP59 extends with Google SRE Four Golden Signals: latency, traffic, errors, saturation.

SLI definitions (how each SLO is measured)
------------------------------------------
All SLIs are event ratios over a rolling window: ``good_events / total_events``.
An empty window is treated as a perfect SLI (1.0) — no traffic burns no budget.

availability
    SLI = 1 - error_traces / total_traces (rolling 30d).
    A "trace" is a root span (``span_type='agent.loop'``); the trace is bad
    when its root span has ``status == 'error'``. ``status == 'blocked'``
    (guardrail rejection) counts as good — it is a deliberate refusal, not a
    failure.

latency_p95
    SLI = share of traces with wall-clock latency <= 3 seconds (rolling 30d).
    Latency = ``end_time - start_time`` on the root span. Target 0.95 means
    "p95 of traces complete within 3 seconds".

quality
    SLI = share of late-annotated eval results with
    ``scores.faithfulness > 0.7`` (rolling 30d, late-annotated). Source is
    ``eval_results_hot`` (LLM-judge writes), not spans.

cost_p99
    SLI = share of traces with per-request cost <= $0.05 (rolling 30d).
    Per-request cost is the sum of ``cost_usd`` across all spans of a trace.

Golden Signals (PP59)
----------------------
Google SRE Four Golden Signals for comprehensive system health monitoring:

latency_p99 (Golden Signal)
    SLI = share of requests with p99 latency <= 2 seconds (rolling 30d).
    Measures user experience and system responsiveness. Target 0.95 means
    "p99 of requests complete within 2 seconds".

traffic (Golden Signal)
    SLI = traffic rate within 20% of 7-day moving average (rolling 30d).
    Measures service load and capacity planning. Target 1.20 allows for
    20% growth above baseline without triggering alerts.

errors (Golden Signal)
    SLI = share of requests without errors (rolling 30d).
    Measures service reliability and error rates. Target 0.995 means
    "less than 0.5% of requests result in errors".

saturation (Golden Signal)
    SLI = share of time system utilization <= 80% (rolling 30d).
    Measures resource pressure and capacity headroom. Target 0.80 means
    "system operates below 80% utilization 95% of the time".

Burn rate
---------
``burn_rate = (1 - actual_sli) / (1 - target)`` — how fast the error budget
is being consumed relative to target. burn_rate == 1.0 means budget is being
burned exactly at the sustainable rate; > 1.0 means the SLO will be missed.

Note: hot-tier ClickHouse retention (spans 21d / evals 14d) is shorter than
the 30d SLO window, so the 30d SLI is a lower bound until warm-tier rollups
are wired in. Short windows (5m..1d) used by burn-rate alerting are fully
covered by the hot tier.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

#: Rolling measurement window for every catalog SLO (PP25 spec: 30 days).
DEFAULT_WINDOW = timedelta(days=30)

#: SLI kinds — which (good, total) pair a refresher must fetch.
SLI_KIND_AVAILABILITY = "availability"
SLI_KIND_LATENCY = "latency"
SLI_KIND_QUALITY = "quality"
SLI_KIND_COST = "cost"
#: SLI kinds for golden signals (PP59)
SLI_KIND_LATENCY_GOLDEN = "latency_golden"
SLI_KIND_TRAFFIC = "traffic"
SLI_KIND_ERRORS = "errors"
SLI_KIND_SATURATION = "saturation"


@dataclass(frozen=True)
class SLO:
    """A single service level objective.

    Attributes:
        name: Catalog key, also the Prometheus ``name`` label.
        target: Required SLI value (0..1) — the share of good events.
        window: Rolling measurement window (30 days for the catalog).
        burn_rate_thresholds: Multi-window burn-rate alert pairs
            ``[(short_window, long_window, threshold), ...]``. PP26 fires an
            alert only when BOTH windows of a pair exceed their threshold.
            Window specs are strings parseable by :func:`parse_window`.
        sli_kind: Which SLI definition to evaluate (SLI_KIND_* constant).
        good_threshold: Per-event "good" threshold for the SLI — latency in
            seconds, cost in USD, faithfulness score. ``None`` for
            availability (good = status != "error", no numeric threshold).
    """

    name: str
    target: float
    window: timedelta = DEFAULT_WINDOW
    burn_rate_thresholds: list[tuple[str, str, float]] = field(default_factory=list)
    sli_kind: str = SLI_KIND_AVAILABILITY
    good_threshold: float | None = None

    def __post_init__(self) -> None:
        # Traffic signal allows targets > 1.0 (e.g., 1.2 = 20% above baseline)
        if self.sli_kind == SLI_KIND_TRAFFIC:
            if not 0.0 < self.target:
                raise ValueError(f"SLO {self.name!r}: traffic target must be > 0, got {self.target}")
        else:
            if not 0.0 < self.target <= 1.0:
                raise ValueError(f"SLO {self.name!r}: target must be in (0, 1], got {self.target}")


def _slo(
    name: str,
    target: float,
    thresholds: list[tuple[str, str, float]],
    sli_kind: str,
    good_threshold: float | None = None,
) -> SLO:
    return SLO(
        name=name,
        target=target,
        window=DEFAULT_WINDOW,
        burn_rate_thresholds=thresholds,
        sli_kind=sli_kind,
        good_threshold=good_threshold,
    )


#: The PP25 catalog: 4 SLOs with target + window + burn-rate thresholds.
#: PP59 extends with 4 golden signals: latency, traffic, errors, saturation.
SLO_CATALOG: dict[str, SLO] = {
    "availability": _slo(
        "availability",
        target=0.999,
        thresholds=[("5m", "1h", 14.4), ("1h", "6h", 6.0)],
        sli_kind=SLI_KIND_AVAILABILITY,
    ),
    "latency_p95": _slo(
        "latency_p95",
        target=0.95,
        thresholds=[("5m", "1h", 14.4), ("1h", "6h", 6.0)],
        sli_kind=SLI_KIND_LATENCY,
        good_threshold=3.0,  # seconds
    ),
    "quality": _slo(
        "quality",
        target=0.90,
        thresholds=[("1h", "6h", 6.0), ("6h", "1d", 3.0)],
        sli_kind=SLI_KIND_QUALITY,
        good_threshold=0.7,  # faithfulness score
    ),
    "cost_p99": _slo(
        "cost_p99",
        target=0.95,
        thresholds=[("1h", "6h", 6.0), ("6h", "1d", 3.0)],
        sli_kind=SLI_KIND_COST,
        good_threshold=0.05,  # USD per request
    ),
    # PP59: Golden Signals (Google SRE Four Golden Signals)
    "latency_p99": _slo(
        "latency_p99",
        target=0.95,
        thresholds=[("5m", "1h", 14.4), ("1h", "6h", 6.0)],
        sli_kind=SLI_KIND_LATENCY_GOLDEN,
        good_threshold=2.0,  # seconds (p99 latency target)
    ),
    "traffic": _slo(
        "traffic",
        target=1.20,  # +20% tolerance above 7-day moving average
        thresholds=[("5m", "1h", 14.4), ("1h", "6h", 6.0)],
        sli_kind=SLI_KIND_TRAFFIC,
    ),
    "errors": _slo(
        "errors",
        target=0.995,  # < 0.5% error rate
        thresholds=[("5m", "1h", 14.4), ("1h", "6h", 6.0)],
        sli_kind=SLI_KIND_ERRORS,
    ),
    "saturation": _slo(
        "saturation",
        target=0.80,  # 80% utilization threshold
        thresholds=[("5m", "1h", 14.4), ("1h", "6h", 6.0)],
        sli_kind=SLI_KIND_SATURATION,
    ),
}

_WINDOW_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_window(spec: str) -> timedelta:
    """Parse a window spec like ``"5m"`` into a timedelta.

    Supported suffixes: ``s`` (seconds), ``m`` (minutes), ``h`` (hours),
    ``d`` (days). Raises ValueError on malformed input.
    """
    if len(spec) < 2 or spec[-1] not in _WINDOW_UNITS:
        raise ValueError(f"invalid window spec: {spec!r}")
    try:
        value = int(spec[:-1])
    except ValueError as exc:
        raise ValueError(f"invalid window spec: {spec!r}") from exc
    if value <= 0:
        raise ValueError(f"window spec must be positive: {spec!r}")
    return timedelta(seconds=value * _WINDOW_UNITS[spec[-1]])


def window_label(delta: timedelta) -> str:
    """Inverse of :func:`parse_window` — timedelta -> canonical spec string.

    ``timedelta(minutes=5) -> "5m"``. Falls back to seconds for windows that
    are not a whole number of minutes/hours/days.
    """
    seconds = int(delta.total_seconds())
    if seconds <= 0:
        raise ValueError(f"window must be positive, got {delta}")
    for unit, unit_seconds in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds % unit_seconds == 0:
            return f"{seconds // unit_seconds}{unit}"
    return f"{seconds}s"


def unique_windows(catalog: dict[str, SLO] | None = None) -> list[timedelta]:
    """Sorted unique windows referenced by burn-rate thresholds in the catalog.

    Used by the refresher to know which rolling windows it must query.
    Always includes each SLO's measurement window as well.
    """
    catalog = SLO_CATALOG if catalog is None else catalog
    windows: set[timedelta] = set()
    for slo in catalog.values():
        windows.add(slo.window)
        for short, long, _threshold in slo.burn_rate_thresholds:
            windows.add(parse_window(short))
            windows.add(parse_window(long))
    return sorted(windows)


def burn_rate(actual_sli: float, target: float) -> float:
    """Error-budget burn rate: ``(1 - actual_sli) / (1 - target)``.

    ``> 1.0`` means the budget is being consumed faster than the target
    allows. Raises ValueError when target == 1.0 (zero budget — burn rate
    is undefined).
    
    For traffic signals with target > 1.0, calculates burn rate as
    ``(actual_sli / target) - 1.0`` where actual_sli is the traffic ratio
    (actual_traffic / baseline_traffic).
    """
    if target == 1.0:
        raise ValueError(f"burn rate undefined for target={target!r} (zero budget)")
    
    if 0.0 < target < 1.0:
        # Regular SLI: burn rate increases as actual_sli decreases
        if not 0.0 <= actual_sli <= 1.0:
            raise ValueError(f"actual_sli must be in [0, 1], got {actual_sli!r}")
        return (1.0 - actual_sli) / (1.0 - target)
    elif target > 1.0:
        # Traffic signal: burn rate increases as actual_sli increases above target
        if actual_sli <= 0.0:
            raise ValueError(f"traffic actual_sli must be > 0, got {actual_sli!r}")
        return (actual_sli / target) - 1.0
    else:
        raise ValueError(f"target must be > 0, got {target!r}")


def sli_from_counts(good_events: int, total_events: int) -> float:
    """SLI from raw event counts. An empty window is a perfect SLI (1.0)."""
    if total_events <= 0:
        return 1.0
    return good_events / total_events
