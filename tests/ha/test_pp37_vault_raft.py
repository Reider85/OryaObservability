"""PP37 — Vault raft HA + auto-unseal via transit (integration).

Requires the HA stack (docker compose -f infra/docker-compose.ha.yml up -d
certs-init vault-unseal vault-1 vault-2 vault-3 vault-init-ha).
Skipped automatically when the Docker daemon is unavailable — consistent with
the rest of tests/ha (never collected by the fast suite).

DoD coverage:
- Raft quorum: 3 peers, exactly one leader.
- SDK VaultClient (localhost:8210-8212, mTLS) survives leader loss via failover.
- kill leader → new leader elected, SDK writes continue in < 5 s.
- restart node → auto-unseal (sealed=false within 60 s), rejoin (3 peers).
"""

from __future__ import annotations

import os
import subprocess
import time

import pytest

# NB: pytest-timeout is not installed in this repo — no timeout mark here.
# The module-level waits are bounded by explicit deadlines in each test.

COMPOSE = ["docker", "compose", "-f", "infra/docker-compose.ha.yml"]
VAULT_NODES = ["vault-1", "vault-2", "vault-3"]
# Host-mapped ports (see docker-compose.ha.yml: 8210..8212 -> 8200).
SDK_ADDRS = [
    "https://localhost:8210",
    "https://localhost:8211",
    "https://localhost:8212",
]
CERT_DIR = "infra/ha/certs"


def _docker_available() -> bool:
    """Probe the daemon with retries (same pattern as PP35)."""
    for attempt in range(3):
        try:
            subprocess.run(
                ["docker", "info"],
                stdin=subprocess.DEVNULL,
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
    reason="Docker daemon unavailable — PP37 integration tests need the HA stack",
)


def _compose(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        [*COMPOSE, *args],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=check,
    )


def _vault_cmd(node: str, *args: str) -> str:
    """Run a vault CLI command inside one node (mTLS is handled by VAULT_* env)."""
    result = _compose("exec", "-T", node, "vault", *args)
    return result.stdout.strip()


def _raft_peers(node: str) -> list[dict]:
    raw = _vault_cmd(node, "operator", "raft", "list-peers", "-format=json")
    import json

    return json.loads(raw)


def _sdk_client():
    """Build a VaultClient pointed at the host-mapped HA ports with mTLS."""
    from agent_obs.guardrail.vault_client import VaultClient

    ca = os.path.join(CERT_DIR, "internal-ca", "ca.pem")
    cert = os.path.join(CERT_DIR, "vault", "vault.pem")
    key = os.path.join(CERT_DIR, "vault", "vault-key.pem")
    os.environ.setdefault("VAULT_TLS_CA", ca)
    os.environ.setdefault("VAULT_CLIENT_CERT", cert)
    os.environ.setdefault("VAULT_CLIENT_KEY", key)
    # Root token is persisted by vault-init-ha to the bootstrap volume.
    token = _bootstrap_token()
    return VaultClient(addrs=SDK_ADDRS, token=token, verify_tls=True)


def _bootstrap_token() -> str:
    """Read the root token written by vault-init-ha into the bootstrap volume."""
    result = _compose(
        "exec", "-T", "vault-1", "cat", "/vault/bootstrap/root_token.txt",
        check=False,
    )
    return result.stdout.strip()


@pytest.fixture(scope="module")
def ha_stack():
    """Bring up the Vault subset of the HA stack; tear it down afterwards."""
    up = _compose(
        "up", "-d", "--wait",
        "certs-init", "vault-unseal", "vault-1", "vault-2", "vault-3", "vault-init-ha",
        check=False,
    )
    if up.returncode != 0:
        pytest.skip(f"HA vault stack failed to start: {up.stderr[:500]}")
    yield
    # Leave the stack running for other HA tests; a targeted down is available
    # via `docker compose -f infra/docker-compose.ha.yml down`.


@requires_docker
class TestRaftQuorum:
    def test_three_peers(self, ha_stack):
        peers = _raft_peers("vault-1")
        assert len(peers) == 3, f"expected 3 raft peers, got {len(peers)}"

    def test_exactly_one_leader(self, ha_stack):
        leaders = 0
        for node in VAULT_NODES:
            raw = _vault_cmd(node, "sys", "leader", "-format=json")
            import json

            if json.loads(raw).get("is_self"):
                leaders += 1
        assert leaders == 1, f"expected exactly one leader, got {leaders}"

    def test_all_unsealed(self, ha_stack):
        for node in VAULT_NODES:
            raw = _vault_cmd(node, "status", "-format=json", check=False)
            # vault status exits rc 2 when sealed; parse what we got
            import json

            if raw:
                status = json.loads(raw)
                assert status.get("sealed") is False, f"{node} is sealed"


@requires_docker
class TestSdkFailover:
    def test_leader_loss_failover(self, ha_stack):
        """Kill the current leader → SDK VaultClient writes continue via failover."""
        # Identify the leader
        leader = None
        for node in VAULT_NODES:
            raw = _vault_cmd(node, "sys", "leader", "-format=json")
            import json

            if json.loads(raw).get("is_self"):
                leader = node
                break
        assert leader, "no leader found"

        client = _sdk_client()
        try:
            # Pre-kill sanity: a KV write succeeds
            key = client._with_failover(
                lambda c: c.secrets.kv.v2.create_or_update_secret(
                    path="pp37/pre-kill",
                    secret={"data": {"probe": "before"}},
                    cas=0,
                )
            )
            assert key is not None

            # Kill the leader
            _compose("stop", leader)

            # SDK write must succeed via failover within the deadline
            deadline = time.monotonic() + 5
            ok = False
            while time.monotonic() < deadline:
                try:
                    client._with_failover(
                        lambda c: c.secrets.kv.v2.create_or_update_secret(
                            path="pp37/post-kill",
                            secret={"data": {"probe": "after"}},
                            cas=0,
                        )
                    )
                    ok = True
                    break
                except (ConnectionError, TimeoutError, OSError):
                    time.sleep(0.5)
            assert ok, "SDK write did not succeed within 5s after leader kill"

            # Exactly one leader must still exist among the survivors
            survivors = [n for n in VAULT_NODES if n != leader]
            leaders = 0
            for node in survivors:
                raw = _vault_cmd(node, "sys", "leader", "-format=json", check=False)
                import json

                if raw and json.loads(raw).get("is_self"):
                    leaders += 1
            assert leaders == 1, f"expected 1 leader among survivors, got {leaders}"
        finally:
            # Restart the killed node so auto-unseal can be verified next
            _compose("start", leader)
            client._drop_client(0)
            client._drop_client(1)
            client._drop_client(2)

    def test_restarted_node_auto_unseals_and_rejoins(self, ha_stack):
        """Restart vault-3 → auto-unseal (sealed=false ≤60s), rejoin (3 peers ≤90s)."""
        _compose("restart", "vault-3")

        # Wait for auto-unseal
        deadline = time.monotonic() + 60
        unsealed = False
        while time.monotonic() < deadline:
            raw = _vault_cmd("vault-3", "status", "-format=json", check=False)
            if raw:
                import json

                try:
                    if json.loads(raw).get("sealed") is False:
                        unsealed = True
                        break
                except ValueError:
                    pass
            time.sleep(3)
        assert unsealed, "vault-3 did not auto-unseal within 60s"

        # Wait for rejoin
        deadline = time.monotonic() + 90
        rejoined = False
        while time.monotonic() < deadline:
            try:
                peers = _raft_peers("vault-1")
                if len(peers) == 3:
                    rejoined = True
                    break
            except Exception:  # noqa: BLE001 — best-effort probe during election window
                time.sleep(5)
        assert rejoined, "vault-3 did not rejoin the raft cluster within 90s"
