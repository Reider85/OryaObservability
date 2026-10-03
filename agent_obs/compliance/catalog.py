"""Compliance catalog — GDPR Data Map side-product of PII masking (PC33).

Every masked PII event is recorded as ``(agent_id, tool, field, pii_type)``
and batch-flushed to the Postgres ``compliance_catalog`` table (PC01 DDL).
Recording is free: it piggybacks on the guardrail masking path (Principle 22).
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional, Protocol

from prometheus_client import Counter, Gauge

logger = logging.getLogger(__name__)

DEFAULT_COMPLIANCE_DSN = "postgresql://warm:warm@localhost:5433/warm_store"

# Env config
ENV_COMPLIANCE_PG = "AGENT_OBS_COMPLIANCE_PG"  # on | off | auto (default auto)
ENV_COMPLIANCE_PG_DSN = "COMPLIANCE_PG_DSN"

# Known PII entity types — used to locate the entity segment inside
# redacted-field paths such as "user_message.email.5f3a" or
# "rows[0].col.email.abc1".
KNOWN_PII_TYPES = frozenset(
    {
        "email",
        "phone",
        "inn",
        "passport",
        "payment",
        "PERSON_NAME",
        "ADDRESS",
        "MEDICAL",
    }
)


# ---------------------------------------------------------------------------
# Prometheus metrics
# ---------------------------------------------------------------------------

compliance_catalog_writes_total = Counter(
    "agent_obs_compliance_catalog_writes_total",
    "PII masking events recorded into the compliance catalog buffer",
)

compliance_catalog_buffer_size = Gauge(
    "agent_obs_compliance_catalog_buffer_size",
    "Number of distinct catalog keys currently buffered awaiting flush",
)

compliance_catalog_flush_errors_total = Counter(
    "agent_obs_compliance_catalog_flush_errors_total",
    "Failed flushes of the compliance catalog buffer to Postgres",
)


# ---------------------------------------------------------------------------
# Data contracts
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ComplianceRow:
    """One aggregated catalog row ready for UPSERT."""

    agent_id: str
    tool: str
    field: str
    pii_type: str
    frequency: int


class CatalogWriter(Protocol):
    """Async sink that persists aggregated compliance rows."""

    async def upsert(self, rows: list[ComplianceRow]) -> None:
        """Insert or increment-frequency the given rows. Raises on failure."""
        ...

    async def close(self) -> None:
        """Release underlying resources. Idempotent."""
        ...


# ---------------------------------------------------------------------------
# Redacted-field parser
# ---------------------------------------------------------------------------


def parse_redacted_field(rf: str) -> tuple[str, str]:
    """Parse a redacted-field path into ``(field, pii_type)``.

    Handles all redacted-field formats produced by the guardrail:

    - plain:      ``"user_message.email.5f3a"``      → ``("user_message", "email")``
    - SQL rows:   ``"tool.rows[0].email.abc1"``      → ``("tool.rows[0]", "email")``
    - JSON path:  ``"tool.user.email.abc1"``         → ``("tool.user", "email")``

    The entity-type segment is located by matching against ``KNOWN_PII_TYPES``;
    the field is everything before it. Falls back to ``(first, second)`` split
    when no known type is found.
    """
    parts = rf.split(".")
    entity_idx: int | None = None
    for i, part in enumerate(parts):
        if part in KNOWN_PII_TYPES:
            entity_idx = i
            break

    if entity_idx is None:
        # Unknown shape — best-effort: first segment is field, second is type.
        if len(parts) >= 2:
            return parts[0], parts[1]
        return rf, ""

    if entity_idx == 0:
        # Path starts with the entity type — no field prefix.
        return "", parts[0]

    field = ".".join(parts[:entity_idx])
    return field, parts[entity_idx]


# ---------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------


class NullCatalogWriter:
    """No-op writer: buffer accumulates but nothing is persisted.

    Used when Postgres export is disabled (``AGENT_OBS_COMPLIANCE_PG=off``)
    or when asyncpg / DSN are unavailable in auto mode.
    """

    async def upsert(self, rows: list[ComplianceRow]) -> None:
        return None

    async def close(self) -> None:
        return None


class HotCatalogWriter:
    """Batch-UPSERT writer for the ``compliance_catalog_hot`` ClickHouse table.

    Writes to ClickHouse hot tier for tiered retention (PC34). Uses
    ReplacingMergeTree for deduplication by (agent_id, tool, field, pii_type).
    """

    def __init__(self) -> None:
        self._hot_store: Any | None = None

    async def _get_hot_store(self) -> Any:
        """Lazy initialization of HotStore."""
        if self._hot_store is None:
            from agent_obs.storage.hot import HotStore
            self._hot_store = HotStore()
        return self._hot_store

    async def upsert(self, rows: list[ComplianceRow]) -> None:
        """Insert or update compliance rows in ClickHouse hot tier."""
        if not rows:
            return
            
        hot_store = await self._get_hot_store()
        hot_rows = [
            {
                "agent_id": r.agent_id,
                "tool": r.tool,
                "field": r.field,
                "pii_type": r.pii_type,
                "frequency": r.frequency,
                "first_seen": datetime.now(timezone.utc),
                "last_seen": datetime.now(timezone.utc),
            }
            for r in rows
        ]
        await hot_store.upsert_compliance_catalog(hot_rows)

    async def close(self) -> None:
        """Close the HotStore connection."""
        if self._hot_store is not None:
            await self._hot_store.close()
            self._hot_store = None


class PostgresCatalogWriter:
    """Batch-UPSERT writer for the ``compliance_catalog`` table.

    Lazy-imports ``asyncpg`` (same convention as ``WarmStore``). Uses the
    unique key ``(agent_id, tool, field, pii_type)`` — see
    ``infra/storage/postgres_warm.sql`` (``uq_compliance_catalog_key``).
    """

    _UPSERT_SQL = """
        INSERT INTO compliance_catalog
            (agent_id, tool, field, pii_type, frequency, last_seen)
        VALUES ($1, $2, $3, $4, $5, now())
        ON CONFLICT (agent_id, tool, field, pii_type)
        DO UPDATE SET
            frequency = compliance_catalog.frequency + EXCLUDED.frequency,
            last_seen = now(),
            updated_at = now()
    """

    def __init__(self, dsn: str | None = None) -> None:
        self._dsn = dsn or os.environ.get(ENV_COMPLIANCE_PG_DSN, DEFAULT_COMPLIANCE_DSN)
        self._pool: Any | None = None

    async def _get_pool(self) -> Any:
        if self._pool is None:
            try:
                import asyncpg
            except ImportError as exc:
                raise RuntimeError(
                    "asyncpg is required for PostgresCatalogWriter. "
                    "Install with: pip install 'agent-obs[cron]'"
                ) from exc
            self._pool = await asyncpg.create_pool(
                dsn=self._dsn,
                min_size=1,
                max_size=5,
                command_timeout=30.0,
            )
            logger.info(
                "compliance catalog Postgres pool created: dsn=%s",
                _redact_dsn(self._dsn),
            )
        return self._pool

    async def upsert(self, rows: list[ComplianceRow]) -> None:
        if not rows:
            return
        pool = await self._get_pool()
        values = [
            (r.agent_id, r.tool, r.field, r.pii_type, r.frequency) for r in rows
        ]
        async with pool.acquire() as conn:
            await conn.executemany(self._UPSERT_SQL, values)

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None


def _redact_dsn(dsn: str) -> str:
    """Strip credentials from a DSN for safe logging."""
    if "@" not in dsn or "://" not in dsn:
        return dsn
    scheme, rest = dsn.split("://", 1)
    creds, host = rest.rsplit("@", 1)
    user = creds.split(":", 1)[0] if ":" in creds else creds
    return f"{scheme}://{user}:***@{host}"


# ---------------------------------------------------------------------------
# ComplianceCatalog
# ---------------------------------------------------------------------------


class ComplianceCatalog:
    """In-memory aggregation + periodic batch flush to a ``CatalogWriter``.

    ``record()`` is synchronous and O(1): it only increments an in-memory
    counter. Persistence happens on a background ``asyncio`` task every
    ``flush_interval`` seconds (default 60). On flush failure the buffer is
    preserved and retried on the next tick; ``flush_errors_total`` counts
    failures.
    """

    def __init__(
        self,
        writer: CatalogWriter,
        *,
        flush_interval: float = 60.0,
    ) -> None:
        self._writer = writer
        self._flush_interval = flush_interval
        self._buffer: dict[tuple[str, str, str, str], int] = {}
        self._task: asyncio.Task[None] | None = None
        self._shutdown = asyncio.Event()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def buffer_size(self) -> int:
        return len(self._buffer)

    def record(
        self,
        *,
        agent_id: str,
        tool: str,
        field: str,
        pii_type: str,
    ) -> None:
        """Record one masking event. Never blocks, never does I/O."""
        key = (agent_id, tool, field, pii_type)
        self._buffer[key] = self._buffer.get(key, 0) + 1
        compliance_catalog_writes_total.inc()
        compliance_catalog_buffer_size.set(len(self._buffer))
        self._ensure_worker()

    async def flush(self) -> None:
        """Flush the buffer to the writer. Safe to call manually."""
        if not self._buffer:
            return
        rows = [
            ComplianceRow(
                agent_id=agent_id,
                tool=tool,
                field=field,
                pii_type=pii_type,
                frequency=freq,
            )
            for (agent_id, tool, field, pii_type), freq in self._buffer.items()
        ]
        self._buffer.clear()
        compliance_catalog_buffer_size.set(len(self._buffer))
        try:
            await self._writer.upsert(rows)
        except Exception:
            # Restore buffer so events are not lost; next tick retries.
            for row in rows:
                key = (row.agent_id, row.tool, row.field, row.pii_type)
                self._buffer[key] = self._buffer.get(key, 0) + row.frequency
            compliance_catalog_flush_errors_total.inc()
            compliance_catalog_buffer_size.set(len(self._buffer))
            logger.warning(
                "compliance catalog flush failed (%d rows buffered again)",
                len(rows),
                exc_info=True,
            )

    def get_filtered_rows(
        self,
        agent_id: Optional[str] = None,
        tool: Optional[str] = None,
        pii_type: Optional[str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> list[ComplianceRow]:
        """Get compliance rows with optional filtering.
        
        Returns a list of ComplianceRow objects from the current buffer.
        Note: This is in-memory only and doesn't include flushed data.
        """
        rows = []
        
        for (row_agent_id, row_tool, row_field, row_pii_type), frequency in self._buffer.items():
            # Apply filters
            if agent_id and row_agent_id != agent_id:
                continue
            if tool and row_tool != tool:
                continue
            if pii_type and row_pii_type != pii_type:
                continue
            # Date filtering is not implemented for in-memory buffer
            if start_date or end_date:
                # For now, include all rows when date filters are specified
                # In a real implementation, you'd need timestamp data
                pass
            
            rows.append(ComplianceRow(
                agent_id=row_agent_id,
                tool=row_tool,
                field=row_field,
                pii_type=row_pii_type,
                frequency=frequency
            ))
        
        # Sort by frequency (descending)
        rows.sort(key=lambda x: x.frequency, reverse=True)
        return rows

    async def query_compliance_catalog(
        self,
        agent_id: Optional[str] = None,
        tool: Optional[str] = None,
        pii_type: Optional[str] = None,
        last_seen_min: Optional[datetime] = None,
        last_seen_max: Optional[datetime] = None,
    ) -> list[ComplianceRow]:
        """Query compliance catalog from both hot and warm tiers.
        
        Args:
            agent_id: Filter by agent_id (optional)
            tool: Filter by tool (optional)
            pii_type: Filter by pii_type (optional)
            last_seen_min: Filter by last_seen >= this date (optional)
            last_seen_max: Filter by last_seen <= this date (optional)
            
        Returns:
            List of ComplianceRow objects from both tiers, merged and deduplicated
        """
        results: list[ComplianceRow] = []
        
        # Check if we have a composite writer (hot + warm)
        if isinstance(self._writer, CompositeCatalogWriter):
            # Query both hot and warm tiers
            hot_results = await self._query_hot_tier(agent_id, tool, pii_type, last_seen_min, last_seen_max)
            warm_results = await self._query_warm_tier(agent_id, tool, pii_type, last_seen_min, last_seen_max)
            
            # Merge results, preferring higher frequency values for duplicates
            all_results = hot_results + warm_results
            results = self._merge_and_deduplicate_compliance_rows(all_results)
            
        elif isinstance(self._writer, HotCatalogWriter):
            # Query hot tier only
            results = await self._query_hot_tier(agent_id, tool, pii_type, last_seen_min, last_seen_max)
            
        elif isinstance(self._writer, PostgresCatalogWriter):
            # Query warm tier only
            results = await self._query_warm_tier(agent_id, tool, pii_type, last_seen_min, last_seen_max)
            
        else:
            # For other writers (like NullCatalogWriter), return empty list
            # or you could optionally query the in-memory buffer
            pass
        
        # Apply final sorting
        results.sort(key=lambda x: x.frequency, reverse=True)
        return results
    
    async def _query_hot_tier(
        self,
        agent_id: Optional[str] = None,
        tool: Optional[str] = None,
        pii_type: Optional[str] = None,
        last_seen_min: Optional[datetime] = None,
        last_seen_max: Optional[datetime] = None,
    ) -> list[ComplianceRow]:
        """Query compliance catalog from hot tier (ClickHouse)."""
        try:
            from agent_obs.storage.hot import HotStore
            hot_store = HotStore()
            
            # Get all compliance catalog rows from hot tier
            hot_rows = await hot_store.get_compliance_catalog_all()
            
            # Convert to ComplianceRow objects and apply filters
            compliance_rows = []
            for row in hot_rows:
                # Apply filters
                if agent_id and row.get("agent_id") != agent_id:
                    continue
                if tool and row.get("tool") != tool:
                    continue
                if pii_type and row.get("pii_type") != pii_type:
                    continue
                
                # Handle date filters
                if last_seen_min:
                    last_seen = row.get("last_seen")
                    if last_seen and last_seen < last_seen_min:
                        continue
                if last_seen_max:
                    last_seen = row.get("last_seen")
                    if last_seen and last_seen > last_seen_max:
                        continue
                
                compliance_rows.append(ComplianceRow(
                    agent_id=row.get("agent_id", ""),
                    tool=row.get("tool", ""),
                    field=row.get("field", ""),
                    pii_type=row.get("pii_type", ""),
                    frequency=row.get("frequency", 0)
                ))
            
            await hot_store.close()
            return compliance_rows
            
        except Exception as e:
            logger.warning("Failed to query hot tier compliance catalog: %s", str(e))
            return []
    
    async def _query_warm_tier(
        self,
        agent_id: Optional[str] = None,
        tool: Optional[str] = None,
        pii_type: Optional[str] = None,
        last_seen_min: Optional[datetime] = None,
        last_seen_max: Optional[datetime] = None,
    ) -> list[ComplianceRow]:
        """Query compliance catalog from warm tier (PostgreSQL)."""
        try:
            from agent_obs.storage.warm import WarmStore
            warm_store = WarmStore()
            
            # Get all compliance catalog rows from warm tier
            warm_rows = await warm_store.get_all_compliance_catalog()
            
            # Convert to ComplianceRow objects and apply filters
            compliance_rows = []
            for row in warm_rows:
                # Apply filters
                if agent_id and row.get("agent_id") != agent_id:
                    continue
                if tool and row.get("tool") != tool:
                    continue
                if pii_type and row.get("pii_type") != pii_type:
                    continue
                
                # Handle date filters
                if last_seen_min:
                    last_seen = row.get("last_seen")
                    if last_seen and last_seen < last_seen_min:
                        continue
                if last_seen_max:
                    last_seen = row.get("last_seen")
                    if last_seen and last_seen > last_seen_max:
                        continue
                
                compliance_rows.append(ComplianceRow(
                    agent_id=row.get("agent_id", ""),
                    tool=row.get("tool", ""),
                    field=row.get("field", ""),
                    pii_type=row.get("pii_type", ""),
                    frequency=row.get("frequency", 0)
                ))
            
            await warm_store.close()
            return compliance_rows
            
        except Exception as e:
            logger.warning("Failed to query warm tier compliance catalog: %s", str(e))
            return []
    
    def _merge_and_deduplicate_compliance_rows(self, rows: list[ComplianceRow]) -> list[ComplianceRow]:
        """Merge compliance rows from different tiers, keeping highest frequency for duplicates."""
        # Use (agent_id, tool, field, pii_type) as unique key
        merged: dict[tuple[str, str, str, str], ComplianceRow] = {}
        
        for row in rows:
            key = (row.agent_id, row.tool, row.field, row.pii_type)
            
            if key in merged:
                # Keep the row with higher frequency
                if row.frequency > merged[key].frequency:
                    merged[key] = row
            else:
                merged[key] = row
        
        return list(merged.values())

    async def close(self) -> None:
        """Stop the flush worker, drain the buffer, close the writer."""
        self._shutdown.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                self._task.cancel()
                try:
                    await self._task
                except (asyncio.CancelledError, Exception):
                    pass
            self._task = None
        await self.flush()
        await self._writer.close()

    # ------------------------------------------------------------------
    # Worker
    # ------------------------------------------------------------------

    def _ensure_worker(self) -> None:
        """Start the background flush task lazily (requires a running loop)."""
        if self._task is not None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No event loop (sync context) — flush must be called manually.
            return
        self._shutdown.clear()
        self._task = loop.create_task(self._flush_worker())

    async def _flush_worker(self) -> None:
        while not self._shutdown.is_set():
            try:
                await asyncio.wait_for(
                    self._shutdown.wait(), timeout=self._flush_interval
                )
                break
            except asyncio.TimeoutError:
                pass
            await self.flush()


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


# Environment config for tiered compliance catalog writing
ENV_COMPLIANCE_TIER = "AGENT_OBS_COMPLIANCE_TIER"  # dual | warm_only | hot_only


def build_compliance_catalog(*, flush_interval: float = 60.0) -> ComplianceCatalog:
    """Construct a ``ComplianceCatalog`` based on env configuration.

    ``AGENT_OBS_COMPLIANCE_PG`` controls Postgres warm tier (existing behavior):
    - ``on`` / ``1`` / ``true``  → ``PostgresCatalogWriter`` (asyncpg + DSN,
      default warm-tier DSN when ``COMPLIANCE_PG_DSN`` is unset).
    - ``off`` / ``0`` / ``false`` → ``NullCatalogWriter``.
    - unset / ``auto``           → Postgres if asyncpg is importable **and**
      ``COMPLIANCE_PG_DSN`` is set; otherwise ``NullWriter``.

    ``AGENT_OBS_COMPLIANCE_TIER`` controls ClickHouse hot tier (PC34):
    - ``dual`` (default) → Postgres + ClickHouse writers
    - ``warm_only`` → Postgres only (existing behavior)
    - ``hot_only`` → ClickHouse only (for tiered retention)
    """
    # Determine writers based on environment
    tier_mode = os.environ.get(ENV_COMPLIANCE_TIER, "dual").strip().lower()
    pg_mode = os.environ.get(ENV_COMPLIANCE_PG, "auto").strip().lower()
    
    writers: list[CatalogWriter] = []
    
    # Always add ClickHot writer if tier mode allows
    if tier_mode in ("dual", "hot_only"):
        try:
            writers.append(HotCatalogWriter())
        except Exception as e:
            logger.warning("HotCatalogWriter unavailable: %s", str(e))
            if tier_mode == "hot_only":
                raise RuntimeError("Hot tier required but not available") from e
    
    # Add Postgres writer if PG mode allows and tier mode allows it
    if pg_mode not in ("off", "0", "false", "no") and tier_mode in ("dual", "warm_only"):
        if pg_mode in ("on", "1", "true", "yes"):
            writer = PostgresCatalogWriter()
        else:  # auto
            dsn = os.environ.get(ENV_COMPLIANCE_PG_DSN)
            try:
                import asyncpg  # noqa: F401
                has_asyncpg = True
            except ImportError:
                has_asyncpg = False
            if has_asyncpg and dsn:
                writer = PostgresCatalogWriter(dsn=dsn)
            else:
                writer = NullCatalogWriter()
        writers.append(writer)
    
    if not writers:
        raise RuntimeError(
            "No compliance catalog writers available. "
            "Check AGENT_OBS_COMPLIANCE_TIER and AGENT_OBS_COMPLIANCE_PG settings."
        )
    
    # For multiple writers, create a composite writer
    if len(writers) == 1:
        return ComplianceCatalog(writers[0], flush_interval=flush_interval)
    else:
        return ComplianceCatalog(CompositeCatalogWriter(writers), flush_interval=flush_interval)


class CompositeCatalogWriter:
    """Composite writer that writes to multiple catalog tiers."""
    
    def __init__(self, writers: list[CatalogWriter]) -> None:
        self._writers = writers
    
    async def upsert(self, rows: list[ComplianceRow]) -> None:
        """Write to all configured writers."""
        for writer in self._writers:
            try:
                await writer.upsert(rows)
            except Exception as e:
                logger.warning("Compliance catalog writer failed: %s", str(e))
                # Continue with other writers (don't fail the entire batch)
    
    async def close(self) -> None:
        """Close all writers."""
        for writer in self._writers:
            try:
                await writer.close()
            except Exception as e:
                logger.warning("Compliance catalog writer close failed: %s", str(e))
