"""Tests for PC31 — HotStore.get_sampler_rate_history.

Covers the ClickHouse SELECT contract (dict parameters, time-range filter,
chronological order) and the flattening of the sampler-specific numbers out of
the ``resource`` JSON blob into top-level keys.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from agent_obs.guardrail.audit import SamplerRateChangeAuditEvent
from agent_obs.storage.hot import HotStore


def _audit_row(
    audit_id: str = "sra-abc123",
    timestamp: datetime | None = None,
    resource: dict | None = None,
    reason: str = "policy changed from 0.10 to 0.05, reason=cpu_high",
) -> tuple:
    """Build an audit_events_hot row tuple as clickhouse-driver returns it."""
    if timestamp is None:
        timestamp = datetime(2026, 9, 20, 12, 0, 0, tzinfo=timezone.utc)
    if resource is None:
        resource = {
            "type": "sampler_policy",
            "id": "normal-trace",
            "prev_rate": 0.10,
            "new_rate": 0.05,
            "prev_reason": "default",
            "new_reason": "cpu_high",
            "system_cpu_ratio": 0.92,
            "agent_error_rate_5m": 0.01,
            "agent_id": "test-agent",
            "triggering_agent_id": "trigger-agent",
        }
    return (
        audit_id,
        timestamp,
        "",
        json.dumps({"type": "system", "id": "policy-engine"}),
        "sampler.rate_change",
        "applied",
        json.dumps(resource),
        reason,
        "unknown",
        "policy-engine",
    )


@pytest.fixture
def clickhouse():
    client = MagicMock()
    client.execute.return_value = []
    return client


@pytest.fixture
def store(clickhouse):
    """HotStore with the mock client injected into the lazy slot."""
    with patch("clickhouse_driver.Client", return_value=clickhouse):
        instance = HotStore()
    instance._client = clickhouse
    return instance


class TestGetSamplerRateHistorySelect:
    """The SELECT statement the Query API depends on."""

    def test_filters_by_action_and_time_range(self, store, clickhouse):
        from_ts = datetime(2026, 9, 20, tzinfo=timezone.utc)
        to_ts = datetime(2026, 9, 21, 23, 59, 59, 999999, tzinfo=timezone.utc)
        store.get_sampler_rate_history(from_ts, to_ts, limit=50)

        sql, params = clickhouse.execute.call_args[0]
        assert "FROM audit_events_hot" in sql
        assert "action = %(action)s" in sql
        assert "timestamp >= %(from_ts)s" in sql
        assert "timestamp <= %(to_ts)s" in sql
        assert "ORDER BY timestamp ASC" in sql
        assert "LIMIT %(limit)s" in sql
        # Dict parameters, not a list — a list is treated as INSERT row data.
        assert isinstance(params, dict)
        assert params["action"] == "sampler.rate_change"
        assert params["from_ts"] == from_ts
        assert params["to_ts"] == to_ts
        assert params["limit"] == 50

    def test_returns_empty_list_when_no_rows(self, store, clickhouse):
        clickhouse.execute.return_value = []
        result = store.get_sampler_rate_history(
            datetime(2026, 9, 20, tzinfo=timezone.utc),
            datetime(2026, 9, 21, tzinfo=timezone.utc),
        )
        assert result == []

    def test_three_rate_changes_yield_three_events(self, store, clickhouse):
        """PC31 DoD: 3 rate changes → 3 audit events with prev/new rate."""
        ts = datetime(2026, 9, 20, tzinfo=timezone.utc)
        clickhouse.execute.return_value = [
            _audit_row(
                "sra-1",
                ts,
                resource={
                    "type": "sampler_policy",
                    "id": "normal-trace",
                    "prev_rate": 0.10,
                    "new_rate": 0.05,
                    "prev_reason": "default",
                    "new_reason": "cpu_high",
                    "system_cpu_ratio": 0.92,
                    "agent_error_rate_5m": 0.01,
                },
            ),
            _audit_row(
                "sra-2",
                ts,
                resource={
                    "type": "sampler_policy",
                    "id": "normal-trace",
                    "prev_rate": 0.05,
                    "new_rate": 0.30,
                    "prev_reason": "cpu_high",
                    "new_reason": "error_high",
                    "system_cpu_ratio": 0.10,
                    "agent_error_rate_5m": 0.07,
                },
            ),
            _audit_row(
                "sra-3",
                ts,
                resource={
                    "type": "sampler_policy",
                    "id": "normal-trace",
                    "prev_rate": 0.30,
                    "new_rate": 0.10,
                    "prev_reason": "error_high",
                    "new_reason": "default",
                    "system_cpu_ratio": 0.20,
                    "agent_error_rate_5m": 0.01,
                },
            ),
        ]

        events = store.get_sampler_rate_history(
            datetime(2026, 9, 20, tzinfo=timezone.utc),
            datetime(2026, 9, 21, tzinfo=timezone.utc),
        )

        assert len(events) == 3
        # Each event contains prev/new rate and reason.
        assert events[0]["prev_rate"] == pytest.approx(0.10)
        assert events[0]["new_rate"] == pytest.approx(0.05)
        assert events[0]["new_reason"] == "cpu_high"
        assert events[1]["prev_rate"] == pytest.approx(0.05)
        assert events[1]["new_rate"] == pytest.approx(0.30)
        assert events[1]["new_reason"] == "error_high"
        assert events[2]["prev_rate"] == pytest.approx(0.30)
        assert events[2]["new_rate"] == pytest.approx(0.10)
        assert events[2]["new_reason"] == "default"
        # Action and decision are preserved.
        assert all(e["action"] == "sampler.rate_change" for e in events)
        assert all(e["decision"] == "applied" for e in events)


class TestResourceFlattening:
    """The sampler-specific numbers are flattened out of resource JSON."""

    def test_flattens_rate_metadata(self, store, clickhouse):
        clickhouse.execute.return_value = [_audit_row()]
        events = store.get_sampler_rate_history(
            datetime(2026, 9, 20, tzinfo=timezone.utc),
            datetime(2026, 9, 21, tzinfo=timezone.utc),
        )
        assert len(events) == 1
        event = events[0]
        assert event["prev_rate"] == pytest.approx(0.10)
        assert event["new_rate"] == pytest.approx(0.05)
        assert event["prev_reason"] == "default"
        assert event["new_reason"] == "cpu_high"
        assert event["system_cpu_ratio"] == pytest.approx(0.92)
        assert event["agent_error_rate_5m"] == pytest.approx(0.01)
        assert event["agent_id"] == "test-agent"
        assert event["triggering_agent_id"] == "trigger-agent"

    def test_flattens_agent_id_fields(self, store, clickhouse):
        """Test that agent_id and triggering_agent_id are properly flattened."""
        clickhouse.execute.return_value = [
            _audit_row(
                resource={
                    "type": "sampler_policy",
                    "id": "normal-trace",
                    "prev_rate": 0.10,
                    "new_rate": 0.05,
                    "prev_reason": "default",
                    "new_reason": "cpu_high",
                    "system_cpu_ratio": 0.92,
                    "agent_error_rate_5m": 0.01,
                    "agent_id": "custom-agent",
                    "triggering_agent_id": "triggering-custom",
                }
            )
        ]
        events = store.get_sampler_rate_history(
            datetime(2026, 9, 20, tzinfo=timezone.utc),
            datetime(2026, 9, 21, tzinfo=timezone.utc),
        )
        assert len(events) == 1
        event = events[0]
        assert event["agent_id"] == "custom-agent"
        assert event["triggering_agent_id"] == "triggering-custom"

    def test_missing_agent_id_fields_default_to_star(self, store, clickhouse):
        """Test that missing agent_id fields default to '*'."""
        clickhouse.execute.return_value = [
            _audit_row(
                resource={
                    "type": "sampler_policy",
                    "id": "normal-trace",
                    "prev_rate": 0.10,
                    "new_rate": 0.05,
                    "prev_reason": "default",
                    "new_reason": "cpu_high",
                    "system_cpu_ratio": 0.92,
                    "agent_error_rate_5m": 0.01,
                    # No agent_id or triggering_agent_id fields
                }
            )
        ]
        events = store.get_sampler_rate_history(
            datetime(2026, 9, 20, tzinfo=timezone.utc),
            datetime(2026, 9, 21, tzinfo=timezone.utc),
        )
        assert len(events) == 1
        event = events[0]
        assert event["agent_id"] == "*"
        assert event["triggering_agent_id"] == "*"

    def test_keeps_base_audit_fields(self, store, clickhouse):
        clickhouse.execute.return_value = [_audit_row()]
        events = store.get_sampler_rate_history(
            datetime(2026, 9, 20, tzinfo=timezone.utc),
            datetime(2026, 9, 21, tzinfo=timezone.utc),
        )
        event = events[0]
        assert event["audit_id"] == "sra-abc123"
        assert event["trace_id"] == ""
        assert event["actor"] == {"type": "system", "id": "policy-engine"}
        assert event["user_agent"] == "policy-engine"
        assert "policy changed from 0.10 to 0.05" in event["reason"]

    def test_missing_resource_keys_become_none(self, store, clickhouse):
        """A malformed resource blob must not raise — missing keys are None."""
        clickhouse.execute.return_value = [
            _audit_row(resource={"type": "sampler_policy", "id": "normal-trace"})
        ]
        events = store.get_sampler_rate_history(
            datetime(2026, 9, 20, tzinfo=timezone.utc),
            datetime(2026, 9, 21, tzinfo=timezone.utc),
        )
        assert events[0]["prev_rate"] is None
        assert events[0]["new_rate"] is None

    def test_resource_already_a_dict_is_not_redecoded(self, store, clickhouse):
        """clickhouse-driver may return JSON columns as str or as parsed dict."""
        resource = {
            "type": "sampler_policy",
            "id": "normal-trace",
            "prev_rate": 0.10,
            "new_rate": 0.30,
            "prev_reason": "default",
            "new_reason": "error_high",
            "system_cpu_ratio": 0.10,
            "agent_error_rate_5m": 0.07,
        }
        row = list(_audit_row(resource=resource))
        row[6] = resource  # dict, not JSON string
        clickhouse.execute.return_value = [tuple(row)]
        events = store.get_sampler_rate_history(
            datetime(2026, 9, 20, tzinfo=timezone.utc),
            datetime(2026, 9, 21, tzinfo=timezone.utc),
        )
        assert events[0]["new_rate"] == pytest.approx(0.30)
        assert events[0]["new_reason"] == "error_high"


class TestRowOrdering:
    def test_rows_preserve_clickhouse_order(self, store, clickhouse):
        """The method must not re-sort — ClickHouse already returns ASC."""
        ts = datetime(2026, 9, 20, tzinfo=timezone.utc)
        clickhouse.execute.return_value = [
            _audit_row("sra-first", ts),
            _audit_row("sra-second", ts),
            _audit_row("sra-third", ts),
        ]
        events = store.get_sampler_rate_history(
            datetime(2026, 9, 20, tzinfo=timezone.utc),
            datetime(2026, 9, 21, tzinfo=timezone.utc),
        )
        assert [e["audit_id"] for e in events] == ["sra-first", "sra-second", "sra-third"]


class TestEndToEndAuditEventToQuery:
    """A real SamplerRateChangeAuditEvent written then read back flattens."""

    def test_event_written_then_read_flattens(self, store, clickhouse):
        event = SamplerRateChangeAuditEvent.for_rate_change(
            prev_rate=0.10,
            new_rate=0.05,
            prev_reason="default",
            new_reason="cpu_high",
            system_cpu_ratio=0.92,
            agent_error_rate_5m=0.01,
            agent_id="test-agent",
            triggering_agent_id="trigger-agent",
        )
        # Simulate what write_audit_event inserts: the ClickHouse row tuple.
        clickhouse.execute.return_value = [event.to_clickhouse_row()]

        events = store.get_sampler_rate_history(
            datetime(2026, 9, 20, tzinfo=timezone.utc),
            datetime(2026, 9, 21, tzinfo=timezone.utc),
        )
        assert len(events) == 1
        read = events[0]
        assert read["audit_id"] == event.audit_id
        assert read["action"] == "sampler.rate_change"
        assert read["prev_rate"] == pytest.approx(0.10)
        assert read["new_rate"] == pytest.approx(0.05)
        assert read["new_reason"] == "cpu_high"
        assert read["system_cpu_ratio"] == pytest.approx(0.92)
        assert read["agent_id"] == "test-agent"
        assert read["triggering_agent_id"] == "trigger-agent"
        # Flattened dict matches the event's own to_dict for the rate fields.
        original = event.to_dict()
        for key in (
            "prev_rate",
            "new_rate",
            "prev_reason",
            "new_reason",
            "system_cpu_ratio",
            "agent_error_rate_5m",
            "agent_id",
            "triggering_agent_id",
        ):
            assert read[key] == original[key]
