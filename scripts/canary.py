#!/usr/bin/env python3
"""Canary ramp / rollback executor (PP29, T3.6.5).

Companion to ``scripts/canary_check.py`` (which only decides): this script
*does* the two terminal actions of the canary workflow and records them.

    canary.py ramp     --new-version X --old-version Y
    canary.py rollback --new-version X --old-version Y

What rollback always does, in order:
  1. runs the revert command (``CANARY_ROLLBACK_CMD`` or ``--cmd``),
  2. writes a ``canary_rollback`` audit event into ``audit_events_hot``,
  3. pushes a Slack/Telegram notification.

Steps 2 and 3 run even when step 1 fails — an on-call must learn about a
failed revert, not just a successful one. Every step is best-effort and never
raises into the caller: the process exit code is the only contract CI relies on.

Exit codes:
    0  ramp executed / rollback executed (or ``--dry-run``)
    1  ramp or rollback did not complete (missing command, non-zero exit,
       or the audit write failed)

Environment:
    CANARY_RAMP_CMD        shell command that ramps traffic to 100%
    CANARY_ROLLBACK_CMD    shell command that reverts the deploy
    CANARY_CMD_TIMEOUT     seconds per command (default 600)
    AGENT_OBS_CANARY_WINDOW / _BURN_THRESHOLD   gate parameters (audit payload)
    CLICKHOUSE_HOST / PORT / DB / USER / PASSWORD   audit destination
    SLACK_WEBHOOK_URL, AGENT_OBS_TELEGRAM_BOT_TOKEN, AGENT_OBS_TELEGRAM_CHAT_ID
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from agent_obs.notify import format_rollback_message, notify
from agent_obs.slo.definitions import SLO_CATALOG

logger = logging.getLogger("canary")

DEFAULT_CMD_TIMEOUT = 600.0
DEFAULT_WINDOW = os.environ.get("AGENT_OBS_CANARY_WINDOW", "30m")
RAMP_CMD_ENV = "CANARY_RAMP_CMD"
ROLLBACK_CMD_ENV = "CANARY_ROLLBACK_CMD"


def run_command(command: str | None, timeout: float = DEFAULT_CMD_TIMEOUT) -> tuple[bool, str | None]:
    """Run a shell command, returning ``(ok, error_detail)``.

    ``None``/empty command -> ``(False, "no command configured")``: a ramp or
    rollback that silently does nothing is exactly the failure mode this
    script exists to prevent.
    """
    if not command or not command.strip():
        return False, "no command configured"
    try:
        proc = subprocess.run(
            command.strip(),
            shell=True,
            # DEVNULL, not the inherited handle: under pytest (and in minimal CI
            # containers) stdin can be an invalid handle, which on Windows makes
            # CreateProcess fail with WinError 6 before the command runs.
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,  # the exit code is the contract, not an exception
        )
    except subprocess.TimeoutExpired:
        return False, f"command timed out after {timeout:.0f}s: {command.strip()!r}"
    except Exception as exc:  # noqa: BLE001 - surfaced as an error string
        return False, f"command failed to start: {exc}"
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()[-500:]
        return False, f"command exited {proc.returncode}: {command.strip()!r} {tail}"
    return True, None


def parse_burn_rates(raw: str) -> dict[str, float]:
    """Parse ``"availability=1.2,latency_p95=3.0"`` into a float map."""
    rates: dict[str, float] = {}
    for chunk in (raw or "").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        name, _, value = chunk.partition("=")
        try:
            rates[name.strip()] = float(value)
        except ValueError:
            raise ValueError(f"invalid burn rate entry: {chunk!r}")
    return rates


def _thresholds_for(slo_names: list[str], override: float | None) -> dict[str, float]:
    if override is not None:
        return dict.fromkeys(slo_names, override)
    thresholds: dict[str, float] = {}
    for name in slo_names:
        slo = SLO_CATALOG[name]
        thresholds[name] = slo.burn_rate_thresholds[0][2] if slo.burn_rate_thresholds else 6.0
    return thresholds


def _collect_burn_rates(args) -> tuple[dict[str, float], dict[str, float], str]:
    """Resolve ``(new_rates, thresholds, slo_name)`` for the audit payload.

    Prefers ``--burn-rates`` (exactly what the gate saw, passed by CI);
    otherwise re-queries ClickHouse best-effort.
    """
    slo_names = [n.strip() for n in (args.slos or "").split(",") if n.strip()] or list(SLO_CATALOG)
    unknown = [name for name in slo_names if name not in SLO_CATALOG]
    if unknown:
        # Same rule as the gate: a typo'd SLO must not raise mid-rollback.
        logger.warning("canary: unknown SLO(s) ignored: %s", ", ".join(unknown))
        slo_names = [name for name in slo_names if name in SLO_CATALOG] or list(SLO_CATALOG)
    env_threshold = os.environ.get("AGENT_OBS_CANARY_BURN_THRESHOLD", "").strip()
    override = args.threshold
    if override is None and env_threshold:
        try:
            override = float(env_threshold)
        except ValueError:
            override = None
    thresholds = _thresholds_for(slo_names, override)

    rates: dict[str, float] = {}
    if args.burn_rates:
        try:
            rates = parse_burn_rates(args.burn_rates)
        except ValueError as exc:
            logger.warning("canary: %s", exc)
    else:
        try:
            from agent_obs.storage.maintenance import build_hot_store
            from scripts.canary_check import fetch_burn_rates

            store = build_hot_store()
            try:
                result = fetch_burn_rates(
                    store, args.new_version, args.old_version, args.window, slo_names
                )
            finally:
                try:
                    store.close()
                except Exception as close_exc:  # noqa: BLE001
                    logger.debug("canary: store close failed: %s", close_exc)
            rates = result.new_rates
        except Exception as exc:  # noqa: BLE001 - audit is best-effort
            logger.warning("canary: could not re-query burn rates: %s", exc)

    breached = [
        name
        for name, value in rates.items()
        if value > thresholds.get(name, float("inf"))
    ]
    primary_slo = breached[0] if breached else ""
    return rates, thresholds, primary_slo


def _write_audit(event) -> bool:
    """Persist an audit event; returns success, never raises."""
    try:
        from agent_obs.storage.maintenance import build_hot_store

        store = build_hot_store()
        try:
            store.write_audit_event(event)
        finally:
            try:
                store.close()
            except Exception as close_exc:  # noqa: BLE001
                logger.debug("canary: store close failed: %s", close_exc)
        return True
    except Exception as exc:  # noqa: BLE001 - audit is best-effort
        logger.warning("canary: audit write failed: %s", exc)
        return False


def cmd_ramp(args) -> int:
    command = args.cmd or os.environ.get(RAMP_CMD_ENV, "")
    if args.dry_run:
        print(f"canary ramp (dry-run): {command or '<CANARY_RAMP_CMD not set>'}")
        return 0
    ok, error = run_command(command, timeout=args.timeout)
    if not ok:
        print(f"canary ramp FAILED: {error}", file=sys.stderr)
        return 1
    print(f"canary ramped to 100% (version={args.new_version})")
    return 0


def cmd_rollback(args) -> int:
    from agent_obs.guardrail.audit import CanaryRollbackAuditEvent

    rates, thresholds, primary_slo = _collect_burn_rates(args)
    # ``thresholds`` already folds in --threshold / AGENT_OBS_CANARY_BURN_THRESHOLD.
    threshold = thresholds.get(primary_slo)
    if threshold is None:
        threshold = max(thresholds.values(), default=6.0)

    # 1) Revert the deploy.
    if args.dry_run:
        decision, error = "rollback_simulated", None
        print(
            f"canary rollback (dry-run): "
            f"{args.cmd or os.environ.get(ROLLBACK_CMD_ENV, '') or '<CANARY_ROLLBACK_CMD not set>'}"
        )
    else:
        command = args.cmd or os.environ.get(ROLLBACK_CMD_ENV, "")
        ok, error = run_command(command, timeout=args.timeout)
        decision = "rolled_back" if ok else "rollback_failed"

    # 2) Audit — always attempted, even when the revert failed.
    event = CanaryRollbackAuditEvent.for_rollback(
        new_version=args.new_version,
        old_version=args.old_version,
        slo_name=primary_slo,
        burn_rates=rates,
        threshold=float(threshold),
        window=args.window,
        decision=decision,
        error=error,
    )
    audit_ok = _write_audit(event)
    if audit_ok:
        print(f"canary rollback audit written: {event.audit_id} ({decision})")
    else:
        print("canary rollback audit write FAILED", file=sys.stderr)

    # 3) Notification — always attempted, best-effort.
    message = format_rollback_message(
        new_version=args.new_version,
        old_version=args.old_version,
        burn_rates=rates,
        threshold=float(threshold),
        window=args.window,
        error=error,
    )
    delivery = notify(message)
    print(f"canary rollback notified: {delivery}")

    if decision == "rollback_failed":
        print(f"canary rollback FAILED: {error}", file=sys.stderr)
        return 1
    # A rehearsal (--dry-run) must stay usable without ClickHouse reachable.
    if not audit_ok and not args.dry_run:
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    for name, help_text in (
        ("ramp", "Ramp the canary to full traffic"),
        ("rollback", "Revert the canary, write the audit event, notify on-call"),
    ):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--new-version", required=True, help="Version being canaried")
        p.add_argument("--old-version", required=True, help="Version traffic returns to")
        p.add_argument("--cmd", default="", help="Override the ramp/rollback shell command")
        p.add_argument("--timeout", type=float, default=DEFAULT_CMD_TIMEOUT)
        p.add_argument(
            "--dry-run",
            action="store_true",
            help="Do not run the command (rollback: records decision=rollback_simulated)",
        )
        p.add_argument(
            "--window",
            default=DEFAULT_WINDOW,
            help="Observation window used for the audit payload (default: %(default)s)",
        )
        p.add_argument(
            "--threshold",
            type=float,
            default=None,
            help="Burn-rate threshold to record in the audit event",
        )
        p.add_argument("--slos", default="", help="Comma-separated SLO subset")
        p.add_argument(
            "--burn-rates",
            default="",
            help='Gate readings to record, e.g. "availability=20.1,latency_p95=3.2" '
            "(default: re-query ClickHouse best-effort)",
        )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "ramp":
        return cmd_ramp(args)
    return cmd_rollback(args)


if __name__ == "__main__":
    logging.basicConfig(
        level=os.environ.get("AGENT_OBS_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    sys.exit(main())
