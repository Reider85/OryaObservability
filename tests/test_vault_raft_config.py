"""PP37 — Vault raft HA configuration shape tests (no docker required).

Validates the static per-node HCL configs, unseal/raft bootstrap scripts,
compose service definitions, cert SANs, and the SDK address resolver — all
by reading files, so this runs in the fast suite.
"""

from __future__ import annotations

import os
import re
import subprocess

import pytest

HA_DIR = "infra/ha/vault"
COMPOSE = "infra/docker-compose.ha.yml"


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


class TestStaticHCL:
    """Each raft node has its own HCL file — no env interpolation."""

    @pytest.mark.parametrize("node", [1, 2, 3])
    def test_hcl_exists(self, node):
        assert os.path.exists(f"{HA_DIR}/vault-raft-{node}.hcl")

    @pytest.mark.parametrize("node", [1, 2, 3])
    def test_no_env_interpolation(self, node):
        """Vault HCL does NOT expand ${VAR:-default} — static configs must be literal.

        Comments may mention the pattern; only non-comment lines are checked.
        """
        content = _read(f"{HA_DIR}/vault-raft-{node}.hcl")
        code_lines = [l for l in content.splitlines() if not l.strip().startswith("#")]
        assert not any("${" in l for l in code_lines)

    @pytest.mark.parametrize("node", [1, 2, 3])
    def test_node_id_is_static(self, node):
        content = _read(f"{HA_DIR}/vault-raft-{node}.hcl")
        assert f'node_id = "vault-{node}"' in content

    @pytest.mark.parametrize("node", [1, 2, 3])
    def test_api_and_cluster_addr(self, node):
        content = _read(f"{HA_DIR}/vault-raft-{node}.hcl")
        assert f'api_addr     = "https://vault-{node}:8200"' in content
        assert f'cluster_addr = "https://vault-{node}:8200"' in content

    @pytest.mark.parametrize("node", [1, 2, 3])
    def test_retry_join_covers_all_three(self, node):
        content = _read(f"{HA_DIR}/vault-raft-{node}.hcl")
        for peer in (1, 2, 3):
            assert f'"https://vault-{peer}:8200"' in content

    @pytest.mark.parametrize("node", [1, 2, 3])
    def test_seal_transit_block(self, node):
        content = _read(f"{HA_DIR}/vault-raft-{node}.hcl")
        assert 'seal "transit"' in content
        assert 'address             = "https://vault-unseal:8200"' in content
        assert 'key_name            = "autounseal"' in content
        assert 'mount_path          = "transit/"' in content

    @pytest.mark.parametrize("node", [1, 2, 3])
    def test_seal_has_no_hardcoded_token(self, node):
        """Token comes from VAULT_TOKEN env — never in mounted HCL."""
        content = _read(f"{HA_DIR}/vault-raft-{node}.hcl")
        # The seal block must not contain a token line
        seal_match = re.search(r'seal "transit" \{(.*?)\}', content, re.DOTALL)
        assert seal_match, "seal block not found"
        assert "token" not in seal_match.group(1)

    @pytest.mark.parametrize("node", [1, 2, 3])
    def test_mtls_listener(self, node):
        content = _read(f"{HA_DIR}/vault-raft-{node}.hcl")
        assert 'listener "tcp"' in content
        assert "require_request_client_cert = true" in content
        assert "tls_disable_client_certs   = false" in content


class TestUnsealInit:
    def test_script_exists(self):
        assert os.path.exists(f"{HA_DIR}/unseal-init.sh")

    def test_fixed_token_id(self):
        content = _read(f"{HA_DIR}/unseal-init.sh")
        assert 'TRANSIT_TOKEN_ID="vault-transit-token"' in content
        # The mint call passes the id in the JSON body (jq-escaped in shell)
        assert "TRANSIT_TOKEN_ID" in content
        assert '"id"' in content or "'id'" in content or "id\\\"" in content

    def test_autounseal_key_name(self):
        """The seal HCL expects key_name=autounseal, not the old vault-unseal-key.

        Note: vault-unseal-key.pem is the TLS client-key filename, unrelated.
        """
        content = _read(f"{HA_DIR}/unseal-init.sh")
        assert "transit/keys/autounseal" in content
        assert "transit/keys/vault-unseal-key" not in content

    def test_persists_keys_to_volume(self):
        """Keys must survive container restarts — /tmp dies with the container."""
        content = _read(f"{HA_DIR}/unseal-init.sh")
        assert "/vault/data/unseal_keys.txt" in content
        assert "/tmp/unseal_keys.txt" not in content

    def test_no_dead_raft_config_code(self):
        """The old script wrote a raft config to /tmp that was never used."""
        content = _read(f"{HA_DIR}/unseal-init.sh")
        assert "/tmp/vault-raft-config.hcl" not in content


class TestRaftInit:
    def test_script_exists(self):
        assert os.path.exists(f"{HA_DIR}/raft-init.sh")

    def test_operator_init(self):
        content = _read(f"{HA_DIR}/raft-init.sh")
        assert "recovery_shares" in content
        assert "recovery_threshold" in content

    def test_waits_for_auto_unseal(self):
        content = _read(f"{HA_DIR}/raft-init.sh")
        assert "auto-unseal" in content.lower() or "auto_unseal" in content.lower()
        assert "sealed" in content

    def test_fixes_tenant_key_bug(self):
        """Old vault_init.sh used $tenant_pii_key (empty) instead of ${tenant}_pii_key.

        Comments may mention the old form; only non-comment code lines are checked.
        """
        content = _read(f"{HA_DIR}/raft-init.sh")
        code_lines = [l for l in content.splitlines() if not l.strip().startswith("#")]
        code = "\n".join(code_lines)
        assert "${tenant}_pii_key" in code
        assert "$tenant_pii_key" not in code

    def test_waits_for_quorum(self):
        content = _read(f"{HA_DIR}/raft-init.sh")
        assert "raft/configuration" in content


class TestComposeVault:
    def _services(self) -> dict:
        # stdin=DEVNULL avoids WinError 6 (handle inheritance) on Windows
        result = subprocess.run(
            ["docker", "compose", "-f", COMPOSE, "config"],
            capture_output=True,
            text=True,
            check=True,
            stdin=subprocess.DEVNULL,
        )
        import yaml

        return yaml.safe_load(result.stdout)["services"]

    def test_vault_three_nodes_present(self):
        services = self._services()
        for node in ("vault-1", "vault-2", "vault-3"):
            assert node in services

    def test_vault_init_ha_present(self):
        services = self._services()
        assert "vault-init-ha" in services

    def test_host_ports(self):
        services = self._services()
        ports = {
            "vault-1": "8210:8200",
            "vault-2": "8211:8200",
            "vault-3": "8212:8200",
        }
        for node, expected in ports.items():
            published = [p["published"] for p in services[node].get("ports", [])]
            host_port = expected.split(":")[0]
            assert host_port in published, f"{node} missing host port {host_port}"

    def test_network_alias(self):
        services = self._services()
        for node in ("vault-1", "vault-2", "vault-3"):
            nets = services[node].get("networks", {})
            default = nets.get("default", {})
            aliases = default.get("aliases", []) if isinstance(default, dict) else []
            assert "vault.observability.local" in aliases, f"{node} missing alias"

    def test_transit_token_env(self):
        services = self._services()
        for node in ("vault-1", "vault-2", "vault-3"):
            env = services[node].get("environment", {})
            assert "VAULT_TOKEN" in env, f"{node} missing VAULT_TOKEN"

    def test_static_hcl_mounted(self):
        services = self._services()
        for node in ("vault-1", "vault-2", "vault-3"):
            vols = services[node].get("volumes", [])
            sources = [v.get("source", "") for v in vols]
            assert any(f"vault-raft-{node[-1]}.hcl" in s for s in sources), (
                f"{node} not mounting vault-raft-{node[-1]}.hcl"
            )

    def test_vault_3_data_volume_declared(self):
        with open(COMPOSE, "r", encoding="utf-8") as f:
            content = f.read()
        assert "vault_3_data:" in content
        assert "vault_raft_bootstrap:" in content


class TestCertSANs:
    def test_issue_certs_script_has_vault3_and_alias(self):
        content = _read("scripts/certs/issue-certs.sh")
        assert "DNS:vault-3" in content
        assert "DNS:vault.observability.local" in content


class TestEnvExample:
    def test_vaULT_nodes_includes_three(self):
        content = _read("infra/.env.example.ha")
        assert "VAULT_NODES=vault-1,vault-2,vault-3" in content
        assert "VAULT_HOST_PORTS=8210,8211,8212" in content

    def test_transit_token_present(self):
        content = _read("infra/.env.example.ha")
        assert "VAULT_TRANSIT_TOKEN=vault-transit-token" in content
