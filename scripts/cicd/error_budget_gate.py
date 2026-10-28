#!/usr/bin/env python3
"""CI/CD error-budget gate (PP27, T3.6.3).

Deploy gate driven by the SLO error budgets: fails the status check (exit 1)
while ANY catalog SLO has exhausted its rolling-window budget
(``slo_error_budget_remaining <= 0``). A frozen budget means the SLO has spent
its whole allowance of bad events — shipping more change now risks an
unrecoverable incident, so deploys wait until the refresher sees recovery.

Wiring it into CI (GitHub example — the repo has no workflows yet, PP29 adds
the canary one)::

    - name: Error budget gate
      run: python scripts/cicd/error_budget_gate.py
      # exit 1 fails the check and blocks the merge/deploy

The gate reads Prometheus (the same channel the sampler policy uses); it
never talks to ClickHouse directly, so it works from any machine that can
reach the Prometheus HTTP API.

Exit codes:
    0  budgets healthy — deploy allowed
    1  at least one SLO budget exhausted — deploy blocked
    2  could not determine (Prometheus unreachable/failed) — deploy blocked
       unless ``--fail-open`` is given.

Environment:
    PROMETHEUS_URL                base URL (default http://localhost:9090)
    AGENT_OBS_GATE_FAIL_OPEN      "1" to allow deploys when Prometheus is down
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import httpx

DEFAULT_PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://localhost:9090")
DEFAULT_TIMEOUT_SECONDS = 10.0

#: Instant query: remaining budget per SLO (label `name`). The gate only
#: cares about exhaustion, so no aggregation is applied — every series counts.
BUDGET_QUERY = "slo_error_budget_remaining"


class GateResult:
    """Outcome of one gate evaluation."""

    def __init__(self, budgets: dict[str, float] | None, error: str | None = None):
        self.budgets = budgets or {}
        self.error = error

    @property
    def exhausted(self) -> list[str]:
        """SLO names whose budget is at/below zero."""
        return sorted(name for name, value in self.budgets.items() if value <= 0.0)

    @property
    def ok(self) -> bool:
        return self.error is None and not self.exhausted


def fetch_budgets(
    prometheus_url: str = DEFAULT_PROMETHEUS_URL,
    client: httpx.Client | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> GateResult:
    """Query Prometheus for per-SLO remaining budgets.

    A transport error, non-200 or non-success payload becomes
    ``GateResult(None, error=...)`` — the caller decides fail-open vs closed.
    """
    url = f"{prometheus_url.rstrip('/')}/api/v1/query"
    owns_client = client is None
    if client is None:
        client = httpx.Client(timeout=timeout)
    try:
        response = client.get(url, params={"query": BUDGET_QUERY})
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:  # noqa: BLE001 - any fetch/parse failure becomes a gate error
        return GateResult(None, error=f"prometheus query failed: {exc}")
    finally:
        if owns_client:
            client.close()

    if payload.get("status") != "success":
        return GateResult(None, error=f"prometheus query failed: {payload.get('error')}")

    budgets: dict[str, float] = {}
    for series in payload.get("data", {}).get("result", []):
        name = series.get("metric", {}).get("name")
        raw = series.get("value", [None, None])[1]
        if name is None or raw is None:
            continue
        try:
            budgets[name] = float(raw)
        except (TypeError, ValueError):
            continue
    return GateResult(budgets)


def evaluate_gate(result: GateResult, fail_open: bool = False) -> int:
    """Map a gate result onto a process exit code."""
    if result.error is not None:
        # Prometheus is the gate's only signal; without it we cannot prove the
        # budget is healthy. Fail closed (block) unless explicitly told to fail
        # open — a monitoring outage must not silently enable deploys.
        print(f"error-budget gate: {result.error}", file=sys.stderr)
        return 0 if fail_open else 2

    if not result.budgets:
        # No SLO series at all: the refresher has not published yet (fresh
        # deployment, no traffic). Nothing is proven exhausted — allow.
        print("error-budget gate: no slo_error_budget_remaining series; allowing")
        return 0

    exhausted = result.exhausted
    if exhausted:
        print(
            "error-budget gate: BLOCKED — exhausted SLOs: "
            + ", ".join(f"{n} ({result.budgets[n]:.4f})" for n in exhausted),
            file=sys.stderr,
        )
        return 1

    summary = ", ".join(f"{n}={v:.4f}" for n, v in sorted(result.budgets.items()))
    print(f"error-budget gate: allowed ({summary})")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--prometheus-url",
        default=DEFAULT_PROMETHEUS_URL,
        help="Prometheus base URL (default: %(default)s)",
    )
    parser.add_argument(
        "--fail-open",
        action="store_true",
        default=os.environ.get("AGENT_OBS_GATE_FAIL_OPEN", "") in ("1", "true", "yes"),
        help="Allow the deploy when Prometheus is unreachable (default: fail closed)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the raw budget map as JSON (for CI annotations)",
    )
    args = parser.parse_args(argv)

    result = fetch_budgets(args.prometheus_url)
    if args.json:
        print(json.dumps({"budgets": result.budgets, "error": result.error}, indent=2))
    return evaluate_gate(result, fail_open=args.fail_open)


if __name__ == "__main__":
    sys.exit(main())
