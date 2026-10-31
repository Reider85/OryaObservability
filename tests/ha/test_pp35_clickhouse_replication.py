"""PP35 — ClickHouse cluster replication + Keeper quorum (integration).

Requires the HA stack (docker compose -f infra/docker-compose.ha.yml up -d
clickhouse1 clickhouse2 clickhouse3 clickhouse4 clickhouse-keeper1
clickhouse-keeper2 clickhouse-keeper3 clickhouse-init-ha certs-init).
Skipped automatically when the Docker daemon is unavailable — consistent with
the rest of tests/ha (never collected by the fast suite).

DoD coverage:
- Keeper quorum: 3 nodes, a leader is elected.
- ReplicatedMergeTree DDL present on all 4 nodes (2 shards x 2 replicas).
- SDK kill ch-1 → writes continue via ch-2 in < 10 s (HotStore failover).
- restart ch-1 → replica resyncs (queue drains, row appears on both replicas).
"""

from __future__ import annotations

import subprocess
import time
import uuid

import pytest

# NB: pytest-timeout is not installed in this repo — no timeout mark here.
# The module-level waits are bounded by explicit deadlines in each test.

COMPOSE = ["docker", "compose", "-f", "infra/docker-compose.ha.yml"]
CH_NODES = ["clickhouse1", "clickhouse2", "clickhouse3", "clickhouse4"]
KEEPER_NODES = ["clickhouse-keeper1", "clickhouse-keeper2", "clickhouse-keeper3"]
# Host-mapped native ports (see docker-compose.ha.yml: 9001..9004 -> 9000).
SDK_HOSTS = [
    "localhost:9001",
    "localhost:9002",
    "localhost:9003",
    "localhost:9004",
]


def _docker_available() -> bool:
    """Probe the daemon with retries.

    Called EAGERLY at import (pytest 9 bool()s skipif conditions — a callable
    object would be truthy and skip unconditionally). Retries + a generous
    timeout absorb the slow `docker info` cold start on WSL2 (~5s warm).
    """
    for attempt in range(3):
        try:
            subprocess.run(
                ["docker", "info"],
                stdin=subprocess.DEVNULL,  # WinError 6: pytest collection runs with an invalid inherited stdin handle
                capture_output=True,
                timeout=30,
                check=True,
            )
            return True
        except (OSError, subprocess.SubprocessError):
            if attempt < 2:
                time.sleep(3)
    return False


requires_docker = pytest.mark.skipif(
    not _docker_available(),
    reason="Docker daemon unavailable — PP35 integration tests need the HA stack",
)


def _compose(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        [*COMPOSE, *args],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=check,
    )


def _ch_query(node: str, query: str) -> str:
    """Run a clickhouse-client query inside one node."""
    result = _compose("exec", "-T", node, "clickhouse-client", "--query", query)
    return result.stdout.strip()


def _keeper_alive(node: str) -> bool:
    result = _compose(
        "exec", "-T", node,
        "clickhouse-keeper-client", "--host", "127.0.0.1", "--port", "9181",
        "--query", "srvr",
        check=False,
    )
    return result.returncode == 0


@pytest.fixture(scope="module")
def ha_stack():
    """Ensure the ClickHouse + Keeper subset of the HA stack is running."""
    up = _compose(
        "up", "-d", "--wait",
        "certs-init",
        "clickhouse-keeper1", "clickhouse-keeper2", "clickhouse-keeper3",
        "clickhouse1", "clickhouse2", "clickhouse3", "clickhouse4",
        "clickhouse-init-ha",
        check=False,
    )
    if up.returncode != 0:
        pytest.skip(f"HA stack failed to start: {up.stderr[-500:]}")
    # --wait honours healthchecks; give ON CLUSTER DDL a moment to settle.
    time.sleep(5)
    yield


@requires_docker
class TestKeeperQuorum:
    def test_all_three_keepers_alive(self, ha_stack):
        for node in KEEPER_NODES:
            assert _keeper_alive(node), f"{node} keeper is not responding"

    def test_keeper_quorum_survives_one_node_loss(self, ha_stack):
        """Quorum is 2/3: killing one keeper must leave the cluster writable."""
        _compose("stop", "clickhouse-keeper2", check=False)
        try:
            assert _keeper_alive("clickhouse-keeper1")
            assert _keeper_alive("clickhouse-keeper3")
            # A DDL round-trip proves the remaining quorum still commits.
            probe = f"pp35_probe_{uuid.uuid4().hex[:8]}"
            _ch_query(
                "clickhouse1",
                f"CREATE TABLE IF NOT EXISTS observability.{probe} (x UInt8) "
                f"ENGINE = Memory",
            )
            _ch_query("clickhouse1", f"DROP TABLE IF EXISTS observability.{probe}")
        finally:
            _compose("start", "clickhouse-keeper2", check=False)
            time.sleep(10)


@requires_docker
class TestReplicatedDDL:
    def test_replicated_tables_on_all_four_nodes(self, ha_stack):
        for node in CH_NODES:
            engines = _ch_query(
                node,
                "SELECT count() FROM system.tables "
                "WHERE database = 'observability' "
                "AND engine LIKE 'Replicated%'",
            )
            assert int(engines) >= 1, f"{node} has no Replicated* tables"

    def test_distributed_table_present(self, ha_stack):
        for node in CH_NODES:
            engine = _ch_query(
                node,
                "SELECT engine FROM system.tables "
                "WHERE database = 'observability' AND name = 'spans_hot'",
            )
            assert engine.startswith("Distributed"), (
                f"{node}: spans_hot engine is {engine!r}, expected Distributed"
            )

    def test_shard_macros_resolve(self, ha_stack):
        """{shard}/{replica} macros must expand to a concrete replication path.

        system.tables.engine_full echoes the CREATE-statement text (macros
        kept as written), so the runtime path is read from system.replicas.
        NB: no double quotes in these queries — they get mangled by
        docker-compose exec argument passing on Windows.
        """
        expected = {
            "clickhouse1": "/clickhouse/observability/spans_hot/01",
            "clickhouse2": "/clickhouse/observability/spans_hot/01",
            "clickhouse3": "/clickhouse/observability/spans_hot/02",
            "clickhouse4": "/clickhouse/observability/spans_hot/02",
        }
        for node in CH_NODES:
            path = _ch_query(
                node,
                "SELECT zookeeper_path FROM system.replicas "
                "WHERE database = 'observability' AND table = 'spans_hot_local'",
            ).strip()
            assert path == expected[node], (
                f"{node}: zookeeper_path={path!r}, expected {expected[node]!r} "
                "(macros {shard}/{replica} must resolve per-node)"
            )


@requires_docker
class TestSdkWriteFailover:
    def test_write_continues_after_replica_loss(self, ha_stack):
        """kill ch-1 → SDK keeps writing via another replica (PP35 DoD < 10 s)."""
        from agent_obs.storage.hot import HotStore

        store = HotStore(
            hosts=SDK_HOSTS,
            user="clickhouse",
            password="clickhouse",
        )
        assert store.is_available(), "SDK cannot reach any replica"

        tenant = _shard1_tenant()
        trace_id = f"pp35-failover-{uuid.uuid4().hex[:12]}"

        # Baseline write while all replicas are up.
        store.write_spans_batch([_span_row(trace_id, "span-0", tenant)])

        _compose("stop", "clickhouse1", check=False)
        try:
            started = time.monotonic()
            store.write_spans_batch([_span_row(trace_id, "span-after-kill", tenant)])
            elapsed = time.monotonic() - started
            assert elapsed < 10.0, (
                f"failover took {elapsed:.1f}s — DoD requires < 10 s"
            )
        finally:
            _compose("start", "clickhouse1", check=False)

        # Restart ch-1 and wait for the replica to catch up.
        deadline = time.monotonic() + 120
        synced = False
        while time.monotonic() < deadline:
            try:
                rows = _ch_query(
                    "clickhouse1",
                    f"SELECT count() FROM observability.spans_hot_local "
                    f"WHERE trace_id = '{trace_id}'",
                )
                if int(rows) >= 2:
                    synced = True
                    break
            except (subprocess.SubprocessError, ValueError):
                pass  # node still starting up
            time.sleep(3)
        assert synced, "restarted replica did not resync the rows written during failover"

        # No replication queue backlog left behind.
        queue = _ch_query(
            "clickhouse1",
            "SELECT sum(queue_size) FROM system.replicas "
            "WHERE database = 'observability'",
        )
        assert int(queue or 0) == 0, f"replication queue not drained: {queue}"

        store.close()

    def test_rows_written_to_both_replicas_of_shard(self, ha_stack):
        """Replication: one Distributed insert must be readable from both
        shard-1 replicas via the local tables."""
        from agent_obs.storage.hot import HotStore

        store = HotStore(
            hosts=SDK_HOSTS,
            user="clickhouse",
            password="clickhouse",
        )
        trace_id = f"pp35-repl-{uuid.uuid4().hex[:12]}"
        # Shard-tenant pairing is by cityHash64(tenant_id) — pick a tenant that
        # deterministically maps to shard 1, so the ch1/ch2 assertions below
        # (and the DoD wording "kill ch-1 → ch-2 accepts") actually hold.
        tenant = _shard1_tenant()
        store.write_spans_batch([_span_row(trace_id, "span-repl", tenant)])
        store.close()

        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            try:
                c1 = int(_ch_query(
                    "clickhouse1",
                    f"SELECT count() FROM observability.spans_hot_local "
                    f"WHERE trace_id = '{trace_id}'",
                ))
                c2 = int(_ch_query(
                    "clickhouse2",
                    f"SELECT count() FROM observability.spans_hot_local "
                    f"WHERE trace_id = '{trace_id}'",
                ))
                if c1 >= 1 and c2 >= 1:
                    return
            except (subprocess.SubprocessError, ValueError):
                pass  # replication still in flight
            time.sleep(2)
        pytest.fail("row did not replicate to both shard-1 replicas within 60s")


def _span_row(trace_id: str, span_id: str, tenant: str = "pp35") -> dict:
    return {
        "trace_id": trace_id,
        "span_id": span_id,
        "parent_span_id": "",
        "agent_id": "pp35-test-agent",
        "tenant_id": tenant,
        "name": "pp35.probe",
        "span_type": "agent.loop",
        "start_time": time.time(),
        "end_time": time.time(),
        "status": "success",
        "attributes": {},
        "events": [],
        "cost_usd": 0.0,
        "response_embedding": [],
    }


def _shard1_tenant() -> str:
    """Find a tenant whose cityHash64 routes Distributed inserts to shard 1.

    The Distributed sharding key is cityHash64(tenant_id) % shard_count
    (0-based slot → shard_num = slot + 1, verified against system.clusters).
    Without this, ~half of the runs land on shard 2 and every ch1/ch2
    assertion silently waits for rows that were never routed there.
    """
    for i in range(100):
        candidate = f"pp35-{i}"
        slot = int(_ch_query(
            "clickhouse1",
            f"SELECT cityHash64('{candidate}') % 2",
        ))
        if slot == 0:
            return candidate
    raise AssertionError("no tenant candidate hashed to shard 1 in 100 tries")
