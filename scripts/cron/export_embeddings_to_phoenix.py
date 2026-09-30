#!/usr/bin/env python3
"""PC28 — Cron script for exporting embeddings to Phoenix UMAP visualizer.

Runs every 5 minutes, pulls embeddings from ClickHouse (last hour),
and pushes them to Arize Phoenix for UMAP visualization and drift
root-cause analysis.
"""

import asyncio
import json
import logging
import os
import sys
import time
from typing import Any

# Add project root to Python path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from agent_obs.storage.hot import HotStore

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# Configuration from environment variables
PHOENIX_HOST = os.environ.get("PHOENIX_HOST", "localhost")
PHOENIX_PORT = int(os.environ.get("PHOENIX_PORT", "6006"))
PHOENIX_BASE_URL = f"http://{PHOENIX_HOST}:{PHOENIX_PORT}"
EXPORT_INTERVAL_SECONDS = int(os.environ.get("PHOENIX_EXPORT_INTERVAL", "300"))  # 5 minutes
BATCH_SIZE = int(os.environ.get("PHOENIX_EXPORT_BATCH_SIZE", "1000"))

# Prometheus metrics (will be exposed by scheduler.py)
EXPORT_RUNS_TOTAL = 0
EXPORT_ROWS_TOTAL = 0
EXPORT_ERRORS_TOTAL = 0
EXPORT_DURATION_SECONDS = 0.0


def _get_clickhouse_client() -> HotStore:
    """Create a HotStore client for ClickHouse queries."""
    return HotStore(
        host=os.environ.get("CLICKHOUSE_HOST", "localhost"),
        port=int(os.environ.get("CLICKHOUSE_PORT", "9000")),
        database=os.environ.get("CLICKHOUSE_DB", "observability"),
        user=os.environ.get("CLICKHOUSE_USER", "observability_user"),
        password=os.environ.get("CLICKHOUSE_PASSWORD", "observability_password"),
    )


async def fetch_embeddings_from_clickhouse(
    hotstore: HotStore, limit: int = BATCH_SIZE
) -> list[dict]:
    """Fetch embeddings from ClickHouse spans_hot table.

    Returns list of dicts with trace_id, agent_id, response_embedding.

    1C: spans_hot has no response_embedding column, so the query fails and
    this returns []. Phoenix stays empty until embeddings return (1A/1B).
    """
    try:
        client = hotstore._get_client()
        rows = client.execute(
            """
            SELECT trace_id, agent_id, response_embedding
            FROM spans_hot
            WHERE response_embedding IS NOT NULL
              AND start_time > now() - INTERVAL 1 HOUR
            ORDER BY start_time DESC
            LIMIT %(limit)s
            """,
            {"limit": limit},
        )

        embeddings = []
        for row in rows:
            trace_id, agent_id, embedding = row
            if embedding is not None:
                embeddings.append(
                    {
                        "trace_id": trace_id,
                        "agent_id": agent_id or "unknown",
                        "embedding": embedding,
                    }
                )

        logger.info("Fetched %d embeddings from ClickHouse", len(embeddings))
        return embeddings

    except Exception as e:
        logger.error("Failed to fetch embeddings from ClickHouse: %s", e)
        return []


async def push_embeddings_to_phoenix(
    embeddings: list[dict], dataset_name: str = "embeddings"
) -> bool:
    """Push embeddings to Phoenix via HTTP API.

    Uses the Phoenix /v1/datasets/{dataset_id}/embeddings endpoint.
    """
    if not embeddings:
        return True

    try:
        import httpx

        # Prepare embedding data for Phoenix
        embedding_records = []
        for emb in embeddings:
            embedding_records.append(
                {
                    "id": emb["trace_id"],
                    "embedding": emb["embedding"],
                    "metadata": {
                        "trace_id": emb["trace_id"],
                        "agent_id": emb["agent_id"],
                    },
                }
            )

        # Phoenix API endpoint for embedding ingestion
        url = f"{PHOENIX_BASE_URL}/v1/datasets/{dataset_name}/embeddings"

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                url,
                json={"embeddings": embedding_records},
                headers={"Content-Type": "application/json"},
            )

            if response.status_code in (200, 201, 204):
                logger.info(
                    "Successfully pushed %d embeddings to Phoenix", len(embeddings)
                )
                return True
            else:
                logger.error(
                    "Phoenix API returned %d: %s",
                    response.status_code,
                    response.text[:200],
                )
                return False

    except ImportError:
        logger.error("httpx is required for Phoenix export. Install with: pip install httpx")
        return False
    except Exception as e:
        logger.error("Failed to push embeddings to Phoenix: %s", e)
        return False


async def run_phoenix_export() -> dict:
    """Execute one embedding export cycle to Phoenix.

    Returns dict with metrics for Prometheus.
    """
    global EXPORT_RUNS_TOTAL, EXPORT_ROWS_TOTAL, EXPORT_ERRORS_TOTAL, EXPORT_DURATION_SECONDS

    start_time = time.time()
    metrics = {
        "runs_total": 0,
        "rows_total": 0,
        "errors_total": 0,
        "duration_seconds": 0.0,
    }

    logger.info("Starting Phoenix embedding export run")

    try:
        # Initialize ClickHouse client
        hotstore = _get_clickhouse_client()

        # Fetch embeddings from ClickHouse
        embeddings = await fetch_embeddings_from_clickhouse(hotstore, BATCH_SIZE)

        if not embeddings:
            logger.info("No embeddings to export")
            metrics["duration_seconds"] = time.time() - start_time
            EXPORT_RUNS_TOTAL += 1
            EXPORT_DURATION_SECONDS += metrics["duration_seconds"]
            return metrics

        # Push to Phoenix
        success = await push_embeddings_to_phoenix(embeddings, dataset_name="embeddings")

        if success:
            metrics["rows_total"] = len(embeddings)
            EXPORT_ROWS_TOTAL += len(embeddings)
        else:
            metrics["errors_total"] = 1
            EXPORT_ERRORS_TOTAL += 1

        # Cleanup
        hotstore.close()

    except Exception as e:
        logger.error("Phoenix export run failed: %s", e)
        metrics["errors_total"] = 1
        EXPORT_ERRORS_TOTAL += 1

    metrics["duration_seconds"] = time.time() - start_time
    EXPORT_RUNS_TOTAL += 1
    EXPORT_DURATION_SECONDS += metrics["duration_seconds"]

    logger.info(
        "Phoenix export completed: rows=%d, errors=%d, duration=%.2fs",
        metrics["rows_total"],
        metrics["errors_total"],
        metrics["duration_seconds"],
    )

    return metrics


async def main():
    """Main cron loop for Phoenix embedding export."""
    logger.info(
        "Phoenix embedding export cron started (interval: %ds)", EXPORT_INTERVAL_SECONDS
    )

    while True:
        try:
            await run_phoenix_export()

            # Sleep until next interval
            logger.info("Next Phoenix export in %d seconds", EXPORT_INTERVAL_SECONDS)
            await asyncio.sleep(EXPORT_INTERVAL_SECONDS)

        except KeyboardInterrupt:
            logger.info("Phoenix export cron stopped by user")
            break
        except Exception as e:
            logger.error("Error in Phoenix export cron: %s", e)
            # Sleep for a shorter interval on error
            await asyncio.sleep(30)


if __name__ == "__main__":
    asyncio.run(main())
