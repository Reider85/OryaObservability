"""
Tests for PP10 trace viewer UI service.

Tests FastAPI endpoints, HTML generation, and API responses.
"""

import pytest
from fastapi.testclient import TestClient
from unittest.mock import Mock, patch, AsyncMock
import json

from agent_obs.trace_viewer.ui import app


# Override get_hot_store for testing
async def mock_get_hot_store():
    """Mock get_hot_store function for testing"""
    return MockHotStore()


# Patch the get_hot_store function
app.dependency_overrides[app.dependency_overrides.get("get_hot_store", lambda: None)] = mock_get_hot_store


class MockHotStore:
    """Mock HotStore for testing"""
    
    def __init__(self, host=None, port=None, database=None):
        self.spans_data = {}
        self.is_available_called = False
        self.host = host
        self.port = port
        self.database = database
    
    def is_available(self):
        return True
    
    def write_audit_event(self, event):
        pass
    
    def _get_client(self):
        return self
    
    def get_spans(self, trace_id: str):
        if trace_id in self.spans_data:
            return self.spans_data[trace_id]
        raise Exception("Trace not found")
    
    def get_eval_results(self, trace_id: str):
        return []
    
    def write_spans_batch(self, spans: list):
        pass
    
    def write_eval_result(self, result):
        pass
    
    def close(self):
        pass


# Import os for environment variable manipulation
import os

# Import the UI module
import agent_obs.trace_viewer.ui as ui_module
from agent_obs.trace_viewer.ui import app

# Mock the HotStore import to prevent ClickHouse connection
import sys
from unittest.mock import MagicMock
sys.modules['agent_obs.storage.hot'] = MagicMock()
sys.modules['agent_obs.storage.hot'].HotStore = MockHotStore

# Create a mock function that returns our mock hot store
def mock_get_hot_store_func():
    print("DEBUG: Mock get_hot_store called")
    return mock_hot_store_instance

# Replace the original function with our mock
ui_module.get_hot_store = mock_get_hot_store_func

# Create a global mock hot store instance that will be used by all tests
mock_hot_store_instance = MockHotStore()


class TestTraceViewer:
    """Test suite for trace viewer service"""
    
    # Class-level mock hot store instance
    mock_hot_store = None
    
    # Mock span data with events
    mock_spans = [
        {
            "trace_id": "01H2ABC123",
            "span_id": "01H2ABC123",
            "parent_span_id": "",
            "name": "agent.loop",
            "span_type": "agent.loop",
            "start_time": 1705326600.0,
            "end_time": 1705326602.0,
            "status": "ok",
            "attributes": {"agent_id": "test-agent"},
            "events": [
                {
                    "name": "reasoning_step",
                    "ts": "2024-01-15T10:30:00.000Z",
                    "attributes": {"step": 1, "input": "Hello"}
                },
                {
                    "name": "error_occurred",
                    "ts": "2024-01-15T10:30:01.000Z",
                    "attributes": {"status": "error", "error_code": "500"}
                }
            ]
        },
        {
            "trace_id": "01H2ABC123",
            "span_id": "01H2DEF456",
            "parent_span_id": "01H2ABC123",
            "name": "llm.call",
            "span_type": "llm.call",
            "start_time": 1705326602.0,
            "end_time": 1705326603.0,
            "status": "ok",
            "attributes": {"model": "gpt-4"},
            "events": []
        }
    ]
    
    def setup_method(self):
        """Setup test client and mock data"""
        self.client = TestClient(app)
        
        # Set test mode to use mock
        os.environ["TEST_MODE"] = "mock"
        
        # Set up the mock data directly
        from agent_obs.trace_viewer.ui import HotStoreFactory
        print(f"DEBUG: Creating MockHotStore directly")
        mock_store = MockHotStore()
        mock_store.spans_data = {
            "01H2ABC123": self.mock_spans
        }
        HotStoreFactory._mock_hot_store = mock_store
        print(f"DEBUG: HotStoreFactory._mock_hot_store: {HotStoreFactory._mock_hot_store}")
        print(f"DEBUG: HotStoreFactory._mock_hot_store.spans_data: {HotStoreFactory._mock_hot_store.spans_data}")
        
        # Set RBAC roles for testing
        os.environ["AGENT_OBS_RBAC_ROLES"] = "alice:admin,bob:developer,charlie:auditor"
    
    def teardown_method(self):
        """Clean up environment"""
        if "TEST_MODE" in os.environ:
            del os.environ["TEST_MODE"]
        if "AGENT_OBS_RBAC_ROLES" in os.environ:
            del os.environ["AGENT_OBS_RBAC_ROLES"]
    
    @patch('agent_obs.trace_viewer.ui.get_hot_store')
    def test_index_page(self, mock_hot_store):
        """Test the main index page with trace ID form"""
        response = self.client.get("/")
        assert response.status_code == 200
        assert "Trace Viewer" in response.text
        assert "trace_id" in response.text
        assert "View Trace" in response.text
    
    def test_trace_view_found(self):
        """Test trace view when trace is found"""
        response = self.client.get("/trace/01H2ABC123")
        assert response.status_code == 200
        assert "agent.loop" in response.text
        assert "llm.call" in response.text
        assert "reasoning_step" in response.text
        assert "AggregatedSpanView" in response.text
    
    def test_trace_view_with_expand_events(self):
        """Test trace view with ?expand=events parameter"""
        response = self.client.get("/trace/01H2ABC123?expand=events")
        assert response.status_code == 200
        assert 'expandEvents: true' in response.text
    
    def test_trace_view_not_found(self):
        """Test trace view when trace is not found"""
        # Clear the mock data for not found test
        global mock_hot_store_instance
        mock_hot_store_instance.spans_data = {}
        
        response = self.client.get("/trace/01H2XYZ789")
        assert response.status_code == 404
        assert "Trace not found" in response.text
    
    def test_get_span_events_endpoint(self):
        """Test the events API endpoint"""
        response = self.client.get("/api/traces/01H2ABC123/spans/01H2ABC123/events")
        assert response.status_code == 200
        
        events = response.json()
        assert len(events) == 2
        assert events[0]["name"] == "reasoning_step"
        assert events[0]["error"] is False
        assert events[1]["name"] == "error_occurred"
        assert events[1]["error"] is True
    
    def test_get_span_events_span_not_found(self):
        """Test events endpoint when span is not found"""
        response = self.client.get("/api/traces/01H2ABC123/spans/01H2XYZ789/events")
        assert response.status_code == 404
        assert "Span not found" in response.text
    
    def test_get_span_events_trace_not_found(self):
        """Test events endpoint when trace is not found"""
        # Clear the mock data for not found test
        global mock_hot_store_instance
        mock_hot_store_instance.spans_data = {}
        
        response = self.client.get("/api/traces/01H2XYZ789/spans/01H2ABC123/events")
        assert response.status_code == 404
        assert "Trace not found" in response.text
    
    def test_health_check(self):
        """Test health check endpoint"""
        response = self.client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        assert data["service"] == "trace-viewer"
    
    def test_build_span_tree(self):
        """Test span tree building functionality"""
        from agent_obs.trace_viewer.ui import build_span_tree
        
        tree = build_span_tree(self.mock_spans)
        
        # Should have root span and one child
        assert len(tree) == 1
        assert tree[0]["name"] == "agent.loop"
        assert len(tree[0]["children"]) == 1
        assert tree[0]["children"][0]["name"] == "llm.call"
    
    def test_span_tree_no_children(self):
        """Test span tree with no parent-child relationships"""
        from agent_obs.trace_viewer.ui import build_span_tree
        
        # Single span with no parent
        single_span = [{"trace_id": "01", "span_id": "01", "parent_span_id": "", "name": "root", "span_type": "root", "start_time": 0, "end_time": 0, "status": "ok", "attributes": {}, "events": []}]
        
        tree = build_span_tree(single_span)
        assert len(tree) == 1
        assert tree[0]["name"] == "root"
        assert tree[0]["children"] == []
    
    def test_empty_events_handling(self):
        """Test handling of spans with no events"""
        # Add a span with no events
        span_no_events = {
            "trace_id": "01H2ABC123",
            "span_id": "01H2GHI789",
            "parent_span_id": "01H2ABC123",
            "name": "tool.call",
            "span_type": "tool.call",
            "start_time": 1705326603.0,
            "end_time": 1705326604.0,
            "status": "ok",
            "attributes": {},
            "events": []
        }
        
        mock_spans_with_empty = self.mock_spans + [span_no_events]
        from agent_obs.trace_viewer.ui import HotStoreFactory
        HotStoreFactory._mock_hot_store.spans_data["01H2ABC123"] = mock_spans_with_empty
        
        response = self.client.get("/trace/01H2ABC123")
        assert response.status_code == 200
        # Should show "No events" for spans without events
        assert "No events" in response.text
    
    def test_events_with_nested_structure(self):
        """Test handling of nested span structure with multiple levels"""
        # Create deeper nesting
        nested_spans = [
            {
                "trace_id": "01H2ABC123",
                "span_id": "01H2ABC123",
                "parent_span_id": "",
                "name": "root",
                "span_type": "root",
                "start_time": 0,
                "end_time": 1,
                "status": "ok",
                "attributes": {},
                "events": []
            },
            {
                "trace_id": "01H2ABC123",
                "span_id": "01H2DEF456",
                "parent_span_id": "01H2ABC123",
                "name": "child1",
                "span_type": "child",
                "start_time": 1,
                "end_time": 2,
                "status": "ok",
                "attributes": {},
                "events": []
            },
            {
                "trace_id": "01H2ABC123",
                "span_id": "01H2GHI789",
                "parent_span_id": "01H2DEF456",
                "name": "grandchild",
                "span_type": "child",
                "start_time": 2,
                "end_time": 3,
                "status": "ok",
                "attributes": {},
                "events": []
            }
        ]
        
        from agent_obs.trace_viewer.ui import HotStoreFactory
        HotStoreFactory._mock_hot_store.spans_data["01H2ABC123"] = nested_spans
        
        response = self.client.get("/trace/01H2ABC123")
        assert response.status_code == 200
        # Should show all three levels
        assert "root" in response.text
        assert "child1" in response.text
        assert "grandchild" in response.text
    
    def test_trace_view_with_compressed_node(self):
        """Test trace view with compressed node rendering"""
        # Create trace with compressed node
        compressed_trace = [
            {
                "trace_id": "compressed_trace",
                "span_id": "root_1",
                "parent_span_id": "",
                "name": "agent.loop",
                "span_type": "agent.loop",
                "start_time": 1705326600.0,
                "end_time": 1705326602.0,
                "status": "ok",
                "attributes": {"agent_id": "test-agent"},
                "events": []
            },
            {
                "trace_id": "compressed_trace",
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
        ]
        
        # Add the compressed trace to the mock hot store
        from agent_obs.trace_viewer.ui import HotStoreFactory
        HotStoreFactory._mock_hot_store.spans_data = {
            "compressed_trace": compressed_trace
        }
        
        response = self.client.get("/trace/compressed_trace")
        assert response.status_code == 200
        html_content = response.text
        
        # Check that compressed node is rendered with badge
        assert "compressed:5" in html_content
        assert "compressed-badge" in html_content
        assert "Expand" in html_content  # Expand button
        assert "Resolving..." in html_content  # Spinner
        
        # Check that user input field is present
        assert "User (for RBAC)" in html_content
        assert 'id="user-input"' in html_content
    
    def test_trace_view_with_compressed_node_and_summary(self):
        """Test trace view with compressed node and summary text"""
        # Create trace with compressed node and summary
        compressed_trace_with_summary = [
            {
                "trace_id": "summary_trace",
                "span_id": "root_1",
                "parent_span_id": "",
                "name": "agent.loop",
                "span_type": "agent.loop",
                "start_time": 1705326600.0,
                "end_time": 1705326602.0,
                "status": "ok",
                "attributes": {"agent_id": "test-agent"},
                "events": []
            },
            {
                "trace_id": "summary_trace",
                "span_id": "compressed_1",
                "parent_span_id": "root_1",
                "name": "compressed:3",
                "span_type": "agent.reasoning.compressed",
                "start_time": 1705326602.0,
                "end_time": 1705326603.0,
                "status": "ok",
                "attributes": {
                    "agent_id": "test-agent",
                    "tree.compressed": True,
                    "tree.children_count": 3,
                    "tree.summary": "This is a summary of the reasoning process",
                    "tree.policy_version": "1"
                },
                "events": []
            }
        ]
        
        # Add the summary trace to the mock hot store
        from agent_obs.trace_viewer.ui import HotStoreFactory
        HotStoreFactory._mock_hot_store.spans_data = {
            "summary_trace": compressed_trace_with_summary
        }
        
        response = self.client.get("/trace/summary_trace")
        assert response.status_code == 200
        html_content = response.text
        
        # Check that summary is displayed
        assert "compressed:3" in html_content
        assert "This is a summary of the reasoning process" in html_content
        assert "Summary:" in html_content
    
    def test_trace_view_with_large_compressed_tree(self):
        """Test trace view with large compressed tree (compression threshold test)"""
        # Create a large trace that should trigger compression
        large_trace = []
        
        # Root node
        large_trace.append({
            "trace_id": "large_trace",
            "span_id": "root_1",
            "parent_span_id": "",
            "name": "agent.loop",
            "span_type": "agent.loop",
            "start_time": 1705326600.0,
            "end_time": 1705326602.0,
            "status": "ok",
            "attributes": {"agent_id": "test-agent"},
            "events": []
        })
        
        # Many reasoning steps (should trigger compression)
        for i in range(25):
            large_trace.append({
                "trace_id": "large_trace",
                "span_id": f"reasoning_{i}",
                "parent_span_id": "root_1",
                "name": f"reasoning_step_{i}",
                "span_type": "agent.reasoning",
                "start_time": 1705326602.0 + i,
                "end_time": 1705326603.0 + i,
                "status": "ok",
                "attributes": {"step": i, "input": f"Step {i} input"},
                "events": []
            })
        
        from agent_obs.trace_viewer.ui import HotStoreFactory
        HotStoreFactory._mock_hot_store.spans_data = {
            "large_trace": large_trace
        }
        
        # Lower compression threshold for testing
        with patch.dict(os.environ, {'TREE_COMPRESSION_THRESHOLD': '5'}):
            response = self.client.get("/trace/large_trace")
            assert response.status_code == 200
            html_content = response.text
            
            # Should show compressed node
            assert "compressed:25" in html_content
            assert "compressed-badge" in html_content