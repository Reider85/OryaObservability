"""PC34 — weekly migration of compliance catalog from hot to warm tier.

Moves compliance catalog rows from ClickHouse hot tier to Postgres warm tier,
then deletes old hot rows to enforce 14-day retention. Follows PC22 pattern:
ship-first-delete-second.
"""

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from prometheus_client import Counter, Histogram

from agent_obs.storage.hot import HotStore
from agent_obs.storage.maintenance import run_cron_job_async
from agent_obs.storage.warm import WarmStore

logger = logging.getLogger(__name__)

# PC34 Metrics
migration_compliance_hot_to_warm_duration_seconds = Histogram(
    "agent_obs_migration_compliance_hot_to_warm_duration_seconds",
    "Time taken to migrate compliance catalog hot to warm",
    buckets=[1, 5, 10, 30, 60, 120],
)

migration_compliance_hot_to_warm_rows_total = Counter(
    "agent_obs_migration_compliance_hot_to_warm_rows_total",
    "Total compliance catalog rows migrated from hot to warm",
)

migration_compliance_hot_to_warm_errors_total = Counter(
    "agent_obs_migration_compliance_hot_to_warm_errors_total",
    "Total errors during compliance catalog hot to warm migration",
)


@dataclass
class ComplianceHotToWarmResult:
    """Result of compliance catalog hot to warm migration."""
    hot_rows_scanned: int
    warm_rows_migrated: int
    hot_rows_deleted: int
    errors: int


async def migrate_compliance_catalog_hot_to_warm(
    hot_store: HotStore,
    warm_store: WarmStore,
    hot_retention_days: int = 14,
    batch_size: int = 1000,
    max_batches: int = 100,
    dry_run: bool = False,
) -> ComplianceHotToWarmResult:
    """
    Move compliance catalog rows from hot to warm tier, then purge hot.
    
    PC34: Each batch of hot catalog rows is UPSERTed into warm tier with
    frequency GREATEST, first_seen LEAST, last_seen GREATEST.
    
    Ordering rule: **ship first, delete second.** If the warm upsert fails,
    the hot rows are left untouched and retried on the next cycle.
    """
    started = time.monotonic()
    result = ComplianceHotToWarmResult(
        hot_rows_scanned=0,
        warm_rows_migrated=0,
        hot_rows_deleted=0,
        errors=0,
    )
    
    # Hot retention cutoff: keep rows with last_seen >= this date in hot tier
    hot_cutoff = datetime.now(timezone.utc) - timedelta(days=hot_retention_days)
    
    for _ in range(max_batches):
        # Get hot rows for migration (all rows, not just old ones)
        # Weekly migration ensures all hot data is synchronized to warm
        hot_rows = await hot_store.get_compliance_catalog_last_seen_cutoff(
            cutoff=hot_cutoff,  # Get all rows (retention boundary for pruning)
            limit=batch_size
        )
        
        if not hot_rows:
            break
            
        result.hot_rows_scanned += len(hot_rows)
        
        if dry_run:
            result.warm_rows_migrated += len(hot_rows)
            logger.info(
                "migrate_compliance_catalog_hot_to_warm: dry-run, would migrate %d rows from hot to warm",
                len(hot_rows),
            )
            break
        
        try:
            # Ship first: UPSERT hot rows to warm tier
            await warm_store.update_compliance_last_seen_from_hot(hot_rows)
            result.warm_rows_migrated += len(hot_rows)
            migration_compliance_hot_to_warm_rows_total.inc(len(hot_rows))
            
            logger.debug(
                "Migrated %d compliance catalog rows from hot to warm",
                len(hot_rows),
            )
            
        except Exception as e:
            logger.error(
                "migrate_compliance_catalog_hot_to_warm: warm upsert failed, keeping hot rows: %s",
                str(e),
            )
            result.errors += 1
            break
        
        # Delete rows from hot tier after successful warm upsert
        # Only delete rows older than retention cutoff
        deleted_count = await hot_store.delete_compliance_catalog_older_than(hot_cutoff)
        result.hot_rows_deleted += deleted_count
        
        if len(hot_rows) < batch_size:
            break
    
    duration = time.monotonic() - started
    migration_compliance_hot_to_warm_duration_seconds.observe(duration)
    
    if result.errors:
        migration_compliance_hot_to_warm_errors_total.inc(result.errors)
    
    logger.info(
        "migrate_compliance_catalog_hot_to_warm: "
        "hot_rows_scanned=%d warm_rows_migrated=%d hot_rows_deleted=%d errors=%d duration=%.2fs",
        result.hot_rows_scanned,
        result.warm_rows_migrated,
        result.hot_rows_deleted,
        result.errors,
        duration,
    )
    
    return result


# Job name constant for scheduler
JOB_MIGRATE_COMPLIANCE_CATALOG_HOT_TO_WARM = "migrate_compliance_catalog_hot_to_warm"


async def _compliance_hot_to_warm_job() -> ComplianceHotToWarmResult:
    """Cron job wrapper for compliance catalog hot to warm migration."""
    hot_store = HotStore()
    warm_store = WarmStore()
    
    try:
        return await migrate_compliance_catalog_hot_to_warm(
            hot_store=hot_store,
            warm_store=warm_store,
        )
    finally:
        await hot_store.close()
        await warm_store.close()


# Register the job in scheduler (will be imported by scheduler.py)
def register_compliance_hot_to_warm_migration_job():
    """Register compliance catalog hot to warm migration job with scheduler."""
    # CronJob is defined in scheduler.py, not maintenance.py to avoid circular imports
    # This function is called by scheduler.py when building the job list
    
    # Import here to avoid circular import at module level
    from scripts.cron.scheduler import CronJob
    
    return CronJob(
        JOB_MIGRATE_COMPLIANCE_CATALOG_HOT_TO_WARM,
        "0 5 * * 1",  # Monday 05:00 UTC (after Sunday cold migration at 04:00)
        _compliance_hot_to_warm_job,
        604800,  # 7 days in seconds (weekly)
    )