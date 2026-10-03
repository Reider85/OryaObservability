"""Daily aggregation of compliance catalog (PC34)."""

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from prometheus_client import Counter, Gauge, Histogram

from agent_obs.compliance.catalog import ComplianceRow
from agent_obs.storage.maintenance import run_cron_job_async
from agent_obs.storage.warm import WarmStore


@dataclass
class ComplianceAggregationResult:
    """Result of compliance catalog aggregation."""
    affected: int  # total rows processed
    errors: int   # rows cleaned (stale)
    new_rows_24h: int  # for alerting


# PC34 Metrics
compliance_catalog_rows_total = Gauge(
    "agent_obs_compliance_catalog_rows_total",
    "Total rows in compliance catalog",
)

compliance_catalog_aggregation_duration_seconds = Histogram(
    "agent_obs_compliance_catalog_aggregation_duration_seconds",
    "Time taken to aggregate compliance catalog",
    buckets=[1, 5, 10, 30, 60, 120],
)

compliance_catalog_rows_cleaned_total = Counter(
    "agent_obs_compliance_catalog_rows_cleaned_total",
    "Total rows cleaned from compliance catalog (stale entries)",
)

compliance_catalog_new_rows_24h = Gauge(
    "agent_obs_compliance_catalog_new_rows_24h",
    "New rows added to compliance catalog in last 24 hours",
)


async def aggregate_compliance_catalog(
    warm_store: WarmStore,
    hot_store: Any | None = None,
    retention_days: int = 90,
    dry_run: bool = False,
) -> ComplianceAggregationResult:
    """
    Daily aggregation of compliance catalog.
    
    - Updates last_seen from hot tier (PC34: real UPDATE, not no-op)
    - Cleans stale records older than retention_days without activity
    - Refreshes compliance views
    - Returns metrics for alerting
    """
    start_time = time.time()
    
    try:
        # 1. SELECT all compliance catalog rows from warm tier
        rows = await warm_store.get_all_compliance_catalog()
        compliance_catalog_rows_total.set(len(rows))
        
        # 2. UPDATE last_seen from hot tier (PC34: real UPDATE, not no-op)
        last_seen_updated_count = 0
        if hot_store is not None and not dry_run:
            try:
                # Get all hot tier rows for last_seen sync
                hot_rows = await hot_store.get_compliance_catalog_all()
                if hot_rows:
                    # Update warm catalog with GREATEST(last_seen) from hot tier
                    last_seen_updated_count = await warm_store.update_compliance_last_seen_from_hot(hot_rows)
                    logger.info("Updated %d compliance catalog last_seen entries from hot tier", last_seen_updated_count)
            except Exception as e:
                logger.warning("Failed to sync last_seen from hot tier: %s", str(e))
        
        # 3. Calculate stale rows (last_seen < now() - retention_days)
        cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
        stale_rows = [r for r in rows if r["last_seen"] < cutoff]
        
        # 4. Calculate new rows in last 24 hours for alerting
        recent_cutoff = datetime.now(timezone.utc) - timedelta(days=1)
        new_rows_24h = len([r for r in rows if r["last_seen"] >= recent_cutoff])
        compliance_catalog_new_rows_24h.set(new_rows_24h)
        
        # 5. Clean stale rows (if not dry run)
        cleaned_count = 0
        if not dry_run and stale_rows:
            await warm_store.delete_compliance_catalog_stale(stale_rows)
            cleaned_count = len(stale_rows)
            compliance_catalog_rows_cleaned_total.inc(cleaned_count)
        
        # 6. Refresh views (if not dry run)
        if not dry_run:
            await warm_store.refresh_compliance_views()
        
        duration = time.time() - start_time
        compliance_catalog_aggregation_duration_seconds.observe(duration)
        
        return ComplianceAggregationResult(
            affected=len(rows),
            errors=cleaned_count,
            new_rows_24h=new_rows_24h,
        )
    finally:
        await warm_store.close()


# Job name constant for scheduler
JOB_AGGREGATE_COMPLIANCE_CATALOG = "aggregate_compliance_catalog"


async def _compliance_aggregation_job() -> ComplianceAggregationResult:
    """Cron job wrapper for compliance catalog aggregation."""
    warm_store = WarmStore()
    hot_store = None
    
    try:
        # Try to initialize hot store for last_seen sync
        try:
            from agent_obs.storage.hot import HotStore
            hot_store = HotStore()
            # Test connection
            if hot_store.is_available():
                logger.info("HotStore available for compliance catalog last_seen sync")
            else:
                hot_store = None
                logger.warning("HotStore not available, skipping last_seen sync")
        except Exception as e:
            logger.warning("Failed to initialize HotStore: %s", str(e))
            hot_store = None
        
        return await aggregate_compliance_catalog(
            warm_store=warm_store,
            hot_store=hot_store
        )
    finally:
        await warm_store.close()
        if hot_store is not None:
            await hot_store.close()


# Register the job in scheduler (will be imported by scheduler.py)
def register_compliance_aggregation_job():
    """Register compliance catalog aggregation job with scheduler."""
    from agent_obs.storage.maintenance import CronJob
    
    return CronJob(
        JOB_AGGREGATE_COMPLIANCE_CATALOG,
        "0 2 * * *",  # 02:00 UTC daily
        _compliance_aggregation_job,
        86400,  # 24 hours in seconds
    )