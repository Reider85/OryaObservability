#!/usr/bin/env python3
"""PC34 — weekly migration of compliance catalog from hot to warm tier.

Moves compliance catalog rows from ClickHouse hot tier to Postgres warm tier,
then deletes old hot rows to enforce 14-day retention. Follows PC22 pattern:
ship-first-delete-second.
"""

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

# Add repo root to Python path for imports
REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from agent_obs.compliance.migrate_hot_to_warm import ComplianceHotToWarmResult, migrate_compliance_catalog_hot_to_warm
from agent_obs.storage.hot import HotStore
from agent_obs.storage.maintenance import build_warm_store

logger = logging.getLogger(__name__)


async def main():
    parser = argparse.ArgumentParser(description="Migrate compliance catalog from hot to warm tier")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Don't modify database, just simulate migration"
    )
    parser.add_argument(
        "--hot-retention-days",
        type=int,
        default=14,
        help="Hot tier retention period in days (default: 14)"
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=1000,
        help="Batch size for migration (default: 1000)"
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable verbose logging"
    )
    args = parser.parse_args()

    if args.verbose:
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.INFO)

    logger.info("Starting compliance catalog hot to warm migration")

    # Build stores
    hot_store = HotStore()
    warm_store = build_warm_store()
    
    try:
        # Run migration
        result: ComplianceHotToWarmResult = await migrate_compliance_catalog_hot_to_warm(
            hot_store=hot_store,
            warm_store=warm_store,
            hot_retention_days=args.hot_retention_days,
            batch_size=args.batch_size,
            dry_run=args.dry_run
        )

        # Report results
        logger.info(f"Scanned {result.hot_rows_scanned} hot catalog rows")
        logger.info(f"Migrated {result.warm_rows_migrated} rows to warm tier")
        logger.info(f"Deleted {result.hot_rows_deleted} old hot rows")
        logger.info(f"Errors: {result.errors}")
        
        if args.dry_run:
            logger.info("DRY RUN: No database changes were made")
        
        # Exit with code 1 if errors occurred
        if result.errors > 0:
            logger.warning(f"Migration completed with {result.errors} errors")
            sys.exit(1)
            
    except Exception as e:
        logger.error(f"Migration failed: {str(e)}")
        sys.exit(1)
    finally:
        await hot_store.close()
        await warm_store.close()
        logger.info("Compliance catalog hot to warm migration completed")


if __name__ == "__main__":
    asyncio.run(main())