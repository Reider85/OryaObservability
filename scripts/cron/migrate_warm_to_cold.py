"""PC23 — weekly migration of traces past their retention window to cold S3 Parquet.

Traces older than 90 days are aggregated by (tenant, agent, day) and written
as Parquet files to the cold-traces bucket with partitioning. Runs out of process;
the logic lives in ``agent_obs.storage.maintenance.migrate_traces_to_cold``.

Run manually::

    python scripts/cron/migrate_warm_to_cold.py --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

from agent_obs.storage.maintenance import (
    DEFAULT_SPAN_RETENTION_DAYS,
    JOB_MIGRATE_TRACES,
    TraceColdResult,
    build_cold_trace_store,
    build_warm_store,
    migrate_traces_to_cold,
    run_cron_job_async,
)

logging.basicConfig(
    level=os.environ.get("AGENT_OBS_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("agent_obs.cron.migrate_warm_to_cold")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Migrate traces past the retention window to cold Parquet, then purge warm."
    )
    parser.add_argument(
        "--retention-days",
        type=int,
        default=int(
            os.environ.get("AGENT_OBS_CRON_COLD_RETENTION_DAYS", DEFAULT_SPAN_RETENTION_DAYS)
        ),
        help="Warm-tier retention for traces in days (default 90)",
    )
    parser.add_argument(
        "--max-partition-rows",
        type=int,
        default=int(os.environ.get("AGENT_OBS_CRON_MAX_PARTITION_ROWS", 200_000)),
        help="Maximum rows per partition (default 200,000)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Report only, write nothing")
    return parser.parse_args(argv)


async def run(args: argparse.Namespace):
    warm = build_warm_store()
    cold = build_cold_trace_store()
    try:
        return await run_cron_job_async(
            JOB_MIGRATE_TRACES,
            migrate_traces_to_cold,
            warm_store=warm,
            cold_store=cold,
            retention_days=args.retention_days,
            max_partition_rows=args.max_partition_rows,
            dry_run=args.dry_run,
        )
    finally:
        await warm.close()
        await cold.close()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    result = asyncio.run(run(args))
    if result is None:
        logger.error("migrate_warm_to_cold failed")
        return 1
    print(
        f"migrate_warm_to_cold: partitions={result.partitions} files={result.files} "
        f"rows={result.rows} bytes={result.bytes_written} deleted={result.traces_deleted} "
        f"errors={result.errors}"
    )
    return 1 if result.errors else 0


if __name__ == "__main__":
    sys.exit(main())