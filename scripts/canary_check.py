#!/usr/bin/env python3
"""SLO gate for canary deployments (PP29, T3.6.5).

Compares the burn rate of ``new_version`` against ``old_version`` over the
last observation window (30 minutes by default) and exits 0 when the canary is
healthy, 1 when it must be rolled back.

Data source: ClickHouse via :meth:`HotStore.get_sli_stats` — the same counts
the SLO refresher uses, scoped to a deploy version by the ``agent_version``
span attribute (stamped in-process by ``ObservabilitySDK._enqueue`` from
``AGENT_OBS_DEPLOY_VERSION``). No Prometheus hop, no version label cardinality.

Wiring it into CI (``.github/workflows/canary.yml``)::

    - name: SLO gate
      run: python scripts/canary_check.py --new-version $NEW --old-version $OLD
      # exit 0 -> ramp to 100%, exit 1 -> rollback + page on-call

Exit codes:
    0  canary healthy — burn rate under threshold, ramp to 100%
    1  canary unhealthy — burn rate over threshold, roll back
    2  could not determine (ClickHouse unreachable, or the new version has no
       telemetry at all) — rolled back unless ``--fail-open`` is given.

    Rule 2 is fail-closed on purpose: a canary that produces no telemetry
    cannot prove itself healthy, and a monitoring outage must never silently
    enable a ramp (same doctrine as scripts/cicd/error_budget_gate.py).

Environment:
    CLICKHOUSE_HOST / PORT / DB / USER / PASSWORD   ClickHouse connection
    AGENT_OBS_CANARY_WINDOW          observation window spec (default "30m")
    AGENT_OBS_CANARY_BURN_THRESHOLD  single threshold for every SLO
                                     (default: per-SLO fast-pair threshold
                                      from SLO_CATALOG)
    AGENT_OBS_CANARY_FAIL_OPEN       "1" to allow the ramp when undeterminable
    AGENT_OBS_LOG_LEVEL              log level (default INFO)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from agent_obs.slo.definitions import (
    SLO_CATALOG,
    burn_rate,
    parse_window,
    sli_from_counts,
)

logger = logging.getLogger(__name__)

DEFAULT_WINDOW = os.environ.get("AGENT_OBS_CANARY_WINDOW", "30m")


class CanaryResult:
    """Outcome of one canary gate evaluation."""

    def __init__(
        self,
        new_rates: dict[str, float] | None = None,
        old_rates: dict[str, float] | None = None,
        new_totals: dict[str, int] | None = None,
        skipped: dict[str, str] | None = None,
        error: str | None = None,
    ):
        self.new_rates = new_rates or {}
        self.old_rates = old_rates or {}
        self.new_totals = new_totals or {}
        #: SLO name -> reason it was not evaluated (e.g. "no_data").
        self.skipped = skipped or {}
        self.error = error

    @property
    def ok(self) -> bool:
        return self.error is None and bool(self.new_rates)

    def to_dict(self) -> dict:
        return {
            "new_rates": self.new_rates,
            "old_rates": self.old_rates,
            "new_totals": self.new_totals,
            "skipped": self.skipped,
            "error": self.error,
        }


def default_thresholds(slo_names: list[str]) -> dict[str, float]:
    """Per-SLO gate threshold: the fast-pair short-window alert threshold.

    Matches what PP26 pages on (e.g. 14.4 for availability/latency_p95), so
    the canary gate and the alert rules agree on what "burning too fast" means.
    """
    thresholds: dict[str, float] = {}
    for name in slo_names:
        slo = SLO_CATALOG[name]
        thresholds[name] = slo.burn_rate_thresholds[0][2] if slo.burn_rate_thresholds else 6.0
    return thresholds


def _burn_rates_for_version(
    store,
    version: str,
    window,
    slo_names: list[str],
) -> tuple[dict[str, float], dict[str, int], dict[str, str]]:
    """Per-SLO burn rates for one deploy version.

    Returns ``(rates, totals, skipped)``. An SLO with zero events for this
    version lands in ``skipped`` (it burns no budget) instead of silently
    resolving to a perfect 0.0 burn.
    """
    rates: dict[str, float] = {}
    totals: dict[str, int] = {}
    skipped: dict[str, str] = {}
    for name in slo_names:
        slo = SLO_CATALOG[name]
        good, total = store.get_sli_stats(
            slo.sli_kind, window, slo.good_threshold, version=version
        )
        if total <= 0:
            skipped[name] = "no_data"
            continue
        totals[name] = total
        rates[name] = burn_rate(sli_from_counts(good, total), slo.target)
    return rates, totals, skipped


def fetch_burn_rates(
    store,
    new_version: str,
    old_version: str,
    window_spec: str = DEFAULT_WINDOW,
    slo_names: list[str] | None = None,
) -> CanaryResult:
    """Query ClickHouse for per-SLO burn rates of both versions.

    A storage/SQL failure becomes ``CanaryResult(None, error=...)`` — the
    caller decides fail-open vs fail-closed.
    """
    slo_names = slo_names or list(SLO_CATALOG)
    try:
        window = parse_window(window_spec)
        new_rates, new_totals, skipped = _burn_rates_for_version(
            store, new_version, window, slo_names
        )
        old_rates, _old_totals, _old_skipped = _burn_rates_for_version(
            store, old_version, window, slo_names
        )
    except Exception as exc:  # noqa: BLE001 - any storage failure is a gate error
        return CanaryResult(error=f"burn-rate query failed: {exc}")

    if not new_rates:
        # No evaluated SLO at all: the canary produced no telemetry in the
        # window (fresh deploy, no traffic, or the exporter is down).
        return CanaryResult(
            new_rates={},
            old_rates=old_rates,
            new_totals=new_totals,
            skipped=skipped,
            error=(
                f"no telemetry for new_version={new_version} "
                f"over {window_spec} (skipped: {', '.join(sorted(skipped)) or 'none'})"
            ),
        )
    return CanaryResult(
        new_rates=new_rates,
        old_rates=old_rates,
        new_totals=new_totals,
        skipped=skipped,
    )


def evaluate_gate(
    result: CanaryResult,
    thresholds: dict[str, float],
    fail_open: bool = False,
) -> int:
    """Map a canary result onto a process exit code.

    Breach is a *strict* inequality (``rate > threshold``) — burning exactly at
    the sustainable threshold is not an incident, matching PP26's alert rule.
    """
    if result.error is not None:
        # Without counts we cannot prove the canary is healthy. Fail closed
        # (roll back) unless explicitly told otherwise.
        print(f"canary gate: {result.error}", file=sys.stderr)
        return 0 if fail_open else 2

    if not result.new_rates:
        # No evaluated SLO at all (nothing to compare against a threshold).
        print("canary gate: no per-SLO burn rates to evaluate", file=sys.stderr)
        return 0 if fail_open else 2

    breached = [
        name
        for name, rate in result.new_rates.items()
        if rate > thresholds.get(name, float("inf"))
    ]
    if breached:
        detail = ", ".join(
            f"{n}={result.new_rates[n]:.3f}>{thresholds.get(n, float('inf'))}"
            for n in breached
        )
        print(f"canary gate: UNHEALTHY — rollback ({detail})", file=sys.stderr)
        return 1

    summary = ", ".join(f"{n}={v:.3f}" for n, v in sorted(result.new_rates.items()))
    skipped = f" (skipped: {', '.join(sorted(result.skipped))})" if result.skipped else ""
    print(f"canary gate: healthy ({summary}){skipped}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--new-version", required=True, help="Version being canaried")
    parser.add_argument("--old-version", required=True, help="Version traffic returns to")
    parser.add_argument(
        "--window",
        default=DEFAULT_WINDOW,
        help="Observation window spec, e.g. 30m (default: %(default)s)",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=None,
        help="Single burn-rate threshold for every SLO "
        "(default: per-SLO fast-pair threshold from SLO_CATALOG, "
        "or AGENT_OBS_CANARY_BURN_THRESHOLD)",
    )
    parser.add_argument(
        "--slos",
        default="",
        help="Comma-separated SLO subset to evaluate (default: whole catalog)",
    )
    parser.add_argument(
        "--fail-open",
        action="store_true",
        default=os.environ.get("AGENT_OBS_CANARY_FAIL_OPEN", "") in ("1", "true", "yes"),
        help="Allow the ramp when the gate cannot determine health (default: fail closed)",
    )
    parser.add_argument("--json", action="store_true", help="Print the result as JSON")
    args = parser.parse_args(argv)

    slo_names = [n.strip() for n in args.slos.split(",") if n.strip()] or list(SLO_CATALOG)
    unknown = [n for n in slo_names if n not in SLO_CATALOG]
    if unknown:
        parser.error(f"unknown SLO(s): {', '.join(unknown)}")

    env_threshold = os.environ.get("AGENT_OBS_CANARY_BURN_THRESHOLD", "").strip()
    thresholds = default_thresholds(slo_names)
    if args.threshold is not None:
        thresholds = dict.fromkeys(slo_names, args.threshold)
    elif env_threshold:
        try:
            thresholds = dict.fromkeys(slo_names, float(env_threshold))
        except ValueError:
            print(
                f"canary gate: invalid AGENT_OBS_CANARY_BURN_THRESHOLD={env_threshold!r}",
                file=sys.stderr,
            )
            return 2

    from agent_obs.storage.maintenance import build_hot_store

    store = build_hot_store()
    try:
        result = fetch_burn_rates(
            store, args.new_version, args.old_version, args.window, slo_names
        )
    finally:
        try:
            store.close()
        except Exception as close_exc:  # noqa: BLE001 - never mask the result
            logger.debug("canary gate: store close failed: %s", close_exc)

    if args.json:
        print(
            json.dumps(
                {
                    **result.to_dict(),
                    "new_version": args.new_version,
                    "old_version": args.old_version,
                    "window": args.window,
                    "thresholds": thresholds,
                },
                indent=2,
            )
        )
    return evaluate_gate(result, thresholds, fail_open=args.fail_open)


if __name__ == "__main__":
    logging.basicConfig(
        level=os.environ.get("AGENT_OBS_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    sys.exit(main())
