"""ClickHouse hot-tier storage client with connection pooling.

Provides read/write access to spans_hot, eval_results_hot, and audit_events_hot
tables in the ClickHouse observability database.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agent_obs.eval.base import EvalResult

logger = logging.getLogger(__name__)


class HotStore:
    """ClickHouse client for hot-tier storage with connection pooling.

    Connection is lazily established on first call. Pool size and timeout
    are configurable via constructor args or environment variables.
    """

    def __init__(
        self,
        host: str | None = None,
        port: int | None = None,
        database: str | None = None,
        user: str | None = None,
        password: str | None = None,
        connect_timeout: int = 10,
        send_receive_timeout: int = 30,
        pool_size: int = 10,
    ) -> None:
        self._host = host or os.environ.get("CLICKHOUSE_HOST", "localhost")
        self._port = port or int(os.environ.get("CLICKHOUSE_PORT", "9000"))
        self._database = database or os.environ.get("CLICKHOUSE_DB", "observability")
        self._user = user or os.environ.get("CLICKHOUSE_USER", "observability_user")
        self._password = password or os.environ.get("CLICKHOUSE_PASSWORD", "observability_password")
        self._connect_timeout = connect_timeout
        self._send_receive_timeout = send_receive_timeout
        self._pool_size = pool_size
        self._client: Any | None = None

    def _get_client(self) -> Any:
        """Lazy initialization of ClickHouse client with connection pooling."""
        if self._client is None:
            try:
                from clickhouse_driver import Client as CHClient

                self._client = CHClient(
                    host=self._host,
                    port=self._port,
                    database=self._database,
                    user=self._user,
                    password=self._password,
                    connect_timeout=self._connect_timeout,
                    send_receive_timeout=self._send_receive_timeout,
                    settings={"max_pool_size": self._pool_size},
                )
                logger.info(
                    "ClickHouse connection established to %s:%d/%s",
                    self._host,
                    self._port,
                    self._database,
                )
            except ImportError:
                raise RuntimeError(
                    "clickhouse-driver is required for HotStore. "
                    "Install with: pip install clickhouse-driver"
                )
        return self._client

    def is_available(self) -> bool:
        """Check if ClickHouse is reachable."""
        try:
            client = self._get_client()
            client.execute("SELECT 1")
            return True
        except Exception:
            return False

    def write_eval_result(self, result: EvalResult) -> None:
        """Write an EvalResult to eval_results_hot table.

        Idempotent: ReplacingMergeTree deduplicates by
        (trace_id, eval_id, eval_name, eval_timestamp).
        """
        client = self._get_client()
        scores_json = json.dumps(result.scores)
        flags_list = result.flags if result.flags else []

        client.execute(
            """
            INSERT INTO eval_results_hot
            (trace_id, eval_id, eval_name, eval_timestamp, eval_latency_seconds,
             scores, judge_model, judge_prompt_sha256, reasoning, flags)
            VALUES
            """,
            [
                (
                    result.trace_id,
                    result.eval_id,
                    result.eval_name,
                    result.eval_timestamp,
                    result.eval_latency_seconds,
                    scores_json,
                    result.judge_model,
                    result.judge_prompt_sha256,
                    result.reasoning,
                    flags_list,
                )
            ],
        )
        logger.debug(
            "Eval result written: trace=%s eval_id=%s name=%s",
            result.trace_id,
            result.eval_id,
            result.eval_name,
        )

    def get_eval_results(self, trace_id: str) -> list[EvalResult]:
        """Retrieve all eval results for a trace from eval_results_hot."""
        from agent_obs.eval.base import EvalResult

        client = self._get_client()
        rows = client.execute(
            """
            SELECT trace_id, eval_id, eval_name, eval_timestamp, eval_latency_seconds,
                   scores, judge_model, judge_prompt_sha256, reasoning, flags
            FROM eval_results_hot
            WHERE trace_id = %s
            ORDER BY eval_timestamp DESC
            """,
            [trace_id],
        )

        results: list[EvalResult] = []
        for row in rows:
            scores = json.loads(row[5]) if isinstance(row[5], str) else row[5]
            results.append(
                EvalResult(
                    trace_id=row[0],
                    eval_id=row[1],
                    eval_name=row[2],
                    eval_timestamp=row[3],
                    eval_latency_seconds=row[4],
                    scores=scores,
                    judge_model=row[6] or "",
                    judge_prompt_sha256=row[7] or "",
                    reasoning=row[8] or "",
                    flags=row[9] if row[9] else [],
                )
            )
        return results

    def write_spans_batch(self, spans: list[dict]) -> None:
        """Write a batch of span dicts to spans_hot table.

        1C: spans_hot has no ``response_embedding`` column (Nullable(Array)
        is illegal in ClickHouse). Extra keys on the span dicts (including
        ``response_embedding``) are ignored.
        """
        if not spans:
            return
        client = self._get_client()
        rows = []
        for span in spans:
            rows.append(
                (
                    span.get("trace_id", ""),
                    span.get("span_id", ""),
                    span.get("parent_span_id", ""),
                    span.get("agent_id", ""),
                    span.get("tenant_id", ""),
                    span.get("name", ""),
                    span.get("span_type", ""),
                    span.get("start_time", 0.0),
                    span.get("end_time", 0.0),
                    span.get("status", ""),
                    json.dumps(span.get("attributes", {})),
                    json.dumps(span.get("events", [])),
                    span.get("cost_usd", 0.0),
                )
            )
        client.execute(
            """
            INSERT INTO spans_hot
            (trace_id, span_id, parent_span_id, agent_id, tenant_id, name,
             span_type, start_time, end_time, status, attributes, events,
             cost_usd)
            VALUES
            """,
            rows,
        )

    def get_spans(self, trace_id: str) -> list[dict]:
        """Retrieve all spans for a trace from spans_hot."""
        client = self._get_client()
        rows = client.execute(
            """
            SELECT trace_id, span_id, parent_span_id, agent_id, tenant_id, name,
                   span_type, start_time, end_time, status, attributes, events,
                   cost_usd
            FROM spans_hot
            WHERE trace_id = %s
            ORDER BY start_time ASC
            """,
            [trace_id],
        )

        spans: list[dict] = []
        for row in rows:
            spans.append(
                {
                    "trace_id": row[0],
                    "span_id": row[1],
                    "parent_span_id": row[2],
                    "agent_id": row[3],
                    "tenant_id": row[4],
                    "name": row[5],
                    "span_type": row[6],
                    "start_time": row[7],
                    "end_time": row[8],
                    "status": row[9],
                    "attributes": json.loads(row[10]) if isinstance(row[10], str) else row[10],
                    "events": json.loads(row[11]) if isinstance(row[11], str) else row[11],
                    "cost_usd": row[12],
                }
            )
        return spans

    def write_audit_event(self, event) -> None:
        """Write a RecoveryAuditEvent to audit_events_hot table.

        Parameters
        ----------
        event:
            RecoveryAuditEvent instance with audit trail record

        Raises
        ------
        Exception
            If ClickHouse write fails
        """
        client = self._get_client()
        row = event.to_clickhouse_row()
        
        client.execute(
            """
            INSERT INTO audit_events_hot
            (audit_id, timestamp, trace_id, actor, action, decision, resource, reason, ip_address, user_agent)
            VALUES
            """,
            [row],
        )
        logger.debug(
            "Audit event written: audit_id=%s action=%s reason=%s",
            event.audit_id,
            event.action,
            event.reason,
        )

    def get_audit_events(self, action: str | None = None, trace_id: str | None = None, limit: int = 100) -> list[dict]:
        """Retrieve audit events from audit_events_hot.
        
        Parameters
        ----------
        action:
            Filter by action (e.g., "vault.recover")
        trace_id:
            Filter by trace_id
        limit:
            Maximum number of events to return
            
        Returns
        -------
        list[dict]
            List of audit events as dicts
        """
        client = self._get_client()
        
        conditions = []
        params = []
        
        if action:
            conditions.append("action = %s")
            params.append(action)
        if trace_id:
            conditions.append("trace_id = %s")
            params.append(trace_id)
        
        where_clause = ""
        if conditions:
            where_clause = "WHERE " + " AND ".join(conditions)
        
        query = f"""
        SELECT audit_id, timestamp, trace_id, actor, action, decision, resource, reason, ip_address, user_agent
        FROM audit_events_hot
        {where_clause}
        ORDER BY timestamp DESC
        LIMIT %s
        """
        
        params.append(limit)
        rows = client.execute(query, params)
        
        events = []
        for row in rows:
            events.append({
                "audit_id": row[0],
                "timestamp": row[1],  # DateTime64(3) → datetime object
                "trace_id": row[2],
                "actor": json.loads(row[3]) if isinstance(row[3], str) else row[3],
                "action": row[4],
                "decision": row[5],
                "resource": json.loads(row[6]) if isinstance(row[6], str) else row[6],
                "reason": row[7],
                "ip_address": row[8],
                "user_agent": row[9],
            })
        
        return events

    def write_audit_events(self, events: list) -> int:
        """Write a batch of audit events to audit_events_hot in one INSERT.

        Accepts any object exposing ``to_clickhouse_row()`` — both
        ``RecoveryAuditEvent`` (PC20) and ``LeaseExpiryAuditEvent`` (PC21).

        Returns
        -------
        int
            Number of rows ClickHouse reported as inserted.

        Raises
        ------
        Exception
            If ClickHouse write fails
        """
        if not events:
            return 0
        client = self._get_client()
        rows = [event.to_clickhouse_row() for event in events]

        inserted = client.execute(
            """
            INSERT INTO audit_events_hot
            (audit_id, timestamp, trace_id, actor, action, decision, resource, reason, ip_address, user_agent)
            VALUES
            """,
            rows,
        )
        logger.debug("Audit batch written: count=%d", len(rows))
        return int(inserted) if inserted is not None else len(rows)

    # --- Tiered retention queries (PC21) -------------------------------------
    # The cron jobs read the hot tier in batches, ship the rows to the warm/cold
    # tier, and only then delete them from ClickHouse. Each SELECT is bounded by
    # `limit` so a single run can never pull the whole table.

    def get_audit_events_older_than(self, cutoff: datetime, limit: int = 10_000) -> list[dict]:
        """Read audit events older than ``cutoff`` for cold archival (PC21).

        Ordered oldest-first so a partially-completed run keeps making progress
        on the same prefix of the data.

        Note: clickhouse-driver only substitutes parameters for a SELECT when
        they are passed as a **dict**. A list is interpreted as INSERT row data
        and the raw ``%s`` is shipped to the server (Code 62).
        """
        client = self._get_client()
        rows = client.execute(
            """
            SELECT audit_id, timestamp, trace_id, actor, action, decision,
                   resource, reason, ip_address, user_agent
            FROM audit_events_hot
            WHERE timestamp < %(cutoff)s
            ORDER BY timestamp ASC
            LIMIT %(limit)s
            """,
            {"cutoff": cutoff, "limit": limit},
        )
        return [self._audit_row_to_dict(row) for row in rows]

    def delete_audit_events(self, audit_ids: list[str]) -> int:
        """Delete archived audit events from the hot tier (PC21).

        Only called after the Parquet object has been uploaded successfully.
        """
        if not audit_ids:
            return 0
        client = self._get_client()
        client.execute(
            "DELETE FROM audit_events_hot WHERE audit_id IN %(ids)s",
            {"ids": audit_ids},
        )
        logger.info("audit_events_hot purged: rows=%d", len(audit_ids))
        return len(audit_ids)

    def get_eval_results_older_than(self, cutoff: datetime, limit: int = 10_000) -> list[dict]:
        """Read eval results older than ``cutoff`` for warm migration (PC21).

        Returns dicts that still carry ``reasoning``; ``WarmStore`` drops it.

        Parameters must be a dict: clickhouse-driver treats a list as INSERT
        row data for any statement, so a SELECT given a list reaches the server
        with an unsubstituted ``%s`` (Code 62).
        """
        client = self._get_client()
        rows = client.execute(
            """
            SELECT trace_id, eval_id, eval_name, eval_timestamp, eval_latency_seconds,
                   scores, judge_model, judge_prompt_sha256, reasoning, flags
            FROM eval_results_hot
            WHERE eval_timestamp < %(cutoff)s
            ORDER BY eval_timestamp ASC
            LIMIT %(limit)s
            """,
            {"cutoff": cutoff, "limit": limit},
        )
        results: list[dict] = []
        for row in rows:
            results.append(
                {
                    "trace_id": row[0],
                    "eval_id": row[1],
                    "eval_name": row[2],
                    "eval_timestamp": row[3],
                    "eval_latency_seconds": row[4],
                    "scores": json.loads(row[5]) if isinstance(row[5], str) else row[5],
                    "judge_model": row[6] or "",
                    "judge_prompt_sha256": row[7] or "",
                    "reasoning": row[8] or "",
                    "flags": list(row[9]) if row[9] else [],
                }
            )
        return results

    def delete_eval_results(self, keys: list[tuple[str, str, str]]) -> int:
        """Delete migrated eval results from the hot tier (PC21).

        ``eval_results_hot`` is a ``ReplacingMergeTree`` ordered by
        ``(trace_id, eval_id, eval_name, eval_timestamp)``, so the delete key is
        that same tuple. Only called after the warm-tier upsert succeeded.
        """
        if not keys:
            return 0
        client = self._get_client()
        client.execute(
            """
            DELETE FROM eval_results_hot
            WHERE (trace_id, eval_id, eval_name, eval_timestamp) IN %(keys)s
            """,
            {"keys": keys},
        )
        logger.info("eval_results_hot purged: rows=%d", len(keys))
        return len(keys)

    @staticmethod
    def _audit_row_to_dict(row: tuple) -> dict:
        """Map an ``audit_events_hot`` row tuple to a dict."""
        return {
            "audit_id": row[0],
            "timestamp": row[1],
            "trace_id": row[2],
            "actor": json.loads(row[3]) if isinstance(row[3], str) else row[3],
            "action": row[4],
            "decision": row[5],
            "resource": json.loads(row[6]) if isinstance(row[6], str) else row[6],
            "reason": row[7],
            "ip_address": row[8],
            "user_agent": row[9],
        }

    # --- Tiered retention: spans_hot -> Postgres warm (PC22) ----------------

    def get_spans_older_than(self, cutoff: datetime, limit: int = 10_000) -> list[dict]:
        """Read spans older than ``cutoff`` for warm migration (PC22).

        Returns dicts with the full span shape so the caller can aggregate
        by ``trace_id``.  Ordered oldest-first so a partially-completed run
        keeps making progress on the same prefix of the data.

        Parameters must be a dict: clickhouse-driver treats a list as INSERT
        row data for any statement.
        """
        client = self._get_client()
        rows = client.execute(
            """
            SELECT trace_id, span_id, parent_span_id, agent_id, tenant_id,
                   name, span_type, start_time, end_time, status,
                   attributes, events, cost_usd
            FROM spans_hot
            WHERE start_time < %(cutoff)s
            ORDER BY start_time ASC
            LIMIT %(limit)s
            """,
            {"cutoff": cutoff, "limit": limit},
        )
        spans: list[dict] = []
        for row in rows:
            spans.append(
                {
                    "trace_id": row[0],
                    "span_id": row[1],
                    "parent_span_id": row[2],
                    "agent_id": row[3],
                    "tenant_id": row[4],
                    "name": row[5],
                    "span_type": row[6],
                    "start_time": row[7],
                    "end_time": row[8],
                    "status": row[9],
                    "attributes": json.loads(row[10]) if isinstance(row[10], str) else row[10],
                    "events": json.loads(row[11]) if isinstance(row[11], str) else row[11],
                    "cost_usd": row[12],
                }
            )
        return spans

    def delete_spans(self, trace_ids: list[str]) -> int:
        """Delete migrated spans from the hot tier (PC22).

        Only called after the warm-tier upsert succeeded.  ``spans_hot`` is a
        ``MergeTree`` so a plain ``DELETE … WHERE trace_id IN (…)`` suffices.
        """
        if not trace_ids:
            return 0
        client = self._get_client()
        client.execute(
            "DELETE FROM spans_hot WHERE trace_id IN %(trace_ids)s",
            {"trace_ids": trace_ids},
        )
        logger.info("spans_hot purged: traces=%d", len(trace_ids))
        return len(trace_ids)

    def _execute_clickhouse(self, query: str, params: dict | None = None) -> list[tuple]:
        """Execute a ClickHouse query and return results.
        
        Args:
            query: ClickHouse SQL query
            params: Query parameters for safe interpolation
            
        Returns:
            List of result rows as tuples
        """
        client = self._get_client()
        if params:
            results = client.execute(query, params)
        else:
            results = client.execute(query)
        return results

    def write_drift_history(self, report: dict) -> None:
        """Write drift detection result to drift_history table.
        
        Args:
            report: DriftReport serialized as dict
        """
        client = self._get_client()
        
        # Convert timestamp to ClickHouse DateTime64(3) format
        eval_timestamp = datetime.fromtimestamp(report["eval_timestamp"], tz=datetime.timezone.utc)
        
        client.execute(
            """
            INSERT INTO drift_history (
                trace_id, eval_id, eval_name, eval_version, eval_timestamp,
                eval_latency_seconds, kl_score, threshold, is_drift_detected,
                severity, baseline_window_start, baseline_window_end,
                last_window_start, last_window_end, sample_size_baseline,
                sample_size_last, agent_id, model_name, flags
            ) VALUES (
                %(trace_id)s, %(eval_id)s, %(eval_name)s, %(eval_version)s, %(eval_timestamp)s,
                %(eval_latency_seconds)s, %(kl_score)s, %(threshold)s, %(is_drift_detected)s,
                %(severity)s, %(baseline_window_start)s, %(baseline_window_end)s,
                %(last_window_start)s, %(last_window_end)s, %(sample_size_baseline)s,
                %(sample_size_last)s, %(agent_id)s, %(model_name)s, %(flags)s
            )
            """,
            {
                "trace_id": report["trace_id"],
                "eval_id": report["eval_id"],
                "eval_name": report["eval_name"],
                "eval_version": report["eval_version"],
                "eval_timestamp": eval_timestamp,
                "eval_latency_seconds": report["eval_latency_seconds"],
                "kl_score": report["kl_score"],
                "threshold": report["threshold"],
                "is_drift_detected": 1 if report["is_drift_detected"] else 0,
                "severity": report["severity"],
                "baseline_window_start": datetime.fromtimestamp(report["baseline_window_start"], tz=datetime.timezone.utc),
                "baseline_window_end": datetime.fromtimestamp(report["baseline_window_end"], tz=datetime.timezone.utc),
                "last_window_start": datetime.fromtimestamp(report["last_window_start"], tz=datetime.timezone.utc),
                "last_window_end": datetime.fromtimestamp(report["last_window_end"], tz=datetime.timezone.utc),
                "sample_size_baseline": report["sample_size_baseline"],
                "sample_size_last": report["sample_size_last"],
                "agent_id": report["agent_id"],
                "model_name": report["model_name"],
                "flags": report["flags"],
            }
        )

    def close(self) -> None:
        """Close the ClickHouse connection."""
        if self._client is not None:
            try:
                self._client.disconnect()
            except Exception:
                pass
            self._client = None
