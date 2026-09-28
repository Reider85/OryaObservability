"""PC21 — daily migration of eval results past their retention window to warm.

Rows older than 14 days move to Postgres warm in structure-only form (scores
and timestamps; the judge's ``reasoning`` is dropped). Runs out of process; the
logic lives in ``agent_obs.storage.maintenance.migrate_eval_results_to_warm``.

This is the only cron job that is a coroutine, because the warm tier is read
through asyncpg. The scheduler and this wrapper therefore use
``run_cron_job_async`` rather than ``run_cron_job``.

Run manually::

    python scripts/cron/cleanup_eval_results.py --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

from agent_obs.storage.maintenance import (
    DEFAULT_BATCH_SIZE,
    DEFAULT_EVAL_RETENTION_DAYS,
    JOB_CLEANUP_EVAL_RESULTS,
    build_hot_store,
    build_warm_store,
    migrate_eval_results_to_warm,
    run_cron_job_async,
)

logging.basicConfig(
    level=os.environ.get("AGENT_OBS_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("agent_obs.cron.cleanup_eval_results")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Migrate eval results past the retention window to Postgres warm, then purge hot."
    )
    parser.add_argument(
        "--retention-days",
        type=int,
        default=int(
            os.environ.get("AGENT_OBS_CRON_EVAL_RETENTION_DAYS", DEFAULT_EVAL_RETENTION_DAYS)
        ),
        help="Hot-tier retention for eval results in days (default 14)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=int(os.environ.get("AGENT_OBS_CRON_BATCH_SIZE", DEFAULT_BATCH_SIZE)),
        help="Rows per batch",
    )
    parser.add_argument("--dry-run", action="store_true", help="Report only, write nothing")
    return parser.parse_args(argv)


async def run(args: argparse.Namespace):
    warm = build_warm_store()
    try:
        return await run_cron_job_async(
            JOB_CLEANUP_EVAL_RESULTS,
            migrate_eval_results_to_warm,
            hot_store=build_hot_store(),
            warm_store=warm,
            retention_days=args.retention_days,
            batch_size=args.batch_size,
            dry_run=args.dry_run,
        )
    finally:
        await warm.close()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    result = asyncio.run(run(args))
    if result is None:
        logger.error("cleanup_eval_results failed")
        return 1
    print(
        f"cleanup_eval_results: scanned={result.scanned} migrated={result.migrated} "
        f"deleted={result.deleted} errors={result.errors}"
    )
    return 1 if result.errors else 0


if __name__ == "__main__":
    sys.exit(main())
