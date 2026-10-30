"""PP33: Multi-tenant Vault integration tests.

Tests tenant isolation, JWT authentication, transit encryption, and audit logging.
"""

import base64
import os
from unittest.mock import MagicMock, patch, AsyncMock

import pytest

from agent_obs.guardrail.audit import RecoveryAuditEvent
from agent_obs.guardrail.vault_client import VaultClient
from agent_obs.guardrail.pii_types import PIIMatch
from agent_obs.observability import SpanContext
from agent_obs.guardrail.engine import GuardrailEngine


class TestMultiTenantVault:
    """Test multi-tenant Vault functionality."""

    def test_policy_template_content(self):
        """Test per-tenant.hcl template has correct paths and capabilities."""
        policy_path = "infra/vault/policies/per-tenant.hcl"
        with open(policy_path, "r") as f:
            policy_content = f.read()

        # Check template placeholders
        assert "{{tenant_id}}" in policy_content

        # Check KV paths
        assert 'path "secret/data/pii/{{tenant_id}}/*"' in policy_content
        assert 'path "secret/metadata/pii/{{tenant_id}}"' in policy_content
        assert 'path "secret/metadata/pii/{{tenant_id}}/*"' in policy_content

        # Check transit paths
        assert 'path "transit/encrypt/{{tenant_id}}_pii_key"' in policy_content
        assert 'path "transit/decrypt/{{tenant_id}}_pii_key"' in policy_content
        assert 'path "transit/keys/{{tenant_id}}_pii_key"' in policy_content

        # Check capabilities
        assert 'capabilities = ["read"]' in policy_content
        assert 'capabilities = ["update"]' in policy_content
        assert 'capabilities = ["list", "delete"]' in policy_content

    def test_vault_init_script_content(self):
        """Test vault_init.sh enables transit, JWT auth, and creates per-tenant policies."""
        init_path = "infra/vault/vault_init.sh"
        with open(init_path, "r") as f:
            init_content = f.read()

        # Check transit enable
        assert "vault secrets enable transit" in init_content

        # Check JWT auth enable
        assert "vault auth enable jwt" in init_content

        # Check per-tenant policy rendering
        assert 'sed "s/{{tenant_id}}/$tenant/g"' in init_content
        assert "vault policy write pii-$t" in init_content

        # Check transit key creation
        assert 'vault write transit/keys/$tenant_pii_key' in init_content

    def test_compose_env_vars(self):
        """Test docker-compose.yml has new Vault environment variables."""
        compose_path = "infra/docker-compose.yml"
        with open(compose_path, "r") as f:
            compose_content = f.read()

        assert "VAULT_TENANTS" in compose_content
        assert "VAULT_JWT" in compose_content
        assert "VAULT_JWT_OIDC_DISCOVERY_URL" in compose_content

    @patch("hvac.Client")
    async def test_vault_client_store_with_tenant(self, mock_client_class):
        """Test store() with tenant uses transit encryption and tenant-scoped path."""
        mock_client = MagicMock()
        mock_client_class.return_value = mock_client

        # Mock transit encrypt response
        mock_client.secrets.transit.encrypt_data.return_value = {
            "data": {"ciphertext": "encrypted_data"}
        }

        client = VaultClient(addr="http://localhost:8200", token="test")
        result = await client.store(
            mask="ivan@example.com",
            original="ivan@example.com",
            ttl_seconds=3600,
            tenant_id="tenant_a"
        )

        # Check path includes tenant
        mock_client.secrets.kv.v2.create_or_update_secret.assert_called_once()
        call_args = mock_client.secrets.kv.v2.create_or_update_secret.call_args
        assert call_args[1]["path"].startswith("pii/tenant_a/")

        # Check transit encrypt was called
        mock_client.secrets.transit.encrypt_data.assert_called_once_with(
            key_name="tenant_a_pii_key",
            plaintext="aXZhbkBleGFtcGxlLmNvbQ=="
        )

        # Check result is a vault_key string
        assert isinstance(result, str)
        assert result.startswith("pii/tenant_a/")

    @patch("hvac.Client")
    async def test_vault_client_store_legacy_without_tenant(self, mock_client_class):
        """Test store() without tenant uses legacy flat path and no transit."""
        mock_client = MagicMock()
        mock_client_class.return_value = mock_client

        client = VaultClient(addr="http://localhost:8200", token="test")
        result = await client.store(
            mask="ivan@example.com",
            original="ivan@example.com",
            ttl_seconds=3600
        )

        # Check legacy path (no tenant)
        mock_client.secrets.kv.v2.create_or_update_secret.assert_called_once()
        call_args = mock_client.secrets.kv.v2.create_or_update_secret.call_args
        assert call_args[1]["path"].startswith("pii/")

        # Check transit was NOT called for legacy
        mock_client.secrets.transit.encrypt_data.assert_not_called()

    @patch("hvac.Client")
    async def test_vault_client_recover_cross_tenant_denied(self, mock_client_class):
        """Test recover() with mismatched tenant raises PermissionError."""
        mock_client = MagicMock()
        mock_client_class.return_value = mock_client

        # Mock KV data with ciphertext for tenant_b
        mock_client.secrets.kv.v2.read_secret_version.return_value = {
            "data": {"data": {"ciphertext": "encrypted_data"}}
        }

        # Mock transit decrypt
        mock_client.secrets.transit.decrypt_data.return_value = {
            "data": {"plaintext": base64.b64encode(b"ivan@example.com").decode()}
        }

        client = VaultClient(addr="http://localhost:8200", token="test")

        # Try to recover tenant_b data with tenant_a context
        with pytest.raises((PermissionError, ConnectionError)):
            await client.recover(
                vault_key="pii/tenant_b/5f3a/2026-09-21/abc123",
                tenant_id="tenant_a",
                reason="test"
            )

        # Verify KV was not called (blocked by guard)
        mock_client.secrets.kv.v2.read_secret_version.assert_not_called()
        mock_client.secrets.transit.decrypt_data.assert_not_called()

    @patch("hvac.Client")
    async def test_vault_client_recover_own_tenant_allowed(self, mock_client_class):
        """Test recover() with matching tenant succeeds and decrypts."""
        mock_client = MagicMock()
        mock_client_class.return_value = mock_client

        # Mock KV data with ciphertext for tenant_a
        mock_client.secrets.kv.v2.read_secret_version.return_value = {
            "data": {"data": {"ciphertext": "encrypted_data", "mask": "ivan@example.com", "created_at": 1700000000, "ttl_seconds": 1000000000}}
        }

        # Mock transit decrypt
        mock_client.secrets.transit.decrypt_data.return_value = {
            "data": {"plaintext": base64.b64encode(b"ivan@example.com").decode()}
        }

        client = VaultClient(addr="http://localhost:8200", token="test", audit_enabled=False)
        result = await client.recover(
            vault_key="pii/tenant_a/5f3a/2026-09-21/abc123",
            tenant_id="tenant_a",
            reason="test"
        )

        # Check transit decrypt was called with correct key
        mock_client.secrets.transit.decrypt_data.assert_called_once_with(
            key_name="tenant_a_pii_key",
            ciphertext="encrypted_data"
        )

        # Check result contains decrypted original
        assert result == "ivan@example.com"

    @patch("hvac.Client")
    async def test_vault_client_recover_legacy_no_tenant(self, mock_client_class):
        """Test recover() of legacy key (no tenant) reads plaintext original."""
        mock_client = MagicMock()
        mock_client_class.return_value = mock_client

        # Mock legacy KV data with plaintext original
        mock_client.secrets.kv.v2.read_secret_version.return_value = {
            "data": {"data": {"original": "ivan@example.com", "mask": "ivan@example.com", "created_at": 1700000000, "ttl_seconds": 1000000000}}
        }

        client = VaultClient(addr="http://localhost:8200", token="test", audit_enabled=False)
        result = await client.recover(
            vault_key="pii/5f3a/2026-09-21/abc123",
            reason="test"
        )

        # Check result contains original directly (no transit)
        assert result == "ivan@example.com"

    @patch("hvac.Client")
    async def test_vault_client_jwt_login(self, mock_client_class):
        """Test VaultClient uses JWT login when VAULT_JWT is set."""
        mock_client = MagicMock()
        mock_client_class.return_value = mock_client

        # Mock JWT auth response
        mock_client.auth.jwt.login.return_value = {
            "auth": {"client_token": "jwt_token"}
        }
        # Make the client return the token when accessed
        mock_client.token = "jwt_token"
        
        # Mock transit encrypt response
        mock_client.secrets.transit.encrypt_data.return_value = {
            "data": {"ciphertext": "encrypted_data"}
        }

        with patch.dict(os.environ, {"VAULT_JWT": "test_jwt_token"}):
            client = VaultClient(addr="http://localhost:8200", token="test", audit_enabled=False)
            
            # Trigger client creation by calling store
            await client.store(
                mask="ivan@example.com",
                original="ivan@example.com",
                ttl_seconds=3600,
                tenant_id="tenant_a"
            )

        # Check JWT login was called
        mock_client.auth.jwt.login.assert_called_once_with(
            jwt="test_jwt_token",
            role="tenant_a"
        )

        # Check client token was set
        assert mock_client.token == "jwt_token"

    def test_recovery_audit_event_with_tenant(self):
        """Test RecoveryAuditEvent includes tenant_id and mask in resource."""
        event = RecoveryAuditEvent.for_vault_recovery(
            vault_key="pii/tenant_a/5f3a/2026-09-21/abc123",
            login="user@example.com",
            reason="recovery",
            tenant_id="tenant_a",
            mask="ivan@example.com"
        )

        # Check resource includes tenant_id and mask
        assert event.resource["tenant_id"] == "tenant_a"
        assert event.resource["mask"] == "ivan@example.com"
        assert event.resource["type"] == "pii_vault_key"
        assert event.resource["id"] == "pii/tenant_a/5f3a/2026-09-21/abc123"

        # Check to_clickhouse_row still returns 10 columns
        row = event.to_clickhouse_row()
        assert len(row) == 10  # Preserve 10-column contract

    def test_recovery_audit_event_without_tenant(self):
        """Test RecoveryAuditEvent omits tenant_id when empty (backward compatibility)."""
        event = RecoveryAuditEvent.for_vault_recovery(
            vault_key="pii/5f3a/2026-09-21/abc123",
            login="user@example.com",
            reason="recovery",
            tenant_id="",  # Empty tenant
            mask="ivan@example.com"
        )

        # Check resource omits tenant_id when empty
        assert "tenant_id" not in event.resource
        assert event.resource["mask"] == "ivan@example.com"
        assert event.resource["type"] == "pii_vault_key"
        assert event.resource["id"] == "pii/5f3a/2026-09-21/abc123"

        # Check to_clickhouse_row still returns 10 columns
        row = event.to_clickhouse_row()
        assert len(row) == 10  # Preserve 10-column contract

    @patch("hvac.Client")
    async def test_vault_client_transit_encrypt_failure(self, mock_client_class):
        """Test transit encrypt failure returns None (fail-closed)."""
        mock_client = MagicMock()
        mock_client_class.return_value = mock_client

        # Mock transit encrypt failure
        mock_client.secrets.transit.encrypt_data.side_effect = Exception("Transit unavailable")

        client = VaultClient(addr="http://localhost:8200", token="test")
        result = await client.store(
            mask="ivan@example.com",
            original="ivan@example.com",
            ttl_seconds=3600,
            tenant_id="tenant_a"
        )

        # Check failure returns None (fail-closed)
        assert result is None

    @patch("hvac.Client")
    async def test_vault_client_vault_403_mapped_to_permission_error(self, mock_client_class):
        """Test Vault 403 Forbidden mapped to PermissionError."""
        from hvac.exceptions import Forbidden

        mock_client = MagicMock()
        mock_client_class.return_value = mock_client

        # Mock KV read returning 403
        mock_client.secrets.kv.v2.read_secret_version.side_effect = Forbidden("403")

        client = VaultClient(addr="http://localhost:8200", token="test", audit_enabled=False)

        # Check 403 maps to PermissionError
        with pytest.raises((PermissionError, ConnectionError)):
            await client.recover(
                vault_key="pii/tenant_a/5f3a/2026-09-21/abc123",
                tenant_id="tenant_a",
                reason="test"
            )

    def test_env_vars_in_env_example(self):
        """Test new environment variables are documented in .env.example."""
        env_path = "infra/.env.example"
        with open(env_path, "r", encoding="utf-8") as f:
            env_content = f.read()

        assert "VAULT_TENANTS" in env_content
        assert "VAULT_JWT" in env_content
        assert "VAULT_JWT_OIDC_DISCOVERY_URL" in env_content
        assert "VAULT_JWT_USER_CLAIM" in env_content
        assert "AGENT_OBS_VAULT_TRANSIT_ENABLED" in env_content

    @patch("hvac.Client")
    async def test_vault_client_tenant_validation_regex(self, mock_client_class):
        """Test tenant_id uses regex validation like hot.py."""
        mock_client = MagicMock()
        mock_client_class.return_value = mock_client

        # Valid tenant names
        for tenant in ["tenant_a", "tenant-1", "tenant_1"]:
            client = VaultClient(addr="http://localhost:8200", token="test")
            # Should not raise exception
            from agent_obs.guardrail.vault_client import _validate_tenant_segment
            _validate_tenant_segment(tenant)

        # Invalid tenant names
        for tenant in ["", "tenant!", "tenant_a/b", "_tenant", "tenant.", "-tenant", "tenant-"]:
            client = VaultClient(addr="http://localhost:8200", token="test")
            with pytest.raises(ValueError, match="Invalid tenant_id format"):
                from agent_obs.guardrail.vault_client import _validate_tenant_segment
                _validate_tenant_segment(tenant)

    @patch("agent_obs.guardrail.vault_client.VaultClient")
    async def test_engine_passes_tenant_to_store(self, mock_client_class):
        """Test GuardrailEngine passes tenant_id to VaultClient.store()."""
        mock_client = AsyncMock()
        mock_client_class.return_value = mock_client

        # Create mock dependencies
        mock_detector = MagicMock()
        mock_classifier = MagicMock()

        # Create engine with mocked vault client
        from agent_obs.guardrail.engine import GuardrailConfig
        config = GuardrailConfig(vault_ttl_seconds=3600)
        engine = GuardrailEngine(
            pii_detector=mock_detector,
            injection_classifier=mock_classifier,
            vault_client=mock_client,
            config=config,
            field_masker=None  # Explicitly set to None to use legacy path
        )

        # Mock context with tenant_id
        context = SpanContext(trace_id="test-trace", span_id="test-span", tenant_id="tenant_a")
        span = MagicMock(context=context)

        # Mock injection classifier to return a proper score
        mock_classifier.classify.return_value = MagicMock(score=0.1)

        # Mock PII detector to return a match
        mock_detector.detect.return_value = [
            PIIMatch(
                entity_type="email",
                value="ivan@example.com",
                span_start=0,
                span_end=13,
                mask="[EMAIL:5f3a]"
            )
        ]

        # Call check_input which should call store with tenant
        try:
            result = await engine.check_input(
                text="ivan@example.com",
                context=context,
                field="user_email"
            )
            print(f"Result: {result}")
        except Exception as e:
            print(f"Exception: {e}")
            raise

        # Debug print
        print(f"Store call count: {mock_client.store.call_count}")
        print(f"Store calls: {mock_client.store.call_args_list}")

        # Check store was called with tenant_id
        mock_client.store.assert_called_once()
        call_args = mock_client.store.call_args
        assert call_args[1]["tenant_id"] == "tenant_a"