#!/usr/bin/env python3
"""Cron script for drift detection - runs every 15 minutes.

PC25: KL-divergence detector comparing last hour vs 7-day baseline.
"""

import asyncio
import logging
import os
import sys
import time
from typing import List

# Add project root to Python path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_obs.drift import DriftDetector
from agent_obs.observability import ObservabilitySDK
from agent_obs.storage.hot import HotStore

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
)
logger = logging.getLogger(__name__)

# Configuration from environment variables
DRIFT_INTERVAL_SECONDS = int(os.environ.get("DRIFT_INTERVAL_SECONDS", "900"))  # 15 minutes
DRIFT_BASELINE_HOURS = int(os.environ.get("DRIFT_BASELINE_HOURS", "168"))  # 7 days
DRIFT_LAST_WINDOW_HOURS = int(os.environ.get("DRIFT_LAST_WINDOW_HOURS", "1"))  # 1 hour
DRIFT_KL_THRESHOLD = float(os.environ.get("DRIFT_KL_THRESHOLD", "0.1"))

# Agent IDs to monitor (comma-separated)
AGENT_IDS = os.environ.get("DRIFT_AGENT_IDS", "default_agent").split(",")


async def run_drift_detection():
    """Execute drift detection for all configured agents."""
    logger.info("Starting drift detection run")
    
    # Initialize SDK and HotStore
    sdk = ObservabilitySDK(exporters=[])
    hotstore = HotStore(
        host=os.environ.get("CLICKHOUSE_HOST", "localhost"),
        port=int(os.environ.get("CLICKHOUSE_PORT", "9000")),
        database=os.environ.get("CLICKHOUSE_DB", "observability"),
        user=os.environ.get("CLICKHOUSE_USER", "observability_user"),
        password=os.environ.get("CLICKHOUSE_PASSWORD", "observability_password"),
    )
    
    # Create drift detector
    detector = DriftDetector(
        sdk=sdk,
        hotstore=hotstore,
        baseline_hours=DRIFT_BASELINE_HOURS,
        last_window_hours=DRIFT_LAST_WINDOW_HOURS,
        kl_threshold=DRIFT_KL_THRESHOLD,
    )
    
    results = []
    
    # Run drift detection for each agent
    for agent_id in AGENT_IDS:
        agent_id = agent_id.strip()
        if not agent_id:
            continue
            
        logger.info(f"Running drift detection for agent: {agent_id}")
        
        try:
            report = await detector.run_once(agent_id)
            results.append((agent_id, report))
            
            # Log summary
            if report.is_drift_detected:
                logger.warning(
                    f"⚠️  DRIFT DETECTED for {agent_id}: "
                    f"KL={report.kl_score:.4f} (threshold={report.threshold}) "
                    f"severity={report.severity}"
                )
            else:
                logger.info(
                    f"✅ No drift for {agent_id}: KL={report.kl_score:.4f} "
                    f"(threshold={report.threshold})"
                )
                
        except Exception as e:
            logger.error(f"❌ Drift detection failed for {agent_id}: {e}")
            results.append((agent_id, None))
    
    # Log summary
    successful = sum(1 for _, report in results if report is not None)
    total = len(results)
    logger.info(f"Drift detection completed: {successful}/{total} agents processed")
    
    # Cleanup
    hotstore.close()
    sdk.close()
    
    return results


async def main():
    """Main cron loop."""
    logger.info(f"Drift detection cron started (interval: {DRIFT_INTERVAL_SECONDS}s)")
    
    while True:
        try:
            start_time = time.time()
            
            # Run drift detection
            await run_drift_detection()
            
            # Calculate sleep time to maintain consistent interval
            elapsed = time.time() - start_time
            sleep_time = max(0, DRIFT_INTERVAL_SECONDS - elapsed)
            
            logger.info(f"Next drift detection run in {sleep_time:.1f} seconds")
            await asyncio.sleep(sleep_time)
            
        except KeyboardInterrupt:
            logger.info("Drift detection cron stopped by user")
            break
        except Exception as e:
            logger.error(f"Error in drift detection cron: {e}")
            # Sleep for a shorter interval on error to avoid rapid retry loops
            await asyncio.sleep(30)


if __name__ == "__main__":
    asyncio.run(main())