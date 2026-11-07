"""PP56: Tests for OPA bundle hot-reload functionality.

Tests bundle polling, policy updates, audit logging, and file watcher integration.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
import httpx

from agent_obs.rbac.policy_engine import PolicyEngine, User, Resource
from agent_obs.rbac.audit_bundle_reload import BundleReloadLogger, get_bundle_logger
from agent_obs.storage.hot import HotStore


class TestOPABundlePolling:
    """Test bundle polling functionality in PolicyEngine."""

    @pytest.fixture
    def mock_hot_store(self):
        """Mock HotStore for audit logging."""
        store = AsyncMock(spec=HotStore)
        store.write_audit_events = AsyncMock()
        return store

    @pytest.fixture
    def policy_engine(self, mock_hot_store):
        """Create PolicyEngine with bundle polling enabled."""
        with patch('agent_obs.rbac.policy_engine.get_bundle_logger') as mock_logger:
            mock_logger.return_value = BundleReloadLogger(mock_hot_store)
            engine = PolicyEngine(
                opa_url="http://test-opa:8181",
                bundle_url="http://test-bundle:8080",
                bundle_poll_interval=1,  # Fast polling for tests
                timeout=1.0,
            )
            return engine

    @pytest.mark.asyncio
    async def test_bundle_polling_no_update(self, policy_engine):
        """Test that polling doesn't trigger updates when bundle hasn't changed."""
        with patch.object(policy_engine, '_fetch_bundle_version') as mock_fetch:
            mock_fetch.return_value = "v1.0.0"
            
            # Set initial version
            policy_engine._bundle_version = "v1.0.0"
            
            # Check updates (should not trigger reload)
            await policy_engine._check_bundle_updates()
            
            # Verify no change
            assert policy_engine._bundle_version == "v1.0.0"
            mock_fetch.assert_called_once()

    @pytest.mark.asyncio
    async def test_bundle_polling_with_update(self, policy_engine):
        """Test that polling triggers reload when bundle changes."""
        with patch.object(policy_engine, '_fetch_bundle_version') as mock_fetch, \
             patch.object(policy_engine, '_fetch_bundle_policies') as mock_policies:
            
            # Mock fetch to return different values on subsequent calls
            call_count = 0
            def mock_fetch_impl():
                nonlocal call_count
                call_count += 1
                return "v2.0.0"  # Always return a different value
            
            mock_fetch.side_effect = mock_fetch_impl
            mock_policies.return_value = {"version": "v1.0.0"}
            
            # Set initial version
            policy_engine._bundle_version = "v1.0.0"
            policy_engine._last_bundle_check = None  # Force first check
            
            # Check updates (should trigger reload)
            await policy_engine._check_bundle_updates()
            
            # Debug: print current state
            print(f"Debug: bundle_version = {policy_engine._bundle_version}")
            print(f"Debug: last_bundle_check = {policy_engine._last_bundle_check}")
            print(f"Debug: fetch_bundle_version call count = {mock_fetch.call_count}")
            
            # Verify update occurred
            assert policy_engine._bundle_version == "v2.0.0"

    @pytest.mark.asyncio
    async def test_force_bundle_reload(self, policy_engine):
        """Test force bundle reload functionality."""
        with patch.object(policy_engine, '_fetch_bundle_version') as mock_fetch, \
             patch.object(policy_engine, '_fetch_bundle_policies') as mock_policies:
            
            mock_fetch.return_value = "v2.0.0"
            mock_policies.return_value = {"version": "v2.0.0"}
            
            # Set initial version
            policy_engine._bundle_version = "v1.0.0"
            
            # Force reload
            await policy_engine.force_bundle_reload()
            
            # Verify update occurred
            assert policy_engine._bundle_version == "v2.0.0"

    @pytest.mark.asyncio
    async def test_bundle_polling_error_handling(self, policy_engine):
        """Test that polling errors don't break the system."""
        with patch.object(policy_engine, '_fetch_bundle_version') as mock_fetch:
            mock_fetch.side_effect = Exception("Network error")
            
            # Should not raise exception
            await policy_engine._check_bundle_updates()
            
            # Verify version unchanged
            assert policy_engine._bundle_version is None


class TestBundleReloadAudit:
    """Test bundle reload audit logging functionality."""

    @pytest.fixture
    def mock_hot_store(self):
        """Mock HotStore for audit logging."""
        store = AsyncMock(spec=HotStore)
        store.write_audit_events = AsyncMock()
        return store

    @pytest.fixture
    def bundle_logger(self, mock_hot_store):
        """Create BundleReloadLogger for testing."""
        return BundleReloadLogger(mock_hot_store)

    @pytest.mark.asyncio
    async def test_log_reload_success(self, bundle_logger, mock_hot_store):
        """Test successful bundle reload logging."""
        old_version = "v1.0.0"
        new_version = "v2.0.0"
        old_policies = {"policy1": "old_content"}
        new_policies = {"policy1": "new_content", "policy2": "added"}
        
        audit_id = await bundle_logger.log_reload(
            old_version,
            new_version,
            old_policies,
            new_policies,
            trigger="file_change",
            triggered_by="admin",
        )
        
        # Verify audit ID is generated
        assert isinstance(audit_id, str)
        assert len(audit_id) == 64  # SHA256 hex length
        
        # Verify audit event was written
        mock_hot_store.write_audit_events.assert_called_once()
        
        # Verify the logged event
        call_args = mock_hot_store.write_audit_events.call_args[0][0]
        assert len(call_args) == 1
        event = call_args[0]
        
        assert event["action"] == "policy_reload"
        assert event["old_version"] == old_version
        assert event["new_version"] == new_version
        assert event["success"] is True
        assert event["trigger"] == "file_change"
        assert event["triggered_by"] == "admin"
        assert event["severity"] == "info"

    @pytest.mark.asyncio
    async def test_log_reload_failure(self, bundle_logger, mock_hot_store):
        """Test failed bundle reload logging."""
        old_version = "v1.0.0"
        new_version = "v2.0.0"
        old_policies = {"policy1": "old_content"}
        new_policies = {"policy1": "new_content"}
        error_message = "Bundle validation failed"
        
        audit_id = await bundle_logger.log_reload(
            old_version,
            new_version,
            old_policies,
            new_policies,
            trigger="manual",
            triggered_by="admin",
            error_message=error_message,
        )
        
        # Verify audit ID is generated
        assert isinstance(audit_id, str)
        
        # Verify the logged event
        call_args = mock_hot_store.write_audit_events.call_args[0][0]
        event = call_args[0]
        
        assert event["success"] is False
        assert event["error_message"] == error_message
        assert event["severity"] == "warning"

    def test_policy_diff_calculation(self, bundle_logger):
        """Test policy difference calculation."""
        old_policies = {
            "policy1": "content1",
            "policy2": "content2",
            "policy3": "content3",
        }
        new_policies = {
            "policy2": "content2_updated",  # Modified
            "policy3": "content3",         # Unchanged
            "policy4": "content4",         # Added
        }
        
        diff = bundle_logger._calculate_policy_diff(old_policies, new_policies)
        
        # Verify removed policies
        assert len(diff["removed"]) == 1
        assert diff["removed"][0]["key"] == "policy1"
        assert diff["removed"][0]["value"] == "content1"
        
        # Verify modified policies
        assert len(diff["modified"]) == 1
        assert diff["modified"][0]["key"] == "policy2"
        assert diff["modified"][0]["old_value"] == "content2"
        assert diff["modified"][0]["new_value"] == "content2_updated"
        
        # Verify added policies
        assert len(diff["added"]) == 1
        assert diff["added"][0]["key"] == "policy4"
        assert diff["added"][0]["value"] == "content4"

    @pytest.mark.asyncio
    async def test_audit_write_failure_handling(self, bundle_logger):
        """Test that audit write failures don't break reload operations."""
        # Make write_audit_events raise an exception
        bundle_logger._hot_store.write_audit_events = AsyncMock(side_effect=Exception("Storage error"))
        
        # Should not raise exception
        audit_id = await bundle_logger.log_reload(
            "v1.0.0",
            "v2.0.0",
            {"old": "policy"},
            {"new": "policy"},
        )
        
        # Verify audit ID was still generated
        assert isinstance(audit_id, str)


class TestBundleIntegration:
    """Test end-to-end bundle functionality integration."""

    @pytest.mark.asyncio
    async def test_policy_engine_with_bundle_updates(self):
        """Test PolicyEngine behavior with bundle updates."""
        with patch('agent_obs.rbac.policy_engine.get_bundle_logger') as mock_logger:
            mock_logger.return_value = BundleReloadLogger(None)
            
            # Create a custom role store with admin role
            from agent_obs.rbac.roles import MemoryRoleStore
            role_store = MemoryRoleStore({"admin": "admin"})
            
            engine = PolicyEngine(
                store=role_store,
                opa_url="http://test-opa:8181",
                bundle_url="http://test-bundle:8080",
                bundle_poll_interval=1,
                timeout=1.0,
            )
            
            # Mock OPA response
            with patch.object(engine, '_query_opa') as mock_query:
                mock_query.return_value = True
                
                # Mock bundle update to return different values
                call_count = 0
                def mock_fetch_impl():
                    nonlocal call_count
                    call_count += 1
                    return "v2.0.0" if call_count > 1 else "v1.0.0"
                
                with patch.object(engine, '_fetch_bundle_version', side_effect=mock_fetch_impl):
                    
                    # Initial check
                    user = User(id="admin", tenant_id="acme", mfa_verified=True)
                    resource = Resource(id="trace:123", tenant_id="acme")
                    
                    # First call should trigger bundle check
                    result1 = await engine.check(user, "trace:read", resource)
                    assert result1 is True
                    
                    # Second call should trigger bundle update
                    result2 = await engine.check(user, "trace:read", resource)
                    assert result2 is True
                    
                    # Verify bundle update was detected
                    assert engine._bundle_version == "v2.0.0"

    @pytest.mark.asyncio
    async def test_bundle_polling_timing(self):
        """Test that polling respects configured intervals."""
        with patch('agent_obs.rbac.policy_engine.get_bundle_logger') as mock_logger:
            mock_logger.return_value = BundleReloadLogger(None)
            
            engine = PolicyEngine(
                opa_url="http://test-opa:8181",
                bundle_url="http://test-bundle:8080",
                bundle_poll_interval=5,  # 5 second interval
                timeout=1.0,
            )
            
            with patch.object(engine, '_fetch_bundle_version') as mock_fetch:
                mock_fetch.return_value = "v1.0.0"
                
                # Set initial state
                engine._bundle_version = "v1.0.0"
                engine._last_bundle_check = datetime.now(timezone.utc) - timedelta(seconds=3)
                
                # Check updates (should not trigger due to timing)
                await engine._check_bundle_updates()
                
                # Verify no unnecessary calls - should not call fetch if interval not met
                mock_fetch.assert_not_called()


class TestBundleMetrics:
    """Test bundle reload metrics."""

    @pytest.mark.asyncio
    async def test_bundle_reload_metrics(self):
        """Test that bundle reload operations update metrics."""
        with patch('agent_obs.rbac.audit_bundle_reload._bundle_reload_total') as mock_total, \
             patch('agent_obs.rbac.audit_bundle_reload._bundle_reload_latency_seconds') as mock_latency:
            
            from agent_obs.rbac.audit_bundle_reload import get_bundle_logger
            
            logger = get_bundle_logger()
            
            # Log a reload
            await logger.log_reload(
                "v1.0.0",
                "v2.0.0",
                {"old": "policy"},
                {"new": "policy"},
            )
            
            # Verify metrics were updated
            mock_total.inc.assert_called_once()
            mock_latency.observe.assert_called_once()


class TestBundleReloadLogger:
    """Test BundleReloadLogger class."""

    def test_singleton_behavior(self):
        """Test that BundleReloadLogger is a singleton."""
        logger1 = get_bundle_logger()
        logger2 = get_bundle_logger()
        
        assert logger1 is logger2


# Add datetime import for timing tests
from datetime import datetime, timedelta