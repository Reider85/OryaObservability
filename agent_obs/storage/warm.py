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

# Columns of traces_warm (PC22).  Full-text fields (llm.input_text /
# llm.output_text) are intentionally absent — only char-count and sha256
# hashes survive the hot→warm compression.
TRACES_WARM_COLUMNS = (
    "trace_id",
    "tenant_id",
    "agent_id",
    "start_time",
    "end_time",
    "status",
    "cost_usd_total",
    "span_count",
    "error_count",
    "eval_avg",
    "input_chars",
    "input_sha256",
    "output_chars",
    "output_sha256",
    "user_hash",
)

# Columns that are part of the primary key (not updated on conflict).
_TRACES_WARM_PK = {"trace_id"}


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

    async def insert_traces(self, rows: list[dict[str, Any]]) -> int:
        """Upsert aggregated trace rows into ``traces_warm``.

        Idempotent by design: ``trace_id`` is the primary key and a conflict
        triggers an ``ON CONFLICT DO UPDATE``, so replaying the same day never
        duplicates rows.

        Each row must contain the keys matching ``TRACES_WARM_COLUMNS``.  The
        caller (``migrate_spans_to_warm``) performs the aggregation from raw
        spans and the compression (stripping ``llm.input_text`` /
        ``llm.output_text``).

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
            eval_avg = row.get("eval_avg")
            if not isinstance(eval_avg, str):
                eval_avg = json.dumps(eval_avg or {})
            values.append(
                (
                    row.get("trace_id", ""),
                    row.get("tenant_id", ""),
                    row.get("agent_id", ""),
                    row.get("start_time"),
                    row.get("end_time"),
                    row.get("status", ""),
                    row.get("cost_usd_total", 0.0),
                    row.get("span_count", 0),
                    row.get("error_count", 0),
                    eval_avg,
                    row.get("input_chars", 0),
                    row.get("input_sha256", ""),
                    row.get("output_chars", 0),
                    row.get("output_sha256", ""),
                )
            )

        columns = ", ".join(TRACES_WARM_COLUMNS)
        placeholders = ", ".join(f"${i + 1}" for i in range(len(TRACES_WARM_COLUMNS)))
        updates = ", ".join(
            f"{col} = EXCLUDED.{col}"
            for col in TRACES_WARM_COLUMNS
            if col not in _TRACES_WARM_PK
        )
        query = (
            f"INSERT INTO traces_warm ({columns}) VALUES ({placeholders}) "
            f"ON CONFLICT (trace_id) DO UPDATE SET {updates}"
        )

        async with pool.acquire() as conn:
            await conn.executemany(query, values)

        logger.info("warm traces written: rows=%d", len(values))
        return len(values)

    async def count_traces(self) -> int:
        """Return the row count of ``traces_warm`` (used by tests/DoD)."""
        pool = await self.connect()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT count(*) AS n FROM traces_warm")
        return int(row["n"]) if row else 0

    async def list_trace_partitions(
        self, cutoff: datetime, limit: int = 10_000
    ) -> list[tuple[str, datetime.date]]:
        """List distinct (tenant_id, day) partitions older than cutoff.

        Each partition is a candidate for cold migration. The day is derived
        from start_time in UTC (no time zones in the table).

        Returns
        -------
        list[tuple[str, datetime.date]]
            Pairs of (tenant_id, day) where day is a date object.
        """
        pool = await self.connect()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT DISTINCT tenant_id, (start_time AT TIME ZONE 'UTC')::date AS day
                FROM traces_warm
                WHERE start_time < $1
                ORDER BY 1, 2
                LIMIT $2
                """,
                cutoff,
                limit,
            )
        return [(row["tenant_id"], row["day"]) for row in rows]

    async def get_traces_for_partition(
        self, tenant_id: str, day: datetime.date, after_trace_id: str = "", limit: int = 10_000
    ) -> list[dict]:
        """Get traces for a specific (tenant_id, day) partition, with keyset pagination.

        Returns rows of TRACES_WARM_COLUMNS, with JSONB fields decoded to dicts
        and DECIMAL cost_usd_total converted to float.

        Parameters
        ----------
        tenant_id:
            Tenant to fetch.
        day:
            Day partition (derived from start_time in UTC).
        after_trace_id:
            Keyset cursor: fetch traces with trace_id > this value.
        limit:
            Maximum number of rows to return.

        Returns
        -------
        list[dict]
            List of trace rows, each with TRACES_WARM_COLUMNS keys.
        """
        pool = await self.connect()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT {columns}
                FROM traces_warm
                WHERE tenant_id = $1
                  AND (start_time AT TIME ZONE 'UTC')::date = $2
                  AND trace_id > $3
                ORDER BY trace_id
                LIMIT $4
                """.format(
                    columns=", ".join(TRACES_WARM_COLUMNS)
                ),
                tenant_id,
                day,
                after_trace_id,
                limit,
            )

        # Decode JSONB to dict and DECIMAL to float for consistency with the
        # insert_traces path and the Parquet schema expectations.
        result = []
        for row in rows:
            trace = dict(row)
            eval_avg = trace.get("eval_avg")
            if isinstance(eval_avg, str):
                import json
                trace["eval_avg"] = json.loads(eval_avg)
            cost_usd_total = trace.get("cost_usd_total")
            if cost_usd_total is not None:
                trace["cost_usd_total"] = float(cost_usd_total)
            result.append(trace)
        return result

    async def delete_traces(self, trace_ids: list[str]) -> int:
        """Delete traces by ID from traces_warm.

        Returns
        -------
        int
            Number of rows actually deleted.
        """
        if not trace_ids:
            return 0

        pool = await self.connect()
        async with pool.acquire() as conn:
            deleted = await conn.fetchval(
                "DELETE FROM traces_warm WHERE trace_id = ANY($1) RETURNING count(*)",
                trace_ids,
            )
        return int(deleted) if deleted else 0

    # PC34: Compliance catalog methods
    async def get_all_compliance_catalog(self) -> list:
        """Get all compliance catalog rows."""
        pool = await self.connect()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, agent_id, tool, field, pii_type, frequency, first_seen, last_seen "
                "FROM compliance_catalog ORDER BY last_seen DESC"
            )
        return rows

    async def delete_compliance_catalog_stale(self, stale_rows: list) -> int:
        """Delete stale compliance catalog rows (older than retention days)."""
        if not stale_rows:
            return 0
        
        row_ids = [r["id"] for r in stale_rows]
        
        pool = await self.connect()
        async with pool.acquire() as conn:
            deleted = await conn.fetchval(
                "DELETE FROM compliance_catalog WHERE id = ANY($1) RETURNING count(*)",
                row_ids,
            )
        return int(deleted) if deleted else 0

    async def refresh_compliance_views(self) -> None:
        """Refresh compliance catalog views (no-op for CREATE OR REPLACE views)."""
        # Views are CREATE OR REPLACE, so no explicit refresh needed
        # This method is kept for consistency with other refresh patterns
        pass

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


__all__ = ["EVAL_RESULTS_WARM_COLUMNS", "TRACES_WARM_COLUMNS", "WarmStore"]
