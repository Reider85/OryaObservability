"""Tests for PP22 — Athena integration for cold-storage SQL queries.

Unit tests drive AthenaClient against a scripted boto3 double (deterministic
control over states, scanned bytes and pagination); one smoke test exercises
the real boto3 path against the moto backend. The live-Athena integration test
is env-gated.
"""

import asyncio
import os
from datetime import date

import pytest
from prometheus_client import REGISTRY

from agent_obs.storage.athena_client import (
    AthenaClient,
    AthenaCostGuardError,
    AthenaQueryError,
    AthenaTimeoutError,
    build_trace_fields_sql,
)

GIB = 1_073_741_824


# ---------------------------------------------------------------------------
# Scripted boto3-Athena double
# ---------------------------------------------------------------------------


def _page(columns, rows, next_token=None):
    page = {
        "ResultSet": {
            "ResultSetMetadata": {
                "ColumnInfo": [
                    {"Name": name, "Type": col_type}
                    for name, col_type in columns
                ]
            },
            "Rows": [
                {"Data": [{"VarCharValue": str(value)} for value in row]}
                for row in rows
            ],
        }
    }
    if next_token:
        page["NextToken"] = next_token
    return page


class FakeAthenaClient:
    """Scripted stand-in for the boto3 Athena client.

    ``states`` is a list of per-poll ``get_query_execution`` responses
    (the last one repeats); ``pages`` is the list of ``get_query_results``
    responses keyed in order via NextToken.
    """

    def __init__(self, states=None, pages=None):
        self.states = list(states) if states else [{"state": "SUCCEEDED", "scanned": 0}]
        self.pages = list(pages) if pages else [_page([], [])]
        self.start_calls = []
        self.result_calls = []
        self.stopped = []
        self._poll_count = 0

    def start_query_execution(self, **kwargs):
        self.start_calls.append(kwargs)
        return {"QueryExecutionId": "qid-1"}

    def get_query_execution(self, QueryExecutionId):
        index = min(self._poll_count, len(self.states) - 1)
        self._poll_count += 1
        entry = self.states[index]
        return {
            "QueryExecution": {
                "Status": {"State": entry["state"]},
                "Statistics": {"DataScannedInBytes": entry.get("scanned", 0)},
            }
        }

    def get_query_results(self, **kwargs):
        self.result_calls.append(kwargs)
        index = len(self.result_calls) - 1
        return self.pages[min(index, len(self.pages) - 1)]

    def stop_query_execution(self, QueryExecutionId):
        self.stopped.append(QueryExecutionId)


def _client_with(fake, **kwargs):
    client = AthenaClient(**kwargs)
    client._client = fake
    return client


def _run(coro):
    return asyncio.run(coro)


def _counter_value(name):
    return REGISTRY.get_sample_value(name) or 0.0


# ---------------------------------------------------------------------------
# AthenaClient.query
# ---------------------------------------------------------------------------


class TestQuery:
    def test_returns_rows_and_skips_athena_header_row(self):
        columns = [("agent_id", "string"), ("traces_count", "int"), ("error_rate", "double")]
        page = _page(
            columns,
            [
                ["agent_id", "traces_count", "error_rate"],  # Athena header row
                ["pilot", "42", "0.25"],
            ],
        )
        fake = FakeAthenaClient(
            states=[{"state": "RUNNING", "scanned": 100}, {"state": "SUCCEEDED", "scanned": 128}],
            pages=[page],
        )
        client = _client_with(fake, database="observability")

        rows = _run(client.query("SELECT * FROM observability.cold_traces LIMIT 10"))

        assert rows == [{"agent_id": "pilot", "traces_count": 42, "error_rate": 0.25}]
        # Database and output location passed to start_query_execution
        assert fake.start_calls[0]["QueryExecutionContext"] == {"Database": "observability"}
        assert fake.start_calls[0]["ResultConfiguration"]["OutputLocation"].startswith("s3://")

    def test_paginates_with_next_token(self):
        columns = [("agent_id", "string")]
        pages = [
            _page(columns, [["agent_id"], ["pilot"]], next_token="tok-1"),
            _page(columns, [["batch"]]),
        ]
        fake = FakeAthenaClient(pages=pages)
        client = _client_with(fake)

        rows = _run(client.query("SELECT agent_id FROM observability.cold_traces"))

        assert [row["agent_id"] for row in rows] == ["pilot", "batch"]
        # First page has no NextToken, second does
        assert "NextToken" not in fake.result_calls[0]
        assert fake.result_calls[1]["NextToken"] == "tok-1"

    def test_empty_result_returns_empty_list(self):
        fake = FakeAthenaClient(pages=[_page([], [])])
        client = _client_with(fake)

        assert _run(client.query("SELECT 1")) == []

    def test_failed_state_raises_with_reason(self):
        fake = FakeAthenaClient()
        # StateChangeReason flows through Status
        fake.get_query_execution = lambda QueryExecutionId: {
            "QueryExecution": {
                "Status": {"State": "FAILED", "StateChangeReason": "TABLE_NOT_FOUND: cold_traces"},
                "Statistics": {"DataScannedInBytes": 0},
            }
        }
        client = _client_with(fake)

        with pytest.raises(AthenaQueryError, match="TABLE_NOT_FOUND"):
            _run(client.query("SELECT 1"))
        # A failed query is not stopped again
        assert fake.stopped == []

    def test_timeout_stops_query(self):
        fake = FakeAthenaClient(states=[{"state": "RUNNING", "scanned": 1}])
        client = _client_with(fake, timeout=0.05, poll_interval=0.01)

        with pytest.raises(AthenaTimeoutError, match="timed out"):
            _run(client.query("SELECT 1"))
        assert fake.stopped == ["qid-1"]

    def test_default_timeout_is_60_seconds(self, monkeypatch):
        monkeypatch.delenv("ATHENA_QUERY_TIMEOUT", raising=False)
        assert AthenaClient().timeout == 60.0

    def test_timeout_env_override(self, monkeypatch):
        monkeypatch.setenv("ATHENA_QUERY_TIMEOUT", "30")
        assert AthenaClient().timeout == 30.0


# ---------------------------------------------------------------------------
# Cost guard (mid-flight abort)
# ---------------------------------------------------------------------------


class TestCostGuard:
    def test_stops_expensive_scan_and_raises(self):
        scanned = GIB + 1
        before = _counter_value("agent_obs_athena_cost_guard_triggered_total")
        fake = FakeAthenaClient(
            states=[{"state": "RUNNING", "scanned": scanned}, {"state": "SUCCEEDED", "scanned": scanned}]
        )
        client = _client_with(fake, max_scan_bytes=GIB, poll_interval=0.01)

        with pytest.raises(AthenaCostGuardError) as exc_info:
            _run(client.query("SELECT * FROM observability.cold_traces"))

        # Mid-flight abort: stopped before SUCCEEDED could be observed
        assert fake.stopped == ["qid-1"]
        assert "partition" in str(exc_info.value)
        assert str(GIB) in str(exc_info.value)
        assert _counter_value("agent_obs_athena_cost_guard_triggered_total") == before + 1

    def test_scanned_bytes_metric_incremented_on_guard_abort(self):
        scanned = GIB + 1
        before = _counter_value("agent_obs_athena_data_scanned_bytes_total")
        fake = FakeAthenaClient(states=[{"state": "RUNNING", "scanned": scanned}])
        client = _client_with(fake, max_scan_bytes=GIB, poll_interval=0.01)

        with pytest.raises(AthenaCostGuardError):
            _run(client.query("SELECT 1"))

        assert _counter_value("agent_obs_athena_data_scanned_bytes_total") == before + scanned

    def test_scan_below_limit_passes(self):
        fake = FakeAthenaClient(
            states=[{"state": "RUNNING", "scanned": GIB - 1}, {"state": "SUCCEEDED", "scanned": GIB - 1}]
        )
        client = _client_with(fake, max_scan_bytes=GIB, poll_interval=0.01)

        assert _run(client.query("SELECT 1")) == []
        assert fake.stopped == []

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("ATHENA_MAX_SCAN_BYTES", "4096")
        assert AthenaClient().max_scan_bytes == 4096


# ---------------------------------------------------------------------------
# SQL builder: partition pruning + injection guard
# ---------------------------------------------------------------------------


class TestBuildTraceFieldsSql:
    def test_partition_pruning_with_tenant_and_day(self):
        sql = build_trace_fields_sql(
            database="observability",
            trace_id="abc-123",
            fields=["traces_count", "cost_usd_sum"],
            tenant_id="acme",
            day="2026-10-28",
        )
        assert "SELECT traces_count, cost_usd_sum FROM observability.cold_traces" in sql
        assert "tenant_id = 'acme'" in sql
        assert "year = 2026" in sql
        assert "month = 10" in sql
        assert "day = 28" in sql
        assert "LIMIT 100" in sql
        assert "-- drill-down trace_id: abc-123" in sql

    def test_accepts_date_object_for_day(self):
        sql = build_trace_fields_sql(tenant_id="acme", day=date(2026, 1, 5))
        assert "year = 2026" in sql
        assert "month = 1" in sql
        assert "day = 5" in sql

    def test_without_partition_info_is_full_scan_with_limit(self):
        sql = build_trace_fields_sql(trace_id="abc-123")
        assert "WHERE" not in sql
        assert "LIMIT 100" in sql

    def test_rejects_sql_injection_in_trace_id(self):
        with pytest.raises(ValueError, match="unsafe trace_id"):
            build_trace_fields_sql(trace_id="x'; DROP TABLE cold_traces; --")

    def test_rejects_sql_injection_in_tenant_id(self):
        with pytest.raises(ValueError, match="unsafe tenant_id"):
            build_trace_fields_sql(tenant_id="a' OR '1'='1")

    def test_rejects_unsafe_field_names(self):
        with pytest.raises(ValueError, match="unsafe field name"):
            build_trace_fields_sql(fields=["1nvalid; DROP TABLE x"])

    def test_rejects_unsafe_database(self):
        with pytest.raises(ValueError, match="unsafe database"):
            build_trace_fields_sql(database="obs; DROP DATABASE observability")

    def test_rejects_malformed_day(self):
        with pytest.raises(ValueError, match="unsafe day"):
            build_trace_fields_sql(day="28-10-2026")

    def test_rejects_day_out_of_range(self):
        with pytest.raises(ValueError, match="unsafe day"):
            build_trace_fields_sql(day="2026-13-45")


# ---------------------------------------------------------------------------
# DrillDownService hook contract (PP21 x PP22)
# ---------------------------------------------------------------------------


class TestDrillDownHookContract:
    def test_query_trace_fields_returns_hook_shape(self):
        columns = [("traces_count", "int"), ("tenant_id", "string")]
        page = _page(
            columns,
            [["traces_count", "tenant_id"], ["42", "acme"]],
        )
        fake = FakeAthenaClient(pages=[page])
        client = _client_with(fake)

        result = _run(
            client.query_trace_fields(
                "abc-123", ["traces_count"], tenant_id="acme", day="2026-10-28"
            )
        )

        assert result["fields"] == {"traces_count": 42}
        assert result["metadata"]["storage_tier"] == "cold"
        assert result["metadata"]["source"] == "athena"
        assert result["metadata"]["mfa_used"] is False
        assert result["metadata"]["query_latency_ms"] >= 0

    def test_drill_down_service_uses_athena_hook_for_cold_tier(self):
        from agent_obs.storage.drill_down import DrillDownRequest, DrillDownService

        async def athena_query(trace_id, fields):
            return {"fields": {"traces_count": 42}, "metadata": {"storage_tier": "cold"}}

        service = DrillDownService(
            hot_store=None,
            warm_store=None,
            cold_store=None,
            redis_client=None,
            athena_query=athena_query,
        )

        class _Missing:
            def get_spans(self, trace_id):
                raise ValueError("not found")

            async def get_trace(self, trace_id):
                raise ValueError("not found")

            def list_keys(self, prefix=""):
                raise ValueError("not found")

        service.hot_store = _Missing()
        service.warm_store = _Missing()
        service.cold_store = _Missing()

        result = _run(
            service.drill_down(DrillDownRequest(trace_id="abc-123", fields=[], user="alice"))
        )

        assert result.storage_tier == "cold"
        assert result.fields == {"traces_count": 42}


# ---------------------------------------------------------------------------
# moto smoke test: real boto3 client against the moto Athena backend
# ---------------------------------------------------------------------------


class TestMotoBackend:
    def test_query_against_moto_backend(self):
        moto = pytest.importorskip("moto")
        boto3 = pytest.importorskip("boto3")

        with moto.mock_aws():
            client = AthenaClient(region="us-east-1")
            client._client = boto3.client("athena", region_name="us-east-1")
            rows = _run(client.query("SELECT 1"))
            assert rows == []


# ---------------------------------------------------------------------------
# Live integration test (env-gated)
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.skipif(
    not os.environ.get("ATHENA_INTEGRATION"),
    reason="Requires real AWS Athena (set ATHENA_INTEGRATION=1)",
)
class TestLiveAthena:
    async def test_real_athena_select_one(self):
        client = AthenaClient()
        rows = await client.query("SELECT 1 AS one")
        assert rows
        assert int(rows[0]["one"]) == 1
