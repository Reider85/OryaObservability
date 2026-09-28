"""PostgreSQL warm-tier storage client for PC21.

Warm tier holds structure-only rows: evaluation scores and timestamps, without
the LLM judge's ``reasoning`` column (PC21 item 3). From PC22 the same table
``traces_warm`` also receives the compressed trace aggregates.

``asyncpg`` is imported lazily so that importing this module does not require
the warm-tier driver — see ``agent_obs/eval/llm_judge_worker.py`` for the same
lazy-import convention.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_DSN = "postgresql://warm:warm@localhost:5433/warm_store"

# Columns of eval_results_warm. `reasoning` is intentionally absent: the warm
# tier stores structure only (scores + timestamp), never judge prose.
EVAL_RESULTS_WARM_COLUMNS = (
    "trace_id",
    "eval_id",
    "eval_name",
    "eval_timestamp",
    "eval_latency_seconds",
    "scores",
    "judge_model",
    "flags",
)


class WarmStore:
    """Async client for the warm-tier Postgres database.

    Use as an async context manager so the pool is always released:

    .. code-block:: python

        async with WarmStore() as warm:
            await warm.insert_eval_results(rows)
    """

    def __init__(
        self,
        dsn: str | None = None,
        min_size: int = 1,
        max_size: int = 5,
        command_timeout: float = 30.0,
    ) -> None:
        self._dsn = dsn or os.environ.get("WARM_PG_DSN", DEFAULT_DSN)
        self._min_size = min_size
        self._max_size = max_size
        self._command_timeout = command_timeout
        self._pool: Any | None = None

    async def connect(self) -> Any:
        """Create the connection pool if it does not exist yet."""
        if self._pool is None:
            try:
                import asyncpg
            except ImportError:
                raise RuntimeError(
                    "asyncpg is required for WarmStore. "
                    "Install with: pip install 'agent-obs[cron]'"
                )

            self._pool = await asyncpg.create_pool(
                dsn=self._dsn,
                min_size=self._min_size,
                max_size=self._max_size,
                command_timeout=self._command_timeout,
            )
            logger.info("Warm Postgres pool created: dsn=%s", _redact_dsn(self._dsn))
        return self._pool

    async def ping(self) -> bool:
        """Return True if the warm Postgres database answers a trivial query."""
        try:
            pool = await self.connect()
            async with pool.acquire() as conn:
                await conn.execute("SELECT 1")
            return True
        except Exception as e:
            logger.warning("warm store health check failed: %s", str(e))
            return False

    async def insert_eval_results(self, rows: list[dict[str, Any]]) -> int:
        """Upsert evaluation results into ``eval_results_warm``.

        Idempotent by design: the primary key is
        ``(trace_id, eval_id, eval_name, eval_timestamp)`` and a conflict
        triggers an ``ON CONFLICT DO UPDATE``, so replaying the same day never
        duplicates rows.

        Any ``reasoning`` key present in ``rows`` is dropped — the warm table
        has no such column and the reduction is enforced at the schema level.

        Returns
        -------
        int
            Number of rows written.
        """
        if not rows:
            return 0

        pool = await self.connect()
        values: list[tuple] = []
        for row in rows:
            scores = row.get("scores")
            if not isinstance(scores, str):
                scores = json.dumps(scores or {})
            flags = row.get("flags") or []
            if not isinstance(flags, str):
                flags = json.dumps(flags)
            values.append(
                (
                    row.get("trace_id", ""),
                    row.get("eval_id", ""),
                    row.get("eval_name", ""),
                    row.get("eval_timestamp"),
                    row.get("eval_latency_seconds"),
                    scores,
                    row.get("judge_model", ""),
                    flags,
                )
            )

        columns = ", ".join(EVAL_RESULTS_WARM_COLUMNS)
        placeholders = ", ".join(f"${i + 1}" for i in range(len(EVAL_RESULTS_WARM_COLUMNS)))
        conflict = "trace_id, eval_id, eval_name, eval_timestamp"
        updates = ", ".join(
            f"{col} = EXCLUDED.{col}"
            for col in EVAL_RESULTS_WARM_COLUMNS
            if col not in ("trace_id", "eval_id", "eval_name", "eval_timestamp")
        )
        query = (
            f"INSERT INTO eval_results_warm ({columns}) VALUES ({placeholders}) "
            f"ON CONFLICT ({conflict}) DO UPDATE SET {updates}"
        )

        async with pool.acquire() as conn:
            await conn.executemany(query, values)

        logger.info("warm eval_results written: rows=%d", len(values))
        return len(values)

    async def count_eval_results(self) -> int:
        """Return the row count of ``eval_results_warm`` (used by tests/DoD)."""
        pool = await self.connect()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT count(*) AS n FROM eval_results_warm")
        return int(row["n"]) if row else 0

    async def close(self) -> None:
        """Close the connection pool."""
        if self._pool is not None:
            try:
                await self._pool.close()
            except Exception as e:
                logger.debug("warm pool close failed: %s", str(e))
            self._pool = None

    async def __aenter__(self) -> "WarmStore":
        await self.connect()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.close()


def _redact_dsn(dsn: str) -> str:
    """Strip the password from a Postgres DSN before it reaches the logs."""
    if "@" not in dsn or "//" not in dsn:
        return dsn
    scheme, rest = dsn.split("//", 1)
    credentials, host = rest.rsplit("@", 1)
    if ":" in credentials:
        user, _ = credentials.split(":", 1)
        credentials = f"{user}:***"
    return f"{scheme}//{credentials}@{host}"


__all__ = ["EVAL_RESULTS_WARM_COLUMNS", "WarmStore"]
