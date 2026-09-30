"""PC21 — long-lived scheduler for the tiered-retention maintenance jobs.

Container entrypoint for the ``cron`` service in ``infra/docker-compose.yml``.
Runs in its own process, never inside the agent (PC21: "cron-jobs — это НЕ
SDK-код").

Why a Python scheduler rather than ofelia/cronic: the jobs must publish
Prometheus metrics that survive across runs, and a counter exported by a
short-lived per-run process resets to zero on every invocation. A single
long-lived process keeps ``agent_obs_cron_runs_total`` and friends in the
Prometheus registry, and lets the "> 2 missed cycles" alert read
``agent_obs_cron_last_success_timestamp_seconds``.

Usage::

    python scripts/cron/scheduler.py                 # serve metrics + run the schedule
    python scripts/cron/scheduler.py --run-once      # run all jobs once and exit
    python scripts/cron/scheduler.py --port 9777 --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import signal
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from agent_obs.storage.maintenance import (
    DEFAULT_AUDIT_RETENTION_DAYS,
    DEFAULT_BATCH_SIZE,
    DEFAULT_EVAL_RETENTION_DAYS,
    DEFAULT_SPAN_RETENTION_DAYS,
    JOB_CALIBRATE_DRIFT_THRESHOLD,
    JOB_CLEANUP_AUDIT_EVENTS,
    JOB_CLEANUP_EVAL_RESULTS,
    JOB_CLEANUP_VAULT,
    JOB_DRIFT_DETECTION,
    JOB_EXPORT_EMBEDDINGS,
    JOB_MIGRATE_SPANS,
    JOB_MIGRATE_TRACES,
    archive_expired_audit_events,
    build_cold_store,
    build_cold_trace_store,
    build_hot_store,
    build_warm_store,
    cleanup_vault_expired,
    migrate_eval_results_to_warm,
    migrate_spans_to_warm,
    migrate_traces_to_cold,
    run_cron_job,
    run_cron_job_async,
)

logging.basicConfig(
    level=os.environ.get("AGENT_OBS_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("agent_obs.cron.scheduler")

DEFAULT_PORT = 9777
# Repository root, so the sibling cron modules stay importable when this file
# is executed directly (``python scripts/cron/scheduler.py`` puts
# scripts/cron on sys.path, not the repo root).
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# Day-of-week uses 0-7 so both Sunday spellings parse; 7 is normalised to 0.
FIELD_RANGES = (range(0, 60), range(0, 24), range(1, 32), range(1, 13), range(0, 8))
FULL_DOW = set(range(7))
FULL_DOM = set(FIELD_RANGES[2])
# Stagger the daily jobs so the two migrations never compete for ClickHouse
# connections at the same instant.
DAILY_AUDIT_SPEC = "17 3 * * *"
DAILY_EVAL_SPEC = "47 3 * * *"
DAILY_MIGRATION_SPEC = "0 4 * * *"
WEEKLY_COLD_SPEC = "17 4 * * 0"  # Sunday 04:17 UTC, staggered off daily migration
# PC26: threshold calibration runs once a month, just after the weekly cold
# migration and well clear of the daily jobs' window.
MONTHLY_CALIBRATION_SPEC = "23 5 1 * *"  # 1st of the month, 05:23 UTC


# ---------------------------------------------------------------------------
# Cron expression parsing
# ---------------------------------------------------------------------------


def parse_cron_field(spec: str, index: int) -> set[int]:
    """Parse one field of a 5-field cron expression into a set of values.

    Supports ``*``, ``a``, ``a-b``, ``*/n``, ``a-b/n`` and comma lists.
    """
    values: set[int] = set()
    low, high = FIELD_RANGES[index][0], FIELD_RANGES[index][-1]
    for part in spec.split(","):
        part = part.strip()
        if not part:
            raise ValueError(f"empty cron field part: {spec!r}")
        step = 1
        if "/" in part:
            part, _, step_str = part.partition("/")
            step = int(step_str)
            if step < 1:
                raise ValueError(f"invalid cron step in {spec!r}")
        if part in ("*", "?"):
            start, end = low, high
        elif "-" in part:
            start_s, _, end_s = part.partition("-")
            start, end = int(start_s), int(end_s)
        else:
            start = end = int(part)
        if start < low or end > high or end < start:
            raise ValueError(f"cron field out of range: {spec!r} (allowed {low}-{high})")
        values.update(range(start, end + 1, step))
    if index == 4:
        values = {0 if v == 7 else v for v in values}
    return values


def validate_cron_spec(spec: str) -> tuple[set[int], ...]:
    """Validate a 5-field cron expression and return the parsed field sets."""
    fields = spec.split()
    if len(fields) != 5:
        raise ValueError(f"expected 5 cron fields, got {len(fields)}: {spec!r}")
    return tuple(parse_cron_field(f, i) for i, f in enumerate(fields))


def next_run_at(spec: str, now: datetime) -> datetime:
    """Return the first datetime strictly after ``now`` matching ``spec``.

    Minute resolution, evaluated in UTC. Pure function so the schedule is unit
    testable without a running loop.

    Raises
    ------
    ValueError
        If ``spec`` is not a valid 5-field cron expression.
    """
    minutes, hours, days, months, weekdays = validate_cron_spec(spec)

    candidate = (now + timedelta(minutes=1)).replace(second=0, microsecond=0)
    # A year of minutes is a safe search horizon for any expression that can
    # ever match (e.g. "0 0 30 2 *" for Feb 30 is invalid and will exhaust it).
    for _ in range(366 * 24 * 60):
        if (
            candidate.minute in minutes
            and candidate.hour in hours
            and candidate.month in months
            and _matches_day(candidate, days, weekdays)
        ):
            return candidate
        candidate += timedelta(minutes=1)
    raise ValueError(f"cron expression never matches: {spec!r}")


def _matches_day(candidate: datetime, days: set[int], weekdays: set[int]) -> bool:
    """Apply Vixie-cron day-of-month / day-of-week semantics.

    When both fields are restricted they are OR-ed; otherwise the restricted
    one must match.
    """
    dom = candidate.day in days
    dow = (candidate.weekday() + 1) % 7 in weekdays  # Python Monday=0 -> cron Sunday=0
    dom_restricted = days != FULL_DOM
    dow_restricted = weekdays != FULL_DOW
    if dom_restricted and dow_restricted:
        return dom or dow
    return dom and dow


@dataclass
class CronJob:
    """A scheduled maintenance job.

    ``invoke`` is an awaitable zero-arg callable, so both the blocking jobs
    (Vault scan, Parquet upload) and the async one (Postgres warm upsert) run
    through the same entry point.
    """

    name: str
    spec: str
    invoke: Callable[[], Awaitable[Any]]
    interval_seconds: int

    def next_after(self, now: datetime) -> datetime:
        return next_run_at(self.spec, now)


def _env_int(name: str, default: int) -> int:
    """Read an int from the environment, falling back to ``default`` if unusable."""
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    """Read a float from the environment, falling back to ``default`` if unusable."""
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default


def _env_list(name: str, default: list[str]) -> list[str]:
    """Read a comma-separated list, falling back to ``default`` if unset/blank."""
    raw = os.environ.get(name)
    if not raw:
        return list(default)
    parts = [part.strip() for part in raw.split(",")]
    items = [part for part in parts if part]
    return items or list(default)


def _env_str(name: str, default: str) -> str:
    """Read a string from the environment, falling back to ``default`` if unset/blank."""
    raw = os.environ.get(name)
    if not raw or not raw.strip():
        return default
    return raw.strip()


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _run_sync(name: str, fn: Callable[[], Any]) -> Awaitable[Any]:
    """Await a blocking job off the event loop, wrapped in the metrics harness."""

    async def _runner() -> Any:
        return await asyncio.to_thread(run_cron_job, name, fn)

    return _runner()


def _vault_job() -> Awaitable[Any]:
    return _run_sync(
        JOB_CLEANUP_VAULT,
        lambda: cleanup_vault_expired(
            hot_store=build_hot_store(),
            delete_expired=_env_flag("AGENT_OBS_CRON_VAULT_DELETE_EXPIRED"),
            dry_run=_env_flag("AGENT_OBS_CRON_DRY_RUN"),
            max_events=_env_int("AGENT_OBS_CRON_VAULT_MAX_EVENTS", 10000),
        ),
    )


def _audit_job() -> Awaitable[Any]:
    return _run_sync(
        JOB_CLEANUP_AUDIT_EVENTS,
        lambda: archive_expired_audit_events(
            hot_store=build_hot_store(),
            cold_store=build_cold_store(),
            retention_days=_env_int(
                "AGENT_OBS_CRON_AUDIT_RETENTION_DAYS", DEFAULT_AUDIT_RETENTION_DAYS
            ),
            batch_size=_env_int("AGENT_OBS_CRON_BATCH_SIZE", DEFAULT_BATCH_SIZE),
            dry_run=_env_flag("AGENT_OBS_CRON_DRY_RUN"),
        ),
    )


def _eval_job() -> Awaitable[Any]:
    """Migrate eval results to warm; the pool is closed even on failure."""
    warm = build_warm_store()

    async def _runner() -> Any:
        try:
            return await run_cron_job_async(
                JOB_CLEANUP_EVAL_RESULTS,
                migrate_eval_results_to_warm,
                hot_store=build_hot_store(),
                warm_store=warm,
                retention_days=_env_int(
                    "AGENT_OBS_CRON_EVAL_RETENTION_DAYS", DEFAULT_EVAL_RETENTION_DAYS
                ),
                batch_size=_env_int("AGENT_OBS_CRON_BATCH_SIZE", DEFAULT_BATCH_SIZE),
                dry_run=_env_flag("AGENT_OBS_CRON_DRY_RUN"),
            )
        finally:
            await warm.close()

    return _runner()


def _migration_job() -> Awaitable[Any]:
    """Migrate spans older than 14d to warm tier; pool closed even on failure."""
    warm = build_warm_store()

    async def _runner() -> Any:
        try:
            return await run_cron_job_async(
                JOB_MIGRATE_SPANS,
                migrate_spans_to_warm,
                hot_store=build_hot_store(),
                warm_store=warm,
                retention_days=_env_int(
                    "AGENT_OBS_CRON_MIGRATION_RETENTION_DAYS", DEFAULT_SPAN_RETENTION_DAYS
                ),
                batch_size=_env_int("AGENT_OBS_CRON_BATCH_SIZE", DEFAULT_BATCH_SIZE),
                dry_run=_env_flag("AGENT_OBS_CRON_DRY_RUN"),
            )
        finally:
            await warm.close()

    return _runner()


def _cold_migration_job() -> Awaitable[Any]:
    """Migrate traces older than 90d to cold tier; pool closed even on failure."""
    warm = build_warm_store()
    cold = build_cold_trace_store()

    async def _runner() -> Any:
        try:
            return await run_cron_job_async(
                JOB_MIGRATE_TRACES,
                migrate_traces_to_cold,
                warm_store=warm,
                cold_store=cold,
                retention_days=_env_int(
                    "AGENT_OBS_CRON_COLD_RETENTION_DAYS", DEFAULT_SPAN_RETENTION_DAYS
                ),
                max_partition_rows=_env_int("AGENT_OBS_CRON_MAX_PARTITION_ROWS", 200_000),
                dry_run=_env_flag("AGENT_OBS_CRON_DRY_RUN"),
            )
        finally:
            await warm.close()
            await cold.close()

    return _runner()


def _drift_job() -> Awaitable[Any]:
    """Run drift detection analysis on LLM response embeddings.

    The KL threshold is *not* passed here: leaving it unset lets DriftDetector
    read the per-agent value that scripts/cron/calibrate_drift_threshold.py
    wrote to configs/drift_thresholds.yaml (PC26). An explicit env override is
    still honoured for operators who need to pin a value.
    """
    from agent_obs.drift import DriftDetector
    from agent_obs.storage.hot import HotStore

    async def _runner() -> Any:
        pinned = os.environ.get("AGENT_OBS_CRON_DRIFT_KL_THRESHOLD")
        detector = DriftDetector(
            sdk=None,  # Drift detection reads Hot directly; the SDK is not used
            hotstore=build_hot_store(),
            baseline_hours=_env_int("AGENT_OBS_CRON_DRIFT_BASELINE_HOURS", 168),  # 7 days
            last_window_hours=_env_int("AGENT_OBS_CRON_DRIFT_WINDOW_HOURS", 1),    # 1 hour
            kl_threshold=_env_float("AGENT_OBS_CRON_DRIFT_KL_THRESHOLD", 0.0) if pinned else None,
            thresholds_path=_env_str(
                "AGENT_OBS_CRON_DRIFT_THRESHOLDS_PATH", DEFAULT_THRESHOLDS_PATH
            ),
        )

        agents = _env_list("AGENT_OBS_CRON_DRIFT_AGENTS", ["default"])

        results = []
        for agent_id in agents:
            try:
                result = await detector.run_once(agent_id)
                results.append(result)
            except Exception as e:
                logger.error(f"Drift detection failed for agent {agent_id}: {e}")

        return results

    return _runner()


def _drift_calibration_job() -> Awaitable[Any]:
    """PC26: recalibrate the drift threshold as p99 of the 30d KL history.

    Monthly, and deliberately not chained to _drift_job: calibration writes
    configs/drift_thresholds.yaml, which the next 15-minute detection tick
    picks up on its own. Running them in the same process would also mean a
    calibration failure took drift detection down with it.
    """
    from agent_obs.drift.threshold import (
        DEFAULT_HISTORY_WINDOW_DAYS,
        DEFAULT_THRESHOLDS_PATH,
    )

    # scheduler.py runs with scripts/cron on sys.path, so the sibling
    # calibration module is not importable as a package by default.
    if REPO_ROOT not in sys.path:
        sys.path.insert(0, REPO_ROOT)
    from scripts.cron.calibrate_drift_threshold import calibrate_all

    async def _runner() -> Any:
        hotstore = build_hot_store()
        try:
            agents = _env_list("AGENT_OBS_CRON_DRIFT_AGENTS", []) or None
            return await calibrate_all(
                hotstore,
                agents,
                thresholds_path=_env_str(
                    "AGENT_OBS_CRON_DRIFT_THRESHOLDS_PATH", DEFAULT_THRESHOLDS_PATH
                ),
                window_days=_env_int(
                    "AGENT_OBS_CRON_DRIFT_CALIBRATION_WINDOW_DAYS",
                    DEFAULT_HISTORY_WINDOW_DAYS,
                ),
                percentile=_env_float("AGENT_OBS_CRON_DRIFT_PERCENTILE", 99.0),
                dry_run=_env_flag("AGENT_OBS_CRON_DRY_RUN"),
            )
        finally:
            hotstore.close()

    return _runner()


def _phoenix_export_job() -> Awaitable[Any]:
    """PC28: export embeddings from ClickHouse to Phoenix UMAP visualizer.

    Runs every 5 minutes, pulls embeddings from the last hour, and pushes
    them to Phoenix for UMAP visualization and drift root-cause analysis.
    """
    # scheduler.py runs with scripts/cron on sys.path, so the sibling
    # export module is not importable as a package by default.
    if REPO_ROOT not in sys.path:
        sys.path.insert(0, REPO_ROOT)
    from scripts.cron.export_embeddings_to_phoenix import run_phoenix_export

    async def _runner() -> Any:
        return await run_phoenix_export()

    return _runner()


def build_jobs() -> list[CronJob]:
    """Return the configured job table.

    ``interval_seconds`` is the cadence used to derive the alert threshold: the
    "> 2 missed cycles" rule in ``infra/prometheus-rules.yml`` uses 2h for the
    hourly job and 48h for the daily ones.
    """
    return [
        CronJob(
            JOB_CLEANUP_VAULT,
            os.environ.get("AGENT_OBS_CRON_VAULT_SPEC", "0 * * * *"),
            _vault_job,
            3600,
        ),
        CronJob(
            JOB_CLEANUP_AUDIT_EVENTS,
            os.environ.get("AGENT_OBS_CRON_AUDIT_SPEC", DAILY_AUDIT_SPEC),
            _audit_job,
            86400,
        ),
        CronJob(
            JOB_CLEANUP_EVAL_RESULTS,
            os.environ.get("AGENT_OBS_CRON_EVAL_SPEC", DAILY_EVAL_SPEC),
            _eval_job,
            86400,
        ),
        CronJob(
            JOB_DRIFT_DETECTION,
            os.environ.get("AGENT_OBS_CRON_DRIFT_SPEC", "*/15 * * * *"),  # Every 15 minutes
            _drift_job,
            900,  # 15 minutes
        ),
        CronJob(
            JOB_CALIBRATE_DRIFT_THRESHOLD,
            os.environ.get("AGENT_OBS_CRON_DRIFT_CALIBRATION_SPEC", MONTHLY_CALIBRATION_SPEC),
            _drift_calibration_job,
            2_592_000,  # 30 days
        ),
        CronJob(
            JOB_MIGRATE_SPANS,
            os.environ.get("AGENT_OBS_CRON_MIGRATION_SPEC", DAILY_MIGRATION_SPEC),
            _migration_job,
            86400,
        ),
        CronJob(
            JOB_MIGRATE_TRACES,
            os.environ.get("AGENT_OBS_CRON_COLD_SPEC", WEEKLY_COLD_SPEC),
            _cold_migration_job,
            604800,  # 7 days * 24 hours * 3600 seconds
        ),
        CronJob(
            JOB_EXPORT_EMBEDDINGS,
            os.environ.get("AGENT_OBS_CRON_PHOENIX_SPEC", "*/5 * * * *"),  # Every 5 minutes
            _phoenix_export_job,
            300,  # 5 minutes
        ),
    ]


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


async def run_once(jobs: list[CronJob]) -> None:
    """Run every job exactly once. Used by ``--run-once`` and on boot."""
    for job in jobs:
        result = await job.invoke()
        logger.info("run-once %s -> %s", job.name, result)


async def run_job(job: CronJob) -> None:
    """Execute one scheduled job."""
    await job.invoke()


async def serve(jobs: list[CronJob], port: int, run_on_boot: bool = True) -> None:
    """Start the metrics endpoint and run the schedule until cancelled."""
    from prometheus_client import start_http_server

    start_http_server(port, addr="0.0.0.0")
    logger.info("cron metrics endpoint listening on :%d/metrics", port)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    if run_on_boot:
        await run_once(jobs)

    pending = {job.name: job for job in jobs}
    while not stop.is_set():
        now = datetime.now(timezone.utc)
        upcoming = sorted(
            ((job.next_after(now), job) for job in pending.values()),
            key=lambda pair: pair[0],
        )
        target, next_job = upcoming[0]
        sleep_for = max(0.0, (target - now).total_seconds())
        logger.info("next job: %s at %s (in %.0fs)", next_job.name, target.isoformat(), sleep_for)

        try:
            await asyncio.wait_for(stop.wait(), timeout=sleep_for)
            break  # stop was set
        except asyncio.TimeoutError:
            await run_job(next_job)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the PC21 maintenance job schedule.")
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("AGENT_OBS_CRON_PORT", DEFAULT_PORT)),
        help="Port for the Prometheus metrics endpoint",
    )
    parser.add_argument(
        "--run-once",
        action="store_true",
        help="Run every job once and exit (skips the metrics endpoint)",
    )
    parser.add_argument(
        "--no-run-on-boot",
        action="store_true",
        help="Wait for the first scheduled tick instead of running immediately",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    jobs = build_jobs()
    for job in jobs:
        logger.info("scheduled job %s with spec %r (%ds cadence)", job.name, job.spec, job.interval_seconds)

    if args.run_once:
        asyncio.run(run_once(jobs))
        return 0

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(serve(jobs, args.port, run_on_boot=not args.no_run_on_boot))
    return 0


if __name__ == "__main__":
    sys.exit(main())
