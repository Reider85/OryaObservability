"""Tests for vault recovery audit trail functionality (PC20)."""

import asyncio
import json
import time
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from agent_obs.guardrail.audit import (
    RecoveryAuditEvent,
    AuditReasonRequiredError,
    AuditWriteError,
    classify_reason_category,
)
from agent_obs.guardrail.vault_client import VaultClient
from agent_obs.storage.hot import HotStore


class TestAuditEventContracts:
    """Test audit event dataclass and related functions."""

    def test_recovery_audit_event_creation(self):
        """Test RecoveryAuditEvent creation with vault recovery context."""
        event = RecoveryAuditEvent.for_vault_recovery(
            vault_key="pii/5f3a/2026-09-21/abc123",
            login="alice@company.com",
            reason="debugging ticket INC-1234 hallucination",
            trace_id="01HZ...",
            ip_address="192.168.1.1",
            user_agent="cli/vault-recover",
        )
        
        assert event.audit_id.startswith("rae-")
        assert event.trace_id == "01HZ..."
        assert event.actor == {"type": "engineer", "id": "alice@company.com"}
        assert event.action == "vault.recover"
        assert event.resource == {"type": "pii_vault_key", "id": "pii/5f3a/2026-09-21/abc123"}
        assert event.reason == "debugging ticket INC-1234 hallucination"
        assert event.ip_address == "192.168.1.1"
        assert event.user_agent == "cli/vault-recover"
        assert event.decision == "allow"

    def test_recovery_audit_event_to_dict(self):
        """Test RecoveryAuditEvent serialization to dict."""
        event = RecoveryAuditEvent.for_vault_recovery(
            vault_key="pii/test/123",
            login="bob@company.com",
            reason="support ticket ST-5678",
        )
        
        data = event.to_dict()
        assert data["audit_id"] == event.audit_id
        assert data["timestamp"] == event.timestamp
        assert data["trace_id"] == ""
        assert data["actor"] == event.actor
        assert data["action"] == "vault.recover"
        assert data["resource"] == event.resource
        assert data["reason"] == event.reason
        assert data["ip_address"] == "unknown"
        assert data["user_agent"] == "cli"
        assert data["decision"] == "allow"
        assert data["reason_category"] == "support"

    def test_recovery_audit_event_to_clickhouse_row(self):
        """Test conversion to ClickHouse INSERT row."""
        event = RecoveryAuditEvent.for_vault_recovery(
            vault_key="pii/test/456",
            login="charlie@company.com",
            reason="compliance audit Q1 2026",
        )
        
        row = event.to_clickhouse_row()
        assert len(row) == 10
        assert row[0] == event.audit_id  # audit_id
        assert row[1] is not None  # timestamp (datetime)
        assert row[2] == ""  # trace_id
        assert isinstance(row[3], str)  # actor (json string)
        assert row[4] == "vault.recover"  # action
        assert row[5] == "allow"  # decision
        assert isinstance(row[6], str)  # resource (json string)
        assert row[7] == event.reason  # reason
        assert row[8] == "unknown"  # ip_address
        assert row[9] == "cli"  # user_agent

    def test_vault_api_path_property(self):
        """Test vault_api_path property for cross-validation."""
        event = RecoveryAuditEvent.for_vault_recovery(
            vault_key="pii/abc/2026-09-28/xyz",
            login="test@company.com",
            reason="test",
        )
        
        assert event.vault_api_path == "secret/data/pii/abc/2026-09-28/xyz"

    def test_classify_reason_category(self):
        """Test reason classification into bounded categories."""
        assert classify_reason_category("debug hallucination") == "debugging"
        assert classify_reason_category("incident outage alert") == "incident"
        assert classify_reason_category("support ticket user complaint") == "support"
        assert classify_reason_category("audit compliance gdpr 152-фз") == "compliance"
        assert classify_reason_category("unknown reason") == "other"
        assert classify_reason_category("") == "other"
        assert classify_reason_category("DEBUG") == "debugging"  # case insensitive


class TestAuditReasonRequiredError:
    """Test audit reason enforcement."""

    @pytest.mark.asyncio
    async def test_empty_reason_raises(self):
        """Test that empty reason raises AuditReasonRequiredError."""
        with pytest.raises(AuditReasonRequiredError, match="Recovery reason is required"):
            await VaultClient().recover("pii/test/123", reason="")

    @pytest.mark.asyncio
    async def test_whitespace_reason_raises(self):
        """Test that whitespace-only reason raises AuditReasonRequiredError."""
        with pytest.raises(AuditReasonRequiredError, match="Recovery reason is required"):
            await VaultClient().recover("pii/test/123", reason="   ")

    @pytest.mark.asyncio
    async def test_valid_reason_passes_validation(self):
        """Test that valid reason passes validation."""
        # This would fail later due to no vault access, but validation passes
        client = VaultClient(audit_enabled=False)  # Disable audit to avoid ClickHouse dependency
        try:
            await client.recover("pii/test/123", reason="debugging test")
            # If no exception, validation passed
        except Exception as e:
            # Expected: vault access failure, not reason validation failure
            assert "Recovery reason is required" not in str(e)


class TestHotStoreAuditWrite:
    """Test ClickHouse audit event writing via HotStore."""

    @pytest.fixture
    def mock_clickhouse_client(self):
        """Mock clickhouse_driver Client."""
        client = MagicMock()
        client.execute.return_value = None  # Simulate successful write
        return client

    @pytest.fixture
    def hot_store_with_mock(self, mock_clickhouse_client):
        """HotStore with mocked client."""
        with patch("clickhouse_driver.Client", return_value=mock_clickhouse_client):
            return HotStore()

    def test_write_audit_event_success(self, hot_store_with_mock):
        """Test successful audit event write."""
        event = RecoveryAuditEvent.for_vault_recovery(
            vault_key="pii/test/audit123",
            login="alice@company.com",
            reason="test audit write",
        )
        
        hot_store_with_mock.write_audit_event(event)
        
        # Verify clickhouse_client.execute was called
        mock_client = hot_store_with_mock._get_client()
        mock_client.execute.assert_called_once()
        
        # Extract the SQL and parameters from the call
        call_args = mock_client.execute.call_args
        sql = call_args[0][0]
        params = call_args[0][1]
        
        assert "INSERT INTO audit_events_hot" in sql
        assert len(params) == 1
        assert isinstance(params[0], tuple)
        assert len(params[0]) == 10  # 10 columns in audit_events_hot
        assert params[0][0] == event.audit_id  # audit_id
        assert params[0][4] == "vault.recover"  # action
        assert params[0][7] == "test audit write"  # reason

    def test_write_audit_event_failure(self, hot_store_with_mock):
        """Test audit event write failure."""
        # Make execute raise an exception
        mock_client = hot_store_with_mock._get_client()
        mock_client.execute.side_effect = Exception("ClickHouse connection failed")
        
        event = RecoveryAuditEvent.for_vault_recovery(
            vault_key="pii/test/audit456",
            login="bob@company.com",
            reason="test audit failure",
        )
        
        with pytest.raises(Exception, match="ClickHouse connection failed"):
            hot_store_with_mock.write_audit_event(event)


class TestVaultRecoveryWithAudit:
    """Test vault recovery with audit integration."""

    @pytest.fixture
    def mock_hot_store(self):
        """Mock HotStore for audit writes."""
        store = MagicMock()
        store.write_audit_event.return_value = None
        return store

    @pytest.fixture
    def vault_client_with_audit(self, mock_hot_store):
        """VaultClient with mocked audit store."""
        with patch("hvac.Client") as mock_client_class:
            mock_hvac = MagicMock()
            mock_hvac.is_authenticated.return_value = True
            mock_hvac.sys.health_check.return_value = {
                "initialized": True,
                "sealed": False,
            }
            mock_hvac.secrets.kv.v2.create_or_update_secret.return_value = {"data": {"metadata": {}}}
            mock_hvac.secrets.kv.v2.read_secret_version.return_value = {
                "data": {
                    "data": {
                        "mask": "[EMAIL:5f3a]",
                        "original": "ivan@example.com",
                        "created_at": time.time(),
                        "ttl_seconds": 86400,
                    }
                }
            }
            mock_client_class.return_value = mock_hvac
            
            client = VaultClient(
                addr="http://localhost:8200",
                token="test-token",
                verify_tls=False,
                audit_store=mock_hot_store,
                audit_enabled=True,
            )
            # Pre-set the mocked client to avoid hvac.Client() call
            client._client = mock_hvac
            return client

    @pytest.mark.asyncio
    async def test_recover_writes_audit_event(self, vault_client_with_audit, mock_hot_store):
        """Test that recovery writes audit event."""
        # Store something first
        vault_key = await vault_client_with_audit.store(
            mask="[EMAIL:5f3a]",
            original="ivan@example.com",
        )
        
        # Recover it
        original = await vault_client_with_audit.recover(
            vault_key,
            login="alice@company.com",
            reason="debugging ticket INC-1234",
        )
        
        assert original == "ivan@example.com"
        
        # Verify audit event was written
        mock_hot_store.write_audit_event.assert_called_once()
        
        # Check the audit event
        audit_event = mock_hot_store.write_audit_event.call_args[0][0]
        assert audit_event.audit_id.startswith("rae-")
        assert audit_event.action == "vault.recover"
        assert audit_event.resource["id"] == vault_key
        assert audit_event.reason == "debugging ticket INC-1234"
        assert audit_event.reason_category == "debugging"

    @pytest.mark.asyncio
    async def test_recover_audit_write_failure_raises(self, vault_client_with_audit, mock_hot_store):
        """Test that audit write failure raises AuditWriteError."""
        # Make audit write fail
        mock_hot_store.write_audit_event.side_effect = Exception("Audit write failed")
        
        # Store something first
        vault_key = await vault_client_with_audit.store(
            mask="[EMAIL:5f3a]",
            original="ivan@example.com",
        )
        
        # Recovery should fail due to audit write error
        with pytest.raises(AuditWriteError, match="Failed to write audit event"):
            await vault_client_with_audit.recover(
                vault_key,
                login="alice@company.com",
                reason="test audit failure",
            )
        
        # Original PII should not be returned
        # (this is tested by the exception being raised)

    @pytest.mark.asyncio
    async def test_recover_vault_native_audit_correlation(self, vault_client_with_audit, mock_hot_store):
        """Test cross-validation between Vault native audit and our audit event."""
        # Store something first
        vault_key = "pii/crossval/2026-09-28/abc"
        await vault_client_with_audit.store(
            mask="[EMAIL:5f3a]",
            original="test@example.com",
        )
        
        # Recover it
        await vault_client_with_audit.recover(
            vault_key,
            login="alice@company.com",
            reason="cross-validation test",
        )
        
        # Verify our audit event was written
        mock_hot_store.write_audit_event.assert_called_once()
        audit_event = mock_hot_store.write_audit_event.call_args[0][0]
        
        # Cross-validation: our event's vault_api_path should match Vault's secret path
        assert audit_event.vault_api_path == f"secret/data/{vault_key}"
        
        # The audit event should contain all necessary fields for correlation
        assert audit_event.resource["id"] == vault_key
        assert audit_event.action == "vault.recover"


class TestVaultRecoveryMetrics:
    """Test vault recovery metrics."""

    @pytest.fixture
    def mock_hot_store(self):
        """Mock HotStore for audit writes."""
        store = MagicMock()
        store.write_audit_event.return_value = None
        return store

    @pytest.mark.asyncio
    async def test_recovery_metrics_incremented(self, mock_hot_store):
        """Test that recovery metrics are properly incremented."""
        with patch("hvac.Client") as mock_client_class:
            mock_hvac = MagicMock()
            mock_hvac.is_authenticated.return_value = True
            mock_hvac.sys.health_check.return_value = {
                "initialized": True,
                "sealed": False,
            }
            mock_hvac.secrets.kv.v2.create_or_update_secret.return_value = {"data": {"metadata": {}}}
            mock_hvac.secrets.kv.v2.read_secret_version.return_value = {
                "data": {
                    "data": {
                        "mask": "[EMAIL:5f3a]",
                        "original": "ivan@example.com",
                        "created_at": time.time(),
                        "ttl_seconds": 86400,
                    }
                }
            }
            mock_client_class.return_value = mock_hvac
            
            client = VaultClient(
                audit_store=mock_hot_store,
                audit_enabled=True,
            )
            
            # Perform recovery
            vault_key = await client.store(
                mask="[EMAIL:5f3a]",
                original="ivan@example.com",
            )
            
            await client.recover(
                vault_key,
                login="alice@company.com",
                reason="debugging ticket",
            )
            
            # Check metrics (these are module-level, so we can inspect them)
            from agent_obs.guardrail.vault_client import vault_recovery_total, vault_recovery_duration_seconds
            
            # vault_recovery_total should have been incremented
            # For Prometheus metrics, we can't easily test the values in unit tests
            # But we can verify that the metric exists and has been called
            assert vault_recovery_total._name == "agent_obs_vault_recovery"
            # vault_recovery_duration_seconds should have observed a value
            assert vault_recovery_duration_seconds._sum.get() > 0


class TestVaultRecoveryCLI:
    """Test vault recovery CLI functionality."""

    @pytest.mark.asyncio
    async def test_cli_recover_missing_reason(self):
        """Test CLI recover without --reason flag."""
        with patch("sys.argv", ["vault_client.py", "recover", "--key", "pii/test/123", "--login", "alice@company.com"]):
            with pytest.raises(SystemExit) as exc_info:
                # Import main to test it
                from agent_obs.guardrail.vault_client import main
                await main()
            
            assert exc_info.value.code == 2

    @pytest.mark.asyncio
    async def test_cli_recover_success(self):
        """Test CLI recover with all required parameters."""
        with patch("sys.argv", [
            "vault_client.py", 
            "recover", 
            "--key", "pii/test/123",
            "--login", "alice@company.com",
            "--reason", "debugging test CLI",
            "--trace-id", "01HZ...",
            "--ip", "192.168.1.1",
            "--ua", "cli/vault-recover"
        ]):
            with patch("agent_obs.guardrail.vault_client.VaultClient") as mock_client_class:
                mock_client = MagicMock()
                mock_client.is_available.return_value = True
                
                # Mock the recovery
                mock_client.recover = AsyncMock()
                mock_client.recover.return_value = "ivan@example.com"
                
                mock_client_class.return_value = mock_client
                
                # Import main to test it
                from agent_obs.guardrail.vault_client import main
                await main()
                
                # Verify client was created and recover was called
                mock_client_class.assert_called_once()
                mock_client.recover.assert_called_once_with(
                    vault_key="pii/test/123",
                    mfa_token="",
                    login="alice@company.com",
                    reason="debugging test CLI",
                    trace_id="01HZ...",
                    ip_address="192.168.1.1",
                    user_agent="cli/vault-recover"
                )

    @pytest.mark.asyncio
    async def test_cli_recover_mfa_with_token(self):
        """Test CLI recover with MFA token."""
        with patch("sys.argv", [
            "vault_client.py", 
            "recover", 
            "--key", "pii/test/123",
            "--login", "alice@company.com",
            "--mfa", "123456",
            "--reason", "MFA test CLI"
        ]):
            with patch("agent_obs.guardrail.vault_client.VaultClient") as mock_client_class:
                mock_client = MagicMock()
                mock_client.is_available.return_value = True
                
                # Mock the recovery
                mock_client.recover = AsyncMock()
                mock_client.recover.return_value = "ivan@example.com"
                
                mock_client_class.return_value = mock_client
                
                # Import main to test it
                from agent_obs.guardrail.vault_client import main
                await main()
                
                # Verify MFA token was passed
                mock_client.recover.assert_called_once_with(
                    vault_key="pii/test/123",
                    mfa_token="123456",
                    login="alice@company.com",
                    reason="MFA test CLI",
                    trace_id="",
                    ip_address="",
                    user_agent=""
                )

    def test_cli_issue_mfa_backward_compatibility(self):
        """Test that issue-mfa command still works with --login."""
        with patch("sys.argv", ["vault_client.py", "issue-mfa", "--login", "alice@company.com"]):
            with patch("agent_obs.guardrail.vault_client.generate_totp_secret") as mock_generate:
                mock_generate.return_value = "JBSWY3DPEHPK3PXP"
                
                # Import main to test it
                from agent_obs.guardrail.vault_client import main
                asyncio.run(main())
                
                # Verify generate_totp_secret was called
                mock_generate.assert_called_once_with("alice@company.com")

    def test_cli_health_command(self):
        """Test that health command still works."""
        with patch("sys.argv", ["vault_client.py", "health"]):
            with patch("agent_obs.guardrail.vault_client.VaultClient") as mock_client_class:
                mock_client = MagicMock()
                mock_client.is_available.return_value = True
                mock_client_class.return_value = mock_client
                
                # Import main to test it
                from agent_obs.guardrail.vault_client import main
                asyncio.run(main())
                
                # Verify client was created and is_available was called
                mock_client_class.assert_called_once()
                mock_client.is_available.assert_called_once()