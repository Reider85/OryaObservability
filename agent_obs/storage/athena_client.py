"""AWS Athena client for SQL queries over cold-tier Parquet (PP22).

The cold tier stores trace aggregates as Hive-style partitioned Parquet
(``tenant_id=<t>/year=<Y>/month=<M>/day=<D>/part-<ts>.parquet``, written by
``agent_obs.storage.maintenance.migrate_traces_to_cold``). Athena queries that
table through the Glue catalog defined in ``infra/storage/athena_glue.sql``.

The ``boto3`` import is deliberately lazy (same pattern as
``agent_obs.storage.cold.ColdStore``) so importing this module never pulls in
the AWS SDK — the SDK itself never talks to Athena.

Cost guard
----------
Athena only reports ``Statistics.DataScannedInBytes`` while/after a query runs
(``EXPLAIN`` does not expose scanned bytes), so the 1 GiB guard is a
mid-flight abort: the client polls ``get_query_execution``, and as soon as the
running scan exceeds ``ATHENA_MAX_SCAN_BYTES`` it calls
``stop_query_execution`` and raises :class:`AthenaCostGuardError` with a hint
to narrow the query with partition filters (``tenant_id``, ``year``/``month``/
``day``).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from collections.abc import Sequence
from datetime import date as date_type
from typing import Any

from agent_obs.metrics import (
    athena_cost_guard_triggered_total,
    athena_data_scanned_bytes_total,
    athena_query_latency_seconds,
)

logger = logging.getLogger(__name__)

DEFAULT_REGION = "us-east-1"
DEFAULT_DATABASE = "observability"
DEFAULT_OUTPUT_LOCATION = "s3://cold-traces/athena-results/"
DEFAULT_TIMEOUT_SECONDS = 60.0
DEFAULT_MAX_SCAN_BYTES = 1_073_741_824  # 1 GiB, per PP22 cost guard
DEFAULT_POLL_INTERVAL = 0.5
DEFAULT_LIMIT = 100

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_SAFE_LITERAL = re.compile(r"^[A-Za-z0-9_.:\-]{1,256}$")
_DAY_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")

_INT_TYPES = {"tinyint", "smallint", "int", "integer", "bigint"}
_FLOAT_TYPES = {"double", "float", "real", "decimal"}
_BOOL_TYPES = {"boolean"}


class AthenaQueryError(RuntimeError):
    """Athena reported FAILED or CANCELLED for the query."""


class AthenaTimeoutError(TimeoutError):
    """Query did not finish within ``ATHENA_QUERY_TIMEOUT`` seconds."""


class AthenaCostGuardError(ValueError):
    """Query stopped because it scanned more bytes than the allowed limit."""


def _validate_identifier(value: str, what: str) -> str:
    if not _IDENTIFIER.match(value):
        raise ValueError(f"unsafe {what}: {value!r}")
    return value


def _validate_literal(value: str, what: str) -> str:
    if not _SAFE_LITERAL.match(value):
        raise ValueError(f"unsafe {what}: {value!r}")
    return value


def _parse_day(day: Any) -> tuple[int, int, int]:
    """Normalize a partition day into ``(year, month, day_of_month)``.

    Accepts ``"YYYY-MM-DD"`` strings and :class:`datetime.date` instances.
    """
    if isinstance(day, date_type):
        return day.year, day.month, day.day
    match = _DAY_RE.match(str(day))
    if not match:
        raise ValueError(f"unsafe day: {day!r} (expected YYYY-MM-DD)")
    year, month, day_of_month = (int(part) for part in match.groups())
    if not (1 <= month <= 12 and 1 <= day_of_month <= 31):
        raise ValueError(f"unsafe day: {day!r} (month/day out of range)")
    return year, month, day_of_month


def build_trace_fields_sql(
    database: str = DEFAULT_DATABASE,
    trace_id: str = "",
    fields: Sequence[str] | None = None,
    tenant_id: str | None = None,
    day: Any = None,
    limit: int = DEFAULT_LIMIT,
) -> str:
    """Build a partition-pruned SELECT for cold-trace drill-down queries.

    The cold tier stores per-(tenant, agent, day) *aggregates* — there is no
    per-``trace_id`` column — so partition pruning narrows the scan to the
    ``tenant_id`` / ``year`` / ``month`` / ``day`` partition when those are
    known. Without them the query is a full scan (capped by ``LIMIT``) and the
    AthenaClient cost guard aborts it above ``ATHENA_MAX_SCAN_BYTES``.

    All interpolated values are strictly validated (identifiers: ``[A-Za-z_]``
    word chars; literals: alphanumerics and ``_.:-`` only), so quotes,
    semicolons and comments are rejected instead of escaped.
    """
    _validate_identifier(database, "database")
    if trace_id:
        _validate_literal(trace_id, "trace_id")
    if tenant_id is not None:
        _validate_literal(tenant_id, "tenant_id")

    columns = "*"
    if fields:
        for field_name in fields:
            _validate_identifier(field_name, "field name")
        columns = ", ".join(fields)

    conditions: list[str] = []
    if tenant_id is not None:
        conditions.append(f"tenant_id = '{tenant_id}'")
    if day is not None:
        year, month, day_of_month = _parse_day(day)
        conditions.append(f"year = {year}")
        conditions.append(f"month = {month}")
        conditions.append(f"day = {day_of_month}")
    where = f" WHERE {' AND '.join(conditions)}" if conditions else ""

    # trace_id goes into a comment for auditability: the aggregate schema has
    # no trace_id column to filter on.
    trace_comment = f"-- drill-down trace_id: {trace_id}\n" if trace_id else ""
    limit = max(1, min(int(limit), 10_000))
    return (
        f"{trace_comment}SELECT {columns} FROM {database}.cold_traces"
        f"{where} LIMIT {limit}"
    )


def _convert_value(varchar_value: str | None, column_type: str) -> Any:
    """Convert an Athena ``VarCharValue`` using its ``ColumnInfo`` type."""
    if varchar_value is None:
        return None
    lowered = column_type.lower()
    if lowered in _INT_TYPES:
        try:
            return int(varchar_value)
        except (TypeError, ValueError):
            return varchar_value
    if lowered in _FLOAT_TYPES:
        try:
            return float(varchar_value)
        except (TypeError, ValueError):
            return varchar_value
    if lowered in _BOOL_TYPES:
        return varchar_value.lower() == "true"
    return varchar_value


def _row_to_dict(raw_data: list[dict[str, Any]], columns: Sequence[str], types: Sequence[str]) -> dict[str, Any]:
    row: dict[str, Any] = {}
    for index, name in enumerate(columns):
        cell = raw_data[index] if index < len(raw_data) else {}
        column_type = types[index] if index < len(types) else "string"
        row[name] = _convert_value(cell.get("VarCharValue"), column_type)
    return row


def _looks_like_header(row: dict[str, Any], columns: Sequence[str]) -> bool:
    """Athena repeats column names as the first data row of the first page."""
    values = [cell.get("VarCharValue") for cell in row.get("Data", [])]
    return list(values) == list(columns)


class AthenaClient:
    """Async Athena query client for cold-tier Parquet in S3.

    Connection is lazily established on first query so that constructing an
    ``AthenaClient`` never requires ``boto3`` to be installed or AWS to be
    reachable.

    Parameters
    ----------
    database:
        Glue database name. Defaults to ``ATHENA_DATABASE`` (``observability``).
    output_location:
        S3 prefix for query result dumps. Defaults to
        ``ATHENA_OUTPUT_LOCATION`` (``s3://cold-traces/athena-results/``).
    region / work_group:
        AWS region and optional work group. Defaults to ``ATHENA_REGION`` /
        ``ATHENA_WORK_GROUP``.
    timeout:
        Max seconds for one query. Defaults to ``ATHENA_QUERY_TIMEOUT`` (60).
    max_scan_bytes:
        Cost-guard limit in bytes. Defaults to ``ATHENA_MAX_SCAN_BYTES``
        (1 GiB).
    poll_interval:
        Seconds between ``get_query_execution`` polls. Defaults to
        ``ATHENA_POLL_INTERVAL`` (0.5).
    """

    def __init__(
        self,
        database: str | None = None,
        output_location: str | None = None,
        region: str | None = None,
        work_group: str | None = None,
        timeout: float | None = None,
        max_scan_bytes: int | None = None,
        poll_interval: float | None = None,
    ) -> None:
        self._database = database or os.environ.get("ATHENA_DATABASE", DEFAULT_DATABASE)
        self._output_location = output_location or os.environ.get(
            "ATHENA_OUTPUT_LOCATION", DEFAULT_OUTPUT_LOCATION
        )
        self._region = region or os.environ.get("ATHENA_REGION", DEFAULT_REGION)
        self._work_group = work_group or os.environ.get("ATHENA_WORK_GROUP") or None
        self._timeout = float(
            timeout if timeout is not None else os.environ.get("ATHENA_QUERY_TIMEOUT", DEFAULT_TIMEOUT_SECONDS)
        )
        self._max_scan_bytes = int(
            max_scan_bytes
            if max_scan_bytes is not None
            else os.environ.get("ATHENA_MAX_SCAN_BYTES", DEFAULT_MAX_SCAN_BYTES)
        )
        self._poll_interval = float(
            poll_interval
            if poll_interval is not None
            else os.environ.get("ATHENA_POLL_INTERVAL", DEFAULT_POLL_INTERVAL)
        )
        self._client: Any | None = None

    @property
    def database(self) -> str:
        return self._database

    @property
    def timeout(self) -> float:
        return self._timeout

    @property
    def max_scan_bytes(self) -> int:
        return self._max_scan_bytes

    def _get_client(self) -> Any:
        """Lazy initialization of the boto3 Athena client."""
        if self._client is None:
            try:
                import boto3
                from botocore.config import Config
            except ImportError:
                raise RuntimeError(
                    "boto3 is required for AthenaClient. "
                    "Install with: pip install 'agent-obs[cron]'"
                )

            self._client = boto3.client(
                "athena",
                region_name=self._region,
                config=Config(read_timeout=60, retries={"max_attempts": 2}),
            )
            logger.info(
                "Athena client created: region=%s database=%s output=%s",
                self._region,
                self._database,
                self._output_location,
            )
        return self._client

    def _start_query(self, sql: str) -> str:
        params: dict[str, Any] = {
            "QueryString": sql,
            "QueryExecutionContext": {"Database": self._database},
            "ResultConfiguration": {"OutputLocation": self._output_location},
        }
        if self._work_group:
            params["WorkGroup"] = self._work_group
        response = self._get_client().start_query_execution(**params)
        return response["QueryExecutionId"]

    def _get_query_execution(self, execution_id: str) -> dict[str, Any]:
        return self._get_client().get_query_execution(QueryExecutionId=execution_id)

    def _stop_query(self, execution_id: str) -> None:
        try:
            self._get_client().stop_query_execution(QueryExecutionId=execution_id)
        except Exception as e:  # noqa: BLE001 - best-effort stop must never raise
            logger.warning("athena stop_query_execution failed for %s: %s", execution_id, e)

    def _get_results_page(self, execution_id: str, next_token: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"QueryExecutionId": execution_id}
        if next_token:
            params["NextToken"] = next_token
        return self._get_client().get_query_results(**params)

    async def _await_results(self, execution_id: str, stats: dict[str, int]) -> None:
        """Poll until the query finishes, enforcing timeout and cost guard."""
        deadline = time.monotonic() + self._timeout
        while True:
            response = await asyncio.to_thread(self._get_query_execution, execution_id)
            query_execution = response.get("QueryExecution", {})
            status = query_execution.get("Status", {})
            statistics = query_execution.get("Statistics", {})
            scanned = int(statistics.get("DataScannedInBytes") or 0)
            stats["data_scanned_bytes"] = max(stats.get("data_scanned_bytes", 0), scanned)

            if stats["data_scanned_bytes"] > self._max_scan_bytes:
                # Mid-flight abort: Athena only exposes scanned bytes while the
                # query runs, so stop it before more bytes are billed.
                await asyncio.to_thread(self._stop_query, execution_id)
                athena_cost_guard_triggered_total.inc()
                raise AthenaCostGuardError(
                    f"Athena query {execution_id} stopped by cost guard: "
                    f"scanned {stats['data_scanned_bytes']} bytes > limit "
                    f"{self._max_scan_bytes}; narrow the query with partition "
                    f"filters (tenant_id, year/month/day)"
                )

            state = status.get("State", "QUEUED")
            if state == "SUCCEEDED":
                return
            if state in ("FAILED", "CANCELLED"):
                reason = status.get("StateChangeReason") or state
                raise AthenaQueryError(f"Athena query {execution_id} {state}: {reason}")
            if time.monotonic() >= deadline:
                await asyncio.to_thread(self._stop_query, execution_id)
                raise AthenaTimeoutError(
                    f"Athena query {execution_id} timed out after {self._timeout}s"
                )
            await asyncio.sleep(self._poll_interval)

    async def _fetch_results(self, execution_id: str) -> list[dict[str, Any]]:
        """Fetch all result pages, skipping Athena's repeated header row."""
        columns: list[str] = []
        types: list[str] = []
        rows: list[dict[str, Any]] = []
        next_token: str | None = None
        first_page = True

        while True:
            response = await asyncio.to_thread(self._get_results_page, execution_id, next_token)
            result_set = response.get("ResultSet", {})
            column_info = result_set.get("ResultSetMetadata", {}).get("ColumnInfo", [])
            if not columns and column_info:
                columns = [info.get("Name", f"col_{i}") for i, info in enumerate(column_info)]
                types = [info.get("Type", "string") for info in column_info]

            raw_rows = result_set.get("Rows", [])
            if first_page and raw_rows and columns and _looks_like_header(raw_rows[0], columns):
                raw_rows = raw_rows[1:]
            first_page = False

            for raw_row in raw_rows:
                rows.append(_row_to_dict(raw_row.get("Data", []), columns, types))

            next_token = response.get("NextToken")
            if not next_token:
                break
        return rows

    async def query(self, sql: str) -> list[dict[str, Any]]:
        """Run ``sql`` and return the result rows as a list of dicts.

        Flow: start query → poll to completion (timeout / cost guard enforced)
        → fetch all pages → skip Athena's header row.

        Raises
        ------
        AthenaQueryError
            Athena reported FAILED/CANCELLED.
        AthenaTimeoutError
            Query exceeded ``ATHENA_QUERY_TIMEOUT``; the query is stopped.
        AthenaCostGuardError
            Query scanned more than ``ATHENA_MAX_SCAN_BYTES``; the query is
            stopped before more bytes are billed.
        """
        started = time.monotonic()
        stats: dict[str, int] = {"data_scanned_bytes": 0}
        execution_id = await asyncio.to_thread(self._start_query, sql)
        try:
            await self._await_results(execution_id, stats)
            rows = await self._fetch_results(execution_id)
        finally:
            scanned = stats.get("data_scanned_bytes", 0)
            if scanned:
                athena_data_scanned_bytes_total.inc(scanned)
            athena_query_latency_seconds.observe(time.monotonic() - started)

        logger.debug(
            "athena query finished: id=%s rows=%d scanned_bytes=%d",
            execution_id,
            len(rows),
            stats.get("data_scanned_bytes", 0),
        )
        return rows

    async def query_trace_fields(
        self,
        trace_id: str,
        fields: Sequence[str] | None = None,
        tenant_id: str | None = None,
        day: Any = None,
    ) -> dict[str, Any]:
        """Drill-down adapter: query cold-trace fields for one trace.

        Matches the ``DrillDownService.athena_query`` hook contract
        (``(trace_id, fields) -> {"fields", "metadata"}``). The cold tier holds
        per-(tenant, agent, day) aggregates, so pass ``tenant_id``/``day`` when
        known to prune partitions; otherwise the query is a full scan guarded
        by the cost guard.
        """
        sql = build_trace_fields_sql(
            database=self._database,
            trace_id=trace_id,
            fields=fields,
            tenant_id=tenant_id,
            day=day,
        )
        started = time.monotonic()
        rows = await self.query(sql)
        recovered: dict[str, Any] = rows[0] if rows else {}
        if fields:
            recovered = {name: recovered[name] for name in fields if name in recovered}
        latency_ms = (time.monotonic() - started) * 1000
        return {
            "fields": recovered,
            "metadata": {
                "storage_tier": "cold",
                "query_latency_ms": latency_ms,
                "source": "athena",
                "mfa_used": False,
            },
        }


__all__ = [
    "AthenaClient",
    "AthenaCostGuardError",
    "AthenaQueryError",
    "AthenaTimeoutError",
    "build_trace_fields_sql",
]
