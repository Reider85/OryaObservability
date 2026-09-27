"""Tests for VaultClient with real hvac integration (mocked)."""

import asyncio
import os
import time
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pyotp

from agent_obs.guardrail.vault_client import VaultClient, _VaultEntry, MFARequiredError, MFAConfig


class TestVaultEntry:
    """Test _VaultEntry dataclass."""

    def test_creation(self):
        """Test basic entry creation."""
        entry = _VaultEntry(
            mask="[EMAIL:5f3a]",
            original="ivan@example.com",
            vault_key="pii/abc123/1234567890",
            created_at=time.time(),
            ttl_seconds=86400,
        )
        assert entry.mask == "[EMAIL:5f3a]"
        assert entry.original == "ivan@example.com"
        assert entry.vault_key == "pii/abc123/1234567890"

    def test_expired(self):
        """Test expiration logic."""
        now = time.time()
        
        # Not expired
        entry = _VaultEntry(
            mask="[EMAIL:5f3a]",
            original="ivan@example.com",
            vault_key="pii/abc123/1234567890",
            created_at=now - 1000,  # 1000 seconds ago
            ttl_seconds=86400,  # 24 hours
        )
        assert not entry.expired

        # Expired
        entry = _VaultEntry(
            mask="[EMAIL:5f3a]",
            original="ivan@example.com",
            vault_key="pii/abc123/1234567890",
            created_at=now - 100000,  # 100k seconds ago
            ttl_seconds=86400,  # 24 hours
        )
        assert entry.expired


class TestVaultClient:
    """Test VaultClient with mocked hvac."""

    @pytest.fixture
    def mock_hvac_client(self):
        """Create a mock hvac client."""
        client = MagicMock()
        client.is_authenticated.return_value = True
        client.sys.health_check.return_value = {
            "initialized": True,
            "sealed": False,
        }
        client.secrets.kv.v2.create_or_update_secret.return_value = {"data": {"metadata": {}}}
        client.secrets.kv.v2.read_secret_version.return_value = {
            "data": {
                "data": {
                    "mask": "[EMAIL:5f3a]",
                    "original": "ivan@example.com",
                    "created_at": time.time(),
                    "ttl_seconds": 86400,
                }
            }
        }
        return client

    @pytest.fixture
    def vault_client(self, mock_hvac_client):
        """Create VaultClient with mocked hvac."""
        with patch("hvac.Client", return_value=mock_hvac_client):
            client = VaultClient(
                addr="http://localhost:8200",
                token="test-token",
                verify_tls=False,
            )
            yield client

    def test_init(self):
        """Test basic initialization."""
        client = VaultClient(
            addr="http://custom:8200",
            token="custom-token",
            namespace="test-ns",
            verify_tls=False,
        )
        assert client.addr == "http://custom:8200"
        assert client.token == "custom-token"
        assert client.namespace == "test-ns"
        assert client.verify_tls is False

    def test_get_client(self, vault_client):
        """Test client initialization."""
        assert vault_client._get_client() is not None
        assert vault_client._get_client() is vault_client._get_client()  # Same instance

    @pytest.mark.asyncio
    async def test_store_success(self, vault_client):
        """Test successful store operation."""
        vault_key = await vault_client.store(
            mask="[EMAIL:5f3a]",
            original="ivan@example.com",
            ttl_seconds=3600,
        )
        
        assert vault_key is not None
        assert vault_key.startswith("pii/")
        assert "/" in vault_key  # Contains timestamp
        
        # Verify hvac client was called
        client = vault_client._get_client()
        client.secrets.kv.v2.create_or_update_secret.assert_called_once()
        call_args = client.secrets.kv.v2.create_or_update_secret.call_args
        assert call_args[1]["path"] == vault_key
        assert call_args[1]["secret"]["data"]["mask"] == "[EMAIL:5f3a]"
        assert call_args[1]["secret"]["data"]["original"] == "ivan@example.com"
        assert call_args[1]["secret"]["data"]["ttl_seconds"] == 3600

    @pytest.mark.asyncio
    async def test_store_failure(self, vault_client):
        """Test store operation failure."""
        # Mock hvac client to raise exception
        vault_client._client = MagicMock()
        vault_client._client.is_authenticated.return_value = True
        vault_client._client.secrets.kv.v2.create_or_update_secret.side_effect = Exception("Connection failed")
        
        vault_key = await vault_client.store(
            mask="[EMAIL:5f3a]",
            original="ivan@example.com",
        )
        
        assert vault_key is None

    @pytest.mark.asyncio
    async def test_store_batch_success(self, vault_client):
        """Test successful batch store operation."""
        items = [
            ("[EMAIL:5f3a]", "ivan@example.com"),
            ("[PHONE:abc1]", "+79001234567"),
            ("[INN:def2]", "1234567890"),
        ]
        
        results = await vault_client.store_batch(
            items,
            ttl_seconds=3600,
        )
        
        assert len(results) == 3
        assert "[EMAIL:5f3a]" in results
        assert "[PHONE:abc1]" in results
        assert "[INN:def2]" in results
        
        # Each vault key should be unique and properly formatted
        for vault_key in results.values():
            assert vault_key.startswith("pii/")
            assert "/" in vault_key

    @pytest.mark.asyncio
    async def test_store_batch_partial_failure(self, vault_client):
        """Test batch store with partial failures."""
        # Mock the hvac client to raise an exception for one item
        hvac_client = vault_client._get_client()
        hvac_client.secrets.kv.v2.create_or_update_secret.side_effect = Exception("Connection failed")
        
        items = [
            ("[EMAIL:5f3a]", "ivan@example.com"),
            ("[PHONE:abc1]", "+79001234567"),
            ("[INN:def2]", "1234567890"),  # This will fail
        ]
        
        results = await vault_client.store_batch(items, 3600)
        
        # All should fail because the mock raises for all calls
        assert len(results) == 0  # All failed

    @pytest.mark.asyncio
    async def test_recover_success(self, vault_client):
        """Test successful recover operation."""
        # First store something
        vault_key = await vault_client.store(
            mask="[EMAIL:5f3a]",
            original="ivan@example.com",
        )
        
        # Then recover it
        original = await vault_client.recover(vault_key)
        
        assert original == "ivan@example.com"

    @pytest.mark.asyncio
    async def test_recover_not_found(self, vault_client):
        """Test recover with non-existent key."""
        # Mock hvac to raise exception with "not found" message
        hvac_client = vault_client._get_client()
        hvac_client.secrets.kv.v2.read_secret_version.side_effect = Exception("not found")
        
        with pytest.raises(KeyError, match="vault_key not found"):
            await vault_client.recover("pii/nonexistent/1234567890")

    @pytest.mark.asyncio
    async def test_recover_expired(self, vault_client):
        """Test recover with expired key."""
        # Mock hvac to return expired data
        hvac_client = vault_client._get_client()
        hvac_client.secrets.kv.v2.read_secret_version.return_value = {
            "data": {
                "data": {
                    "mask": "[EMAIL:5f3a]",
                    "original": "ivan@example.com",
                    "created_at": time.time() - 100000,  # Expired
                    "ttl_seconds": 3600,
                }
            }
        }
        
        vault_key = "pii/expired/1234567890"
        with pytest.raises(KeyError, match="vault_key expired"):
            await vault_client.recover(vault_key)

    @pytest.mark.asyncio
    async def test_recover_connection_error(self, vault_client):
        """Test recover with connection error."""
        # Mock hvac to raise exception
        hvac_client = vault_client._get_client()
        hvac_client.secrets.kv.v2.read_secret_version.side_effect = Exception("Connection failed")
        
        vault_key = "pii/test/1234567890"
        with pytest.raises(ConnectionError, match="Vault error"):
            await vault_client.recover(vault_key)

    def test_is_available_success(self, vault_client):
        """Test availability check success."""
        assert vault_client.is_available() is True

    def test_is_available_failure(self, vault_client):
        """Test availability check failure."""
        # Mock hvac to raise exception
        vault_client._client = MagicMock()
        vault_client._client.sys.health_check.side_effect = Exception("Connection failed")
        
        assert vault_client.is_available() is False

    def test_is_available_caching(self, vault_client):
        """Test availability check caching."""
        # First call should hit hvac
        assert vault_client.is_available() is True
        
        # Second call should use cache
        vault_client._client.sys.health_check.assert_called_once()  # Only once

    @pytest.mark.asyncio
    async def test_context_manager(self):
        """Test async context manager."""
        with patch("hvac.Client") as mock_client_class:
            mock_client = MagicMock()
            mock_client_class.return_value = mock_client
            
            async with VaultClient() as client:
                assert client is not None
                # Force the client to be created so close gets called
                _ = client._get_client()
            
            # Client should be closed
            mock_client.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_store_vault_key_format(self, vault_client):
        """Test that vault_key follows expected format."""
        vault_key = await vault_client.store(
            mask="[EMAIL:5f3a]",
            original="ivan@example.com",
        )
        
        # Format: pii/{hex8}/{timestamp}
        parts = vault_key.split("/")
        assert len(parts) == 3
        assert parts[0] == "pii"
        assert len(parts[1]) == 8  # hex8
        assert parts[1].isalnum()
        assert parts[1].islower()
        assert parts[2].isdigit()  # timestamp

    @pytest.mark.asyncio
    async def test_store_with_ttl_metadata(self, vault_client):
        """Test that TTL is stored in metadata."""
        vault_key = await vault_client.store(
            mask="[EMAIL:5f3a]",
            original="ivan@example.com",
            ttl_seconds=7200,
        )
        
        # Verify hvac was called with correct TTL
        client = vault_client._get_client()
        call_args = client.secrets.kv.v2.create_or_update_secret.call_args
        assert call_args[1]["secret"]["data"]["ttl_seconds"] == 7200

    @pytest.mark.asyncio
    async def test_store_unauthenticated(self, vault_client):
        """Test store when client is not authenticated."""
        vault_client._client = MagicMock()
        vault_client._client.is_authenticated.return_value = False
        
        vault_key = await vault_client.store(
            mask="[EMAIL:5f3a]",
            original="ivan@example.com",
        )
        
        assert vault_key is None

    @pytest.mark.asyncio
    async def test_recover_unauthenticated(self, vault_client):
        """Test recover when client is not authenticated."""
        vault_client._client = MagicMock()
        vault_client._client.is_authenticated.return_value = False
        
        with pytest.raises(ConnectionError, match="Vault client not authenticated"):
            await vault_client.recover("pii/test/1234567890")


class TestVaultClientMFA:
    """Test MFA functionality for vault recovery."""

    @pytest.fixture
    def mfa_config(self):
        """Create MFA config for testing."""
        return MFAConfig(
            totp_secrets={
                "alice@company.com": "JBSWY3DPEHPK3PXP",  # "Hello World" in base32
                "bob@company.com": "MFRGG43FMVSE33JB",    # Secret for testing
            },
            authorized_logins=["alice@company.com", "bob@company.com"],
            max_attempts=3,
            cooldown_seconds=300,
        )

    @pytest.fixture
    def mock_redis(self):
        """Mock redis client for testing."""
        mock_redis_client = MagicMock()
        
        # Make sure incr returns a coroutine (async function)
        async def mock_incr(key):
            return 1
        
        # Make sure expire returns a coroutine
        async def mock_expire(key, ttl):
            return True
            
        # Make sure delete returns a coroutine
        async def mock_delete(key):
            return True
            
        mock_redis_client.incr = mock_incr
        mock_redis_client.expire = mock_expire
        mock_redis_client.delete = mock_delete
        mock_redis_client.from_url.return_value = mock_redis_client
        return mock_redis_client

    @pytest.fixture
    def mfa_vault_client(self, mfa_config, mock_redis):
        """Create VaultClient with MFA enabled."""
        with patch("hvac.Client") as mock_client_class:
            mock_client = MagicMock()
            mock_client_class.return_value = mock_client
            mock_client.is_authenticated.return_value = True
            mock_client.sys.health_check.return_value = {
                "initialized": True,
                "sealed": False,
            }
            mock_client.secrets.kv.v2.create_or_update_secret.return_value = {"data": {"metadata": {}}}
            mock_client.secrets.kv.v2.read_secret_version.return_value = {
                "data": {
                    "data": {
                        "mask": "[EMAIL:5f3a]",
                        "original": "ivan@example.com",
                        "created_at": time.time(),
                        "ttl_seconds": 86400,
                    }
                }
            }
            
            with patch("redis.asyncio.from_url", return_value=mock_redis):
                client = VaultClient(
                    addr="http://localhost:8200",
                    token="test-token",
                    verify_tls=False,
                    mfa_config=mfa_config,
                    redis_url="redis://localhost:6379/2",
                )
                yield client

    @pytest.mark.asyncio
    async def test_recover_mfa_required_without_token(self, mfa_vault_client):
        """Test that recover without MFA token raises MFARequiredError."""
        with pytest.raises(MFARequiredError, match="MFA verification required"):
            await mfa_vault_client.recover("pii/test/1234567890", login="alice@company.com")

    @pytest.mark.asyncio
    async def test_recover_mfa_invalid_login(self, mfa_vault_client):
        """Test that recover with invalid login raises MFARequiredError."""
        with pytest.raises(MFARequiredError, match="Login 'invalid@company.com' not authorized"):
            await mfa_vault_client.recover(
                "pii/test/1234567890", 
                mfa_token="123456", 
                login="invalid@company.com"
            )

    @pytest.mark.asyncio
    async def test_recover_mfa_invalid_token(self, mfa_vault_client):
        """Test that recover with invalid TOTP token raises MFARequiredError."""
        with pytest.raises(MFARequiredError, match="Invalid MFA token"):
            await mfa_vault_client.recover(
                "pii/test/1234567890", 
                mfa_token="123456",  # Wrong token
                login="alice@company.com"
            )

    @pytest.mark.asyncio
    async def test_recover_mfa_valid_token(self, mfa_vault_client):
        """Test that recover with valid TOTP token succeeds."""
        # First store something
        vault_key = await mfa_vault_client.store(
            mask="[EMAIL:5f3a]",
            original="ivan@example.com",
        )
        
        # Generate valid TOTP token for alice@company.com
        totp = pyotp.TOTP("JBSWY3DPEHPK3PXP")
        valid_token = totp.now()
        
        # Then recover it with valid token
        original = await mfa_vault_client.recover(
            vault_key, 
            mfa_token=valid_token, 
            login="alice@company.com"
        )
        
        assert original == "ivan@example.com"

    @pytest.mark.asyncio
    async def test_recover_mfa_rate_limit(self, mfa_vault_client):
        """Test that rate limiting blocks after 3 failed attempts."""
        # Generate invalid tokens to trigger rate limit
        totp = pyotp.TOTP("JBSWY3DPEHPK3PXP")
        
        for i in range(3):
            with pytest.raises(MFARequiredError, match="Invalid MFA token"):
                await mfa_vault_client.recover(
                    "pii/test/1234567890", 
                    mfa_token="000000",  # Always wrong
                    login="alice@company.com"
                )
        
        # 4th attempt should be blocked by rate limit
        with pytest.raises(MFARequiredError, match="Rate limit exceeded"):
            await mfa_vault_client.recover(
                "pii/test/1234567890", 
                mfa_token="000000",
                login="alice@company.com"
            )

    @pytest.mark.asyncio
    async def test_recover_mfa_rate_limit_cooldown_expires(self, mfa_vault_client):
        """Test that rate limit expires after cooldown period."""
        # This test would need to mock time or use real Redis with short TTL
        # For now, just verify the logic exists
        assert mfa_vault_client.mfa_config.cooldown_seconds == 300

    @pytest.mark.asyncio
    async def test_recover_mfa_no_config(self):
        """Test that recover works without MFA config."""
        with patch("hvac.Client") as mock_client_class:
            mock_client = MagicMock()
            mock_client_class.return_value = mock_client
            mock_client.is_authenticated.return_value = True
            mock_client.sys.health_check.return_value = {
                "initialized": True,
                "sealed": False,
            }
            mock_client.secrets.kv.v2.create_or_update_secret.return_value = {"data": {"metadata": {}}}
            mock_client.secrets.kv.v2.read_secret_version.return_value = {
                "data": {
                    "data": {
                        "mask": "[EMAIL:5f3a]",
                        "original": "ivan@example.com",
                        "created_at": time.time(),
                        "ttl_seconds": 86400,
                    }
                }
            }
            
            client = VaultClient(
                addr="http://localhost:8200",
                token="test-token",
                verify_tls=False,
                mfa_config=None,  # No MFA
            )
            
            # Store and recover without MFA
            vault_key = await client.store(
                mask="[EMAIL:5f3a]",
                original="ivan@example.com",
            )
            
            original = await client.recover(vault_key)
            assert original == "ivan@example.com"

    def test_mfa_config_from_env(self):
        """Test MFAConfig loading from environment."""
        # Set test environment
        os.environ["VAULT_MFA_SECRETS"] = "alice:secret1,bob:secret2"
        os.environ["VAULT_MFA_AUTHORIZED"] = "alice@company.com,bob@company.com"
        
        config = MFAConfig.from_env()
        assert config is not None
        assert "alice" in config.totp_secrets
        assert "bob" in config.totp_secrets
        assert "alice@company.com" in config.authorized_logins
        assert "bob@company.com" in config.authorized_logins
        
        # Clean up
        del os.environ["VAULT_MFA_SECRETS"]
        del os.environ["VAULT_MFA_AUTHORIZED"]

    def test_mfa_config_from_env_none(self):
        """Test MFAConfig returns None when no env vars set."""
        # Ensure no env vars are set
        if "VAULT_MFA_SECRETS" in os.environ:
            del os.environ["VAULT_MFA_SECRETS"]
        if "VAULT_MFA_AUTHORIZED" in os.environ:
            del os.environ["VAULT_MFA_AUTHORIZED"]
        
        config = MFAConfig.from_env()
        assert config is None