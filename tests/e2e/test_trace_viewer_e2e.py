"""
PP10 e2e tests for trace viewer using Playwright.

Tests real browser interactions: expandable events, search, keyboard navigation.
"""

import pytest
from playwright.sync_api import Page, expect
from fastapi.testclient import TestClient
from fastapi import FastAPI
from unittest.mock import Mock, patch
import uvicorn
import threading
import time
import json

# Import the trace viewer app
from agent_obs.trace_viewer.ui import app as trace_viewer_app


@pytest.fixture(scope="module")
def trace_viewer_server():
    """Start trace viewer server in background for e2e tests"""
    # Override the hot_store dependency with mock
    mock_store = Mock()
    
    # Mock data
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
    
    mock_store.get_spans.return_value = mock_spans
    
    # Override the dependency
    trace_viewer_app.dependency_overrides[trace_viewer_app.dependency_overrides.get("get_hot_store")] = lambda: mock_store
    
    # Start server in background
    server_config = uvicorn.Config(
        app=trace_viewer_app,
        host="127.0.0.1",
        port=18089,
        log_level="error"
    )
    server = uvicorn.Server(server_config)
    
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    
    # Wait for server to start
    time.sleep(2)
    
    yield "http://127.0.0.1:18089"
    
    # Server will be stopped when thread is garbage collected


@pytest.fixture
def page(trace_viewer_server):
    """Playwright page fixture with server URL"""
    from playwright.sync_api import sync_playwright
    
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(trace_viewer_server)
        yield page
        browser.close()


class TestTraceViewerE2E:
    """End-to-end tests for trace viewer"""
    
    def test_index_page_loads(self, page: Page):
        """Test that index page loads correctly"""
        expect(page).to_have_title("Agent Observability Trace Viewer")
        expect(page.locator("h1")).to_have_text("Agent Observability Trace Viewer")
        expect(page.locator("input#trace_id")).to_be_visible()
        expect(page.locator("button")).to_have_text("View Trace")
    
    def test_trace_view_loads(self, page: Page):
        """Test loading a trace view"""
        # Go to trace view directly
        page.goto(f"{page.url}trace/01H2ABC123")
        
        # Wait for content to load
        expect(page.locator(".trace-id")).to_have_text("01H2ABC123")
        expect(page.locator(".span-name")).to_have_text("agent.loop")
        expect(page.locator(".span-name")).to_have_text("llm.call")
    
    def test_expand_events_button(self, page: Page):
        """Test expanding events via button click"""
        page.goto(f"{page.url}trace/01H2ABC123")
        
        # Find the span with events
        span_header = page.locator(".span-header").first()
        events_button = span_header.locator(".events-toggle")
        
        # Initially button should say "Show"
        expect(events_button).to_have_text("Show 2 events")
        
        # Click to expand
        events_button.click()
        
        # Events section should be visible
        events_section = page.locator(".events-section")
        expect(events_section).to_be_visible()
        
        # Button should now say "Hide"
        expect(events_button).to_have_text("Hide 2 events")
        
        # Events should be loading
        expect(events_section.locator(".loading")).to_be_visible()
    
    def test_expand_events_url_param(self, page: Page):
        """Test ?expand=events URL parameter"""
        page.goto(f"{page.url}trace/01H2ABC123?expand=events")
        
        # Events should be auto-expanded
        events_section = page.locator(".events-section")
        expect(events_section).to_be_visible()
        
        # Events should be loading
        expect(events_section.locator(".loading")).to_be_visible()
    
    def test_events_loading_and_display(self, page: Page):
        """Test that events load and display correctly"""
        page.goto(f"{page.url}trace/01H2ABC123")
        
        # Expand events
        span_header = page.locator(".span-header").first()
        events_button = span_header.locator(".events-toggle")
        events_button.click()
        
        # Wait for events to load (simulate API call)
        # In real scenario, this would be handled by the mock API
        page.wait_for_selector(".events-table tbody tr")
        
        # Check that events are displayed
        event_rows = page.locator(".events-table tbody tr")
        expect(event_rows).to_have_count(2)
        
        # Check first event
        first_row = event_rows.first()
        expect(first_row.locator(".event-name")).to_have_text("reasoning_step")
        expect(first_row.locator(".event-attrs")).to_contain_text("step")
        
        # Check error event styling
        error_row = page.locator(".event-row--error")
        expect(error_row).to_be_visible()
        expect(error_row.locator(".event-name")).to_have_text("error_occurred")
    
    def test_keyboard_navigation(self, page: Page):
        """Test keyboard navigation between spans"""
        page.goto(f"{page.url}trace/01H2ABC123")
        
        # Focus first span
        first_span = page.locator(".span-row").first()
        first_span.click()
        
        # Press arrow down to move to next span
        page.keyboard.press("ArrowDown")
        
        # Second span should be focused
        second_span = page.locator(".span-row").nth(1)
        expect(second_span).to_have_class("focused")
        
        # Press arrow up to go back
        page.keyboard.press("ArrowUp")
        
        # First span should be focused again
        expect(first_span).to_have_class("focused")
    
    def test_keyboard_toggle_expand(self, page: Page):
        """Test keyboard shortcuts for expand/collapse"""
        page.goto(f"{page.url}trace/01H2ABC123")
        
        # Focus first span
        first_span = page.locator(".span-row").first()
        first_span.click()
        
        # Press Enter to expand
        page.keyboard.press("Enter")
        
        # Events section should be visible
        events_section = page.locator(".events-section")
        expect(events_section).to_be_visible()
        
        # Press Escape to collapse
        page.keyboard.press("Escape")
        
        # Events section should be hidden
        expect(events_section).to_be_hidden()
    
    def test_search_events_input(self, page: Page):
        """Test search events input functionality"""
        page.goto(f"{page.url}trace/01H2ABC123")
        
        # Find search input
        search_input = page.locator("#events-search")
        expect(search_input).to_be_visible()
        
        # Type in search
        search_input.fill("error")
        
        # In real implementation, this would filter events
        # For now, just verify input has the value
        expect(search_input).to_have_value("error")
    
    def test_no_events_span(self, page: Page):
        """Test span with no events shows appropriate message"""
        page.goto(f"{page.url}trace/01H2ABC123")
        
        # Find the span without events
        spans = page.locator(".span-row")
        llm_span = spans.nth(1)  # Second span
        
        # Should not have events toggle button
        expect(llm_span.locator(".events-toggle")).to_be_hidden()
        
        # Should show "No events" message
        expect(llm_span.locator(".no-events")).to_have_text("No events")
    
    def test_health_check(self, page: Page):
        """Test health check endpoint"""
        response = page.request.get(f"{page.url}health")
        assert response.status == 200
        data = response.json()
        assert data["status"] == "healthy"
        assert data["service"] == "trace-viewer"
    
    def test_trace_not_found(self, page: Page):
        """Test handling of non-existent trace"""
        # Mock store to raise exception
        with patch('agent_obs.trace_viewer.ui.hot_store') as mock_store:
            mock_store.get_spans.side_effect = Exception("Trace not found")
            
            page.goto(f"{page.url}trace/01H2XYZ789")
            
            # Should show error message
            expect(page.locator("text=Trace not found")).to_be_visible()
    
    def test_nested_span_structure(self, page: Page):
        """Test rendering of nested span structure"""
        # Mock deeper nesting
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
                "name": "child",
                "span_type": "child",
                "start_time": 1,
                "end_time": 2,
                "status": "ok",
                "attributes": {},
                "events": []
            }
        ]
        
        with patch('agent_obs.trace_viewer.ui.hot_store') as mock_store:
            mock_store.get_spans.return_value = nested_spans
            
            page.goto(f"{page.url}trace/01H2ABC123")
            
            # Should show both spans with proper indentation
            expect(page.locator(".span-name").first()).to_have_text("root")
            expect(page.locator(".span-name").nth(1)).to_have_text("child")
            
            # Child should have indentation (visual representation)
            child_span = page.locator(".span-row").nth(1)
            expect(child_span).to_have_css("margin-left", "30px")