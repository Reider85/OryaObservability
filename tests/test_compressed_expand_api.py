"""
Tests for PP19 compressed node expand API.

Tests FastAPI endpoint, RBAC, MFA, audit logging, and UI integration.
"""

import pytest
from fastapi.testclient import TestClient
from unittest.mock import Mock, patch, AsyncMock
import json
import time
from datetime import datetime
import os

from agent_obs.trace_viewer.ui import app, MockHotStore


# Create a single mock store instance for all tests
# self.mock_store = MockTestHotStore()

# Patch the get_hot_store function
from agent_obs.trace_viewer.ui import get_hot_store
import unittest.mock
# patcher = unittest.mock.patch('agent_obs.trace_viewer.ui.get_hot_store', return_value=None)
# patcher.start()


class MockTestHotStore(MockHotStore):
    """Enhanced MockHotStore for expand API tests"""
    
    def __init__(self):
        super().__init__()
        self.spans_data = {
            "trace_123": self._create_compressed_trace_data(),
            "trace_empty": [],
            "trace_no_node": [
                {
                    "trace_id": "trace_no_node",
                    "span_id": "root_1",
                    "parent_span_id": "",
                    "name": "agent.loop",
                    "span_type": "agent.loop",
                    "start_time": 1705326600.0,
                    "end_time": 1705326602.0,
                    "status": "ok",
                    "attributes": {"agent_id": "test-agent"},
                    "events": []
                }
            ]
        }
        self.audit_events = []
    
    def _create_compressed_trace_data(self):
        """Create trace data with a compressed node"""
        root_span = {
            "trace_id": "trace_123",
            "span_id": "root_1",
            "parent_span_id": "",
            "name": "agent.loop",
            "span_type": "agent.loop",
            "start_time": 1705326600.0,
            "end_time": 1705326602.0,
            "status": "ok",
            "attributes": {"agent_id": "test-agent"},
            "events": []
        }
        
        compressed_node = {
            "trace_id": "trace_123",
            "span_id": "compressed_1",
            "parent_span_id": "root_1",
            "name": "compressed:5",
            "span_type": "agent.reasoning.compressed",
            "start_time": 1705326602.0,
            "end_time": 1705326603.0,
            "status": "ok",
            "attributes": {
                "agent_id": "test-agent",
                "tree.compressed": True,
                "tree.children_count": 5,
                "tree.summary": "pending",
                "tree.policy_version": "1"
            },
            "events": []
        }
        
        child1 = {
            "trace_id": "trace_123",
            "span_id": "child_1",
            "parent_span_id": "compressed_1",
            "name": "llm.call",
            "span_type": "llm.call",
            "start_time": 1705326603.0,
            "end_time": 1705326604.0,
            "status": "ok",
            "attributes": {
                "agent_id": "test-agent",
                "model": "gpt-4",
                "llm.input_text": "Hello world",
                "llm.output_text": "Hello there"
            },
            "events": []
        }
        
        child2 = {
            "trace_id": "trace_123",
            "span_id": "child_2",
            "parent_span_id": "compressed_1",
            "name": "tool.call",
            "span_type": "tool.call",
            "start_time": 1705326604.0,
            "end_time": 1705326605.0,
            "status": "ok",
            "attributes": {
                "agent_id": "test-agent",
                "tool.name": "calculator",
                "tool.input": "2+2"
            },
            "events": []
        }
        
        return [root_span, compressed_node, child1, child2]
    
    def write_audit_event(self, event):
        """Mock method to write audit events"""
        print(f"DEBUG: Writing audit event to store: {id(self)}")
        print(f"DEBUG: Writing audit event: {event}")
        self.audit_events.append(event)
    
    def get_spans(self, trace_id: str):
        """Mock method to get spans for a trace"""
        if trace_id in self.spans_data:
            return self.spans_data[trace_id]
        raise Exception("Trace not found")


class TestCompressedExpandAPI:
    """Test suite for compressed node expand API"""
    
    def setup_method(self):
        """Setup test client and mock data"""
        self.client = TestClient(app)
        self.trace_id = "trace_123"
        self.node_id = "compressed_1"
        self.user = "bob"  # Use bob who has developer role
        
        # Set RBAC roles for testing
        os.environ["AGENT_OBS_RBAC_ROLES"] = "alice:admin,bob:developer,charlie:auditor"
        
        # Debug: print environment variable
        print(f"RBAC_ROLES set to: {os.environ.get('AGENT_OBS_RBAC_ROLES')}")
        
        # Create a single mock store instance for all tests
        self.mock_store = MockTestHotStore()

        # Patch the HotStore class to use our mock
        import unittest.mock
        from agent_obs.storage.hot import HotStore
        self.patcher = unittest.mock.patch('agent_obs.storage.hot.HotStore', return_value=self.mock_store)
        self.patcher.start()
    
    def teardown_method(self):
        """Clean up environment"""
        if "AGENT_OBS_RBAC_ROLES" in os.environ:
            del os.environ["AGENT_OBS_RBAC_ROLES"]
        self.patcher.stop()
    
    def test_expand_compressed_node_success(self):
        """Test successful expand of compressed node"""
        response = self.client.get(
            f"/api/v1/traces/{self.trace_id}/compressed/{self.node_id}/expand",
            headers={"X-OryaObs-User": self.user}
        )
        
        assert response.status_code == 200
        data = response.json()
        
        assert data["trace_id"] == self.trace_id
        assert data["node_id"] == self.node_id
        assert data["storage_tier"] == "hot"
        assert data["mfa_used"] == False
        assert "query_latency_ms" in data
        assert len(data["children"]) == 2  # child_1 + child_2
        assert data["children_count"] == 2
        
        # Check children structure
        child_names = [child["name"] for child in data["children"]]
        assert "llm.call" in child_names
        assert "tool.call" in child_names
        
        # Check audit event was logged
        mock_store = self.mock_store
        print(f"DEBUG: Checking store with ID: {id(mock_store)}")
        print(f"DEBUG: Number of audit events: {len(mock_store.audit_events)}")
        print(f"DEBUG: Audit events: {mock_store.audit_events}")
        assert len(mock_store.audit_events) == 1
        audit_event = mock_store.audit_events[0]
        assert audit_event.decision == "allow"
        assert audit_event.reason == "success"
        assert audit_event.user == self.user
    
    def test_expand_compressed_node_with_mfa(self):
        """Test expand with MFA for PII fields"""
        response = self.client.get(
            f"/api/v1/traces/{self.trace_id}/compressed/{self.node_id}/expand",
            headers={
                "X-OryaObs-User": self.user,
                "X-OryaObs-MFA": "123456"
            },
            params={"fields": "llm.input_text,llm.output_text"}
        )
        
        assert response.status_code == 200
        data = response.json()
        assert data["mfa_used"] == True
        
        # Check audit event shows MFA was used
        mock_store = self.mock_store
        audit_event = mock_store.audit_events[-1]
        assert audit_event.mfa_used == True
    
    def test_expand_missing_user_header(self):
        """Test expand without user header (RBAC fail)"""
        response = self.client.get(
            f"/api/v1/traces/{self.trace_id}/compressed/{self.node_id}/expand"
        )
        
        assert response.status_code == 403
        assert "X-OryaObs-User header required" in response.json()["detail"]
        
        # Check deny audit event
        mock_store = self.mock_store
        audit_event = mock_store.audit_events[-1]
        assert audit_event.decision == "deny"
        assert audit_event.reason == "rbac_denied"
    
    def test_expand_insufficient_permissions(self):
        """Test expand with insufficient permissions (auditor role)"""
        # Use a user with auditor role (only has trace:read, not compressed:expand)
        auditor_user = "charlie"
        
        response = self.client.get(
            f"/api/v1/traces/{self.trace_id}/compressed/{self.node_id}/expand",
            headers={"X-OryaObs-User": auditor_user}
        )
        
        assert response.status_code == 403
        assert "compressed:expand permission required" in response.json()["detail"]
        
        # Check deny audit event
        mock_store = self.mock_store
        audit_event = mock_store.audit_events[-1]
        assert audit_event.decision == "deny"
        assert audit_event.reason == "rbac_denied"
        assert audit_event.user == auditor_user
    
    def test_expand_missing_mfa_for_pii(self):
        """Test expand without MFA for PII fields"""
        response = self.client.get(
            f"/api/v1/traces/{self.trace_id}/compressed/{self.node_id}/expand",
            headers={"X-OryaObs-User": self.user},
            params={"fields": "llm.input_text"}
        )
        
        assert response.status_code == 403
        assert "MFA: X-OryaObs-MFA header required for PII fields" in response.json()["detail"]
        
        # Check deny audit event
        mock_store = self.mock_store
        audit_event = mock_store.audit_events[-1]
        assert audit_event.decision == "deny"
        assert audit_event.reason == "mfa_missing"
    
    def test_expand_invalid_mfa(self):
        """Test expand with invalid MFA token"""
        response = self.client.get(
            f"/api/v1/traces/{self.trace_id}/compressed/{self.node_id}/expand",
            headers={
                "X-OryaObs-User": self.user,
                "X-OryaObs-MFA": "invalid_token"
            },
            params={"fields": "llm.input_text"}
        )
        
        assert response.status_code == 403
        assert "MFA: Invalid token provided" in response.json()["detail"]
        
        # Check deny audit event
        mock_store = self.mock_store
        audit_event = mock_store.audit_events[-1]
        assert audit_event.decision == "deny"
        assert audit_event.reason == "mfa_invalid"
    
    def test_expand_trace_not_found(self):
        """Test expand for non-existent trace"""
        response = self.client.get(
            f"/api/v1/traces/nonexistent_trace/compressed/{self.node_id}/expand",
            headers={"X-OryaObs-User": self.user}
        )
        
        assert response.status_code == 404
        assert "not found" in response.json()["detail"]
        assert "PP21" in response.json()["detail"]
        
        # Check deny audit event
        mock_store = self.mock_store
        audit_event = mock_store.audit_events[-1]
        assert audit_event.decision == "deny"
        assert audit_event.reason == "trace_not_found"
    
    def test_expand_node_not_found(self):
        """Test expand for non-existent node"""
        response = self.client.get(
            f"/api/v1/traces/{self.trace_id}/compressed/nonexistent_node/expand",
            headers={"X-OryaObs-User": self.user}
        )
        
        assert response.status_code == 404
        assert "not found in trace" in response.json()["detail"]
        
        # Check deny audit event
        mock_store = self.mock_store
        audit_event = mock_store.audit_events[-1]
        assert audit_event.decision == "deny"
        assert audit_event.reason == "node_not_found"
    
    def test_expand_latency_measurement(self):
        """Test that expand API measures and reports latency"""
        response = self.client.get(
            f"/api/v1/traces/{self.trace_id}/compressed/{self.node_id}/expand",
            headers={"X-OryaObs-User": self.user}
        )
        
        assert response.status_code == 200
        data = response.json()
        assert "query_latency_ms" in data
        assert data["query_latency_ms"] >= 0  # Should be non-negative
    
    def test_expand_field_filtering(self):
        """Test field filtering when requested"""
        response = self.client.get(
            f"/api/v1/traces/{self.trace_id}/compressed/{self.node_id}/expand",
            headers={"X-OryaObs-User": self.user},
            params={"fields": "model,tool.name"}
        )
        
        assert response.status_code == 200
        data = response.json()
        
        # Check that only requested fields are present in children
        for child in data["children"]:
            if child["span_type"] == "llm.call":
                assert "model" in child["attributes"]
                assert "agent_id" not in child["attributes"]  # Should be filtered out
            elif child["span_type"] == "tool.call":
                assert "tool.name" in child["attributes"]
    
    def test_expand_empty_fields(self):
        """Test expand with no fields specified (should return all fields)"""
        response = self.client.get(
            f"/api/v1/traces/{self.trace_id}/compressed/{self.node_id}/expand",
            headers={"X-OryaObs-User": self.user}
        )
        
        assert response.status_code == 200
        data = response.json()
        
        # Check that all fields are present when no field filter
        for child in data["children"]:
            if child["span_type"] == "llm.call":
                assert "llm.input_text" in child["attributes"]
                assert "llm.output_text" in child["attributes"]
                assert "model" in child["attributes"]
            elif child["span_type"] == "tool.call":
                assert "tool.input" in child["attributes"]