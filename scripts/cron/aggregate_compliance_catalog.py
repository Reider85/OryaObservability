#!/usr/bin/env python3
"""Daily compliance catalog aggregation (PC34)."""

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

# Add repo root to Python path for imports
REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from agent_obs.compliance.aggregate import ComplianceAggregationResult, aggregate_compliance_catalog
from agent_obs.storage.maintenance import build_warm_store

logger = logging.getLogger(__name__)


async def main():
    parser = argparse.ArgumentParser(description="Aggregate compliance catalog daily")
    parser.add_argument(
        "--dry-run", 
        action="store_true", 
        help="Don't modify database, just simulate"
    )
    parser.add_argument(
        "--retention-days", 
        type=int, 
        default=90,
        help="Stale retention period in days (default: 90)"
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

    logger.info("Starting compliance catalog aggregation")

    # Build warm store connection
    warm_store = build_warm_store()
    
    try:
        # Run aggregation
        result = await aggregate_compliance_catalog(
            warm_store=warm_store,
            retention_days=args.retention_days,
            dry_run=args.dry_run
        )

        # Report results
        logger.info(f"Processed {result.affected} rows total")
        logger.info(f"Cleaned {result.errors} stale rows")
        logger.info(f"New rows in last 24h: {result.new_rows_24h}")
        
        if args.dry_run:
            logger.info("DRY RUN: No database changes were made")
        
        # Exit with code 1 if errors occurred during cleaning
        if result.errors > 0:
            logger.warning(f"Cleaned {result.errors} stale rows")
            sys.exit(1)
            
    except Exception as e:
        logger.error(f"Aggregation failed: {str(e)}")
        sys.exit(1)
    finally:
        await warm_store.close()
        logger.info("Compliance catalog aggregation completed")


if __name__ == "__main__":
    asyncio.run(main())