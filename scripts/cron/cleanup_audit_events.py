"""PC21 — daily archival of audit events past their retention window to S3.

Rows older than 365 days (§3.4 ARCHITECT.md requires a minimum 1-year audit
retention) are written to the cold bucket as Snappy Parquet and only then
purged from ClickHouse. Runs out of process; the logic lives in
``agent_obs.storage.maintenance.archive_expired_audit_events``.

Run manually::

    python scripts/cron/cleanup_audit_events.py --dry-run
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

from agent_obs.storage.maintenance import (
    DEFAULT_AUDIT_RETENTION_DAYS,
    DEFAULT_BATCH_SIZE,
    JOB_CLEANUP_AUDIT_EVENTS,
    archive_expired_audit_events,
    build_cold_store,
    build_hot_store,
    run_cron_job,
)

logging.basicConfig(
    level=os.environ.get("AGENT_OBS_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("agent_obs.cron.cleanup_audit_events")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Archive audit events past the retention window to S3 Parquet, then purge hot."
    )
    parser.add_argument(
        "--retention-days",
        type=int,
        default=int(
            os.environ.get("AGENT_OBS_CRON_AUDIT_RETENTION_DAYS", DEFAULT_AUDIT_RETENTION_DAYS)
        ),
        help="Minimum audit retention in days before archival (default 365)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=int(os.environ.get("AGENT_OBS_CRON_BATCH_SIZE", DEFAULT_BATCH_SIZE)),
        help="Rows per batch",
    )
    parser.add_argument("--dry-run", action="store_true", help="Report only, write nothing")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    result = run_cron_job(
        JOB_CLEANUP_AUDIT_EVENTS,
        archive_expired_audit_events,
        hot_store=build_hot_store(),
        cold_store=build_cold_store(),
        retention_days=args.retention_days,
        batch_size=args.batch_size,
        dry_run=args.dry_run,
    )
    if result is None:
        logger.error("cleanup_audit_events failed")
        return 1
    print(
        f"cleanup_audit_events: scanned={result.scanned} archived={result.archived} "
        f"deleted={result.deleted} bytes={result.bytes_written} "
        f"objects={len(result.objects)} errors={result.errors}"
    )
    return 1 if result.errors else 0


if __name__ == "__main__":
    sys.exit(main())
