"""PP35 — HotStore multi-host failover.

Covers replica-list parsing (``CLICKHOUSE_HOSTS``), failover on
connection-level errors, sticky behaviour after failover, cooldown, and the
critical invariant that query errors are NEVER retried on another replica.

The driver client is mocked throughout — no real ClickHouse is involved, so
this file runs in the fast suite.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest
from clickhouse_driver import errors as ch_errors

from agent_obs.storage.hot import (
    REPLICA_COOLDOWN_SECONDS,
    HotStore,
    _parse_replica,
    _resolve_replicas,
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.delenv("CLICKHOUSE_HOSTS", raising=False)
    monkeypatch.delenv("CLICKHOUSE_HOST", raising=False)
    monkeypatch.delenv("CLICKHOUSE_PORT", raising=False)


def _network_error() -> ch_errors.NetworkError:
    return ch_errors.NetworkError("Connection refused")


@pytest.fixture
def make_store(request):
    """Factory: ``make_store({("ch-1", 9000): mock_client, ...})`` → HotStore.

    The driver patch stays active for the whole test (connection is lazy, so
    a with-block around construction only would miss the actual connect).
    """

    def _make(replica_clients: dict[tuple[str, int], MagicMock]) -> HotStore:
        def factory(host, port, **kwargs):
            key = (host, port)
            if key not in replica_clients:
                raise AssertionError(f"unexpected connect to {key}")
            return replica_clients[key]

        patcher = patch("clickhouse_driver.Client", side_effect=factory)
        patcher.start()
        request.addfinalizer(patcher.stop)
        return HotStore(hosts=[f"{h}:{p}" for h, p in replica_clients])

    return _make


class TestReplicaParsing:
    def test_single_host_fallback(self):
        with patch.dict(os.environ, {"CLICKHOUSE_HOST": "ch-a", "CLICKHOUSE_PORT": "9001"}, clear=False):
            assert _resolve_replicas() == [("ch-a", 9001)]

    def test_env_host_list(self):
        env = {"CLICKHOUSE_HOSTS": "ch-1:9000, ch-2, ch-3:9010"}
        with patch.dict(os.environ, env, clear=False):
            assert _resolve_replicas() == [("ch-1", 9000), ("ch-2", 9000), ("ch-3", 9010)]

    def test_explicit_hosts_arg_beats_env(self):
        with patch.dict(os.environ, {"CLICKHOUSE_HOSTS": "env-host:9000"}, clear=False):
            assert _resolve_replicas(["arg-host:9002"]) == [("arg-host", 9002)]

    def test_parse_replica_rejects_empty(self):
        with pytest.raises(ValueError):
            _parse_replica("", 9000)

    def test_store_defaults_to_single_replica(self):
        with patch.dict(os.environ, {"CLICKHOUSE_HOST": "only"}, clear=False):
            store = HotStore()
        assert store._replicas == [("only", 9000)]

    def test_store_takes_replicas_from_env(self):
        with patch.dict(os.environ, {"CLICKHOUSE_HOSTS": "a:9000,b:9001"}, clear=False):
            store = HotStore()
        assert store._replicas == [("a", 9000), ("b", 9001)]


class TestFailoverOnWrite:
    def test_write_fails_over_to_second_replica(self, make_store):
        """kill ch-1 → SDK continues writing via ch-2 (PP35 DoD)."""
        dead = MagicMock()
        dead.execute.side_effect = _network_error()
        alive = MagicMock()
        alive.execute.return_value = 1

        store = make_store({("ch-1", 9000): dead, ("ch-2", 9000): alive})

        result = store._with_failover(lambda c: c.execute("INSERT INTO spans_hot VALUES"))

        assert result == 1
        dead.execute.assert_called_once()
        alive.execute.assert_called_once()
        # Sticky: subsequent ops go straight to ch-2 without touching ch-1.
        store._with_failover(lambda c: c.execute("SELECT 1"))
        assert dead.execute.call_count == 1
        assert alive.execute.call_count == 2

    def test_failover_marks_replica_dead_with_cooldown(self, make_store):
        dead = MagicMock()
        dead.execute.side_effect = _network_error()
        alive = MagicMock()
        alive.execute.return_value = 1
        store = make_store({("ch-1", 9000): dead, ("ch-2", 9000): alive})

        store._with_failover(lambda c: c.execute("SELECT 1"))

        assert 0 in store._dead_until
        assert store._dead_until[0] > 0

    def test_dead_replica_retried_after_cooldown(self, make_store):
        """After the cooldown expires the replica is eligible again."""
        ch1 = MagicMock()
        ch1.execute.side_effect = _network_error()
        ch2 = MagicMock()
        ch2.execute.return_value = 1
        store = make_store({("ch-1", 9000): ch1, ("ch-2", 9000): ch2})

        store._with_failover(lambda c: c.execute("SELECT 1"))  # ch-1 dies
        assert 0 in store._dead_until

        # ch-1 recovers: clear the error and expire the cooldown.
        ch1.execute.side_effect = None
        ch1.execute.return_value = 1
        store._dead_until[0] = 0.0
        store._active_idx = 0
        store._drop_client()
        store._client_host = ("ch-1", 9000)

        result = store._with_failover(lambda c: c.execute("SELECT 1"))
        assert result == 1
        # ch-1 was reconnected (fresh client) and served the query — one
        # failed call before recovery + one successful call after.
        assert ch1.execute.call_count == 2

    def test_all_replicas_down_raises_last_error(self, make_store):
        first = MagicMock()
        first.execute.side_effect = _network_error()
        second = MagicMock()
        second.execute.side_effect = TimeoutError("timed out")
        store = make_store({("ch-1", 9000): first, ("ch-2", 9000): second})

        with pytest.raises((ch_errors.NetworkError, TimeoutError)):
            store._with_failover(lambda c: c.execute("SELECT 1"))

        first.execute.assert_called_once()
        second.execute.assert_called_once()

    def test_failover_does_not_loop_more_than_replica_count(self, make_store):
        flaky = MagicMock()
        flaky.execute.side_effect = _network_error()
        store = make_store({("ch-1", 9000): flaky})

        with pytest.raises(ch_errors.NetworkError):
            store._with_failover(lambda c: c.execute("SELECT 1"))
        assert flaky.execute.call_count == 1


class TestNoFailoverOnQueryError:
    def test_server_exception_does_not_failover(self, make_store):
        """A query error must surface immediately, not hop replicas."""
        broken = MagicMock()
        broken.execute.side_effect = ch_errors.ServerException("Syntax error", code=62)
        healthy = MagicMock()
        store = make_store({("ch-1", 9000): broken, ("ch-2", 9000): healthy})

        with pytest.raises(ch_errors.ServerException):
            store._with_failover(lambda c: c.execute("SELEC 1"))

        healthy.execute.assert_not_called()
        assert 0 not in store._dead_until


class TestAvailability:
    def test_available_when_any_replica_up(self, make_store):
        dead = MagicMock()
        dead.execute.side_effect = _network_error()
        alive = MagicMock()
        alive.execute.return_value = [(1,)]
        store = make_store({("ch-1", 9000): dead, ("ch-2", 9000): alive})

        assert store.is_available() is True

    def test_unavailable_when_all_down(self, make_store):
        a = MagicMock()
        a.execute.side_effect = _network_error()
        b = MagicMock()
        b.execute.side_effect = _network_error()
        store = make_store({("ch-1", 9000): a, ("ch-2", 9000): b})

        assert store.is_available() is False


class TestPublicApiUsesFailover:
    """Public methods must go through the failover executor, not raw execute."""

    def test_write_audit_event_fails_over(self, make_store):
        dead = MagicMock()
        dead.execute.side_effect = _network_error()
        alive = MagicMock()
        alive.execute.return_value = 1
        store = make_store({("ch-1", 9000): dead, ("ch-2", 9000): alive})

        event = MagicMock()
        event.to_clickhouse_row.return_value = (
            "a", "b", "c", "d", "e", "f", "g", "h", "i", "j",
        )
        event.audit_id = "audit-1"
        event.action = "test"
        event.reason = "unit"

        store.write_audit_event(event)

        alive.execute.assert_called_once()
        sql = alive.execute.call_args[0][0]
        assert "INSERT INTO audit_events_hot" in sql

    def test_spans_batch_write_fails_over(self, make_store):
        dead = MagicMock()
        dead.execute.side_effect = _network_error()
        alive = MagicMock()
        alive.execute.return_value = 1
        store = make_store({("ch-1", 9000): dead, ("ch-2", 9000): alive})

        store.write_spans_batch([{"trace_id": "t1", "span_id": "s1"}])

        alive.execute.assert_called_once()
        sql = alive.execute.call_args[0][0]
        assert "INSERT INTO spans_hot" in sql

    def test_injected_client_is_respected(self):
        """Existing tests inject ``store._client`` — the failover layer must
        not replace it on the success path."""
        injected = MagicMock()
        injected.execute.return_value = []
        store = HotStore(hosts=["only:9000"])
        store._client = injected

        store.get_audit_events(limit=5)

        injected.execute.assert_called_once()


class TestCooldownConstant:
    def test_cooldown_is_thirty_seconds(self):
        assert REPLICA_COOLDOWN_SECONDS == 30.0
