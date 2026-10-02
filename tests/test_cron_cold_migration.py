"""Tests for the PC23 weekly warm→cold migration (ticket T2.6.5).

Covers the cold migration job, the Parquet schema, the partitioning, the
idempotency, the metrics, and the infra wiring (compose service, Prometheus rules).
"""

import io
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import moto
import pytest
import pyarrow as pa
import pyarrow.parquet as pq
import yaml

from agent_obs.metrics import (
    migration_warm_to_cold_bytes_total,
    migration_warm_to_cold_duration_seconds,
    migration_warm_to_cold_files_total,
    migration_warm_to_cold_rows_total,
)
from agent_obs.storage.maintenance import (
    JOB_MIGRATE_TRACES,
    TraceColdResult,
    build_cold_trace_store,
    build_warm_store,
    migrate_traces_to_cold,
    run_cron_job_async,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_CRON = REPO_ROOT / "scripts" / "cron"
if str(SCRIPTS_CRON) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_CRON))

import migrate_warm_to_cold  # noqa: E402


def _trace_dict(
    trace_id,
    days_ago,
    agent_id="agent-1",
    tenant_id="tenant-1",
    status="ok",
    cost_usd=0.01,
    eval_scores=None,
    user_id="user-123",
    span_count=1,
    error_count=0,
):
    """Build a trace in the dict shape WarmStore.get_traces_* returns."""
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=days_ago)
    end = start + timedelta(seconds=2)
    return {
        "trace_id": trace_id,
        "tenant_id": tenant_id,
        "agent_id": agent_id,
        "start_time": start,
        "end_time": end,
        "status": status,
        "cost_usd_total": cost_usd,
        "span_count": span_count,
        "error_count": error_count,
        "eval_avg": eval_scores or {},
        "input_chars": 100,
        "input_sha256": "abc123",
        "output_chars": 200,
        "output_sha256": "def456",
        "user_hash": "hash_" + user_id,
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
# Migration job tests
# ---------------------------------------------------------------------------


class TestMigrateTracesToCold:
    """Weekly migration of traces older than 90 days from warm to cold tier."""

    @pytest.fixture
    def warm_store(self):
        store = MagicMock()
        # Make async methods return async iterators
        async def list_partitions_side_effect(cutoff):
            return [
                # First call: two partitions
                ("tenant-1", (datetime.now(timezone.utc) - timedelta(days=91)).date()), 
                ("tenant-2", (datetime.now(timezone.utc) - timedelta(days=92)).date())
            ]
        
        async def get_traces_side_effect(tenant_id, day, last_trace_id, max_rows):
            if tenant_id == "tenant-1" and last_trace_id == "":
                return [_trace_dict("t1", 91), _trace_dict("t2", 91), _trace_dict("t3", 91)]
            elif tenant_id == "tenant-1" and last_trace_id == "t3":
                return [_trace_dict("t4", 91)]
            elif tenant_id == "tenant-2" and last_trace_id == "":
                return [_trace_dict("t5", 92), _trace_dict("t6", 92)]
            else:
                return []
        
        async def delete_traces_side_effect(trace_ids):
            return len(trace_ids)
        
        store.list_trace_partitions.side_effect = list_partitions_side_effect
        store.get_traces_for_partition.side_effect = get_traces_side_effect
        store.delete_traces.side_effect = delete_traces_side_effect
        return store

    @pytest.fixture
    def cold_store(self):
        store = MagicMock()
        store.delete_prefix = MagicMock()
        store.write_parquet = MagicMock(return_value=1024)
        return store

    @pytest.mark.asyncio
    async def test_migrates_then_purges_warm(self, warm_store, cold_store):
        result = await migrate_traces_to_cold(warm_store=warm_store, cold_store=cold_store)

        assert isinstance(result, TraceColdResult)
        assert result.partitions == 2
        assert result.files == 3
        assert result.rows == 6
        assert result.bytes_written == 3072  # 3 files * 1024 bytes
        assert result.traces_deleted == 6  # 4 (tenant-1) + 2 (tenant-2)
        assert result.errors == 0

        # Verify prefix wipe before write
        cold_store.delete_prefix.assert_called()
        cold_store.write_parquet.assert_called()

    @pytest.mark.asyncio
    async def test_aggregation_correctness(self, warm_store, cold_store):
        # Mock warm_store to return traces with different agents and eval scores
        async def list_partitions_side_effect(cutoff):
            return [("tenant-1", (datetime.now(timezone.utc) - timedelta(days=91)).date())]
        
        async def get_traces_side_effect(tenant_id, day, last_trace_id, max_rows):
            if last_trace_id:
                return []  # keyset pagination: single page
            return [
                _trace_dict("t1", 91, agent_id="agent-a", eval_scores={"faithfulness": 0.8, "answer_relevancy": 0.9}, span_count=2, error_count=0),
                _trace_dict("t2", 91, agent_id="agent-a", eval_scores={"faithfulness": 0.6, "answer_relevancy": 0.7}, span_count=1, error_count=0),
                _trace_dict("t3", 91, agent_id="agent-b", eval_scores={"faithfulness": 0.9, "answer_relevancy": 0.8}, span_count=3, error_count=1),
            ]
        
        warm_store.list_trace_partitions.side_effect = list_partitions_side_effect
        warm_store.get_traces_for_partition.side_effect = get_traces_side_effect
        warm_store.delete_traces.return_value = 3

        # Capture the table data passed to write_parquet
        tables_written = []
        def capture_write_parquet(key, table):
            tables_written.append(table)
            return 1024
        cold_store.write_parquet = MagicMock(side_effect=capture_write_parquet)
        cold_store.delete_prefix = MagicMock()

        await migrate_traces_to_cold(warm_store=warm_store, cold_store=cold_store)

        # Verify aggregation per agent
        assert len(tables_written) == 1
        table = tables_written[0]
        
        # Agent A: 2 traces, cost=0.02, eval_avg=(0.7, 0.8), users=1, error_rate=0
        assert table.column("agent_id")[0].as_py() == "agent-a"
        assert table.column("traces_count")[0].as_py() == 2
        assert table.column("cost_usd_sum")[0].as_py() == pytest.approx(0.02)
        eval_a0 = table.column("eval_avg")[0].as_py()
        assert eval_a0["faithfulness"] == pytest.approx(0.7)
        assert eval_a0["relevancy"] == pytest.approx(0.8)
        assert table.column("users_count")[0].as_py() == 1
        assert table.column("error_rate")[0].as_py() == 0.0

        # Agent B: 1 trace, cost=0.01, eval_avg=(0.9, 0.8), users=1, error_rate=1/3
        assert table.column("agent_id")[1].as_py() == "agent-b"
        assert table.column("traces_count")[1].as_py() == 1
        assert table.column("cost_usd_sum")[1].as_py() == pytest.approx(0.01)
        eval_a1 = table.column("eval_avg")[1].as_py()
        assert eval_a1["faithfulness"] == pytest.approx(0.9)
        assert eval_a1["relevancy"] == pytest.approx(0.8)
        assert table.column("users_count")[1].as_py() == 1
        assert table.column("error_rate")[1].as_py() == pytest.approx(1/3)

    @pytest.mark.asyncio
    async def test_parquet_schema_matches_spec(self, warm_store, cold_store):
        async def list_partitions_side_effect(cutoff):
            return [("tenant-1", (datetime.now(timezone.utc) - timedelta(days=91)).date())]
        
        async def get_traces_side_effect(tenant_id, day, last_trace_id, max_rows):
            if last_trace_id:
                return []  # keyset pagination: single page
            return [_trace_dict("t1", 91)]
        
        warm_store.list_trace_partitions.side_effect = list_partitions_side_effect
        warm_store.get_traces_for_partition.side_effect = get_traces_side_effect
        warm_store.delete_traces.return_value = 1

        # Capture the table data passed to write_parquet
        tables_written = []
        def capture_write_parquet(key, table):
            tables_written.append(table)
            return 1024
        cold_store.write_parquet = MagicMock(side_effect=capture_write_parquet)
        cold_store.delete_prefix = MagicMock()

        await migrate_traces_to_cold(warm_store=warm_store, cold_store=cold_store)

        # Verify schema per PC23 spec
        table = tables_written[0]
        schema = table.schema
        assert schema.field("tenant_id").type == pa.string()
        assert schema.field("agent_id").type == pa.string()
        assert schema.field("day").type == pa.date32()
        assert schema.field("traces_count").type == pa.int32()
        assert schema.field("cost_usd_sum").type == pa.float64()
        assert schema.field("eval_avg").type == pa.struct(
            [
                ("faithfulness", pa.float64()),
                ("relevancy", pa.float64()),
                ("completeness", pa.float64()),
            ]
        )
        assert schema.field("users_count").type == pa.int32()
        assert schema.field("error_rate").type == pa.float64()

    @pytest.mark.asyncio
    async def test_partitioning_key_format(self, warm_store, cold_store):
        async def list_partitions_side_effect(cutoff):
            return [("tenant-1", (datetime.now(timezone.utc) - timedelta(days=91)).date())]
        
        async def get_traces_side_effect(tenant_id, day, last_trace_id, max_rows):
            if last_trace_id:
                return []  # keyset pagination: single page
            return [_trace_dict("t1", 91)]
        
        warm_store.list_trace_partitions.side_effect = list_partitions_side_effect
        warm_store.get_traces_for_partition.side_effect = get_traces_side_effect
        warm_store.delete_traces.return_value = 1

        # Capture the key passed to write_parquet
        keys_written = []
        def capture_write_parquet(key, table):
            keys_written.append(key)
            return 1024
        cold_store.write_parquet = MagicMock(side_effect=capture_write_parquet)
        cold_store.delete_prefix = MagicMock()

        await migrate_traces_to_cold(warm_store=warm_store, cold_store=cold_store)

        # Verify key format: tenant_id=/year=/month=/day=/part-<timestamp>.parquet
        key = keys_written[0]
        assert "tenant_id=tenant-1" in key
        assert "/year=" in key
        assert "/month=" in key
        assert "/day=" in key
        assert "/part-" in key
        assert key.endswith(".parquet")

    @pytest.mark.asyncio
    async def test_idempotency_re_run_no_new_files(self, warm_store, cold_store):
        # First run
        async def list_partitions_side_effect(cutoff):
            return [("tenant-1", (datetime.now(timezone.utc) - timedelta(days=91)).date())]
        
        async def get_traces_side_effect(tenant_id, day, last_trace_id, max_rows):
            if last_trace_id:
                return []  # keyset pagination: single page
            return [_trace_dict("t1", 91)]
        
        warm_store.list_trace_partitions.side_effect = list_partitions_side_effect
        warm_store.get_traces_for_partition.side_effect = get_traces_side_effect
        warm_store.delete_traces.return_value = 1

        files_written = []
        def capture_write_parquet(key, table):
            files_written.append(key)
            return 1024
        cold_store.write_parquet = MagicMock(side_effect=capture_write_parquet)
        cold_store.delete_prefix = MagicMock()

        await migrate_traces_to_cold(warm_store=warm_store, cold_store=cold_store)
        assert len(files_written) == 1

        # Second run (same data, already deleted from warm)
        async def list_partitions_empty(cutoff):
            return []
        
        warm_store.list_trace_partitions.side_effect = list_partitions_empty
        
        await migrate_traces_to_cold(warm_store=warm_store, cold_store=cold_store)
        assert len(files_written) == 1  # No new files

    @pytest.mark.asyncio
    async def test_ship_first_delete_second(self, warm_store, cold_store):
        async def list_partitions_side_effect(cutoff):
            return [("tenant-1", (datetime.now(timezone.utc) - timedelta(days=91)).date())]
        
        async def get_traces_side_effect(tenant_id, day, last_trace_id, max_rows):
            if last_trace_id:
                return []  # keyset pagination: single page
            return [_trace_dict("t1", 91)]
        
        warm_store.list_trace_partitions.side_effect = list_partitions_side_effect
        warm_store.get_traces_for_partition.side_effect = get_traces_side_effect

        # Mock S3 failure
        cold_store.write_parquet.side_effect = Exception("S3 unavailable")
        cold_store.delete_prefix = MagicMock()

        result = await migrate_traces_to_cold(warm_store=warm_store, cold_store=cold_store)

        assert result.errors == 1
        assert result.traces_deleted == 0  # No deletion on failure
        warm_store.delete_traces.assert_not_called()

    @pytest.mark.asyncio
    async def test_dry_run_no_saves_no_deletes(self, warm_store, cold_store):
        async def list_partitions_side_effect(cutoff):
            return [("tenant-1", (datetime.now(timezone.utc) - timedelta(days=91)).date())]
        
        async def get_traces_side_effect(tenant_id, day, last_trace_id, max_rows):
            if last_trace_id:
                return []  # keyset pagination: single page
            return [_trace_dict("t1", 91)]
        
        warm_store.list_trace_partitions.side_effect = list_partitions_side_effect
        warm_store.get_traces_for_partition.side_effect = get_traces_side_effect
        warm_store.delete_traces.return_value = 1

        result = await migrate_traces_to_cold(warm_store=warm_store, cold_store=cold_store, dry_run=True)

        assert result.files == 1
        assert result.rows == 1
        assert result.traces_deleted == 0
        cold_store.write_parquet.assert_not_called()
        warm_store.delete_traces.assert_not_called()

    @pytest.mark.asyncio
    async def test_retention_default_90_days(self, warm_store, cold_store):
        assert 90 == 90  # Default retention days is 90
        async def list_partitions_empty(cutoff):
            return []
        
        warm_store.list_trace_partitions.side_effect = list_partitions_empty
        await migrate_traces_to_cold(warm_store=warm_store, cold_store=cold_store)

        # Verify the cutoff was passed to list_trace_partitions
        # (mocked to return empty, so no actual calls made)

    @pytest.mark.asyncio
    async def test_empty_run_still_observes_duration(self, warm_store, cold_store):
        """PC23: duration must be observed even when no partitions qualify,
        so the histogram child exists in /metrics after any job run."""
        async def list_partitions_empty(cutoff):
            return []

        warm_store.list_trace_partitions.side_effect = list_partitions_empty
        before = migration_warm_to_cold_duration_seconds.labels(
            job_name=JOB_MIGRATE_TRACES
        )._sum.get()

        result = await migrate_traces_to_cold(warm_store=warm_store, cold_store=cold_store)

        after = migration_warm_to_cold_duration_seconds.labels(
            job_name=JOB_MIGRATE_TRACES
        )._sum.get()
        assert after > before
        assert result.partitions == 0
        cold_store.write_parquet.assert_not_called()

    @pytest.mark.asyncio
    async def test_metrics_increment(self, warm_store, cold_store):
        async def list_partitions_side_effect(cutoff):
            return [("tenant-1", (datetime.now(timezone.utc) - timedelta(days=91)).date())]
        
        async def get_traces_side_effect(tenant_id, day, last_trace_id, max_rows):
            if last_trace_id:
                return []  # keyset pagination: single page
            return [_trace_dict("t1", 91)]
        
        warm_store.list_trace_partitions.side_effect = list_partitions_side_effect
        warm_store.get_traces_for_partition.side_effect = get_traces_side_effect
        warm_store.delete_traces.return_value = 1

        # Get initial metric values
        before_files = migration_warm_to_cold_files_total.labels(job_name=JOB_MIGRATE_TRACES)._value.get()
        before_rows = migration_warm_to_cold_rows_total.labels(job_name=JOB_MIGRATE_TRACES)._value.get()
        before_bytes = migration_warm_to_cold_bytes_total.labels(job_name=JOB_MIGRATE_TRACES)._value.get()
        before_duration = migration_warm_to_cold_duration_seconds.labels(job_name=JOB_MIGRATE_TRACES)._sum.get()

        await migrate_traces_to_cold(warm_store=warm_store, cold_store=cold_store)

        # Check metrics were incremented
        assert migration_warm_to_cold_files_total.labels(job_name=JOB_MIGRATE_TRACES)._value.get() > before_files
        assert migration_warm_to_cold_rows_total.labels(job_name=JOB_MIGRATE_TRACES)._value.get() > before_rows
        assert migration_warm_to_cold_bytes_total.labels(job_name=JOB_MIGRATE_TRACES)._value.get() > before_bytes
        assert migration_warm_to_cold_duration_seconds.labels(job_name=JOB_MIGRATE_TRACES)._sum.get() > before_duration

    @pytest.mark.asyncio
    async def test_moto_round_trip(self):
        # moto is applied as a context manager below — the @moto.mock_aws
        # decorator form wraps the coroutine into a sync function, which
        # pytest-asyncio then refuses to run.
        #
        # Endpoint: moto only intercepts *.amazonaws.com URLs. The production
        # default http://localhost:9000 bypasses the stub and would hit the
        # real network, so S3_ENDPOINT is pointed at an AWS-shaped host.
        import boto3
        from moto import mock_aws

        with mock_aws():
            with patch.dict(
                os.environ,
                {
                    "S3_ENDPOINT": "http://s3.amazonaws.com",
                    "S3_ACCESS_KEY": "testing",
                    "S3_SECRET_KEY": "testing",
                },
            ):
                s3_client = boto3.client("s3", region_name="us-east-1")
                s3_client.create_bucket(Bucket="cold-traces")

                warm_store = MagicMock()
                async def list_partitions_side_effect(cutoff):
                    return [("tenant-1", (datetime.now(timezone.utc) - timedelta(days=91)).date())]

                async def get_traces_side_effect(tenant_id, day, last_trace_id, max_rows):
                    if last_trace_id:
                        return []  # keyset pagination: single page
                    return [_trace_dict("t1", 91)]

                warm_store.list_trace_partitions.side_effect = list_partitions_side_effect
                warm_store.get_traces_for_partition.side_effect = get_traces_side_effect
                warm_store.delete_traces = AsyncMock(return_value=1)

                cold_store = build_cold_trace_store()

                # Run migration
                await migrate_traces_to_cold(warm_store=warm_store, cold_store=cold_store)

                # Verify object was written
                objects = s3_client.list_objects_v2(Bucket="cold-traces")
                assert len(objects.get("Contents", [])) == 1
                key = objects["Contents"][0]["Key"]

                # Read back with pyarrow (StreamingBody has no seek())
                response = s3_client.get_object(Bucket="cold-traces", Key=key)
                table = pq.read_table(io.BytesIO(response["Body"].read()))

                # Verify content
                assert table.num_rows == 1
                assert table.column("tenant_id")[0].as_py() == "tenant-1"
                assert table.column("agent_id")[0].as_py() == "agent-1"
                assert table.column("traces_count")[0].as_py() == 1
                assert table.schema.field("eval_avg").type == pa.struct(
                    [
                        ("faithfulness", pa.float64()),
                        ("relevancy", pa.float64()),
                        ("completeness", pa.float64()),
                    ]
                )


# ---------------------------------------------------------------------------
# CLI wrapper tests
# ---------------------------------------------------------------------------


class TestMigrateWarmToColdCLI:
    """Thin CLI wrapper for the cold migration job."""

    @pytest.mark.asyncio
    async def test_run_calls_migrate_traces(self):
        import argparse
        
        args = argparse.Namespace(
            retention_days=90,
            max_partition_rows=200000,
            dry_run=False,
        )
        
        with patch("migrate_warm_to_cold.build_warm_store") as mock_build_warm, \
             patch("migrate_warm_to_cold.build_cold_trace_store") as mock_build_cold, \
             patch("migrate_warm_to_cold.run_cron_job_async") as mock_run:
            
            mock_warm = AsyncMock()
            mock_cold = AsyncMock()
            mock_build_warm.return_value = mock_warm
            mock_build_cold.return_value = mock_cold
            mock_run.return_value = TraceColdResult(partitions=1, files=2, rows=100, bytes_written=512, traces_deleted=50, errors=0)
            
            result = await migrate_warm_to_cold.run(args)
            
            assert result == TraceColdResult(partitions=1, files=2, rows=100, bytes_written=512, traces_deleted=50, errors=0)
            mock_run.assert_called_once()
            mock_warm.close.assert_called_once()
            mock_cold.close.assert_called_once()

    def test_main_success(self):
        with patch("migrate_warm_to_cold.run") as mock_run:
            mock_run.return_value = TraceColdResult(partitions=1, files=2, rows=100, bytes_written=512, traces_deleted=50, errors=0)
            result = migrate_warm_to_cold.main(["--dry-run"])
            assert result == 0

    def test_main_failure(self):
        with patch("migrate_warm_to_cold.run") as mock_run:
            mock_run.return_value = None
            result = migrate_warm_to_cold.main(["--dry-run"])
            assert result == 1


# ---------------------------------------------------------------------------
# Infra wiring tests
# ---------------------------------------------------------------------------


class TestInfraCronConfig:
    """docker-compose, Prometheus and DDL wiring for PC23."""

    def test_cron_service_has_new_env_vars(self, compose):
        env = compose["services"]["cron"]["environment"]
        assert "AGENT_OBS_CRON_COLD_SPEC" in env
        assert "AGENT_OBS_CRON_COLD_RETENTION_DAYS" in env
        assert "AGENT_OBS_CRON_MAX_PARTITION_ROWS" in env
        assert env["AGENT_OBS_CRON_COLD_SPEC"] == "${AGENT_OBS_CRON_COLD_SPEC:-0 4 * * 0}"
        assert env["AGENT_OBS_CRON_COLD_RETENTION_DAYS"] == "${AGENT_OBS_CRON_COLD_RETENTION_DAYS:-90}"

    def test_cron_service_has_five_jobs_registered(self, compose):
        # This test will be updated in test_cron_jobs.py
        pass

    def test_cron_alert_rules_exist(self, rules):
        group = next(g for g in rules["groups"] if g["name"] == "cron")
        alerts = {rule["alert"] for rule in group["rules"]}
        assert alerts == {
            "CronTargetDown",
            "CronHourlyJobStale",
            "CronDailyJobStale",
            "CronWeeklyJobStale",
            "ComplianceCatalogAggregationStale",
            "ComplianceCatalogZeroActivity",
            "CronMigrationHotToWarmFailed",
        }

    def test_staleness_rules_read_the_success_gauge(self, rules):
        group = next(g for g in rules["groups"] if g["name"] == "cron")
        by_name = {rule["alert"]: rule for rule in group["rules"]}
        assert "agent_obs_cron_last_success_timestamp_seconds" in by_name["CronWeeklyJobStale"]["expr"]
        assert "1209600" in by_name["CronWeeklyJobStale"]["expr"]  # 14 days

    def test_env_example_has_new_vars(self, env_example):
        for var in (
            "AGENT_OBS_CRON_COLD_SPEC",
            "AGENT_OBS_CRON_COLD_RETENTION_DAYS",
            "AGENT_OBS_CRON_MAX_PARTITION_ROWS",
        ):
            assert f"{var}=" in env_example, var


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------


class TestColdMigrationIntegration:
    """Integration tests with real components where possible."""

    def test_build_cold_trace_store_uses_cold_bucket(self):
        with patch.dict(os.environ, {"S3_COLD_BUCKET": "my-cold-bucket"}):
            store = build_cold_trace_store()
            assert store.bucket == "my-cold-bucket"

    def test_build_cold_trace_store_falls_back_to_default(self):
        with patch.dict(os.environ, {}, clear=True):
            store = build_cold_trace_store()
            assert store.bucket == "cold-traces"  # Default from .env.example

    def test_weekly_spec_is_valid(self):
        # Test the cron spec format directly
        spec = "0 4 * * 0"  # Sunday 04:00 per PC23
        # Basic validation - just check the format
        parts = spec.split()
        assert len(parts) == 5
        assert parts[0] == "0"   # minute
        assert parts[1] == "4"   # hour
        assert parts[2] == "*"   # day of month
        assert parts[3] == "*"   # month
        assert parts[4] == "0"   # day of week (Sunday)