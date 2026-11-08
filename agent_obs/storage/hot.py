"""ClickHouse hot-tier storage client with connection pooling and HA failover.

Provides read/write access to spans_hot, eval_results_hot, and audit_events_hot
tables in the ClickHouse observability database.

PP35 (T3.8.1): the client understands a replica list (``CLICKHOUSE_HOSTS``)
and automatically fails over to the next replica on connection-level errors.
Query errors (syntax, missing table) are never retried on another replica —
they would fail identically everywhere. Writes target the Distributed tables,
so after a replica is lost the surviving replica of the shard accepts writes
and the dead one resyncs from Keeper-tracked parts on restart.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, TypeVar

if TYPE_CHECKING:
    from agent_obs.eval.base import EvalResult

logger = logging.getLogger(__name__)

T = TypeVar("T")

# A replica that failed with a connection-level error is skipped for this many
# seconds before it is tried again (PP35). Keeps a flapping host from being
# re-probed on every single operation while the healthy replica serves traffic.
REPLICA_COOLDOWN_SECONDS = 30.0


def _parse_replica(target: str, default_port: int) -> tuple[str, int]:
    """Parse one ``host`` or ``host:port`` replica spec."""
    target = target.strip()
    if not target:
        raise ValueError("empty replica spec in CLICKHOUSE_HOSTS")
    if ":" in target:
        host, _, port_s = target.rpartition(":")
        if not host:
            raise ValueError(f"invalid replica spec: {target!r}")
        return (host, int(port_s))
    return (target, default_port)


def _resolve_replicas(
    hosts: list[str] | str | None = None,
    default_port: int | None = None,
) -> list[tuple[str, int]]:
    """Build the ordered replica list.

    Priority: explicit ``hosts`` argument > ``CLICKHOUSE_HOSTS`` env >
    single-host ``CLICKHOUSE_HOST``/``CLICKHOUSE_PORT`` fallback (pre-PP35
    behaviour, byte-for-byte).

    ``CLICKHOUSE_HOSTS`` is a comma-separated list of ``host[:port]`` specs,
    e.g. ``clickhouse1:9000,clickhouse2:9000,clickhouse3:9000,clickhouse4:9000``.
    """
    if default_port is None:
        default_port = int(os.environ.get("CLICKHOUSE_PORT", "9000"))

    if hosts is None:
        raw = os.environ.get("CLICKHOUSE_HOSTS", "").strip()
        hosts = [part for part in (p.strip() for p in raw.split(",")) if part] if raw else None

    if not hosts:
        single = os.environ.get("CLICKHOUSE_HOST", "localhost")
        return [_parse_replica(single, default_port)]

    if isinstance(hosts, str):
        hosts = [part for part in (p.strip() for p in hosts.split(",")) if part]

    return [_parse_replica(spec, default_port) for spec in hosts]


def _to_datetime64(value: Any) -> Any:
    """Coerce span timestamps for clickhouse-driver DateTime64 columns (PP35).

    The driver accepts ``datetime`` (and bare int seconds, bypassing tz
    handling), but span dicts carry epoch floats (``time.time()``) or ISO-8601
    strings (``Span.to_dict``) — both crashed the driver with
    ``AttributeError: 'float' object has no attribute 'tzinfo'`` the first
    time a real cluster INSERT was attempted. Epoch values become aware UTC
    datetimes (absolute instants survive server-tz conversion); anything else
    passes through untouched so existing datetime callers are unaffected.
    """
    if isinstance(value, datetime):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(value, tz=UTC)
    if isinstance(value, str) and value:
        try:
            # py3.11+ fromisoformat accepts the trailing 'Z' directly (FURB162).
            return datetime.fromisoformat(value)
        except ValueError:
            return value
    return value


def _failover_errors() -> tuple[type[BaseException], ...]:
    """Connection-level errors that justify retrying on another replica.

    ServerException is deliberately absent: a query error would fail on every
    replica, and retrying only burns time and obscures the real error.
    """
    errors: list[type[BaseException]] = [ConnectionError, TimeoutError, OSError, EOFError]
    # NB: EOFError covers accept-then-close handshake races (a port proxy whose
    # backend just died, or a server still starting up) — the driver surfaces
    # it raw from receive_hello(), not wrapped in NetworkError, and without it
    # the very write that is supposed to fail over would instead propagate.
    try:
        from clickhouse_driver import errors as ch_errors
    except ImportError:
        return tuple(errors)
    errors.extend([ch_errors.NetworkError, ch_errors.SocketTimeoutError])
    return tuple(errors)


def _validate_tenant_identifier(tenant_id: str) -> None:
    """Validate tenant identifier for use in ClickHouse identifiers.
    
    Raises ValueError if tenant_id contains invalid characters.
    """
    if not re.match(r"^[A-Za-z0-9_]+$", tenant_id):
        raise ValueError(
            f"Invalid tenant identifier: {tenant_id!r}. "
            "Only letters, numbers, and underscore are allowed."
        )


def _tenant_user_statements(
    tenant_id: str, password: str, db: str = "observability"
) -> list[str]:
    """Generate SQL statements for creating a per-tenant ClickHouse user and row policy.
    
    Args:
        tenant_id: Tenant identifier (validated by _validate_tenant_identifier)
        password: Password for the user (will be quoted by ClickHouse)
        db: Database name (default: observability)
    
    Returns:
        List of SQL statements to execute
    """
    _validate_tenant_identifier(tenant_id)
    
    user_name = f"ch_user_{tenant_id}"
    policy_name = f"{tenant_id}_select"
    
    return [
        f"CREATE USER IF NOT EXISTS {user_name} IDENTIFIED WITH sha256_password BY '{password}'",
        (
            f"CREATE ROW POLICY IF NOT EXISTS {policy_name} ON {db}.spans_hot "
            f"USING tenant_id = '{tenant_id}' FOR SELECT TO {user_name}"
        ),
        f"GRANT SELECT ON {db}.spans_hot TO {user_name}",
    ]


class _FailoverExecutor:
    """Execute-only proxy returned by :meth:`HotStore._get_client`.

    Presents the ``execute`` surface that every existing call site uses, but
    routes each call through the owning store's failover executor. Any other
    attribute (``disconnect``, driver internals) passes through to the raw
    client.
    """

    __slots__ = ("_store",)

    def __init__(self, store: HotStore) -> None:
        self._store = store

    def execute(self, *args: Any, **kwargs: Any) -> Any:
        return self._store._with_failover(lambda client: client.execute(*args, **kwargs))

    def __getattr__(self, name: str) -> Any:
        return getattr(self._store._raw_client(), name)


class HotStore:
    """ClickHouse client for hot-tier storage with connection pooling.

    Connection is lazily established on first call. Pool size and timeout
    are configurable via constructor args or environment variables.

    PP35: when a replica list is configured (``CLICKHOUSE_HOSTS`` or the
    ``hosts`` argument), connection-level failures trigger automatic failover
    to the next replica. All public read/write methods go through the
    failover executor; single-host setups behave exactly as before.
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
        hosts: list[str] | str | None = None,
        circuit_breaker: Any | None = None,
    ) -> None:
        if hosts is not None or (host is None and os.environ.get("CLICKHOUSE_HOSTS", "").strip()):
            self._replicas = _resolve_replicas(hosts, port)
        elif host is not None:
            self._replicas = [(host, port if port is not None else int(os.environ.get("CLICKHOUSE_PORT", "9000")))]
        else:
            self._replicas = _resolve_replicas(None, port)
        self._database = database or os.environ.get("CLICKHOUSE_DB", "observability")
        self._user = user or os.environ.get("CLICKHOUSE_USER", "observability_user")
        self._password = password or os.environ.get("CLICKHOUSE_PASSWORD", "observability_password")
        self._connect_timeout = connect_timeout
        self._send_receive_timeout = send_receive_timeout
        self._pool_size = pool_size
        self._client: Any | None = None
        self._active_idx = 0
        # Host:port the current ``_client`` is connected to. Set to the first
        # replica in __init__ so a client injected by tests (``store._client =
        # mock``) is treated as already being on the active replica.
        self._client_host: tuple[str, int] = self._replicas[0]
        self._dead_until: dict[int, float] = {}
        self._failover_errors = _failover_errors()
        # PP38: per-dependency circuit breaker. Once every replica has failed
        # ``failure_threshold`` operations in a row, further calls fail fast
        # with CircuitOpenError instead of burning connect timeouts.
        if circuit_breaker is None:
            from agent_obs.ha.health_checks import circuit_breaker_for

            circuit_breaker = circuit_breaker_for("ch")
        self._breaker = circuit_breaker

    def _connect(self, host: str, port: int) -> Any:
        """Create a raw clickhouse-driver client for one replica."""
        try:
            from clickhouse_driver import Client as CHClient
        except ImportError:
            raise RuntimeError(
                "clickhouse-driver is required for HotStore. "
                "Install with: pip install clickhouse-driver"
            ) from None
        client = CHClient(
            host=host,
            port=port,
            database=self._database,
            user=self._user,
            password=self._password,
            connect_timeout=self._connect_timeout,
            send_receive_timeout=self._send_receive_timeout,
            settings={"max_pool_size": self._pool_size},
        )
        logger.info(
            "ClickHouse connection established to %s:%d/%s",
            host,
            port,
            self._database,
        )
        return client

    def _get_client(self) -> _FailoverExecutor:
        """Lazy initialization of ClickHouse client with failover (PP35).

        Returns a thin executor proxy: existing call sites keep using
        ``client.execute(...)``, but every call is routed through
        :meth:`_with_failover`.
        """
        return _FailoverExecutor(self)

    def _raw_client(self) -> Any:
        if self._client is None:
            self._client = self._connect(*self._client_host)
        return self._client

    def _drop_client(self) -> None:
        if self._client is not None:
            try:
                self._client.disconnect()
            except Exception:  # noqa: BLE001, S110 — best-effort teardown, mirrors close()
                pass
            self._client = None

    def _select_replica(self, exclude: set[int] | None = None) -> int:
        """Pick the next replica to use, skipping cooled-down and excluded ones."""
        exclude = exclude or set()
        n = len(self._replicas)
        now = time.monotonic()
        # Prefer the active replica, then round-robin forward.
        for offset in range(n):
            idx = (self._active_idx + offset) % n
            if idx in exclude:
                continue
            if self._dead_until.get(idx, 0.0) > now:
                continue
            return idx
        # Everything is excluded or cooling down — take the next non-excluded
        # anyway (cooldown is a hint, not a hard fence), else the active one.
        for offset in range(n):
            idx = (self._active_idx + offset) % n
            if idx not in exclude:
                return idx
        return self._active_idx

    def _client_for(self, idx: int) -> Any:
        """Return a client connected to replica ``idx``, reconnecting if needed."""
        replica = self._replicas[idx]
        if self._client is None or replica != self._client_host:
            self._drop_client()
            self._client = self._connect(*replica)
            self._client_host = replica
        self._active_idx = idx
        return self._client

    def _mark_dead(self, idx: int) -> None:
        self._dead_until[idx] = time.monotonic() + REPLICA_COOLDOWN_SECONDS

    def _with_failover(self, fn: Callable[[Any], T]) -> T:
        """Run ``fn(client)`` against the active replica, failing over on
        connection-level errors (PP35).

        Each replica is tried at most once per call. If every replica fails,
        the last connection error propagates — callers see the same exception
        type they would have seen pre-PP35 from a single-host client.

        PP38: the call is gated by the ``ch`` circuit breaker. While the
        breaker is open (every replica failed ``failure_threshold``
        operations in a row) the call raises :class:`CircuitOpenError`
        immediately without touching any replica; the first request after
        the recovery window is admitted as a half-open probe.
        """
        from agent_obs.ha.health_checks import CircuitOpenError

        if not self._breaker.allow_request():
            raise CircuitOpenError(self._breaker.dependency)
        tried: set[int] = set()
        last_exc: BaseException | None = None
        for _ in range(len(self._replicas)):
            idx = self._select_replica(exclude=tried)
            tried.add(idx)
            try:
                result = fn(self._client_for(idx))
            except self._failover_errors as exc:
                last_exc = exc
                host, port = self._replicas[idx]
                self._mark_dead(idx)
                self._drop_client()
                remaining = len(self._replicas) - len(tried)
                logger.warning(
                    "ClickHouse operation failed on %s:%d (%s: %s) — %s",
                    host,
                    port,
                    type(exc).__name__,
                    exc,
                    f"failing over, {remaining} replica(s) left"
                    if remaining
                    else "no replicas left",
                )
                continue
            self._breaker.record_success()
            return result
        assert last_exc is not None  # loop always runs >= 1 iteration
        self._breaker.record_failure()
        raise last_exc

    def is_available(self) -> bool:
        """Check if ClickHouse is reachable (any replica, PP35)."""
        try:
            self._with_failover(lambda client: client.execute("SELECT 1"))
            return True
        except Exception:  # noqa: BLE001 — any failure means "not available"
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
            WHERE trace_id = %(trace_id)s
            ORDER BY eval_timestamp DESC
            """,
            {"trace_id": trace_id},
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
                    flags=row[9] if isinstance(row[9], list) else [],
                )
            )

        return results

    def write_tree_summary(self, trace_id: str, node_span_id: str, summary: str,
                           policy_version: str, subtree_size: int, total_cost: float) -> None:
        """Write a tree summary to the tree_summaries table.
        
        Args:
            trace_id: Trace ID
            node_span_id: Span ID of the compressed summary node
            summary: LLM-generated summary text
            policy_version: Compression policy version used
            subtree_size: Number of spans in the original subtree
            total_cost: Total cost of the original subtree
        """
        client = self._get_client()
        client.execute(
            """
            INSERT INTO tree_summaries
            (trace_id, node_span_id, summary, policy_version, subtree_size, total_cost)
            VALUES
            """,
            [(trace_id, node_span_id, summary, policy_version, subtree_size, total_cost)],
        )

    def get_tree_summaries(self, trace_id: str) -> list[dict]:
        """Retrieve all tree summaries for a trace from tree_summaries.
        
        Args:
            trace_id: Trace ID to query
            
        Returns:
            List of summary dicts with node_span_id, summary, policy_version, etc.
        """
        client = self._get_client()
        rows = client.execute(
            """
            SELECT trace_id, node_span_id, summary, policy_version, subtree_size, total_cost
            FROM tree_summaries
            WHERE trace_id = %(trace_id)s
            ORDER BY created_at DESC
            """,
            {"trace_id": trace_id},
        )
        
        summaries = []
        for row in rows:
            summaries.append({
                "trace_id": row[0],
                "node_span_id": row[1],
                "summary": row[2],
                "policy_version": row[3],
                "subtree_size": row[4],
                "total_cost": row[5],
            })
        
        return summaries

    def write_spans_batch(self, spans: list[dict]) -> None:
        """Write a batch of span dicts to spans_hot table.

        Includes response_embedding Array(Float32) for drift detection (PC24).
        In sharded setup, writes to Distributed table which routes to appropriate shard.
        NOTE (PC24): the primary embedding store is span_embeddings (sidecar);
        this method writes the embedding column for compatibility but it's not
        the main path for storing embeddings (they arrive asynchronously via
        ObservabilitySDK._embedding_clickhouse_write).
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
                    _to_datetime64(span.get("start_time", 0.0)),
                    _to_datetime64(span.get("end_time", 0.0)),
                    span.get("status", ""),
                    json.dumps(span.get("attributes", {})),
                    json.dumps(span.get("events", [])),
                    span.get("cost_usd", 0.0),
                    span.get("response_embedding", []),
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

    def get_spans(self, trace_id: str, tenant_id: str | None = None) -> list[dict]:
        """Retrieve all spans for a trace from spans_hot.
        
        Includes response_embedding Array(Float32) for drift detection (PC24).
        The primary embedding store is span_embeddings (sidecar), but this method
        returns the embedding field from spans_hot for compatibility.
        
        Args:
            trace_id: Trace ID to retrieve
            tenant_id: Optional tenant filter (PP32 tenant isolation)
        """
        client = self._get_client()
        
        query = """
            SELECT trace_id, span_id, parent_span_id, agent_id, tenant_id, name,
                   span_type, start_time, end_time, status, attributes, events,
                   cost_usd, response_embedding
            FROM spans_hot
            WHERE trace_id = %s
        """
        params = [trace_id]
        
        if tenant_id is not None:
            query += " AND tenant_id = %s"
            params.append(tenant_id)
        
        query += " ORDER BY start_time ASC"
        
        rows = client.execute(query, params)

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
                    "response_embedding": row[13] if len(row) > 13 else [],
                }
            )
        return spans

    def trace_exists(self, trace_id: str) -> bool:
        """Check if a trace exists in spans_hot (unscoped, for audit probing).
        
        Returns True if trace exists, False otherwise.
        PP32: Used for cross-tenant access detection when scoped queries return 0 rows.
        """
        client = self._get_client()
        rows = client.execute(
            "SELECT count() FROM spans_hot WHERE trace_id = %s",
            [trace_id],
        )
        return int(rows[0][0]) > 0

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

    def get_sampler_rate_history(
        self, from_ts: datetime, to_ts: datetime, limit: int = 1000
    ) -> list[dict]:
        """Read tail-sampler rate-change audit events within a time range (PC31).

        Returns rows from ``audit_events_hot`` where
        ``action = 'sampler.rate_change'``, ordered oldest-first so the Query
        API can present a chronological history for incident investigation.

        The sampler-specific numbers (prev/new rate, reasons, driving metrics)
        ride inside the ``resource`` JSON blob — this method flattens them into
        top-level keys so the API response matches
        :meth:`SamplerRateChangeAuditEvent.to_dict`.

        Parameters must be a dict for SELECT queries: clickhouse-driver treats
        a list as INSERT row data, so a list reaches the server with an
        unsubstituted ``%s`` (Code 62).
        """
        client = self._get_client()
        rows = client.execute(
            """
            SELECT audit_id, timestamp, trace_id, actor, action, decision,
                   resource, reason, ip_address, user_agent
            FROM audit_events_hot
            WHERE action = %(action)s
              AND timestamp >= %(from_ts)s
              AND timestamp <= %(to_ts)s
            ORDER BY timestamp ASC
            LIMIT %(limit)s
            """,
            {
                "action": "sampler.rate_change",
                "from_ts": from_ts,
                "to_ts": to_ts,
                "limit": limit,
            },
        )
        events: list[dict] = []
        for row in rows:
            resource = json.loads(row[6]) if isinstance(row[6], str) else row[6]
            if not isinstance(resource, dict):
                resource = {}
            event = self._audit_row_to_dict(row)
            event.update(
                {
                    "prev_rate": resource.get("prev_rate"),
                    "new_rate": resource.get("new_rate"),
                    "prev_reason": resource.get("prev_reason"),
                    "new_reason": resource.get("new_reason"),
                    "system_cpu_ratio": resource.get("system_cpu_ratio"),
                    "agent_error_rate_5m": resource.get("agent_error_rate_5m"),
                    "agent_id": resource.get("agent_id", "*"),
                    "triggering_agent_id": resource.get("triggering_agent_id", "*"),
                }
            )
            events.append(event)
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

    # --- PC34: Compliance catalog hot tier methods ---

    def upsert_compliance_catalog(self, rows: list[dict]) -> None:
        """Write compliance catalog rows to compliance_catalog_hot.
        
        Uses ReplacingMergeTree to deduplicate by (agent_id, tool, field, pii_type).
        Updates frequency, first_seen, last_seen, and updated_at on duplicate.
        """
        if not rows:
            return
        client = self._get_client()
        
        # Prepare batch of rows for ClickHouse
        clickhouse_rows = []
        for row in rows:
            clickhouse_rows.append((
                row["agent_id"],
                row["tool"],
                row["field"],
                row["pii_type"],
                row["frequency"],
                row["first_seen"],
                row["last_seen"],
            ))
        
        client.execute(
            """
            INSERT INTO compliance_catalog_hot
            (agent_id, tool, field, pii_type, frequency, first_seen, last_seen)
            VALUES
            """,
            clickhouse_rows,
        )
        logger.debug(
            "Compliance catalog hot upserted: rows=%d",
            len(rows),
        )

    def get_compliance_catalog_last_seen_cutoff(
        self, 
        cutoff: datetime, 
        limit: int = 10_000
    ) -> list[dict]:
        """Read compliance catalog rows with last_seen >= cutoff for warm migration.
        
        Returns dicts ordered by last_seen ASC for migration progress tracking.
        """
        client = self._get_client()
        rows = client.execute(
            """
            SELECT agent_id, tool, field, pii_type, frequency, first_seen, last_seen, updated_at
            FROM compliance_catalog_hot
            WHERE last_seen >= %(cutoff)s
            ORDER BY last_seen ASC
            LIMIT %(limit)s
            """,
            {"cutoff": cutoff, "limit": limit},
        )
        
        results: list[dict] = []
        for row in rows:
            results.append({
                "agent_id": row[0],
                "tool": row[1],
                "field": row[2],
                "pii_type": row[3],
                "frequency": row[4],
                "first_seen": row[5],
                "last_seen": row[6],
                "updated_at": row[7],
            })
        return results

    def get_compliance_catalog_all_batched(
        self, 
        batch_size: int = 1000,
        last_trace_id: str | None = None
    ) -> tuple[list[dict], str | None]:
        """Read compliance catalog rows in batches for migration.
        
        Uses keyset pagination to efficiently scan all rows.
        Returns (batch_rows, next_trace_id) where next_trace_id is None when done.
        
        Args:
            batch_size: Number of rows to return per batch
            last_trace_id: Trace ID from previous batch for pagination
            
        Returns:
            Tuple of (batch_rows, next_trace_id)
        """
        client = self._get_client()
        
        if last_trace_id is None:
            # First batch - get earliest rows
            rows = client.execute(
                """
                SELECT agent_id, tool, field, pii_type, frequency, first_seen, last_seen, updated_at
                FROM compliance_catalog_hot
                ORDER BY last_seen ASC, agent_id ASC, tool ASC, field ASC, pii_type ASC
                LIMIT %(limit)s
                """,
                {"limit": batch_size},
            )
        else:
            # Subsequent batches - use keyset pagination
            rows = client.execute(
                """
                SELECT agent_id, tool, field, pii_type, frequency, first_seen, last_seen, updated_at
                FROM compliance_catalog_hot
                WHERE (
                    last_seen > %(cutoff)s OR
                    (last_seen = %(cutoff)s AND agent_id > %(agent_id)s) OR
                    (last_seen = %(cutoff)s AND agent_id = %(agent_id)s AND tool > %(tool)s) OR
                    (last_seen = %(cutoff)s AND agent_id = %(agent_id)s AND tool = %(tool)s AND field > %(field)s) OR
                    (last_seen = %(cutoff)s AND agent_id = %(agent_id)s AND tool = %(tool)s AND field = %(field)s AND pii_type > %(pii_type)s)
                )
                ORDER BY last_seen ASC, agent_id ASC, tool ASC, field ASC, pii_type ASC
                LIMIT %(limit)s
                """,
                {
                    "cutoff": last_trace_id.split("|")[0] if "|" in last_trace_id else last_trace_id,
                    "agent_id": last_trace_id.split("|")[1] if "|" in last_trace_id else "",
                    "tool": last_trace_id.split("|")[2] if "|" in last_trace_id else "",
                    "field": last_trace_id.split("|")[3] if "|" in last_trace_id else "",
                    "pii_type": last_trace_id.split("|")[4] if "|" in last_trace_id else "",
                    "limit": batch_size,
                },
            )
        
        # Extract last values for pagination
        if not rows:
            return [], None
        
        results: list[dict] = []
        for row in rows:
            results.append({
                "agent_id": row[0],
                "tool": row[1],
                "field": row[2],
                "pii_type": row[3],
                "frequency": row[4],
                "first_seen": row[5],
                "last_seen": row[6],
                "updated_at": row[7],
            })
        
        # Get the last row's values for pagination
        last_row = rows[-1]
        next_trace_id = f"{last_row[6]}|{last_row[0]}|{last_row[1]}|{last_row[2]}|{last_row[3]}"
        
        return results, next_trace_id

    def get_compliance_catalog_all(self) -> list[dict]:
        """Read all compliance catalog rows from hot tier.
        
        Returns all rows ordered by last_seen DESC for testing/analysis.
        """
        client = self._get_client()
        rows = client.execute(
            """
            SELECT agent_id, tool, field, pii_type, frequency, first_seen, last_seen, updated_at
            FROM compliance_catalog_hot
            ORDER BY last_seen DESC
            """
        )
        
        results: list[dict] = []
        for row in rows:
            results.append({
                "agent_id": row[0],
                "tool": row[1],
                "field": row[2],
                "pii_type": row[3],
                "frequency": row[4],
                "first_seen": row[5],
                "last_seen": row[6],
                "updated_at": row[7],
            })
        return results

    def delete_compliance_catalog_older_than(self, cutoff: datetime) -> int:
        """Delete compliance catalog rows with last_seen < cutoff from hot tier.
        
        Only called after successful warm-tier upsert (ship-first-delete-second).
        
        Returns:
            Number of rows actually deleted.
        """
        client = self._get_client()
        
        # Count rows to be deleted first (ClickHouse doesn't support RETURNING)
        count_query = "SELECT count() FROM compliance_catalog_hot WHERE last_seen < %(cutoff)s"
        count_result = client.execute(count_query, {"cutoff": cutoff})
        deleted_count = int(count_result[0][0]) if count_result else 0
        
        if deleted_count > 0:
            # Delete old rows
            client.execute(
                "DELETE FROM compliance_catalog_hot WHERE last_seen < %(cutoff)s",
                {"cutoff": cutoff},
            )
            logger.info("compliance_catalog_hot purged: rows=%d", deleted_count)
        
        return deleted_count

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

    def write_eval_golden_results(self, rows: list[dict[str, Any]]) -> int:
        """Batch-insert nightly golden-eval rows into eval_golden_results (PP13).

        Each row is a dict with keys: nightly_ts (datetime), question_id,
        metric_name, expected_score, actual_score, pass (bool|int). Returns the
        number of rows written.
        """
        if not rows:
            return 0
        client = self._get_client()
        payload = [
            (
                row["nightly_ts"],
                row["question_id"],
                row["metric_name"],
                float(row["expected_score"]),
                float(row["actual_score"]),
                1 if row["pass"] else 0,
            )
            for row in rows
        ]
        client.execute(
            """
            INSERT INTO eval_golden_results
            (nightly_ts, question_id, metric_name, expected_score, actual_score, pass)
            VALUES
            """,
            payload,
        )
        logger.info("eval_golden_results written: rows=%d", len(payload))
        return len(payload)

    def get_sli_stats(
        self,
        sli_kind: str,
        window: timedelta,
        good_threshold: float | None = None,
        version: str | None = None,
    ) -> tuple[int, int]:
        """Aggregate (good_events, total_events) for an SLO SLI (PP25, PP59).

        Args:
            sli_kind: One of "availability" | "latency" | "quality" | "cost" |
                "latency_golden" | "traffic" | "errors" | "saturation".
                PP59 adds Google SRE Four Golden Signals: latency_golden (p99),
                traffic (request rate), errors (error rate), saturation (utilization).
            window: Rolling window length, measured back from now.
            good_threshold: Per-event good threshold — latency seconds for
                "latency"/"latency_golden", USD for "cost", faithfulness score for "quality".
                Unused for "availability" and "errors" (good = status != "error").
                For "traffic", this parameter is ignored (baseline-based calculation).
            version: Optional deploy version to scope the counts to (PP29).
                Matches the span attribute ``agent_version``, stamped into
                ``span.attributes`` by ``ObservabilitySDK._enqueue`` from
                ``AGENT_OBS_DEPLOY_VERSION`` (or the context's agent_version).
                ``None`` keeps the unversioned, whole-fleet query byte-for-byte
                identical to the pre-PP29 behaviour.

        Returns:
            (good_events, total_events). Empty window -> (0, 0); callers apply
            the empty-is-perfect convention via sli_from_counts().

        Note: the 30d SLO window exceeds hot-tier TTLs (spans 21d, evals 14d),
        so long-window results are a lower bound until warm rollups land (PP25).
        Golden signals use the same TTL constraints as the base SLOs.
        """
        cutoff = time.time() - window.total_seconds()
        params: dict = {"cutoff": cutoff}
        # Span-side version predicate (PP29): spans_hot has no version column,
        # so the deploy version rides in the attributes JSON. Constant on purpose
        # — the value is bound, never interpolated.
        version_predicate = ""
        if version is not None:
            version_predicate = (
                "AND JSONExtractString(attributes, 'agent_version') = %(version)s"
            )
            params["version"] = version

        if sli_kind in ("availability", "latency"):
            # "Trace" = root span (agent.loop). availability: good = non-error
            # status (blocked = deliberate refusal, counts as good). latency:
            # good = wall-clock duration <= good_threshold seconds.
            if sli_kind == "latency":
                if good_threshold is None:
                    raise ValueError("latency SLI requires good_threshold (seconds)")
                params["threshold"] = float(good_threshold)
                good_expr = "countIf(end_time - start_time <= %(threshold)s)"
            else:
                good_expr = "countIf(status != 'error')"
            rows = self._execute_clickhouse(
                f"""
                SELECT count(), {good_expr}
                FROM spans_hot
                WHERE span_type = 'agent.loop'
                  AND start_time >= toDateTime64(%(cutoff)s, 3)
                  {version_predicate}
                """,
                params,
            )
        elif sli_kind == "cost":
            if good_threshold is None:
                raise ValueError("cost SLI requires good_threshold (USD)")
            params["threshold"] = float(good_threshold)
            # Per-request cost = sum of cost_usd across all spans of a trace.
            rows = self._execute_clickhouse(
                f"""
                SELECT count(), countIf(trace_cost <= %(threshold)s)
                FROM (
                    SELECT trace_id, sum(cost_usd) AS trace_cost
                    FROM spans_hot
                    WHERE start_time >= toDateTime64(%(cutoff)s, 3)
                      {version_predicate}
                    GROUP BY trace_id
                )
                """,
                params,
            )
        elif sli_kind == "quality":
            if good_threshold is None:
                raise ValueError("quality SLI requires good_threshold (faithfulness score)")
            params["threshold"] = float(good_threshold)
            # Late-annotated LLM-judge scores, filtered by eval_timestamp.
            # eval_results_hot has no version column: scope it through the
            # traces it annotated (trace_id is its primary sort key).
            version_scope = ""
            if version is not None:
                version_scope = """
                AND trace_id IN (
                    SELECT trace_id
                    FROM spans_hot
                    WHERE start_time >= toDateTime64(%(cutoff)s, 3)
                      AND JSONExtractString(attributes, 'agent_version') = %(version)s
                )"""
            rows = self._execute_clickhouse(
                f"""
                SELECT count(), countIf(JSONExtractFloat(scores, 'faithfulness') > %(threshold)s)
                FROM eval_results_hot
                WHERE eval_timestamp >= toDateTime64(%(cutoff)s, 3)
                {version_scope}
                """,
                params,
            )
        elif sli_kind == "latency_golden":
            # PP59: Golden Signal latency - p99 latency <= 2 seconds
            if good_threshold is None:
                raise ValueError("latency_golden SLI requires good_threshold (seconds)")
            params["threshold"] = float(good_threshold)
            # Calculate p99 latency per trace, then count traces within threshold
            rows = self._execute_clickhouse(
                f"""
                SELECT count(), countIf(p99_latency <= %(threshold)s)
                FROM (
                    SELECT 
                        trace_id,
                        quantileExact(0.99)(end_time - start_time) AS p99_latency
                    FROM spans_hot
                    WHERE span_type = 'agent.loop'
                      AND start_time >= toDateTime64(%(cutoff)s, 3)
                      {version_predicate}
                    GROUP BY trace_id
                    HAVING p99_latency <= 60  -- Filter out traces with impossible latency
                )
                """,
                params,
            )
        elif sli_kind == "traffic":
            # PP59: Golden Signal traffic - within 20% of 7-day moving average
            # SLI is calculated as current_rate / baseline_rate, where baseline is 7-day moving average
            current_rows = self._execute_clickhouse(
                f"""
                SELECT count() as current_count
                FROM spans_hot
                WHERE span_type = 'agent.loop'
                  AND start_time >= toDateTime64(%(cutoff)s, 3)
                  {version_predicate}
                """,
                params,
            )
            if not current_rows:
                return (0, 0)
            current_count = int(current_rows[0][0])
            
            # Calculate 7-day moving average from historical data
            baseline_rows = self._execute_clickhouse(
                f"""
                SELECT count() as baseline_count
                FROM spans_hot
                WHERE span_type = 'agent.loop'
                  AND start_time >= toDateTime64(%(cutoff)s - 7*86400, 3)
                  AND start_time < toDateTime64(%(cutoff)s, 3)
                  {version_predicate}
                """,
                params,
            )
            if not baseline_rows:
                return (current_count, current_count)  # No baseline data, treat as perfect SLI
            
            baseline_count = int(baseline_rows[0][0])
            baseline_rate = baseline_count / 7.0  # 7-day moving average
            
            # SLI = current_rate / baseline_rate, capped at 1.0 for SLI calculation
            # Target is 1.20 (20% above baseline), so we count as "good" if current_rate <= 1.20 * baseline_rate
            target_multiplier = 1.20
            good_count = current_count if baseline_rate == 0 else min(current_count, int(baseline_rate * target_multiplier))
            
            return (good_count, current_count)
        elif sli_kind == "errors":
            # PP59: Golden Signal errors - share of requests without errors
            # Similar to availability but for all span types, not just root spans
            rows = self._execute_clickhouse(
                f"""
                SELECT count(), countIf(status != 'error')
                FROM spans_hot
                WHERE start_time >= toDateTime64(%(cutoff)s, 3)
                  {version_predicate}
                """,
                params,
            )
        elif sli_kind == "saturation":
            # PP59: Golden Signal saturation - system utilization <= 80%
            # Track concurrent spans as proxy for system utilization
            rows = self._execute_clickhouse(
                f"""
                SELECT count(), countIf(max_concurrent <= 0.8)
                FROM (
                    SELECT 
                        trace_id,
                        max(concurrent_spans) AS max_concurrent
                    FROM (
                        SELECT 
                            trace_id,
                            count() OVER (PARTITION BY trace_id ORDER BY start_time) AS concurrent_spans
                        FROM spans_hot
                        WHERE start_time >= toDateTime64(%(cutoff)s, 3)
                          {version_predicate}
                    )
                    GROUP BY trace_id
                )
                """,
                params,
            )
        else:
            raise ValueError(f"unknown sli_kind: {sli_kind!r}")

        if not rows:
            return (0, 0)
        total, good = int(rows[0][0]), int(rows[0][1])
        return (good, total)

    def get_tenant_cost_daily(self, start_ts: float) -> list[tuple[str, str, float]]:
        """Aggregate per-(tenant, agent) USD cost of spans since *start_ts* (PP42).

        Args:
            start_ts: Unix timestamp lower bound (inclusive) on ``start_time``,
                typically the start of the current UTC day.

        Returns:
            ``(tenant_id, agent_id, total_cost_usd)`` triples. Rows with an
            empty ``tenant_id`` are excluded (unattributed SDK spans must not
            pollute the proxy billing buckets).
        """
        rows = self._execute_clickhouse(
            """
            SELECT tenant_id, agent_id, sum(cost_usd) AS total
            FROM spans_hot
            WHERE start_time >= toDateTime64(%(start_ts)s, 3)
              AND tenant_id != ''
            GROUP BY tenant_id, agent_id
            """,
            {"start_ts": start_ts},
        )
        return [(str(r[0]), str(r[1]), float(r[2] or 0.0)) for r in rows]

    def create_tenant_user(self, tenant_id: str, password: str) -> None:
        """Create a ClickHouse user and row policy for a specific tenant.
        
        Creates ch_user_{tenant_id} with SELECT access only to rows where
        tenant_id = '{tenant_id}' on the spans_hot table.
        
        Args:
            tenant_id: Tenant identifier (must be alphanumeric/underscore)
            password: Password for the user
            
        Raises:
            ValueError: If tenant_id contains invalid characters
        """
        statements = _tenant_user_statements(tenant_id, password, self._database)
        for statement in statements:
            try:
                self._execute_clickhouse(statement, [])
                logger.info("Created tenant user and policy for tenant: %s", tenant_id)
            except Exception as e:
                logger.error("Failed to create tenant user for %s: %s", tenant_id, e)
                raise

    def close(self) -> None:
        """Close the ClickHouse connection and reset failover state."""
        self._drop_client()
        self._dead_until.clear()
        self._active_idx = 0
        self._client_host = self._replicas[0]
