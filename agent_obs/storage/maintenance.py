"""Tiered-retention maintenance jobs (PC21, ticket T2.3.5).

Three jobs, each run by the out-of-process cron service
(``scripts/cron/scheduler.py``) — never by the SDK in-process:

============================  =========================================  =========
Job                           What it does                               Cadence
============================  =========================================  =========
``cleanup_vault``             Finds PII vault entries past their TTL,      hourly
                              records a ``vault.lease_expired`` audit
                              event. Vault itself reclaims the secret.
``cleanup_audit_events``      audit_events older than 365d → S3 Parquet    daily
                              (cold), then purged from ClickHouse hot.
``cleanup_eval_results``      eval_results older than 14d → Postgres      daily
                              warm (structure only: no ``reasoning``),
                              then purged from ClickHouse hot.
============================  =========================================  =========

Ordering rule for both archival jobs: **ship first, delete second.** If the
upload or the warm upsert fails, the hot rows are left untouched and the batch
is retried on the next cycle, so no data is ever lost by a partial failure.

Retention windows come from the ClickHouse DDL (``infra/storage/clickhouse_ddl.sql``):
``audit_events_hot`` has ``TTL 365 DAY``, ``eval_results_hot`` has ``TTL 14 DAY``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from agent_obs.metrics import (
    cron_duration_seconds,
    cron_errors_total,
    cron_last_success_timestamp_seconds,
    cron_rows_deleted_total,
    cron_runs_total,
)

logger = logging.getLogger(__name__)

JOB_CLEANUP_VAULT = "cleanup_vault"
JOB_CLEANUP_AUDIT_EVENTS = "cleanup_audit_events"
JOB_CLEANUP_EVAL_RESULTS = "cleanup_eval_results"
JOB_MIGRATE_SPANS = "migrate_spans_to_warm"
JOB_MIGRATE_TRACES = "migrate_traces_to_cold"
JOB_DRIFT_DETECTION = "drift_detection"
JOB_CALIBRATE_DRIFT_THRESHOLD = "calibrate_drift_threshold"
JOB_EXPORT_EMBEDDINGS = "export_embeddings_to_phoenix"
JOB_AGGREGATE_COMPLIANCE_CATALOG = "aggregate_compliance_catalog"

DEFAULT_AUDIT_RETENTION_DAYS = 90
DEFAULT_EVAL_RETENTION_DAYS = 7
DEFAULT_SPAN_RETENTION_DAYS = 7
DEFAULT_BATCH_SIZE = 10_000
DEFAULT_MAX_EVENTS = 1000
VAULT_PREFIX = "pii/"


def _user_hash(user_id: str) -> str:
    """Generate a pseudonymous hash for a user identifier.
    
    Uses SHA-256 for consistency with input_sha256/output_sha256. The hash is
    not reversible; the same user_id always produces the same hash. No salt by
    default to preserve cross-deploy aggregation, but can be HMAC'd with
    AGENT_OBS_USER_HASH_SALT if set.
    """
    if not user_id:
        return ""
    salt = os.environ.get("AGENT_OBS_USER_HASH_SALT", "")
    if salt:
        key = f"{salt}{user_id}".encode()
    else:
        key = user_id.encode()
    return hashlib.sha256(key).hexdigest()


@dataclass
class VaultCleanupResult:
    """Outcome of one ``cleanup_vault`` run."""

    scanned: int = 0
    expired: int = 0
    audited: int = 0
    deleted: int = 0
    errors: int = 0

    @property
    def affected(self) -> int:
        """Rows the job changed — drives ``cron_rows_deleted_total``."""
        return self.audited + self.deleted


@dataclass
class AuditArchiveResult:
    """Outcome of one ``cleanup_audit_events`` run."""

    scanned: int = 0
    archived: int = 0
    deleted: int = 0
    bytes_written: int = 0
    objects: list[str] = field(default_factory=list)
    errors: int = 0

    @property
    def affected(self) -> int:
        return self.archived


@dataclass
class EvalWarmResult:
    """Outcome of one ``cleanup_eval_results`` run."""

    scanned: int = 0
    migrated: int = 0
    deleted: int = 0
    errors: int = 0

    @property
    def affected(self) -> int:
        return self.migrated


@dataclass
class TraceWarmResult:
    """Outcome of one ``migrate_spans_to_warm`` run (PC22)."""

    scanned_spans: int = 0
    traces_migrated: int = 0
    traces_deleted: int = 0
    errors: int = 0

    @property
    def affected(self) -> int:
        """Rows the job changed — drives ``cron_rows_deleted_total``."""
        return self.traces_migrated


@dataclass
class TraceColdResult:
    """Outcome of one ``migrate_traces_to_cold`` run (PC23)."""

    partitions: int = 0
    files: int = 0
    rows: int = 0
    bytes_written: int = 0
    traces_deleted: int = 0
    errors: int = 0

    @property
    def affected(self) -> int:
        """Rows the job changed — drives ``cron_rows_deleted_total``."""
        return self.traces_deleted


# ---------------------------------------------------------------------------
# Metrics wrapper
# ---------------------------------------------------------------------------


def _job_started(job_name: str) -> float:
    """Count a run and start its timer."""
    cron_runs_total.labels(job_name=job_name).inc()
    return time.monotonic()


def _job_succeeded(job_name: str, result: Any, started: float) -> None:
    """Record duration, row count and the success timestamp for a finished run."""
    cron_duration_seconds.labels(job_name=job_name).observe(time.monotonic() - started)

    affected = getattr(result, "affected", None)
    if isinstance(affected, int):
        if affected:
            cron_rows_deleted_total.labels(job_name=job_name).inc(affected)
        errors = getattr(result, "errors", 0)
        if errors:
            cron_errors_total.labels(job_name=job_name).inc(errors)

    cron_last_success_timestamp_seconds.labels(job_name=job_name).set(time.time())


def _job_failed(job_name: str, exc: BaseException) -> None:
    """Count a failed run. The success gauge is deliberately left untouched —
    that is exactly what the "more than 2 missed cycles" alert reads."""
    cron_errors_total.labels(job_name=job_name).inc()
    logger.exception("cron job %s failed: %s", job_name, str(exc))


def run_cron_job(job_name: str, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Run one synchronous maintenance job with metrics and error accounting.

    Never raises: a failing job must not kill the scheduler loop, otherwise a
    transient ClickHouse blip would silently stop every subsequent cycle. The
    failure is logged, counted in ``cron_errors_total`` and returned as
    ``None``.
    """
    started = _job_started(job_name)
    try:
        result = fn(*args, **kwargs)
    except Exception as e:
        _job_failed(job_name, e)
        return None

    _job_succeeded(job_name, result, started)
    logger.info("cron job %s ok: %s", job_name, result)
    return result


async def run_cron_job_async(job_name: str, coro_fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Async twin of :func:`run_cron_job` for the asyncpg-backed warm migration.

    Same never-raise contract: a failed batch is counted and reported as
    ``None`` so the scheduler loop keeps going.
    """
    started = _job_started(job_name)
    try:
        result = await coro_fn(*args, **kwargs)
    except Exception as e:
        _job_failed(job_name, e)
        return None

    _job_succeeded(job_name, result, started)
    logger.info("cron job %s ok: %s", job_name, result)
    return result


# ---------------------------------------------------------------------------
# Job 1: vault TTL cleanup
# ---------------------------------------------------------------------------


def list_kv_keys_recursive(client: Any, path: str, mount_point: str = "secret") -> list[str]:
    """List every secret key under ``path``, descending nested KV v2 paths.

    ``VaultClient.store()`` writes to ``pii/{hex8}/{timestamp}``, so the tree is
    two levels deep: ``list_secrets("pii")`` yields the ``{hex8}`` segments and
    each of those yields the timestamps.
    """
    keys: list[str] = []
    try:
        listing = client.secrets.kv.v2.list_secrets(path=path, mount_point=mount_point)
    except Exception as e:
        logger.debug("vault list_secrets(%s) failed: %s", path, str(e))
        return keys

    for key in (listing or {}).get("data", {}).get("keys", []) or []:
        child = f"{path.strip('/')}/{key}"
        if key.endswith("/"):
            keys.extend(list_kv_keys_recursive(client, child, mount_point))
        else:
            keys.append(child)
    return keys


def _as_utc(value: Any) -> datetime:
    """Coerce a ClickHouse timestamp column to a timezone-aware UTC datetime."""
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _kv_entry_expiry(client: Any, key: str, mount_point: str = "secret") -> Optional[float]:
    """Return the expiry unix timestamp of a KV v2 entry, or None if unreadable.

    Uses the ``created_at`` / ``ttl_seconds`` fields written by
    ``VaultClient.store()``, i.e. exactly the rule ``recover()`` enforces.
    """
    try:
        response = client.secrets.kv.v2.read_secret_version(
            path=key, mount_point=mount_point
        )
        data = response["data"]["data"]
        return float(data["created_at"]) + float(data.get("ttl_seconds", 86400))
    except Exception as e:
        logger.debug("vault read_secret_version(%s) failed: %s", key, str(e))
        return None


def _lease_expiry(client: Any, lease_id: str) -> Optional[float]:
    """Return the ``expire_time`` of a Vault lease as a unix timestamp."""
    try:
        response = client.sys.leases.read_lease(lease_id=lease_id)
    except Exception as e:
        logger.debug("vault read_lease(%s) failed: %s", lease_id, str(e))
        return None

    lease = (response or {}).get("data") or {}
    if not lease:
        return None
    raw = lease.get("expire_time")
    if raw is None:
        return None
    try:
        if isinstance(raw, datetime):
            return raw.replace(tzinfo=timezone.utc).timestamp()
        return float(raw)
    except (TypeError, ValueError):
        return None


def cleanup_vault_expired(
    hot_store: Any,
    vault_addr: str | None = None,
    vault_token: str | None = None,
    vault_namespace: str = "",
    verify_tls: bool = False,
    prefix: str = VAULT_PREFIX,
    max_events: int = DEFAULT_MAX_EVENTS,
    delete_expired: bool = False,
    dry_run: bool = False,
) -> VaultCleanupResult:
    """Find PII vault entries past their TTL and record an audit event for each.

    Two sources are consulted, because ``VaultClient.store()`` writes to the KV
    v2 engine which registers **no** Vault lease, so ``sys/leases`` alone would
    always come up empty for the current layout:

    1. ``secret/metadata/pii`` LIST, with expiry computed from the entry's own
       ``created_at + ttl_seconds`` (source ``kv_metadata``);
    2. ``sys/leases`` LIST, with the lease's own ``expire_time`` (source
       ``lease``) — this catches any genuinely leased secret.

    The job does **not** delete anything by default: per PC21 the secret itself
    is reclaimed by Vault on TTL expiry. Set ``delete_expired=True`` to also
    purge the KV metadata of expired entries (opt-in, because KV v2 has no
    server-side TTL and would otherwise never be reclaimed).

    Parameters
    ----------
    hot_store:
        ``HotStore`` (or any object with ``write_audit_events``) receiving the
        ``vault.lease_expired`` events.
    prefix:
        KV v2 sub-path to scan.
    max_events:
        Hard cap on audit events written per run, so a large backlog cannot
        produce a single unbounded batch.
    dry_run:
        Detect and count, but write no audit events and delete nothing.
    """
    import hvac

    from agent_obs.guardrail.audit import LeaseExpiryAuditEvent

    result = VaultCleanupResult()
    now = time.time()

    client = hvac.Client(
        url=vault_addr or os.environ.get("VAULT_ADDR", "http://localhost:8200"),
        token=vault_token if vault_token is not None else os.environ.get("VAULT_TOKEN", ""),
        namespace=vault_namespace or os.environ.get("VAULT_NAMESPACE", "") or None,
        verify=verify_tls,
    )

    expired: dict[str, tuple[float, str]] = {}

    # --- Source 1: KV v2 metadata -------------------------------------------
    for key in list_kv_keys_recursive(client, prefix.strip("/")):
        if result.scanned >= max_events:
            logger.warning("cleanup_vault: max_events=%d reached, stopping scan", max_events)
            break
        result.scanned += 1
        expires_at = _kv_entry_expiry(client, key)
        if expires_at is None:
            result.errors += 1
            continue
        if expires_at <= now:
            expired[key] = (expires_at, "kv_metadata")

    # --- Source 2: Vault leases ---------------------------------------------
    try:
        listing = client.sys.leases.list_leases(prefix=prefix) or {}
        lease_ids = listing.get("data", {}).get("keys", []) or []
    except Exception as e:
        logger.warning("cleanup_vault: sys.leases.list_leases failed: %s", str(e))
        lease_ids = []

    for lease_id in lease_ids:
        if result.scanned >= max_events:
            break
        result.scanned += 1
        expires_at = _lease_expiry(client, lease_id)
        if expires_at is None:
            result.errors += 1
            continue
        if expires_at <= now:
            expired.setdefault(lease_id, (expires_at, "lease"))

    result.expired = len(expired)
    if not expired:
        logger.info("cleanup_vault: no expired entries among %d scanned", result.scanned)
        return result

    events = [
        LeaseExpiryAuditEvent.for_expired_lease(key, expires_at, source=source)
        for key, (expires_at, source) in expired.items()
    ]

    if dry_run:
        logger.info("cleanup_vault: dry-run, %d expired entries detected", len(events))
        result.audited = 0
        return result

    try:
        hot_store.write_audit_events(events)
    except Exception as e:
        logger.error("cleanup_vault: audit batch write failed, nothing deleted: %s", str(e))
        result.errors += 1
        return result

    result.audited = len(events)

    if delete_expired:
        for key, (_, source) in expired.items():
            if source != "kv_metadata":
                continue
            try:
                client.secrets.kv.v2.delete_metadata_and_all_versions(path=key)
                result.deleted += 1
            except Exception as e:
                logger.warning("cleanup_vault: delete %s failed: %s", key, str(e))
                result.errors += 1

    logger.info(
        "cleanup_vault: scanned=%d expired=%d audited=%d deleted=%d errors=%d",
        result.scanned, result.expired, result.audited, result.deleted, result.errors,
    )
    return result


# ---------------------------------------------------------------------------
# Job 2: audit_events 365d -> S3 Parquet (cold)
# ---------------------------------------------------------------------------

AUDIT_ARCHIVE_SCHEMA_FIELDS = (
    "audit_id",
    "timestamp",
    "trace_id",
    "actor",
    "action",
    "decision",
    "resource",
    "reason",
    "ip_address",
    "user_agent",
)


def _audit_rows_to_table(rows: list[dict]) -> Any:
    """Build a ``pyarrow.Table`` mirroring the ``audit_events_hot`` column set."""
    import json as _json

    import pyarrow as pa

    return pa.table(
        {
            "audit_id": pa.array([r["audit_id"] for r in rows], pa.string()),
            "timestamp": pa.array(
                [
                    r["timestamp"] if isinstance(r["timestamp"], datetime) else datetime.fromisoformat(str(r["timestamp"]))
                    for r in rows
                ],
                pa.timestamp("ms", tz="UTC"),
            ),
            "trace_id": pa.array([r.get("trace_id", "") for r in rows], pa.string()),
            "actor": pa.array(
                [r["actor"] if isinstance(r["actor"], str) else _json.dumps(r.get("actor", {})) for r in rows],
                pa.string(),
            ),
            "action": pa.array([r.get("action", "") for r in rows], pa.string()),
            "decision": pa.array([r.get("decision", "") for r in rows], pa.string()),
            "resource": pa.array(
                [r["resource"] if isinstance(r["resource"], str) else _json.dumps(r.get("resource", {})) for r in rows],
                pa.string(),
            ),
            "reason": pa.array([r.get("reason", "") for r in rows], pa.string()),
            "ip_address": pa.array([r.get("ip_address", "") for r in rows], pa.string()),
            "user_agent": pa.array([r.get("user_agent", "") for r in rows], pa.string()),
        }
    )


def archive_expired_audit_events(
    hot_store: Any,
    cold_store: Any,
    retention_days: int = DEFAULT_AUDIT_RETENTION_DAYS,
    batch_size: int = DEFAULT_BATCH_SIZE,
    max_batches: int = 100,
    dry_run: bool = False,
) -> AuditArchiveResult:
    """Archive audit events past ``retention_days`` to S3 Parquet, then purge hot.

    The Parquet key is derived from the batch's own oldest row plus the run
    timestamp, so re-running the same batch overwrites the object rather than
    appending a duplicate.

    §3.4 ARCHITECT.md requires a minimum 1-year audit retention, so the cold
    bucket's lifecycle must not expire sooner — see
    ``infra/storage/s3_lifecycle.json``.
    """
    result = AuditArchiveResult()
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)

    for _ in range(max_batches):
        rows = hot_store.get_audit_events_older_than(cutoff, limit=batch_size)
        if not rows:
            break
        result.scanned += len(rows)

        table = _audit_rows_to_table(rows)
        oldest = _as_utc(min(r["timestamp"] for r in rows))
        run_ts = int(oldest.timestamp())
        key = f"year={oldest:%Y}/month={oldest:%m}/day={oldest:%d}/part-{run_ts}.parquet"

        if dry_run:
            # One batch is enough to size the job; looping would re-read the same rows.
            result.archived += len(rows)
            logger.info("cleanup_audit_events: dry-run, would archive %d rows to %s", len(rows), key)
            break

        try:
            result.bytes_written += cold_store.write_parquet(key, table)
        except Exception as e:
            logger.error("cleanup_audit_events: upload of %s failed, keeping hot rows: %s", key, str(e))
            result.errors += 1
            break

        deleted = hot_store.delete_audit_events([r["audit_id"] for r in rows])
        result.archived += len(rows)
        result.deleted += deleted
        result.objects.append(key)

        if len(rows) < batch_size:
            break

    logger.info(
        "cleanup_audit_events: scanned=%d archived=%d deleted=%d bytes=%d objects=%d errors=%d",
        result.scanned, result.archived, result.deleted,
        result.bytes_written, len(result.objects), result.errors,
    )
    return result


# ---------------------------------------------------------------------------
# Job 3: eval_results 14d -> Postgres warm
# ---------------------------------------------------------------------------


async def migrate_eval_results_to_warm(
    hot_store: Any,
    warm_store: Any,
    retention_days: int = DEFAULT_EVAL_RETENTION_DAYS,
    batch_size: int = DEFAULT_BATCH_SIZE,
    max_batches: int = 100,
    dry_run: bool = False,
) -> EvalWarmResult:
    """Move eval results past ``retention_days`` to warm tier, then purge hot.

    Structure-only reduction: ``reasoning`` (the LLM judge's prose) is dropped
    by ``WarmStore`` and the warm table has no such column, so only ``scores``,
    ``flags`` and the timestamp survive.
    """
    result = EvalWarmResult()
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)

    for _ in range(max_batches):
        rows = hot_store.get_eval_results_older_than(cutoff, limit=batch_size)
        if not rows:
            break
        result.scanned += len(rows)

        if dry_run:
            result.migrated += len(rows)
            logger.info("cleanup_eval_results: dry-run, would migrate %d rows", len(rows))
            break

        try:
            await warm_store.insert_eval_results(rows)
        except Exception as e:
            logger.error("cleanup_eval_results: warm upsert failed, keeping hot rows: %s", str(e))
            result.errors += 1
            break

        keys = [
            (r["trace_id"], r["eval_id"], r["eval_name"], r["eval_timestamp"])
            for r in rows
        ]
        result.deleted += hot_store.delete_eval_results(keys)
        result.migrated += len(rows)

        if len(rows) < batch_size:
            break

    logger.info(
        "cleanup_eval_results: scanned=%d migrated=%d deleted=%d errors=%d",
        result.scanned, result.migrated, result.deleted, result.errors,
    )
    return result


# ---------------------------------------------------------------------------
# Job 4: spans_hot 14d -> Postgres traces_warm (PC22, T2.6.4)
# ---------------------------------------------------------------------------

def _aggregate_spans_by_trace(spans: list[dict]) -> dict[str, dict]:
    """Group raw spans into trace-level aggregates for ``traces_warm``.

    Each trace produces exactly one row with:
    - ``cost_usd_total`` = sum of ``cost_usd`` across spans
    - ``span_count`` = number of spans
    - ``error_count`` = spans whose ``status`` is not ``"ok"``
    - ``input_chars`` / ``input_sha256`` / ``output_chars`` / ``output_sha256``
      extracted from span attributes (SDK pre-computes these)
    - ``user_hash`` = pseudonymous identifier from the root span's ``user_id``
      attribute (for cold-tier distinct-user counts)
    - ``eval_avg`` left empty (eval scores are migrated separately)
    - ``start_time`` / ``end_time`` = earliest / latest span in the trace
    """
    traces: dict[str, dict] = {}
    for span in spans:
        tid = span["trace_id"]
        if tid not in traces:
            attrs = span.get("attributes") or {}
            traces[tid] = {
                "trace_id": tid,
                "tenant_id": span.get("tenant_id", ""),
                "agent_id": span.get("agent_id", ""),
                "start_time": span["start_time"],
                "end_time": span["end_time"],
                "status": span.get("status", ""),
                "cost_usd_total": 0.0,
                "span_count": 0,
                "error_count": 0,
                "eval_avg": {},
                "input_chars": attrs.get("llm.input_chars", 0),
                "input_sha256": attrs.get("llm.input_sha256", ""),
                "output_chars": attrs.get("llm.output_chars", 0),
                "output_sha256": attrs.get("llm.output_sha256", ""),
                "user_hash": _user_hash(attrs.get("user_id", "")),
            }
        t = traces[tid]
        t["cost_usd_total"] += span.get("cost_usd", 0.0)
        t["span_count"] += 1
        if span.get("status") != "ok":
            t["error_count"] += 1
        if span["start_time"] < t["start_time"]:
            t["start_time"] = span["start_time"]
        if span["end_time"] > t["end_time"]:
            t["end_time"] = span["end_time"]
        # Merge compressed metadata from the root span (agent.loop) if present
        attrs = span.get("attributes") or {}
        for key in ("llm.input_chars", "llm.input_sha256", "llm.output_chars", "llm.output_sha256"):
            val = attrs.get(key)
            if val:
                t[key] = val
    return traces


async def migrate_spans_to_warm(
    hot_store: Any,
    warm_store: Any,
    retention_days: int = DEFAULT_SPAN_RETENTION_DAYS,
    batch_size: int = DEFAULT_BATCH_SIZE,
    max_batches: int = 100,
    dry_run: bool = False,
) -> TraceWarmResult:
    """Move spans past ``retention_days`` to warm tier, then purge hot.

    PC22: each batch of raw spans is aggregated by ``trace_id`` into a single
    ``traces_warm`` row with content compression (``llm.input_text`` /
    ``llm.output_text`` dropped; only char-counts and sha256 hashes survive).

    Ordering rule: **ship first, delete second.** If the warm upsert fails, the
    hot rows are left untouched and retried on the next cycle.
    """
    result = TraceWarmResult()
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)

    for _ in range(max_batches):
        spans = hot_store.get_spans_older_than(cutoff, limit=batch_size)
        if not spans:
            break
        result.scanned_spans += len(spans)

        traces = _aggregate_spans_by_trace(spans)

        if dry_run:
            result.traces_migrated += len(traces)
            logger.info(
                "migrate_spans_to_warm: dry-run, would migrate %d traces from %d spans",
                len(traces), len(spans),
            )
            break

        try:
            await warm_store.insert_traces(list(traces.values()))
        except Exception as e:
            logger.error("migrate_spans_to_warm: warm upsert failed, keeping hot rows: %s", str(e))
            result.errors += 1
            break

        trace_ids = list(traces.keys())
        result.traces_deleted += hot_store.delete_spans(trace_ids)
        result.traces_migrated += len(traces)

        if len(spans) < batch_size:
            break

    logger.info(
        "migrate_spans_to_warm: scanned_spans=%d traces_migrated=%d traces_deleted=%d errors=%d",
        result.scanned_spans, result.traces_migrated, result.traces_deleted, result.errors,
    )
    return result


async def migrate_traces_to_cold(
    warm_store: Any,
    cold_store: Any,
    retention_days: int = DEFAULT_SPAN_RETENTION_DAYS,
    max_partition_rows: int = 200_000,
    dry_run: bool = False,
) -> TraceColdResult:
    """Move traces past ``retention_days`` to cold tier, then purge warm.

    PC23: each (tenant_id, day) partition is aggregated into Parquet with
    traces_count, cost_usd_sum, eval_avg (struct), users_count, error_rate.
    Idempotent by partition key: the prefix is wiped before writing.

    Ordering rule: **ship first, delete second.** If the S3 upload fails, the
    warm rows are left untouched and retried on the next cycle.
    """
    import pyarrow as pa
    from agent_obs.storage.cold import parq_key

    result = TraceColdResult()
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)

    # List candidate partitions (tenant_id, day)
    partitions = await warm_store.list_trace_partitions(cutoff)
    if not partitions:
        logger.info("migrate_traces_to_cold: no partitions older than %d days", retention_days)
        return result

    result.partitions = len(partitions)
    logger.info("migrate_traces_to_cold: processing %d partitions", result.partitions)

    for tenant_id, day in partitions:
        logger.debug("migrate_traces_to_cold: processing tenant=%s day=%s", tenant_id, day)

        # Keyset pagination: fetch the partition in chunks
        last_trace_id = ""
        partition_rows = 0
        while True:
            rows = await warm_store.get_traces_for_partition(
                tenant_id, day, last_trace_id, max_partition_rows
            )
            if not rows:
                break

            # Aggregate this chunk by agent_id
            chunk_aggregates = {}
            for row in rows:
                aid = row["agent_id"]
                if aid not in chunk_aggregates:
                    chunk_aggregates[aid] = {
                        "tenant_id": tenant_id,
                        "agent_id": aid,
                        "day": day,
                        "traces_count": 0,
                        "cost_usd_sum": 0.0,
                        "eval_avg": {},
                        "users_count": 0,
                        "user_hashes": set(),
                        "error_count": 0,
                        "span_count": 0,
                    }
                agg = chunk_aggregates[aid]
                agg["traces_count"] += 1
                agg["cost_usd_sum"] += row.get("cost_usd_total", 0.0)
                if row.get("eval_avg"):
                    for key, val in row["eval_avg"].items():
                        agg["eval_avg"][key] = agg["eval_avg"].get(key, 0.0) + val
                user_hash = row.get("user_hash", "")
                if user_hash:
                    agg["user_hashes"].add(user_hash)
                agg["error_count"] += row.get("error_count", 0)
                agg["span_count"] += row.get("span_count", 0)

                last_trace_id = row["trace_id"]
                partition_rows += 1

            # Build Parquet table for this chunk
            table_data = {
                "tenant_id": [],
                "agent_id": [],
                "day": [],
                "traces_count": [],
                "cost_usd_sum": [],
                "eval_avg_faithfulness": [],
                "eval_avg_relevancy": [],
                "eval_avg_completeness": [],
                "users_count": [],
                "error_rate": [],
            }
            for agg in chunk_aggregates.values():
                table_data["tenant_id"].append(agg["tenant_id"])
                table_data["agent_id"].append(agg["agent_id"])
                table_data["day"].append(agg["day"])
                table_data["traces_count"].append(agg["traces_count"])
                table_data["cost_usd_sum"].append(agg["cost_usd_sum"])
                
                # eval_avg struct with mapping: answer_relevancy -> relevancy
                eval_avg = agg["eval_avg"]
                table_data["eval_avg_faithfulness"].append(eval_avg.get("faithfulness", 0.0))
                table_data["eval_avg_relevancy"].append(eval_avg.get("answer_relevancy", eval_avg.get("relevancy", 0.0)))
                table_data["eval_avg_completeness"].append(eval_avg.get("completeness", 0.0))
                
                table_data["users_count"].append(len(agg["user_hashes"]))
                error_rate = agg["error_count"] / agg["span_count"] if agg["span_count"] > 0 else 0.0
                table_data["error_rate"].append(error_rate)

            table = pa.table(table_data)

            # Write to S3
            prefix = f"tenant_id={tenant_id}/year={day.year}/month={day.month}/day={day.day}"
            if dry_run:
                logger.info("migrate_traces_to_cold: dry-run, would write %s/part-0000.parquet", prefix)
                result.files += 1
                result.rows += len(rows)
            else:
                try:
                    # Wipe prefix first for idempotency
                    cold_store.delete_prefix(prefix)
                    key = parq_key(prefix, str(day), int(time.time()))
                    bytes_written = cold_store.write_parquet(key, table)
                    result.files += 1
                    result.bytes_written += bytes_written
                    result.rows += len(rows)
                    logger.debug("migrate_traces_to_cold: wrote %s bytes to %s", bytes_written, key)
                except Exception as e:
                    logger.error("migrate_traces_to_cold: upload of %s failed, keeping warm rows: %s", prefix, str(e))
                    result.errors += 1
                    break

            # Exit loop if this chunk was smaller than the limit (no more rows)
            if len(rows) < max_partition_rows:
                break

        # Delete all traces from this partition if successful
        if not dry_run and result.errors == 0:
            try:
                # Fetch all trace_ids in this partition to delete
                all_rows = []
                last_trace_id = ""
                while True:
                    rows = await warm_store.get_traces_for_partition(
                        tenant_id, day, last_trace_id, max_partition_rows
                    )
                    if not rows:
                        break
                    all_rows.extend(rows)
                    last_trace_id = rows[-1]["trace_id"]
                
                if all_rows:
                    trace_ids = [row["trace_id"] for row in all_rows]
                    deleted = await warm_store.delete_traces(trace_ids)
                    result.traces_deleted += deleted
                    logger.info("migrate_traces_to_cold: deleted %d traces from %s", deleted, prefix)
            except Exception as e:
                logger.error("migrate_traces_to_cold: delete of %s failed: %s", prefix, str(e))
                result.errors += 1

    logger.info(
        "migrate_traces_to_cold: partitions=%d files=%d rows=%d bytes=%d deleted=%d errors=%d",
        result.partitions, result.files, result.rows, result.bytes_written,
        result.traces_deleted, result.errors,
    )
    return result


def build_hot_store() -> Any:
    """Construct a ``HotStore`` from the standard ClickHouse env variables."""
    from agent_obs.storage.hot import HotStore

    return HotStore()


def build_cold_store() -> Any:
    """Construct a ``ColdStore`` from the standard S3 env variables."""
    from agent_obs.storage.cold import ColdStore

    return ColdStore()


def build_cold_trace_store() -> Any:
    """Construct a ``ColdStore`` for cold-tier trace aggregates.

    Uses the S3_COLD_BUCKET environment variable (default cold-traces) instead
    of the default audit-events bucket.
    """
    from agent_obs.storage.cold import ColdStore

    bucket = os.environ.get("S3_COLD_BUCKET", "cold-traces")
    return ColdStore(bucket=bucket)


def build_warm_store() -> Any:
    """Construct a ``WarmStore`` from the standard warm Postgres env variables."""
    from agent_obs.storage.warm import WarmStore

    return WarmStore()


__all__ = [
    "AUDIT_ARCHIVE_SCHEMA_FIELDS",
    "DEFAULT_AUDIT_RETENTION_DAYS",
    "DEFAULT_BATCH_SIZE",
    "DEFAULT_EVAL_RETENTION_DAYS",
    "DEFAULT_MAX_EVENTS",
    "DEFAULT_SPAN_RETENTION_DAYS",
    "JOB_CALIBRATE_DRIFT_THRESHOLD",
    "JOB_CLEANUP_AUDIT_EVENTS",
    "JOB_CLEANUP_EVAL_RESULTS",
    "JOB_CLEANUP_VAULT",
    "JOB_DRIFT_DETECTION",
    "JOB_MIGRATE_SPANS",
    "JOB_MIGRATE_TRACES",
    "VAULT_PREFIX",
    "AuditArchiveResult",
    "EvalWarmResult",
    "TraceWarmResult",
    "TraceColdResult",
    "VaultCleanupResult",
    "archive_expired_audit_events",
    "build_cold_store",
    "build_cold_trace_store",
    "build_hot_store",
    "build_warm_store",
    "cleanup_vault_expired",
    "list_kv_keys_recursive",
    "migrate_eval_results_to_warm",
    "migrate_spans_to_warm",
    "migrate_traces_to_cold",
    "run_cron_job",
    "run_cron_job_async",
]
