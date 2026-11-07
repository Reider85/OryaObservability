#!/usr/bin/env python3
"""
Cron script for scheduling weekly chaos engineering tests.
"""
import asyncio
import logging
import sys
from pathlib import Path

from scripts.chaos import WeeklyChaosEngine, ChaosScheduleConfig, create_target_scenarios


def setup_cron_logging():
    """Setup logging for cron execution."""
    log_file = Path("/var/log/chaos-engineering.log")
    
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler(log_file),
            logging.StreamHandler(sys.stdout)
        ]
    )


def main():
    """Main cron entry point."""
    setup_cron_logging()
    logger = logging.getLogger(__name__)
    
    try:
        logger.info("Starting scheduled chaos engineering session")
        
        # Load schedule configuration
        schedule_config = ChaosScheduleConfig.from_env()
        
        if not schedule_config.enabled:
            logger.info("Chaos engineering schedule is disabled")
            return
        
        # Get target scenarios
        all_scenarios = create_target_scenarios()
        target_scenarios = schedule_config.get_target_scenarios([s.name for s in all_scenarios])
        
        if not target_scenarios:
            logger.info("No scenarios to run based on schedule configuration")
            return
        
        logger.info(f"Running {len(target_scenarios)} chaos scenarios")
        
        # Initialize chaos engine
        output_dir = Path(schedule_config.output_dir)
        chaos_engine = WeeklyChaosEngine(output_dir)
        
        # Run chaos session
        asyncio.run(
            chaos_engine.run_chaos_session(
                random_order=schedule_config.random_order
            )
        )
        
        logger.info("Scheduled chaos engineering session completed successfully")
        
    except Exception as e:
        logger.error(f"Scheduled chaos engineering session failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()