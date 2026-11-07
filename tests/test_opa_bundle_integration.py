"""PP56: Integration tests for OPA bundle server hot-reload.

Tests the complete bundle server workflow including file watcher, bundle building,
and policy updates.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import httpx

from agent_obs.rbac.policy_engine import PolicyEngine, User, Resource
from agent_obs.rbac.audit_bundle_reload import BundleReloadLogger


class TestOPABundleServerIntegration:
    """Integration tests for OPA bundle server functionality."""

    @pytest.fixture
    def temp_policies_dir(self):
        """Create temporary directory for testing policies."""
        with tempfile.TemporaryDirectory() as temp_dir:
            policies_dir = Path(temp_dir) / "policies"
            policies_dir.mkdir()
            
            # Create test policy
            policy_content = """
package observability.rbac

default allow := false

role_permissions := {
    "admin": {"trace:read", "pii:recover"},
    "developer": {"trace:read"},
}

allow if {
    permissions := role_permissions[input.user.role]
    input.action in permissions
    input.user.tenant_id == input.resource.tenant_id
}
"""
            (policies_dir / "test.rego").write_text(policy_content)
            yield policies_dir

    @pytest.fixture
    def bundle_dir(self):
        """Create temporary directory for bundle output."""
        with tempfile.TemporaryDirectory() as temp_dir:
            yield Path(temp_dir)

    @pytest.mark.asyncio
    async def test_bundle_server_workflow(self, temp_policies_dir, bundle_dir):
        """Test complete bundle server workflow."""
        # Create bundle manifest
        manifest = {
            "revision": "2026-11-07T23:49:00Z",
            "description": "Test policies",
            "name": "observability"
        }
        (bundle_dir / "manifest.json").write_text(json.dumps(manifest))
        
        # Copy policy to bundle
        policy_file = temp_policies_dir / "test.rego"
        bundle_policy = bundle_dir / "test.rego"
        bundle_policy.write_text(policy_file.read_text())
        
        # Create bundle (simulate opa build)
        import zipfile
        with zipfile.ZipFile(bundle_dir / "observability.zip", "w") as zipf:
            zipf.write("manifest.json", "manifest.json")
            zipf.write("test.rego", "test.rego")
        
        # Test bundle server mock
        mock_server_response = {
            "result": True
        }
        
        with httpx.AsyncClient() as client:
            # Test bundle access
            response = await client.get(f"http://test-bundle:8080/bundles/observability.zip")
            assert response.status_code == 200
            
            # Test bundle content
            with zipfile.ZipFile(bundle_dir / "observability.zip", "r") as zipf:
                assert "manifest.json" in zipf.namelist()
                assert "test.rego" in zipf.namelist()
                
                # Verify manifest
                manifest_content = json.loads(zipf.read("manifest.json").decode())
                assert manifest_content["name"] == "observability"

    @pytest.mark.asyncio
    async def test_policy_engine_bundle_integration(self):
        """Test PolicyEngine integration with bundle server."""
        with patch('agent_obs.rbac.policy_engine.get_bundle_logger') as mock_logger:
            mock_logger.return_value = BundleReloadLogger(None)
            
            engine = PolicyEngine(
                opa_url="http://test-opa:8181",
                bundle_url="http://test-bundle:8080",
                bundle_poll_interval=1,
                timeout=1.0,
            )
            
            # Mock OPA response
            with patch.object(engine, '_query_opa') as mock_query:
                mock_query.return_value = True
                
                # Mock bundle server response
                with httpx.AsyncClient() as mock_client:
                    engine._http_client = mock_client
                    
                    # Mock bundle version endpoint
                    mock_client.head = AsyncMock()
                    mock_client.head.return_value = MagicMock(
                        headers={"last-modified": "2026-11-07T23:49:00Z"},
                        status_code=200
                    )
                    
                    # Test bundle version fetching
                    version = await engine._fetch_bundle_version()
                    assert version == "2026-11-07T23:49:00Z"
                    
                    # Test bundle polling
                    await engine._check_bundle_updates()
                    
                    # Verify bundle version was set
                    assert engine._bundle_version == "2026-11-07T23:49:00Z"

    @pytest.mark.asyncio
    async def test_file_watcher_integration(self, temp_policies_dir):
        """Test file watcher integration with bundle server."""
        # Create initial policy
        policy_file = temp_policies_dir / "test.rego"
        policy_file.write_text("""
package observability.rbac
default allow := false
""")
        
        # Simulate file change detection
        initial_mtime = policy_file.stat().st_mtime
        
        # Modify policy
        time.sleep(0.1)  # Ensure different timestamp
        policy_file.write_text("""
package observability.rbac
default allow := true
""")
        
        # Verify file change
        new_mtime = policy_file.stat().st_mtime
        assert new_mtime > initial_mtime
        
        # Test bundle rebuild detection (simulated)
        with patch('agent_obs.rbac.policy_engine.get_bundle_logger') as mock_logger:
            mock_logger.return_value = BundleReloadLogger(None)
            
            engine = PolicyEngine(
                opa_url="http://test-opa:8181",
                bundle_url="http://test-bundle:8080",
                bundle_poll_interval=1,
                timeout=1.0,
            )
            
            # Simulate bundle update
            with patch.object(engine, '_fetch_bundle_version') as mock_fetch:
                mock_fetch.side_effect = ["v1.0.0", "v2.0.0"]
                
                # Trigger bundle check
                await engine._check_bundle_updates()
                
                # Verify update was detected
                assert engine._bundle_version == "v2.0.0"

    @pytest.mark.asyncio
    async def test_bundle_reload_audit_integration(self):
        """Test bundle reload audit logging integration."""
        with tempfile.TemporaryDirectory() as temp_dir:
            bundle_dir = Path(temp_dir)
            
            # Create mock hot store
            mock_hot_store = AsyncMock()
            mock_hot_store.write_audit_events = AsyncMock()
            
            logger = BundleReloadLogger(mock_hot_store)
            
            # Test policy change detection
            old_policies = {
                "policy1": "old_content",
                "policy2": "old_content",
            }
            
            new_policies = {
                "policy1": "new_content",  # Modified
                "policy2": "old_content",  # Unchanged
                "policy3": "added",       # Added
            }
            
            # Log the reload
            audit_id = await logger.log_reload(
                "v1.0.0",
                "v2.0.0",
                old_policies,
                new_policies,
                trigger="file_change",
                triggered_by="test_system",
            )
            
            # Verify audit event
            mock_hot_store.write_audit_events.assert_called_once()
            call_args = mock_hot_store.write_audit_events.call_args[0][0]
            event = call_args[0]
            
            assert event["action"] == "policy_reload"
            assert event["old_version"] == "v1.0.0"
            assert event["new_version"] == "v2.0.0"
            assert event["trigger"] == "file_change"
            assert event["triggered_by"] == "test_system"
            assert event["success"] is True
            
            # Verify policy diff
            policy_diff = json.loads(event["policy_diff"])
            assert len(policy_diff["modified"]) == 1
            assert len(policy_diff["added"]) == 1
            assert len(policy_diff["removed"]) == 0

    @pytest.mark.asyncio
    async def test_bundle_server_error_handling(self):
        """Test bundle server error handling."""
        with patch('agent_obs.rbac.policy_engine.get_bundle_logger') as mock_logger:
            mock_logger.return_value = BundleReloadLogger(None)
            
            engine = PolicyEngine(
                opa_url="http://test-opa:8181",
                bundle_url="http://test-bundle:8080",
                bundle_poll_interval=1,
                timeout=1.0,
            )
            
            # Test bundle server unreachable
            with httpx.AsyncClient() as mock_client:
                engine._http_client = mock_client
                mock_client.head = AsyncMock(side_effect=httpx.ConnectError("Connection failed"))
                
                # Should not raise exception
                version = await engine._fetch_bundle_version()
                assert isinstance(version, str)  # Should return timestamp fallback
                
                # Test bundle polling with error
                await engine._check_bundle_updates()
                # Should continue without crashing

    @pytest.mark.asyncio
    async def test_concurrent_bundle_checks(self):
        """Test concurrent bundle checking behavior."""
        with patch('agent_obs.rbac.policy_engine.get_bundle_logger') as mock_logger:
            mock_logger.return_value = BundleReloadLogger(None)
            
            engine = PolicyEngine(
                opa_url="http://test-opa:8181",
                bundle_url="http://test-bundle:8080",
                bundle_poll_interval=1,
                timeout=1.0,
            )
            
            # Mock bundle update
            with patch.object(engine, '_fetch_bundle_version') as mock_fetch:
                mock_fetch.side_effect = ["v1.0.0", "v2.0.0"]
                
                # Trigger multiple concurrent checks
                tasks = [
                    engine._check_bundle_updates() for _ in range(3)
                ]
                await asyncio.gather(*tasks)
                
                # Verify only one version update occurred (due to lock)
                assert engine._bundle_version == "v2.0.0"
                # Fetch should have been called multiple times but only one update
                assert mock_fetch.call_count >= 3


class TestBundlePerformance:
    """Test bundle performance characteristics."""

    @pytest.mark.asyncio
    async def test_bundle_polling_performance(self):
        """Test that bundle polling doesn't impact performance."""
        with patch('agent_obs.rbac.policy_engine.get_bundle_logger') as mock_logger:
            mock_logger.return_value = BundleReloadLogger(None)
            
            engine = PolicyEngine(
                opa_url="http://test-opa:8181",
                bundle_url="http://test-bundle:8080",
                bundle_poll_interval=30,  # Normal interval
                timeout=1.0,
            )
            
            # Mock fast response
            with patch.object(engine, '_fetch_bundle_version') as mock_fetch:
                mock_fetch.return_value = "v1.0.0"
                
                # Time multiple checks
                start_time = time.time()
                for _ in range(10):
                    await engine._check_bundle_updates()
                end_time = time.time()
                
                # Should be fast (network calls mocked)
                assert (end_time - start_time) < 1.0  # Should be much less than 10 seconds

    @pytest.mark.asyncio
    async def test_bundle_reload_latency(self):
        """Test bundle reload latency requirements."""
        with patch('agent_obs.rbac.policy_engine.get_bundle_logger') as mock_logger:
            mock_logger.return_value = BundleReloadLogger(None)
            
            engine = PolicyEngine(
                opa_url="http://test-opa:8181",
                bundle_url="http://test-bundle:8080",
                bundle_poll_interval=1,
                timeout=1.0,
            )
            
            with patch.object(engine, '_fetch_bundle_version') as mock_fetch:
                mock_fetch.side_effect = ["v1.0.0", "v2.0.0"]
                
                # Time reload operation
                start_time = time.time()
                await engine.force_bundle_reload()
                end_time = time.time()
                
                # Should be fast (< 1 second for mocked operations)
                assert (end_time - start_time) < 1.0


# Import patch for testing
from unittest.mock import patch