"""ClickHouse hot-tier storage client with connection pooling.

Provides read/write access to spans_hot, eval_results_hot, and audit_events_hot
tables in the ClickHouse observability database.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict
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
        """Write a batch of span dicts to spans_hot table."""
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
                    span.get("response_embedding"),
                )
            )
        client.execute(
            """
            INSERT INTO spans_hot
            (trace_id, span_id, parent_span_id, agent_id, tenant_id, name,
             span_type, start_time, end_time, status, attributes, events,
             cost_usd, response_embedding)
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
                   cost_usd, response_embedding
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
                    "response_embedding": row[13],
                }
            )
        return spans

    def close(self) -> None:
        """Close the ClickHouse connection."""
        if self._client is not None:
            try:
                self._client.disconnect()
            except Exception:
                pass
            self._client = None
