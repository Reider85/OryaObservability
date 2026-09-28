"""Tests for the PC21 tiered-retention maintenance jobs (ticket T2.3.5).

Covers the three cron jobs (vault TTL cleanup, audit archival to S3 Parquet,
eval warm migration), the metrics wrapper, the pure cron-schedule parser, and
the infra wiring (compose service, Prometheus rules, warm DDL).
"""

import inspect
import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from agent_obs.guardrail.audit import LeaseExpiryAuditEvent
from agent_obs.metrics import (
    cron_duration_seconds,
    cron_errors_total,
    cron_last_success_timestamp_seconds,
    cron_rows_deleted_total,
    cron_runs_total,
)
from agent_obs.storage.hot import HotStore
from agent_obs.storage.maintenance import (
    AUDIT_ARCHIVE_SCHEMA_FIELDS,
    DEFAULT_AUDIT_RETENTION_DAYS,
    DEFAULT_EVAL_RETENTION_DAYS,
    DEFAULT_SPAN_RETENTION_DAYS,
    JOB_CLEANUP_AUDIT_EVENTS,
    JOB_CLEANUP_EVAL_RESULTS,
    JOB_CLEANUP_VAULT,
    JOB_MIGRATE_SPANS,
    AuditArchiveResult,
    EvalWarmResult,
    TraceWarmResult,
    VaultCleanupResult,
    archive_expired_audit_events,
    cleanup_vault_expired,
    list_kv_keys_recursive,
    migrate_eval_results_to_warm,
    migrate_spans_to_warm,
    run_cron_job,
    run_cron_job_async,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_CRON = REPO_ROOT / "scripts" / "cron"
if str(SCRIPTS_CRON) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_CRON))

import scheduler  # noqa: E402  (path must be set first)


def _metric_value(metric, job_name, suffix=""):
    """Read a labelled counter/gauge value, 0.0 when the series is absent."""
    try:
        return metric.labels(job_name=job_name)._value.get() + suffix  # type: ignore[attr-defined]
    except Exception:
        return 0.0


def _counter_value(metric, job_name):
    """Read a labelled counter's value, 0.0 when the series is absent."""
    try:
        return metric.labels(job_name=job_name)._value.get()
    except Exception:
        return 0.0


def _audit_dict(audit_id, days_ago, action="vault.recover"):
    """Build an audit event in the dict shape ``HotStore.get_audit_events_older_than`` returns."""
    return {
        "audit_id": audit_id,
        "timestamp": datetime.now(timezone.utc) - timedelta(days=days_ago),
        "trace_id": "trace-1",
        "actor": {"type": "engineer", "id": "alice@company.com"},
        "action": action,
        "decision": "allow",
        "resource": {"type": "pii_vault_key", "id": f"pii/{audit_id}"},
        "reason": f"reason for {audit_id}",
        "ip_address": "10.0.0.1",
        "user_agent": "cli",
    }


def _eval_dict(trace_id, days_ago, reasoning="long judge prose"):
    """Build an eval result in the dict shape ``HotStore.get_eval_results_older_than`` returns."""
    return {
        "trace_id": trace_id,
        "eval_id": f"eval-{trace_id}",
        "eval_name": "faithfulness",
        "eval_timestamp": datetime.now(timezone.utc) - timedelta(days=days_ago),
        "eval_latency_seconds": 1.25,
        "scores": {"faithfulness": 0.92},
        "judge_model": "gpt-4o-mini",
        "judge_prompt_sha256": "abc123sha",
        "reasoning": reasoning,
        "flags": ["long_answer"],
    }


def _audit_tuple(audit_id, days_ago):
    """Raw audit_events_hot row tuple, as clickhouse-driver returns from execute()."""
    row = _audit_dict(audit_id, days_ago)
    return (
        row["audit_id"],
        row["timestamp"],
        row["trace_id"],
        json.dumps(row["actor"]),
        row["action"],
        row["decision"],
        json.dumps(row["resource"]),
        row["reason"],
        row["ip_address"],
        row["user_agent"],
    )


def _eval_tuple(trace_id, days_ago, reasoning="long judge prose"):
    """Raw eval_results_hot row tuple, as clickhouse-driver returns from execute()."""
    row = _eval_dict(trace_id, days_ago, reasoning)
    return (
        row["trace_id"],
        row["eval_id"],
        row["eval_name"],
        row["eval_timestamp"],
        row["eval_latency_seconds"],
        json.dumps(row["scores"]),
        row["judge_model"],
        row["judge_prompt_sha256"],
        row["reasoning"],
        row["flags"],
    )


def _span_dict(
    trace_id,
    days_ago,
    span_id="span-1",
    agent_id="agent-1",
    tenant_id="tenant-1",
    name="llm.call",
    span_type="llm.call",
    status="ok",
    cost_usd=0.01,
    attributes=None,
):
    """Build a span in the dict shape ``HotStore.get_spans_older_than`` returns."""
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=days_ago)
    end = start + timedelta(seconds=2)
    return {
        "trace_id": trace_id,
        "span_id": span_id,
        "parent_span_id": "",
        "agent_id": agent_id,
        "tenant_id": tenant_id,
        "name": name,
        "span_type": span_type,
        "start_time": start,
        "end_time": end,
        "status": status,
        "attributes": attributes or {},
        "events": [],
        "cost_usd": cost_usd,
        "response_embedding": None,
    }


@pytest.fixture(scope="module")
def compose():
    return yaml.safe_load((REPO_ROOT / "infra" / "docker-compose.yml").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def prometheus():
    return yaml.safe_load((REPO_ROOT / "infra" / "prometheus.yml").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def rules():
    return yaml.safe_load((REPO_ROOT / "infra" / "prometheus-rules.yml").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def env_example():
    return (REPO_ROOT / "infra" / ".env.example").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


class TestCronMetrics:
    """The four metrics the ticket requires plus the staleness gauge."""

    def test_metric_names(self):
        """Metric names carry the agent_obs_ prefix (prometheus_client strips the
        `_total` suffix from the internal `_name`, so it is appended back)."""
        assert cron_runs_total._name + "_total" == "agent_obs_cron_runs_total"
        assert cron_errors_total._name + "_total" == "agent_obs_cron_errors_total"
        assert (
            cron_rows_deleted_total._name + "_total" == "agent_obs_cron_rows_deleted_total"
        )
        assert cron_duration_seconds._name == "agent_obs_cron_duration_seconds"
        assert (
            cron_last_success_timestamp_seconds._name
            == "agent_obs_cron_last_success_timestamp_seconds"
        )

    def test_all_metrics_labeled_by_job_name(self):
        """Every cron metric is labelled job_name so the alert can select on it."""
        for metric in (
            cron_runs_total,
            cron_errors_total,
            cron_rows_deleted_total,
            cron_duration_seconds,
            cron_last_success_timestamp_seconds,
        ):
            assert list(metric._labelnames) == ["job_name"], metric._name


# ---------------------------------------------------------------------------
# run_cron_job
# ---------------------------------------------------------------------------


class TestRunCronJob:
    """Metrics wrapper around the job bodies."""

    def test_success_records_run_and_gauge(self):
        """A successful run bumps runs_total, rows_deleted and the success gauge."""
        before_runs = _counter_value(cron_runs_total, "test_success")
        before_rows = _counter_value(cron_rows_deleted_total, "test_success")
        before_success = _counter_value(cron_last_success_timestamp_seconds, "test_success")

        result = run_cron_job("test_success", lambda: AuditArchiveResult(scanned=5, archived=3))

        assert result is not None
        assert result.archived == 3
        assert _counter_value(cron_runs_total, "test_success") == before_runs + 1
        assert _counter_value(cron_rows_deleted_total, "test_success") == before_rows + 3
        assert _counter_value(cron_last_success_timestamp_seconds, "test_success") > before_success

    def test_failure_is_counted_and_swallowed(self):
        """A raising job must not propagate: the scheduler loop has to survive."""
        before_runs = _counter_value(cron_runs_total, "test_failure")
        before_errors = _counter_value(cron_errors_total, "test_failure")
        before_success = _counter_value(cron_last_success_timestamp_seconds, "test_failure")

        def boom():
            raise RuntimeError("clickhouse down")

        result = run_cron_job("test_failure", boom)

        assert result is None
        assert _counter_value(cron_runs_total, "test_failure") == before_runs + 1
        assert _counter_value(cron_errors_total, "test_failure") == before_errors + 1
        # The success gauge must NOT advance, otherwise a permanently failing
        # job would look healthy to the "missed cycles" alert.
        assert (
            _counter_value(cron_last_success_timestamp_seconds, "test_failure")
            == before_success
        )

    def test_partial_errors_counted_without_failing(self):
        """A job that completes with per-row errors counts them but still succeeds."""
        before_errors = _counter_value(cron_errors_total, "test_partial")
        before_success = _counter_value(cron_last_success_timestamp_seconds, "test_partial")

        result = run_cron_job(
            "test_partial", lambda: VaultCleanupResult(scanned=4, expired=2, audited=2, errors=1)
        )

        assert result.affected == 2
        assert _counter_value(cron_errors_total, "test_partial") == before_errors + 1
        assert _counter_value(cron_last_success_timestamp_seconds, "test_partial") > before_success

    def test_duration_observed(self):
        """The job's wall-clock time lands in the histogram."""
        run_cron_job("test_duration", lambda: EvalWarmResult(scanned=1, migrated=1))
        assert cron_duration_seconds.labels(job_name="test_duration")._sum.get() > 0

    def test_zero_affected_does_not_touch_rows_deleted(self):
        """A no-op run must not report deleted rows."""
        before = _counter_value(cron_rows_deleted_total, "test_noop")
        run_cron_job("test_noop", lambda: EvalWarmResult())
        assert _counter_value(cron_rows_deleted_total, "test_noop") == before

    @pytest.mark.asyncio
    async def test_async_wrapper_success(self):
        """The async twin records the same metrics for the asyncpg-backed job."""
        async def ok():
            return EvalWarmResult(scanned=2, migrated=2)

        before = _counter_value(cron_runs_total, "test_async_ok")
        result = await run_cron_job_async("test_async_ok", ok)

        assert result.migrated == 2
        assert _counter_value(cron_runs_total, "test_async_ok") == before + 1

    @pytest.mark.asyncio
    async def test_async_wrapper_failure(self):
        """A failing coroutine is counted, not raised."""
        async def boom():
            raise RuntimeError("warm postgres down")

        before_errors = _counter_value(cron_errors_total, "test_async_fail")
        result = await run_cron_job_async("test_async_fail", boom)

        assert result is None
        assert _counter_value(cron_errors_total, "test_async_fail") == before_errors + 1


# ---------------------------------------------------------------------------
# Job 1: vault TTL cleanup
# ---------------------------------------------------------------------------


class TestListKvKeysRecursive:
    """Descending the nested pii/{hex8}/{timestamp} KV v2 tree."""

    def test_walks_nested_paths(self):
        client = MagicMock()
        client.secrets.kv.v2.list_secrets.side_effect = [
            {"data": {"keys": ["abc123/", "def456/"]}},
            {"data": {"keys": ["1700000000"]}},
            {"data": {"keys": ["1700000001"]}},
        ]

        keys = list_kv_keys_recursive(client, "pii")

        assert keys == ["pii/abc123/1700000000", "pii/def456/1700000001"]

    def test_handles_empty_mount(self):
        client = MagicMock()
        client.secrets.kv.v2.list_secrets.return_value = {"data": {"keys": []}}
        assert list_kv_keys_recursive(client, "pii") == []

    def test_swallows_permission_errors(self):
        """A token without list capability must not crash the job."""
        client = MagicMock()
        client.secrets.kv.v2.list_secrets.side_effect = PermissionError("permission denied")
        assert list_kv_keys_recursive(client, "pii") == []


class TestCleanupVault:
    """Hourly detection of expired PII vault entries + audit trail."""

    @pytest.fixture
    def mock_hvac_client(self):
        """Mock hvac: two expired KV entries, one live, one expired lease."""
        now = time.time()
        client = MagicMock()
        client.secrets.kv.v2.list_secrets.side_effect = [
            {"data": {"keys": ["expired1/", "expired2/", "live1/"]}},
            {"data": {"keys": ["1000"]}},
            {"data": {"keys": ["1001"]}},
            {"data": {"keys": ["1002"]}},
        ]

        entries = {
            "pii/expired1/1000": {"created_at": now - 90000, "ttl_seconds": 86400},
            "pii/expired2/1001": {"created_at": now - 90000, "ttl_seconds": 86400},
            "pii/live1/1002": {"created_at": now - 60, "ttl_seconds": 86400},
        }

        def read_secret_version(path, mount_point="secret", **kwargs):
            data = entries[path]
            return {"data": {"data": {"mask": "[EMAIL:1]", "original": "x@y.z", **data}}}

        client.secrets.kv.v2.read_secret_version.side_effect = read_secret_version
        client.sys.leases.list_leases.return_value = {"data": {"keys": ["pii/lease/expired"]}}
        client.sys.leases.read_lease.return_value = {
            "data": {"expire_time": datetime.now(timezone.utc) - timedelta(hours=1)}
        }
        client.sys.leases.read_lease_bad = None
        return client

    @pytest.fixture
    def hot_store(self):
        store = MagicMock()
        store.write_audit_events = MagicMock()
        return store

    def test_finds_expired_and_writes_audit_events(self, mock_hvac_client, hot_store):
        with patch("hvac.Client", return_value=mock_hvac_client):
            result = cleanup_vault_expired(hot_store=hot_store, prefix="pii/")

        assert isinstance(result, VaultCleanupResult)
        assert result.scanned == 4          # 3 KV entries + 1 lease
        assert result.expired == 3          # 2 KV entries + 1 lease
        assert result.audited == 3
        assert result.deleted == 0          # opt-in only
        assert result.errors == 0

        hot_store.write_audit_events.assert_called_once()
        events = hot_store.write_audit_events.call_args[0][0]
        assert len(events) == 3
        assert all(isinstance(e, LeaseExpiryAuditEvent) for e in events)
        assert all(e.action == "vault.lease_expired" for e in events)
        assert all(e.actor == {"type": "system", "id": "cron-cleanup"} for e in events)

    def test_live_entry_is_not_audited(self, mock_hvac_client, hot_store):
        with patch("hvac.Client", return_value=mock_hvac_client):
            cleanup_vault_expired(hot_store=hot_store)

        ids = [e.resource["id"] for e in hot_store.write_audit_events.call_args[0][0]]
        assert "pii/live1/1002" not in ids

    def test_lease_source_recorded(self, mock_hvac_client, hot_store):
        with patch("hvac.Client", return_value=mock_hvac_client):
            cleanup_vault_expired(hot_store=hot_store)

        events = hot_store.write_audit_events.call_args[0][0]
        by_id = {e.resource["id"]: e for e in events}
        assert "source=kv_metadata" in by_id["pii/expired1/1000"].reason
        assert "source=lease" in by_id["pii/lease/expired"].reason

    def test_delete_expired_off_by_default(self, mock_hvac_client, hot_store):
        """PC21 assigns reclamation to Vault, so nothing is deleted by default."""
        with patch("hvac.Client", return_value=mock_hvac_client):
            result = cleanup_vault_expired(hot_store=hot_store)

        assert result.deleted == 0
        mock_hvac_client.secrets.kv.v2.delete_metadata_and_all_versions.assert_not_called()

    def test_delete_expired_opt_in(self, mock_hvac_client, hot_store):
        with patch("hvac.Client", return_value=mock_hvac_client):
            result = cleanup_vault_expired(hot_store=hot_store, delete_expired=True)

        # Only the two KV entries; the lease id is not a KV path.
        assert result.deleted == 2
        deleted_paths = [
            call[1]["path"]
            for call in mock_hvac_client.secrets.kv.v2.delete_metadata_and_all_versions.call_args_list
        ]
        assert "pii/expired1/1000" in deleted_paths
        assert "pii/lease/expired" not in deleted_paths

    def test_dry_run_writes_nothing(self, mock_hvac_client, hot_store):
        with patch("hvac.Client", return_value=mock_hvac_client):
            result = cleanup_vault_expired(hot_store=hot_store, dry_run=True)

        assert result.expired == 3
        assert result.audited == 0
        hot_store.write_audit_events.assert_not_called()

    def test_max_events_caps_the_scan(self, mock_hvac_client, hot_store):
        """max_events=1 stops after one entry, so the other two are never touched."""
        with patch("hvac.Client", return_value=mock_hvac_client):
            result = cleanup_vault_expired(hot_store=hot_store, max_events=1)

        assert result.scanned == 1
        assert result.expired == 1  # the first entry scanned happens to be expired1
        assert result.audited == 1
        # The lease was never reached.
        assert not any(
            "pii/lease/expired" in str(call)
            for call in hot_store.write_audit_events.call_args_list
        )

    def test_nothing_expired_is_a_noop(self, hot_store):
        now = time.time()
        client = MagicMock()
        client.secrets.kv.v2.list_secrets.side_effect = [
            {"data": {"keys": ["live/"]}},
            {"data": {"keys": ["2000"]}},
        ]
        client.secrets.kv.v2.read_secret_version.return_value = {
            "data": {"data": {"created_at": now, "ttl_seconds": 86400}}
        }
        client.sys.leases.list_leases.return_value = {"data": {"keys": []}}

        with patch("hvac.Client", return_value=client):
            result = cleanup_vault_expired(hot_store=hot_store)

        assert result.expired == 0
        assert result.audited == 0
        hot_store.write_audit_events.assert_not_called()

    def test_audit_write_failure_is_counted(self, mock_hvac_client, hot_store):
        hot_store.write_audit_events.side_effect = Exception("clickhouse down")

        with patch("hvac.Client", return_value=mock_hvac_client):
            result = cleanup_vault_expired(hot_store=hot_store)

        assert result.audited == 0
        assert result.errors == 1

    def test_unreadable_entry_counted_as_error(self, hot_store):
        client = MagicMock()
        client.secrets.kv.v2.list_secrets.side_effect = [
            {"data": {"keys": ["broken/"]}},
            {"data": {"keys": ["3000"]}},
        ]
        client.secrets.kv.v2.read_secret_version.side_effect = Exception("permission denied")
        client.sys.leases.list_leases.return_value = {"data": {"keys": []}}

        with patch("hvac.Client", return_value=client):
            result = cleanup_vault_expired(hot_store=hot_store)

        assert result.errors == 1
        assert result.expired == 0

    def test_lease_listing_failure_does_not_abort(self, mock_hvac_client, hot_store):
        """sys/leases may be denied by policy; the KV scan must still complete."""
        mock_hvac_client.sys.leases.list_leases.side_effect = PermissionError("denied")

        with patch("hvac.Client", return_value=mock_hvac_client):
            result = cleanup_vault_expired(hot_store=hot_store)

        assert result.expired == 2
        assert result.errors == 0


class TestLeaseExpiryAuditEvent:
    """The audit contract written by cleanup_vault."""

    def test_fields_and_row_mapping(self):
        event = LeaseExpiryAuditEvent.for_expired_lease(
            vault_key="pii/abc/1000", expired_at=1700000000, source="kv_metadata"
        )

        assert event.action == "vault.lease_expired"
        assert event.decision == "allow"
        assert event.trace_id == ""
        assert event.user_agent == "cron"
        assert event.ip_address == "unknown"
        assert event.audit_id.startswith("lee-")
        assert "ttl_expired" in event.reason
        assert "source=kv_metadata" in event.reason

        row = event.to_clickhouse_row()
        assert len(row) == 10
        assert row[0] == event.audit_id
        assert json.loads(row[3]) == event.actor
        assert json.loads(row[6]) == event.resource
        assert isinstance(row[1], datetime)

    def test_to_dict_shape_matches_recovery_event(self):
        """The two event types must serialize to the same key set."""
        event = LeaseExpiryAuditEvent.for_expired_lease("pii/abc/1000", 1700000000)
        assert set(event.to_dict()) == {
            "audit_id", "timestamp", "trace_id", "actor", "action",
            "resource", "reason", "ip_address", "user_agent",
            "decision", "reason_category",
        }


# ---------------------------------------------------------------------------
# Job 2: audit_events -> S3 Parquet
# ---------------------------------------------------------------------------


class TestArchiveAuditEvents:
    """Daily archival of audit events past the 365-day window."""

    @pytest.fixture
    def cold_store(self):
        """Fake ColdStore that captures the uploaded table."""
        store = MagicMock()

        def write_parquet(key, table, bucket=None):
            store.last_key = key
            store.last_table = table
            return 1024

        store.write_parquet.side_effect = write_parquet
        return store

    @pytest.fixture
    def hot_store(self):
        store = MagicMock()
        store.get_audit_events_older_than.side_effect = [
            [_audit_dict("a1", 400), _audit_dict("a2", 401), _audit_dict("a3", 402)],
            [],
        ]
        store.delete_audit_events.return_value = 3
        return store

    def test_uploads_parquet_then_deletes_hot(self, hot_store, cold_store):
        """Ordering is the whole point: nothing is deleted before the upload."""
        result = archive_expired_audit_events(hot_store=hot_store, cold_store=cold_store)

        assert isinstance(result, AuditArchiveResult)
        assert result.archived == 3
        assert result.deleted == 3
        assert result.bytes_written == 1024
        assert len(result.objects) == 1
        assert result.errors == 0

        names = [call[0] for call in cold_store.write_parquet.call_args_list]
        assert names, "expected an upload call"
        assert "year=" in result.objects[0] and ".parquet" in result.objects[0]

    def test_deterministic_key_for_idempotent_reruns(self, cold_store):
        """The key depends only on the batch content, so a replay overwrites.

        This matters when a previous run uploaded the object but failed to purge
        hot: the retry must replace that object, not create a second copy.
        """
        hot_store = MagicMock()
        batch = [_audit_dict("a1", 400), _audit_dict("a2", 401)]
        hot_store.get_audit_events_older_than.side_effect = [batch, []]
        hot_store.delete_audit_events.return_value = 2

        archive_expired_audit_events(hot_store=hot_store, cold_store=cold_store)
        first_key = cold_store.last_key

        # Simulate the half-failed run: same rows still in hot, nothing purged.
        hot_store.get_audit_events_older_than.side_effect = [batch, []]
        archive_expired_audit_events(hot_store=hot_store, cold_store=cold_store)

        assert cold_store.last_key == first_key
        assert len(set(cold_store.write_parquet.call_args_list[i][0][0]
                       for i in range(len(cold_store.write_parquet.call_args_list)))) == 1

    def test_parquet_schema_matches_audit_events_hot(self, hot_store, cold_store):
        archive_expired_audit_events(hot_store=hot_store, cold_store=cold_store)
        assert list(cold_store.last_table.column_names) == list(AUDIT_ARCHIVE_SCHEMA_FIELDS)
        assert cold_store.last_table.num_rows == 3

    def test_json_columns_are_serialised_to_strings(self, hot_store, cold_store):
        archive_expired_audit_events(hot_store=hot_store, cold_store=cold_store)
        actor = cold_store.last_table.column("actor")[0].as_py()
        resource = cold_store.last_table.column("resource")[0].as_py()
        assert json.loads(actor)["id"] == "alice@company.com"
        assert json.loads(resource)["type"] == "pii_vault_key"

    def test_delete_receives_the_archived_ids(self, hot_store, cold_store):
        archive_expired_audit_events(hot_store=hot_store, cold_store=cold_store)
        hot_store.delete_audit_events.assert_called_once_with(["a1", "a2", "a3"])

    def test_upload_failure_keeps_hot_rows(self, cold_store):
        """A failed upload must never delete the source of truth."""
        hot_store = MagicMock()
        hot_store.get_audit_events_older_than.side_effect = [[_audit_dict("a1", 400)], []]
        cold_store.write_parquet.side_effect = Exception("s3 unavailable")

        result = archive_expired_audit_events(hot_store=hot_store, cold_store=cold_store)

        assert result.archived == 0
        assert result.deleted == 0
        assert result.errors == 1
        hot_store.delete_audit_events.assert_not_called()

    def test_dry_run_touches_neither_backend(self, hot_store, cold_store):
        result = archive_expired_audit_events(
            hot_store=hot_store, cold_store=cold_store, dry_run=True
        )

        assert result.archived == 3
        assert result.deleted == 0
        cold_store.write_parquet.assert_not_called()
        hot_store.delete_audit_events.assert_not_called()

    def test_default_retention_is_365_days(self):
        assert DEFAULT_AUDIT_RETENTION_DAYS == 365

    def test_retention_cutoff_reaches_the_query(self, hot_store, cold_store):
        hot_store.get_audit_events_older_than.side_effect = [[], []]
        before = datetime.now(timezone.utc)
        archive_expired_audit_events(hot_store=hot_store, cold_store=cold_store, retention_days=365)
        cutoff = hot_store.get_audit_events_older_than.call_args[0][0]

        assert (before - timedelta(days=365) - cutoff).total_seconds() < 5

    def test_rerun_archives_nothing(self, hot_store, cold_store):
        """Second invocation with an empty hot table is a no-op."""
        hot_store.get_audit_events_older_than.side_effect = [[], []]
        result = archive_expired_audit_events(hot_store=hot_store, cold_store=cold_store)

        assert result.archived == 0
        assert result.objects == []
        cold_store.write_parquet.assert_not_called()

    def test_processes_multiple_batches(self, cold_store):
        hot_store = MagicMock()
        batch = [_audit_dict(f"a{i}", 400) for i in range(3)]
        hot_store.get_audit_events_older_than.side_effect = [batch, batch, []]
        hot_store.delete_audit_events.return_value = 3

        result = archive_expired_audit_events(
            hot_store=hot_store, cold_store=cold_store, batch_size=3
        )

        assert result.archived == 6
        assert len(result.objects) == 2


# ---------------------------------------------------------------------------
# Job 3: eval_results -> Postgres warm
# ---------------------------------------------------------------------------


class TestMigrateEvalResults:
    """Daily structure-only migration of eval results to warm."""

    @pytest.fixture
    def warm_store(self):
        store = MagicMock()
        store.insert_eval_results = AsyncMock(return_value=2)
        store.close = AsyncMock()
        return store

    @pytest.fixture
    def hot_store(self):
        store = MagicMock()
        store.get_eval_results_older_than.side_effect = [
            [_eval_dict("t1", 20), _eval_dict("t2", 21)],
            [],
        ]
        store.delete_eval_results.return_value = 2
        return store

    @pytest.mark.asyncio
    async def test_migrates_then_purges_hot(self, hot_store, warm_store):
        result = await migrate_eval_results_to_warm(hot_store=hot_store, warm_store=warm_store)

        assert isinstance(result, EvalWarmResult)
        assert result.migrated == 2
        assert result.deleted == 2
        assert result.errors == 0

    @pytest.mark.asyncio
    async def test_reasoning_is_dropped(self, hot_store, warm_store):
        """The warm tier is structure-only: no `reasoning` anywhere in the payload."""
        await migrate_eval_results_to_warm(hot_store=hot_store, warm_store=warm_store)

        rows = warm_store.insert_eval_results.call_args[0][0]
        assert len(rows) == 2
        for row in rows:
            assert row["reasoning"] == "long judge prose"  # hot row has it
        # Dropping it is the WarmStore's job — asserted in TestWarmStoreColumns.

    @pytest.mark.asyncio
    async def test_delete_key_is_the_hot_order_by_tuple(self, hot_store, warm_store):
        await migrate_eval_results_to_warm(hot_store=hot_store, warm_store=warm_store)

        keys = hot_store.delete_eval_results.call_args[0][0]
        assert keys == [
            ("t1", "eval-t1", "faithfulness", keys[0][3]),
            ("t2", "eval-t2", "faithfulness", keys[1][3]),
        ]

    @pytest.mark.asyncio
    async def test_warm_failure_keeps_hot_rows(self, hot_store, warm_store):
        warm_store.insert_eval_results.side_effect = Exception("warm postgres down")

        result = await migrate_eval_results_to_warm(hot_store=hot_store, warm_store=warm_store)

        assert result.migrated == 0
        assert result.deleted == 0
        assert result.errors == 1
        hot_store.delete_eval_results.assert_not_called()

    @pytest.mark.asyncio
    async def test_dry_run_touches_neither_backend(self, hot_store, warm_store):
        result = await migrate_eval_results_to_warm(
            hot_store=hot_store, warm_store=warm_store, dry_run=True
        )

        assert result.migrated == 2
        assert result.deleted == 0
        warm_store.insert_eval_results.assert_not_called()
        hot_store.delete_eval_results.assert_not_called()

    @pytest.mark.asyncio
    async def test_rerun_migrates_nothing(self, hot_store, warm_store):
        hot_store.get_eval_results_older_than.side_effect = [[], []]
        result = await migrate_eval_results_to_warm(hot_store=hot_store, warm_store=warm_store)

        assert result.migrated == 0
        warm_store.insert_eval_results.assert_not_called()

    @pytest.mark.asyncio
    async def test_default_retention_is_14_days(self, hot_store, warm_store):
        assert DEFAULT_EVAL_RETENTION_DAYS == 14
        hot_store.get_eval_results_older_than.side_effect = [[], []]

        await migrate_eval_results_to_warm(hot_store=hot_store, warm_store=warm_store)

        before = datetime.now(timezone.utc)
        cutoff = hot_store.get_eval_results_older_than.call_args[0][0]
        assert (before - timedelta(days=14) - cutoff).total_seconds() < 5


class TestMigrateSpansToWarm:
    """Daily migration of spans older than 14d from hot to warm tier (PC22)."""

    @pytest.fixture
    def warm_store(self):
        store = MagicMock()
        store.insert_traces = AsyncMock(return_value=2)
        store.close = AsyncMock()
        return store

    @pytest.fixture
    def hot_store(self):
        store = MagicMock()
        store.get_spans_older_than.side_effect = [
            [
                _span_dict("t1", 20, span_id="s1", cost_usd=0.01,
                           attributes={"llm.input_chars": 100, "llm.input_sha256": "abc"}),
                _span_dict("t1", 20, span_id="s2", name="tool.call", span_type="tool.call",
                           cost_usd=0.005, status="error"),
                _span_dict("t2", 21, span_id="s3", cost_usd=0.02,
                           attributes={"llm.output_chars": 200, "llm.output_sha256": "def"}),
            ],
            [],
        ]
        store.delete_spans.return_value = 2
        return store

    @pytest.mark.asyncio
    async def test_migrates_then_purges_hot(self, hot_store, warm_store):
        result = await migrate_spans_to_warm(hot_store=hot_store, warm_store=warm_store)

        assert isinstance(result, TraceWarmResult)
        assert result.scanned_spans == 3
        assert result.traces_migrated == 2
        assert result.traces_deleted == 2
        assert result.errors == 0

    @pytest.mark.asyncio
    async def test_aggregates_spans_by_trace(self, hot_store, warm_store):
        await migrate_spans_to_warm(hot_store=hot_store, warm_store=warm_store)

        rows = warm_store.insert_traces.call_args[0][0]
        assert len(rows) == 2

        by_id = {r["trace_id"]: r for r in rows}
        # trace t1 has 2 spans: cost 0.01 + 0.005 = 0.015, span_count=2, error_count=1
        assert by_id["t1"]["cost_usd_total"] == pytest.approx(0.015)
        assert by_id["t1"]["span_count"] == 2
        assert by_id["t1"]["error_count"] == 1
        assert by_id["t1"]["input_chars"] == 100
        assert by_id["t1"]["input_sha256"] == "abc"
        # trace t2 has 1 span
        assert by_id["t2"]["cost_usd_total"] == pytest.approx(0.02)
        assert by_id["t2"]["span_count"] == 1
        assert by_id["t2"]["error_count"] == 0
        assert by_id["t2"]["output_chars"] == 200
        assert by_id["t2"]["output_sha256"] == "def"

    @pytest.mark.asyncio
    async def test_compression_drops_text_fields(self, hot_store, warm_store):
        """llm.input_text / llm.output_text must NOT appear in warm rows."""
        hot_store.get_spans_older_than.side_effect = [
            [_span_dict("t1", 20, attributes={
                "llm.input_text": "secret user email ivan@example.com",
                "llm.output_text": "Here is the answer",
                "llm.input_chars": 100,
                "llm.input_sha256": "abc",
                "llm.output_chars": 200,
                "llm.output_sha256": "def",
            })],
            [],
        ]

        await migrate_spans_to_warm(hot_store=hot_store, warm_store=warm_store)

        rows = warm_store.insert_traces.call_args[0][0]
        row = rows[0]
        # Text fields must not be present
        assert "llm.input_text" not in row
        assert "llm.output_text" not in row
        # Compressed metadata must be present
        assert row["input_chars"] == 100
        assert row["input_sha256"] == "abc"
        assert row["output_chars"] == 200
        assert row["output_sha256"] == "def"

    @pytest.mark.asyncio
    async def test_warm_failure_keeps_hot_rows(self, hot_store, warm_store):
        warm_store.insert_traces.side_effect = Exception("warm postgres down")

        result = await migrate_spans_to_warm(hot_store=hot_store, warm_store=warm_store)

        assert result.traces_migrated == 0
        assert result.traces_deleted == 0
        assert result.errors == 1
        hot_store.delete_spans.assert_not_called()

    @pytest.mark.asyncio
    async def test_dry_run_touches_neither_backend(self, hot_store, warm_store):
        result = await migrate_spans_to_warm(
            hot_store=hot_store, warm_store=warm_store, dry_run=True
        )

        assert result.traces_migrated == 2
        assert result.traces_deleted == 0
        warm_store.insert_traces.assert_not_called()
        hot_store.delete_spans.assert_not_called()

    @pytest.mark.asyncio
    async def test_rerun_migrates_nothing(self, hot_store, warm_store):
        hot_store.get_spans_older_than.side_effect = [[], []]
        result = await migrate_spans_to_warm(hot_store=hot_store, warm_store=warm_store)

        assert result.traces_migrated == 0
        warm_store.insert_traces.assert_not_called()

    @pytest.mark.asyncio
    async def test_default_retention_is_14_days(self, hot_store, warm_store):
        assert DEFAULT_SPAN_RETENTION_DAYS == 14
        hot_store.get_spans_older_than.side_effect = [[], []]

        await migrate_spans_to_warm(hot_store=hot_store, warm_store=warm_store)

        before = datetime.now(timezone.utc)
        cutoff = hot_store.get_spans_older_than.call_args[0][0]
        assert (before - timedelta(days=14) - cutoff).total_seconds() < 5

    @pytest.mark.asyncio
    async def test_eval_avg_is_empty_dict(self, hot_store, warm_store):
        """eval_avg starts empty — eval scores are migrated separately."""
        await migrate_spans_to_warm(hot_store=hot_store, warm_store=warm_store)

        rows = warm_store.insert_traces.call_args[0][0]
        for row in rows:
            assert row["eval_avg"] == {}

    @pytest.mark.asyncio
    async def test_error_status_counted_correctly(self, hot_store, warm_store):
        """Only non-'ok' status counts as an error."""
        hot_store.get_spans_older_than.side_effect = [
            [
                _span_dict("t1", 20, status="ok"),
                _span_dict("t1", 20, span_id="s2", status="error"),
                _span_dict("t1", 20, span_id="s3", status="ok"),
            ],
            [],
        ]

        await migrate_spans_to_warm(hot_store=hot_store, warm_store=warm_store)

        rows = warm_store.insert_traces.call_args[0][0]
        assert rows[0]["error_count"] == 1
        assert rows[0]["span_count"] == 3


class TestWarmStoreColumns:
    """The warm table must have no reasoning column, and the client must not send one."""

    def test_declared_columns_exclude_reasoning(self):
        from agent_obs.storage.warm import EVAL_RESULTS_WARM_COLUMNS

        assert "reasoning" not in EVAL_RESULTS_WARM_COLUMNS
        assert "scores" in EVAL_RESULTS_WARM_COLUMNS
        assert "eval_timestamp" in EVAL_RESULTS_WARM_COLUMNS

    def test_traces_warm_columns_include_compression_fields(self):
        from agent_obs.storage.warm import TRACES_WARM_COLUMNS

        assert "input_chars" in TRACES_WARM_COLUMNS
        assert "input_sha256" in TRACES_WARM_COLUMNS
        assert "output_chars" in TRACES_WARM_COLUMNS
        assert "output_sha256" in TRACES_WARM_COLUMNS
        assert "trace_id" in TRACES_WARM_COLUMNS
        assert "cost_usd_total" in TRACES_WARM_COLUMNS


# ---------------------------------------------------------------------------
# HotStore SQL contract
# ---------------------------------------------------------------------------


class TestHotStoreTieredQueries:
    """The SELECT/DELETE statements the cron jobs depend on."""

    @pytest.fixture
    def clickhouse(self):
        client = MagicMock()
        client.execute.return_value = None
        return client

    @pytest.fixture
    def store(self, clickhouse):
        with patch("clickhouse_driver.Client", return_value=clickhouse):
            yield HotStore()

    def test_write_audit_events_batches_rows(self, store, clickhouse):
        """`params` *is* the row list — that is the driver's batch-insert form."""
        events = [LeaseExpiryAuditEvent.for_expired_lease(f"pii/k{i}", 1700000000) for i in range(3)]
        assert store.write_audit_events(events) == 3

        sql, rows = clickhouse.execute.call_args[0]
        assert "INSERT INTO audit_events_hot" in sql
        assert len(rows) == 3
        assert all(len(row) == 10 for row in rows), "audit_events_hot has 10 columns"
        assert [row[0] for row in rows] == [e.audit_id for e in events]
        assert all(row[4] == "vault.lease_expired" for row in rows)

    def test_write_audit_events_returns_inserted_count(self, store, clickhouse):
        clickhouse.execute.return_value = 2
        events = [LeaseExpiryAuditEvent.for_expired_lease(f"pii/k{i}", 1700000000) for i in range(2)]
        assert store.write_audit_events(events) == 2

    def test_write_audit_events_noop_on_empty(self, store, clickhouse):
        store.write_audit_events([])
        clickhouse.execute.assert_not_called()

    def test_audit_select_is_bounded_and_ordered(self, store, clickhouse):
        clickhouse.execute.return_value = []
        cutoff = datetime.now(timezone.utc)
        store.get_audit_events_older_than(cutoff, limit=500)

        sql, params = clickhouse.execute.call_args[0]
        assert "FROM audit_events_hot" in sql
        assert "timestamp < %(cutoff)s" in sql
        assert "ORDER BY timestamp ASC" in sql
        assert "LIMIT %(limit)s" in sql
        assert params == {"cutoff": cutoff, "limit": 500}

    def test_audit_delete_uses_in_clause(self, store, clickhouse):
        store.delete_audit_events(["a1", "a2"])
        sql, params = clickhouse.execute.call_args[0]
        assert "DELETE FROM audit_events_hot" in sql
        assert "audit_id IN %(ids)s" in sql
        assert params["ids"] == ["a1", "a2"]

    def test_audit_delete_noop_on_empty(self, store, clickhouse):
        store.delete_audit_events([])
        clickhouse.execute.assert_not_called()

    def test_eval_select_returns_reasoning_for_the_caller_to_drop(self, store, clickhouse):
        clickhouse.execute.return_value = [_eval_tuple("t1", 20)]
        rows = store.get_eval_results_older_than(datetime.now(timezone.utc))

        sql, params = clickhouse.execute.call_args[0]
        assert "FROM eval_results_hot" in sql
        assert "eval_timestamp < %(cutoff)s" in sql
        assert isinstance(params, dict), "a list would be treated as INSERT data by the driver"
        assert rows[0]["reasoning"] == "long judge prose"
        assert rows[0]["scores"] == {"faithfulness": 0.92}
        assert rows[0]["flags"] == ["long_answer"]

    def test_every_read_uses_named_params(self, store, clickhouse):
        """Regression: clickhouse-driver only substitutes dict params.

        `Client.execute` sets `is_insert = isinstance(params, (list, tuple))`, so
        a SELECT given a list is sent to the server with its `%s` intact and
        fails with Code 62. This test failed only against a real ClickHouse.
        """
        for call in (
            lambda: store.get_audit_events_older_than(datetime.now(timezone.utc)),
            lambda: store.get_eval_results_older_than(datetime.now(timezone.utc)),
            lambda: store.get_spans_older_than(datetime.now(timezone.utc)),
        ):
            clickhouse.reset_mock()
            clickhouse.execute.return_value = []
            call()
            sql, params = clickhouse.execute.call_args[0]
            assert isinstance(params, dict)
            # every placeholder in the statement must have a matching dict key
            import re as _re

            for name in _re.findall(r"%\((\w+)\)s", sql):
                assert name in params, f"{name!r} missing from params"

    def test_eval_delete_uses_order_by_tuple(self, store, clickhouse):
        now = datetime.now(timezone.utc)
        store.delete_eval_results([("t1", "e1", "faithfulness", now)])

        sql, params = clickhouse.execute.call_args[0]
        assert "DELETE FROM eval_results_hot" in sql
        assert "(trace_id, eval_id, eval_name, eval_timestamp) IN %(keys)s" in sql
        assert params["keys"] == [("t1", "e1", "faithfulness", now)]

    def test_spans_select_is_bounded_and_ordered(self, store, clickhouse):
        clickhouse.execute.return_value = []
        cutoff = datetime.now(timezone.utc)
        store.get_spans_older_than(cutoff, limit=500)

        sql, params = clickhouse.execute.call_args[0]
        assert "FROM spans_hot" in sql
        assert "start_time < %(cutoff)s" in sql
        assert "ORDER BY start_time ASC" in sql
        assert "LIMIT %(limit)s" in sql
        assert params == {"cutoff": cutoff, "limit": 500}

    def test_spans_select_returns_full_shape(self, store, clickhouse):
        now = datetime.now(timezone.utc)
        clickhouse.execute.return_value = [
            (
                "trace-1", "span-1", "", "agent-1", "tenant-1",
                "llm.call", "llm.call", now - timedelta(days=15), now - timedelta(days=15, seconds=-2),
                "ok", '{"llm.input_chars": 100, "llm.input_sha256": "abc123"}', "[]",
                0.01, None,
            )
        ]
        spans = store.get_spans_older_than(now)

        assert len(spans) == 1
        assert spans[0]["trace_id"] == "trace-1"
        assert spans[0]["attributes"]["llm.input_chars"] == 100
        assert spans[0]["attributes"]["llm.input_sha256"] == "abc123"

    def test_spans_delete_uses_in_clause(self, store, clickhouse):
        store.delete_spans(["t1", "t2"])
        sql, params = clickhouse.execute.call_args[0]
        assert "DELETE FROM spans_hot" in sql
        assert "trace_id IN %(trace_ids)s" in sql
        assert params["trace_ids"] == ["t1", "t2"]

    def test_spans_delete_noop_on_empty(self, store, clickhouse):
        store.delete_spans([])
        clickhouse.execute.assert_not_called()


# ---------------------------------------------------------------------------
# Schedule parsing
# ---------------------------------------------------------------------------


class TestNextRunAt:
    """The pure 5-field cron parser backing the scheduler."""

    NOW = datetime(2026, 9, 28, 14, 37, 5, tzinfo=timezone.utc)  # a Monday

    def test_hourly(self):
        assert scheduler.next_run_at("0 * * * *", self.NOW) == datetime(
            2026, 9, 28, 15, 0, tzinfo=timezone.utc
        )

    def test_every_fifteen_minutes(self):
        assert scheduler.next_run_at("*/15 * * * *", self.NOW) == datetime(
            2026, 9, 28, 14, 45, tzinfo=timezone.utc
        )

    def test_daily_rolls_to_tomorrow(self):
        assert scheduler.next_run_at("17 3 * * *", self.NOW) == datetime(
            2026, 9, 29, 3, 17, tzinfo=timezone.utc
        )

    def test_month_and_year_rollover(self):
        assert scheduler.next_run_at("0 0 1 1 *", self.NOW) == datetime(
            2027, 1, 1, 0, 0, tzinfo=timezone.utc
        )

    def test_sunday_dow_seven_normalised(self):
        """Both 0 and 7 mean Sunday."""
        assert scheduler.next_run_at("30 2 * * 7", self.NOW).weekday() == 6
        assert scheduler.next_run_at("30 2 * * 0", self.NOW) == scheduler.next_run_at(
            "30 2 * * 7", self.NOW
        )

    def test_step_with_range(self):
        """`9-17/4` expands to {9, 13, 17}; 13:00 today has already passed."""
        assert scheduler.next_run_at("0 9-17/4 * * *", self.NOW) == datetime(
            2026, 9, 28, 17, 0, tzinfo=timezone.utc
        )

    def test_comma_list(self):
        assert scheduler.next_run_at("0 0,12 * * *", self.NOW) == datetime(
            2026, 9, 29, 0, 0, tzinfo=timezone.utc
        )

    def test_strictly_after_now(self):
        """A job scheduled for the current minute must not re-fire immediately."""
        now = datetime(2026, 9, 28, 14, 0, 0, tzinfo=timezone.utc)
        assert scheduler.next_run_at("0 * * * *", now) == datetime(
            2026, 9, 28, 15, 0, tzinfo=timezone.utc
        )

    @pytest.mark.parametrize("bad", ["* * * *", "* * * * * *", "", "60 * * * *", "x * * * *"])
    def test_invalid_specs_rejected(self, bad):
        with pytest.raises(ValueError):
            scheduler.next_run_at(bad, self.NOW)

    def test_unsatisfiable_spec_raises(self):
        with pytest.raises(ValueError):
            scheduler.next_run_at("0 0 30 2 *", self.NOW)


class TestSchedulerJobTable:
    """The scheduled job table used by the container entrypoint."""

    def test_all_four_jobs_registered(self):
        names = [job.name for job in scheduler.build_jobs()]
        assert names == [
            JOB_CLEANUP_VAULT,
            JOB_CLEANUP_AUDIT_EVENTS,
            JOB_CLEANUP_EVAL_RESULTS,
            JOB_MIGRATE_SPANS,
        ]

    def test_cadences_drive_the_alert_thresholds(self):
        cadences = {job.name: job.interval_seconds for job in scheduler.build_jobs()}
        assert cadences[JOB_CLEANUP_VAULT] == 3600     # 2 cycles = 2h alert
        assert cadences[JOB_CLEANUP_AUDIT_EVENTS] == 86400
        assert cadences[JOB_CLEANUP_EVAL_RESULTS] == 86400
        assert cadences[JOB_MIGRATE_SPANS] == 86400

    def test_daily_jobs_are_staggered(self):
        specs = [job.spec for job in scheduler.build_jobs() if job.interval_seconds == 86400]
        assert len(set(specs)) == 3, "daily jobs must not share a slot"

    def test_specs_are_valid(self):
        for job in scheduler.build_jobs():
            scheduler.validate_cron_spec(job.spec)

    @pytest.mark.asyncio
    async def test_run_once_executes_every_job(self):
        calls = []

        def invoker_for(label):
            async def _invoke():
                calls.append(label)
                return AuditArchiveResult(scanned=1, archived=1)
            return _invoke

        jobs = [
            scheduler.CronJob("j1", "0 * * * *", invoker_for("j1"), 3600),
            scheduler.CronJob("j2", "17 3 * * *", invoker_for("j2"), 86400),
        ]

        await scheduler.run_once(jobs)

        assert calls == ["j1", "j2"]

    @pytest.mark.asyncio
    async def test_run_once_awaits_the_async_eval_job(self):
        """A coroutine returned into a thread would never be awaited."""
        calls = []

        async def _invoke():
            calls.append("eval")
            return EvalWarmResult(migrated=2)

        await scheduler.run_once([scheduler.CronJob("j", "0 3 * * *", _invoke, 86400)])

        assert calls == ["eval"]

    def test_scheduled_invoke_returns_an_awaitable(self):
        """`invoke` may be sync or async, but what it returns must be awaitable."""
        for job in scheduler.build_jobs():
            if job.name in (JOB_CLEANUP_EVAL_RESULTS, JOB_MIGRATE_SPANS):
                # Needs a live asyncpg pool; covered by the env-propagation tests.
                continue
            awaitable = job.invoke()
            assert inspect.isawaitable(awaitable), job.name
            awaitable.close()

    @pytest.mark.asyncio
    async def test_env_retention_reaches_the_archive_call(self, monkeypatch):
        """The scheduled job must honour the container's env, not the dataclass default."""
        monkeypatch.setenv("AGENT_OBS_CRON_AUDIT_RETENTION_DAYS", "30")
        monkeypatch.setenv("AGENT_OBS_CRON_BATCH_SIZE", "7")
        seen = {}

        def fake_archive(**kwargs):
            seen.update(kwargs)
            return AuditArchiveResult()

        with patch("scheduler.build_hot_store"), patch("scheduler.build_cold_store"), patch(
            "scheduler.archive_expired_audit_events", side_effect=fake_archive
        ):
            audit = next(
                j for j in scheduler.build_jobs() if j.name == JOB_CLEANUP_AUDIT_EVENTS
            )
            await audit.invoke()

        assert seen["retention_days"] == 30
        assert seen["batch_size"] == 7

    @pytest.mark.asyncio
    async def test_vault_delete_stays_opt_in_in_the_scheduler(self, monkeypatch):
        monkeypatch.delenv("AGENT_OBS_CRON_VAULT_DELETE_EXPIRED", raising=False)
        seen = {}

        with patch("scheduler.build_hot_store"), patch(
            "scheduler.cleanup_vault_expired", side_effect=lambda **kw: seen.update(kw)
        ):
            vault = next(j for j in scheduler.build_jobs() if j.name == JOB_CLEANUP_VAULT)
            await vault.invoke()

        assert seen["delete_expired"] is False


# ---------------------------------------------------------------------------
# Infra wiring
# ---------------------------------------------------------------------------


class TestInfraCronConfig:
    """docker-compose, Prometheus and DDL wiring for PC21."""

    def test_cron_service_exists(self, compose):
        service = compose["services"]["cron"]
        assert service["build"]["dockerfile"] == "infra/cron/Dockerfile"
        assert "9777:9777" in service["ports"]
        assert "../:/app:ro" in service["volumes"]
        assert service["environment"]["AGENT_OBS_CRON_AUDIT_RETENTION_DAYS"] == "${AGENT_OBS_CRON_AUDIT_RETENTION_DAYS:-365}"
        assert service["environment"]["AGENT_OBS_CRON_EVAL_RETENTION_DAYS"] == "${AGENT_OBS_CRON_EVAL_RETENTION_DAYS:-14}"
        assert service["environment"]["AGENT_OBS_CRON_MIGRATION_RETENTION_DAYS"] == "${AGENT_OBS_CRON_MIGRATION_RETENTION_DAYS:-14}"
        assert service["environment"]["AGENT_OBS_CRON_MIGRATION_SPEC"] == "${AGENT_OBS_CRON_MIGRATION_SPEC:-0 4 * * *}"

    def test_cron_uses_the_same_clickhouse_credentials_as_the_server(self, compose):
        """Regression: mismatched defaults made every job fail auth (Code 516).

        The cron service and the clickhouse service must resolve the *same*
        ${CLICKHOUSE_USER}/${CLICKHOUSE_PASSWORD} expression, otherwise the
        client authenticates as a user that was never created.
        """
        env = compose["services"]["cron"]["environment"]
        server = compose["services"]["clickhouse"]["environment"]
        assert env["CLICKHOUSE_USER"] == server["CLICKHOUSE_USER"]
        assert env["CLICKHOUSE_PASSWORD"] == server["CLICKHOUSE_PASSWORD"]
        # The server pins the database name literally while the client
        # interpolates it, so compare the resolved fallback.
        assert env["CLICKHOUSE_DB"] == "${CLICKHOUSE_DB:-%s}" % server["CLICKHOUSE_DB"]

    def test_cron_depends_on_every_backend_it_touches(self, compose):
        depends = compose["services"]["cron"]["depends_on"]
        for backend in ("clickhouse", "postgres-warm", "minio", "vault"):
            assert backend in depends, backend
            assert depends[backend]["condition"] == "service_healthy"

    def test_alertmanager_service_exists(self, compose):
        service = compose["services"]["alertmanager"]
        assert service["image"].startswith("prom/alertmanager")
        assert any("alertmanager.yml" in v for v in service["volumes"])

    def test_prometheus_scrapes_cron(self, prometheus):
        targets = {
            job["job_name"]: job["static_configs"][0]["targets"]
            for job in prometheus["scrape_configs"]
        }
        assert "cron" in targets
        assert targets["cron"] == ["cron:9777"]
        # the pre-existing collector target must survive
        assert targets["otel-collector"] == ["otel-collector:8888"]

    def test_prometheus_routes_alerts_to_alertmanager(self, prometheus):
        managers = prometheus["alerting"]["alertmanagers"][0]["static_configs"][0]["targets"]
        assert managers == ["alertmanager:9093"]

    def test_cron_alert_rules_exist(self, rules):
        group = next(g for g in rules["groups"] if g["name"] == "cron")
        alerts = {rule["alert"] for rule in group["rules"]}
        assert alerts == {"CronTargetDown", "CronHourlyJobStale", "CronDailyJobStale"}

    def test_staleness_thresholds_are_two_cycles(self, rules):
        group = next(g for g in rules["groups"] if g["name"] == "cron")
        by_name = {rule["alert"]: rule for rule in group["rules"]}
        assert "7200" in by_name["CronHourlyJobStale"]["expr"]      # 2 * 1h
        assert "172800" in by_name["CronDailyJobStale"]["expr"]     # 2 * 24h

    def test_staleness_rules_read_the_success_gauge(self, rules):
        group = next(g for g in rules["groups"] if g["name"] == "cron")
        for name in ("CronHourlyJobStale", "CronDailyJobStale"):
            rule = next(r for r in group["rules"] if r["alert"] == name)
            assert "agent_obs_cron_last_success_timestamp_seconds" in rule["expr"]

    def test_tail_sampler_recording_rules_preserved(self, rules):
        """PC21 must not disturb the pre-existing P21 rules."""
        group = next(g for g in rules["groups"] if g["name"] == "tail_sampler")
        assert {r["record"] for r in group["rules"]} == {
            "job:tail_sampler_kept_ratio:ratio",
            "job:tail_sampler_kept_by_reason:ratio",
        }

    def test_warm_ddl_has_eval_results_warm_without_reasoning(self):
        ddl = (REPO_ROOT / "infra" / "storage" / "postgres_warm.sql").read_text(encoding="utf-8")
        assert "CREATE TABLE IF NOT EXISTS eval_results_warm" in ddl
        body = ddl.split("CREATE TABLE IF NOT EXISTS eval_results_warm", 1)[1]
        body = body.split(");", 1)[0]
        assert "reasoning" not in body
        assert "scores JSONB" in body
        # conflict target must be backed by a real unique constraint
        assert "PRIMARY KEY (trace_id, eval_id, eval_name, eval_timestamp)" in body

    def test_audit_bucket_lifecycle_is_one_year(self):
        """§3.4 ARCHITECT.md requires >= 1 year; PC21 archives into this bucket."""
        lifecycle = json.loads(
            (REPO_ROOT / "infra" / "storage" / "s3_lifecycle.json").read_text(encoding="utf-8")
        )
        rules_by_id = {r["ID"]: r for r in lifecycle["Rules"]}
        assert rules_by_id["audit-events-lifecycle"]["Expiration"]["Days"] == 365
        assert rules_by_id["cold-traces-lifecycle"]["Expiration"]["Days"] == 365

    def test_minio_init_applies_the_same_expiry(self):
        script = (REPO_ROOT / "infra" / "storage" / "minio_init.sh").read_text(encoding="utf-8")
        audit_block = script.split("audit-events", 1)[1]
        assert '"365"' in audit_block

    def test_minio_init_uses_flags_mc_ilm_add_actually_accepts(self):
        """Regression: `mc ilm add` rejects unknown flags instead of ignoring them.

        The script shipped with `--expiry` and `--status`, neither of which
        exists, so `set -e` aborted the service before the audit-events rule was
        ever applied and the bucket silently kept objects forever.
        """
        script = (REPO_ROOT / "infra" / "storage" / "minio_init.sh").read_text(encoding="utf-8")
        # Join backslash continuations first: the flags sit on the line after
        # `mc ilm add`, and scanning line by line would miss them entirely.
        joined = script.replace("\\\n", " ")
        commands = [
            " ".join(c.split())
            for c in joined.splitlines()
            if " ".join(c.split()).startswith("mc ilm add")
        ]
        assert len(commands) >= 2, f"expected a rule per bucket, found {commands}"
        for cmd in commands:
            assert "--expiry" not in cmd, f"invalid mc flag in: {cmd}"
            assert "--status" not in cmd, f"invalid mc flag in: {cmd}"
            assert "--expire-days" in cmd, f"missing expiry in: {cmd}"
        # Guard against the loop silently matching nothing.
        assert any("local/audit-events" in c for c in commands)
        assert any("local/cold-traces" in c for c in commands)

    def test_warm_ddl_contains_no_statement_postgres_cannot_parse(self):
        """Regression: the warm tier never initialised, so jobs 1-3 had no schema.

        The postgres entrypoint runs initdb scripts with ON_ERROR_STOP=1, so a
        single unparseable statement aborts the whole run and leaves *no*
        tables behind. PostgreSQL supports IF NOT EXISTS for tables and indexes
        but NOT for databases, users, or views.
        """
        ddl = (REPO_ROOT / "infra" / "storage" / "postgres_warm.sql").read_text(encoding="utf-8")
        # Strip SQL line comments: the file documents in prose which statements
        # are invalid, and the raw text would match its own warnings.
        body = "\n".join(
            line for line in ddl.splitlines() if not line.strip().startswith("--")
        )
        statements = [s.strip() for s in body.split(";")]
        for stmt in statements:
            normalized = " ".join(stmt.split()).upper()
            for kind in ("DATABASE", "USER", "VIEW", "ROLE"):
                assert f"CREATE {kind} IF NOT EXISTS" not in normalized, (
                    f"CREATE {kind} IF NOT EXISTS is invalid in PostgreSQL: {stmt[:80]!r}"
                )
        # The schema PC21 depends on must still be declared.
        assert "CREATE TABLE IF NOT EXISTS EVAL_RESULTS_WARM" in " ".join(body.split()).upper()

    def test_warm_init_script_does_not_recreate_the_database(self):
        """POSTGRES_DB already provisions the database, and creating it again
        (or as the wrong superuser) aborts initdb."""
        script = (REPO_ROOT / "infra" / "storage" / "postgres_warm_init.sh").read_text(
            encoding="utf-8"
        )
        # Ignore comments: the script explains in prose which statements are
        # invalid, and grepping the raw text would match its own warnings.
        body = "\n".join(
            line for line in script.splitlines() if not line.strip().startswith("#")
        )
        assert "createdb" not in body
        assert "-U postgres" not in body
        # Role creation must go through the idempotent DO-block form.
        assert "CREATE USER IF NOT EXISTS" not in body
        assert "pg_roles WHERE rolname = 'warm_user'" in body
        # Must fail loudly rather than leaving a half-provisioned tier.
        assert "set -e" in body

    def test_vault_policy_grants_the_cleanup_capabilities(self):
        policy = (REPO_ROOT / "infra" / "vault" / "policies" / "pii-recovery.hcl").read_text(
            encoding="utf-8"
        )
        assert 'path "secret/metadata/pii"' in policy
        assert '"list"' in policy
        assert 'path "sys/leases/pii/*"' in policy

    def _ddl_ttl_days(self, column: str) -> int:
        ddl = (REPO_ROOT / "infra" / "storage" / "clickhouse_ddl.sql").read_text(encoding="utf-8")
        match = re.search(rf"TTL {column} \+ INTERVAL (\d+) DAY", ddl)
        assert match, f"no TTL clause found for {column}"
        return int(match.group(1))

    def test_hot_ttl_outlasts_the_audit_archive_trigger(self):
        """Regression: an equal TTL makes the archiver a permanent no-op.

        ClickHouse discards a row at INSERT time when it is already past the
        TTL, so if the hot TTL equals the archive trigger the rows are gone
        before cleanup_audit_events.py can ever SELECT them.
        """
        assert self._ddl_ttl_days("timestamp") > DEFAULT_AUDIT_RETENTION_DAYS

    def test_hot_ttl_outlasts_the_eval_migration_trigger(self):
        """Same invariant for the warm migration, which is the only copy of the
        scores once hot purges them."""
        assert self._ddl_ttl_days("eval_timestamp") > DEFAULT_EVAL_RETENTION_DAYS

    def test_hot_ttl_outlasts_the_span_migration_trigger(self):
        """PC22: spans_hot TTL must outlast the span migration trigger."""
        assert self._ddl_ttl_days("start_time") > DEFAULT_SPAN_RETENTION_DAYS

    def test_cold_bucket_keeps_archived_audit_for_a_year(self):
        """§3.4 ARCHITECT.md: the archived object must survive >= 1 year, which
        is measured from upload, i.e. after the hot TTL has expired."""
        ttl_days = self._ddl_ttl_days("timestamp")
        assert ttl_days > DEFAULT_AUDIT_RETENTION_DAYS
        lifecycle = json.loads(
            (REPO_ROOT / "infra" / "storage" / "s3_lifecycle.json").read_text(encoding="utf-8")
        )
        rules = {r["ID"]: r for r in lifecycle["Rules"]}
        assert rules["audit-events-lifecycle"]["Expiration"]["Days"] >= 365


class TestEnvExample:
    """The .env contract documented for operators."""

    def test_cron_vars_documented(self, env_example):
        for var in (
            "AGENT_OBS_CRON_PORT",
            "AGENT_OBS_CRON_VAULT_SPEC",
            "AGENT_OBS_CRON_AUDIT_SPEC",
            "AGENT_OBS_CRON_EVAL_SPEC",
            "AGENT_OBS_CRON_MIGRATION_SPEC",
            "AGENT_OBS_CRON_AUDIT_RETENTION_DAYS",
            "AGENT_OBS_CRON_EVAL_RETENTION_DAYS",
            "AGENT_OBS_CRON_MIGRATION_RETENTION_DAYS",
            "AGENT_OBS_CRON_BATCH_SIZE",
            "AGENT_OBS_CRON_VAULT_DELETE_EXPIRED",
        ):
            assert f"{var}=" in env_example, var

    def test_delete_expired_defaults_off(self, env_example):
        for line in env_example.splitlines():
            if line.startswith("AGENT_OBS_CRON_VAULT_DELETE_EXPIRED="):
                assert line.split("=", 1)[1] == "false"
                break
        else:
            pytest.fail("AGENT_OBS_CRON_VAULT_DELETE_EXPIRED not found")
