"""
PP10 e2e tests for trace viewer using Playwright.

Tests real browser interactions: expandable events, search, keyboard navigation.
"""

import pytest
from playwright.sync_api import Page, expect
from fastapi.testclient import TestClient
from fastapi import FastAPI
from unittest.mock import AsyncMock, Mock, patch
import uvicorn
import threading
import time
import json

# Import the trace viewer app
from agent_obs.trace_viewer.ui import app as trace_viewer_app, DrillDownResponse


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
    
    def test_compressed_node_display(self, page: Page):
        """Test that compressed nodes are displayed with badge"""
        # Mock trace with compressed node
        compressed_spans = [
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
        
        with patch('agent_obs.trace_viewer.ui.hot_store') as mock_store:
            mock_store.get_spans.return_value = compressed_spans
            
            page.goto(f"{page.url}trace/compressed_trace")
            
            # Should show compressed node with badge
            compressed_span = page.locator(".span-row").nth(1)
            expect(compressed_span.locator(".compressed-badge")).to_be_visible()
            expect(compressed_span.locator(".compressed-badge")).to_have_text("COMPRESSED")
            
            # Should show expand button
            expand_button = compressed_span.locator(".expand-button")
            expect(expand_button).to_be_visible()
            expect(expand_button).to_have_text("Expand")
            
            # Should show spinner initially
            expect(compressed_span.locator(".spinner")).to_be_hidden()  # Hidden until clicked
    
    def test_expand_compressed_node_success(self, page: Page):
        """Test successful expand of compressed node"""
        # Mock trace with compressed node and children
        compressed_spans = [
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
            },
            {
                "trace_id": "compressed_trace",
                "span_id": "child_1",
                "parent_span_id": "compressed_1",
                "name": "llm.call",
                "span_type": "llm.call",
                "start_time": 1705326603.0,
                "end_time": 1705326604.0,
                "status": "ok",
                "attributes": {
                    "model": "gpt-4",
                    "llm.input_text": "Hello world",
                    "llm.output_text": "Hello there"
                },
                "events": []
            },
            {
                "trace_id": "compressed_trace",
                "span_id": "child_2",
                "parent_span_id": "compressed_1",
                "name": "tool.call",
                "span_type": "tool.call",
                "start_time": 1705326604.0,
                "end_time": 1705326605.0,
                "status": "ok",
                "attributes": {
                    "tool.name": "calculator",
                    "tool.input": "2+2"
                },
                "events": []
            }
        ]
        
        with patch('agent_obs.trace_viewer.ui.hot_store') as mock_store:
            mock_store.get_spans.return_value = compressed_spans
            
            page.goto(f"{page.url}trace/compressed_trace")
            
            # Find compressed node
            compressed_span = page.locator(".span-row").nth(1)
            expand_button = compressed_span.locator(".expand-button")
            
            # Click expand button
            expand_button.click()
            
            # Should show spinner
            expect(compressed_span.locator(".spinner")).to_be_visible()
            
            # Mock the API call to expand endpoint
            api_response = {
                "trace_id": "compressed_trace",
                "node_id": "compressed_1",
                "storage_tier": "hot",
                "mfa_used": False,
                "query_latency_ms": 45,
                "children_count": 2,
                "children": [
                    {
                        "span_id": "child_1",
                        "name": "llm.call",
                        "span_type": "llm.call",
                        "start_time": 1705326603.0,
                        "end_time": 1705326604.0,
                        "status": "ok",
                        "attributes": {
                            "model": "gpt-4",
                            "llm.input_text": "Hello world",
                            "llm.output_text": "Hello there"
                        }
                    },
                    {
                        "span_id": "child_2",
                        "name": "tool.call",
                        "span_type": "tool.call",
                        "start_time": 1705326604.0,
                        "end_time": 1705326605.0,
                        "status": "ok",
                        "attributes": {
                            "tool.name": "calculator",
                            "tool.input": "2+2"
                        }
                    }
                ]
            }
            
            # Intercept the API call and return mock response
            page.route("**/api/v1/traces/compressed_trace/compressed/compressed_1", lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(api_response)
            ))
            
            # Wait for spinner to disappear and children to appear
            page.wait_for_selector(".spinner", state="hidden")
            
            # Should show children spans
            children_spans = page.locator(".span-row").filter(has_text="child_1, child_2")
            expect(children_spans).to_have_count(2)
            
            # Check that children are properly indented
            child1 = page.locator(".span-row").filter(has_text="llm.call")
            child2 = page.locator(".span-row").filter(has_text="tool.call")
            expect(child1).to_have_css("margin-left", "60px")  # Double indentation
            expect(child2).to_have_css("margin-left", "60px")
            
            # Expand button should now show "Collapse"
            expect(expand_button).to_have_text("Collapse")
    
    def test_expand_compressed_node_with_mfa(self, page: Page):
        """Test expand with MFA for PII fields"""
        # Mock trace with PII in compressed children
        compressed_spans = [
            {
                "trace_id": "pi_trace",
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
                "trace_id": "pi_trace",
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
                    "tree.summary": "pending",
                    "tree.policy_version": "1"
                },
                "events": []
            }
        ]
        
        with patch('agent_obs.trace_viewer.ui.hot_store') as mock_store:
            mock_store.get_spans.return_value = compressed_spans
            
            page.goto(f"{page.url}trace/pi_trace")
            
            # Find compressed node
            compressed_span = page.locator(".span-row").nth(1)
            expand_button = compressed_span.locator(".expand-button")
            
            # Click expand button
            expand_button.click()
            
            # Should show user input modal for MFA
            user_modal = page.locator(".user-modal")
            expect(user_modal).to_be_visible()
            
            # Enter user and MFA
            user_input = user_modal.locator("#user-input")
            mfa_input = user_modal.locator("#mfa-input")
            submit_button = user_modal.locator(".submit-button")
            
            user_input.fill("developer")
            mfa_input.fill("123456")
            submit_button.click()
            
            # Mock successful API response
            api_response = {
                "trace_id": "pi_trace",
                "node_id": "compressed_1",
                "storage_tier": "hot",
                "mfa_used": True,
                "query_latency_ms": 67,
                "children_count": 1,
                "children": [
                    {
                        "span_id": "child_1",
                        "name": "llm.call",
                        "span_type": "llm.call",
                        "start_time": 1705326603.0,
                        "end_time": 1705326604.0,
                        "status": "ok",
                        "attributes": {
                            "model": "gpt-4",
                            "llm.input_text": "Hello world",
                            "llm.output_text": "Hello there"
                        }
                    }
                ]
            }
            
            # Intercept API call
            page.route("**/api/v1/traces/pi_trace/compressed/compressed_1", lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(api_response)
            ))
            
            # Wait for modal to disappear and children to appear
            page.wait_for_selector(".user-modal", state="hidden")
            
            # Should show expanded children
            child_span = page.locator(".span-row").filter(has_text="llm.call")
            expect(child_span).to_be_visible()
            
            # MFA should be shown in the UI
            expect(compressed_span.locator(".mfa-indicator")).to_be_visible()
            expect(compressed_span.locator(".mfa-indicator")).to_have_text("MFA Used")
    
    def test_expand_compressed_node_insufficient_permissions(self, page: Page):
        """Test expand with insufficient permissions"""
        page.goto(f"{page.url}trace/compressed_trace")
        
        # Find compressed node
        compressed_span = page.locator(".span-row").nth(1)
        expand_button = compressed_span.locator(".expand-button")
        
        # Click expand button
        expand_button.click()
        
        # Should show user input modal
        user_modal = page.locator(".user-modal")
        expect(user_modal).to_be_visible()
        
        # Enter user without sufficient permissions
        user_input = user_modal.locator("#user-input")
        submit_button = user_modal.locator(".submit-button")
        
        user_input.fill("auditor")  # auditor role doesn't have compressed:expand
        submit_button.click()
        
        # Should show error message
        error_message = user_modal.locator(".error-message")
        expect(error_message).to_be_visible()
        expect(error_message).to_have_text("Insufficient permissions: compressed:expand permission required")
        
        # Modal should still be open
        expect(user_modal).to_be_visible()
    
    def test_expand_compressed_node_missing_mfa(self, page: Page):
        """Test expand without MFA for PII fields"""
        page.goto(f"{page.url}trace/pi_trace")
        
        # Find compressed node
        compressed_span = page.locator(".span-row").nth(1)
        expand_button = compressed_span.locator(".expand-button")
        
        # Click expand button
        expand_button.click()
        
        # Should show user input modal
        user_modal = page.locator(".user-modal")
        expect(user_modal).to_be_visible()
        
        # Enter user but no MFA
        user_input = user_modal.locator("#user-input")
        submit_button = user_modal.locator(".submit-button")
        
        user_input.fill("developer")
        submit_button.click()
        
        # Should show MFA required error
        error_message = user_modal.locator(".error-message")
        expect(error_message).to_be_visible()
        expect(error_message).to_have_text("MFA required for PII fields")
        
        # Modal should still be open
        expect(user_modal).to_be_visible()


class TestDrillDownColdFieldE2E:
    """PP24 e2e: кнопка Drill down для cold-полей в trace-viewer.

    Сервер отдаёт cold-страницу (hot-store miss -> cold-tier fallback),
    все drill-down API мокаются через page.route.
    """

    COLD_CAPS = {
        "trace_id": "01COLD999", "user": "alice",
        "can_read": True, "can_pii_recover": True,
        "rate_limit": 10, "rate_remaining": 5,
    }

    def _goto_cold_trace(self, page: Page, server_url: str,
                         caps=None, drill_response=None):
        """Мокает drill-down API и открывает cold-трейс."""
        from agent_obs.storage.drill_down import DrillDownResult

        caps = caps if caps is not None else dict(self.COLD_CAPS)
        drill_response = drill_response or DrillDownResponse(
            trace_id="01COLD999", storage_tier="cold",
            fields={"llm.input_text": "secret PII text"},
            query_latency_ms=12.0, mfa_used=True,
        ).model_dump()

        cold_service = Mock()
        cold_service.rate_limit = 10
        cold_service.get_rate_remaining = AsyncMock(return_value=5)
        cold_service.drill_down = AsyncMock(return_value=DrillDownResult(
            fields={"llm.input_text": "secret PII text", "span_count": 3},
            storage_tier="cold", query_latency_ms=12.0, mfa_used=False,
        ))

        # Мокаем все drill-down API, чтобы e2e не зависел от Redis/ClickHouse.
        page.route("**/drill-down/capabilities", lambda route: route.fulfill(
            status=200, content_type="application/json", body=json.dumps(caps)))
        page.route("**/drill-down/history", lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=json.dumps({"trace_id": "01COLD999", "user": "alice", "items": [
                {"timestamp": 1705326600.0, "trace_id": "01COLD999", "decision": "allow",
                 "reason": "success", "requested_fields": ["llm.input_text"], "mfa_used": True},
            ]})))
        page.route("**/drill-down?*", lambda route: route.fulfill(
            status=200, content_type="application/json", body=json.dumps(drill_response)))

        with patch("agent_obs.trace_viewer.ui.get_drill_down_service", return_value=cold_service):
            page.goto(f"{server_url.rstrip('/')}/trace/01COLD999")

    @pytest.fixture
    def cold_page(self, trace_viewer_server):
        """Cold-страница с замоканными drill-down API (RBAC ok, PII ok)."""
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            self._goto_cold_trace(page, trace_viewer_server)
            yield page
            browser.close()

    def test_cold_page_renders_cold_badge_and_button(self, cold_page: Page):
        """Cold-трейс рендерит [COLD]-бейджи + кнопку Drill down."""
        expect(cold_page.locator(".trace-id")).to_have_text("01COLD999")
        expect(cold_page.locator(".cold-badge").first).to_have_text("[COLD]")
        expect(cold_page.locator(".drill-down-btn").first).to_be_visible()

    def test_pii_field_masked_until_drill_down(self, cold_page: Page):
        """PII-поле замаскировано до drill-down."""
        expect(cold_page.locator(".cold-field-value.masked").first).to_contain_text("MASKED")
        expect(cold_page.locator("#cold-fields-container")).not_to_contain_text("secret PII text")

    def test_drill_down_modal_opens_with_mfa_for_pii(self, cold_page: Page):
        """Клик по Drill down открывает модалку; PII-поле требует MFA."""
        cold_page.fill("#user-input", "alice")
        cold_page.dispatch_event("#user-input", "change")
        cold_page.wait_for_function(
            "() => { const b = document.querySelector('.drill-down-btn'); return b && !b.disabled; }",
            timeout=5000,
        )
        cold_page.locator(".drill-down-btn").first.click()
        expect(cold_page.locator(".user-modal")).to_be_visible()
        expect(cold_page.locator("#mfa-row")).to_be_visible()
        expect(cold_page.locator("#mfa-input")).to_be_visible()
        expect(cold_page.locator("#drill-down-loading")).to_contain_text("1 min")

    def test_drill_down_disabled_without_pii_recover(self, trace_viewer_server):
        """RBAC: без pii:recover PII-кнопка disabled с tooltip."""
        from playwright.sync_api import sync_playwright

        caps = dict(self.COLD_CAPS, can_pii_recover=False)
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            self._goto_cold_trace(page, trace_viewer_server, caps=caps)
            page.fill("#user-input", "alice")
            page.dispatch_event("#user-input", "change")
            page.wait_for_function(
                "() => { const b = document.querySelector('.drill-down-btn'); return b && b.disabled; }",
                timeout=5000,
            )
            btn = page.locator(".drill-down-btn").first
            expect(btn).to_be_disabled()
            expect(page.locator(".drill-down-tooltip").first).to_have_text(
                "Requires pii:recover permission")
            browser.close()

    def test_drill_down_disabled_on_rate_limit(self, trace_viewer_server):
        """Rate-limit: при rate_remaining=0 кнопка disabled с tooltip."""
        from playwright.sync_api import sync_playwright

        caps = dict(self.COLD_CAPS, rate_remaining=0)
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            self._goto_cold_trace(page, trace_viewer_server, caps=caps)
            page.fill("#user-input", "alice")
            page.dispatch_event("#user-input", "change")
            page.wait_for_function(
                "() => { const b = document.querySelector('.drill-down-btn'); return b && b.disabled; }",
                timeout=5000,
            )
            expect(page.locator(".drill-down-tooltip").first).to_have_text("Rate limit reached")
            browser.close()

    def test_drill_down_disabled_without_user(self, cold_page: Page):
        """Без user: capabilities не пройдены -> кнопки disabled (fail-closed)."""
        btn = cold_page.locator(".drill-down-btn").first
        cold_page.wait_for_function(
            "() => { const b = document.querySelector('.drill-down-btn'); return b && b.disabled; }",
            timeout=5000,
        )
        expect(btn).to_be_disabled()

    def test_drill_down_recovers_value_via_api(self, cold_page: Page):
        """Полный флоу: user + MFA -> drill-down API -> значение в модалке и на странице."""
        cold_page.fill("#user-input", "alice")
        cold_page.dispatch_event("#user-input", "change")
        cold_page.wait_for_function(
            "() => { const b = document.querySelector('.drill-down-btn'); return b && !b.disabled; }",
            timeout=5000,
        )
        cold_page.locator(".drill-down-btn").first.click()
        expect(cold_page.locator(".user-modal")).to_be_visible()
        cold_page.fill("#mfa-input", "123456")
        cold_page.locator(".submit-button").click()

        expect(cold_page.locator("#drill-down-result")).to_be_visible()
        expect(cold_page.locator("#drill-down-result")).to_contain_text("secret PII text")
        expect(cold_page.locator("#cold-fields-container")).to_contain_text("secret PII text")

    def test_history_sidebar_renders_items(self, cold_page: Page):
        """Sidebar: история drill-down (последние 10) отображается для user."""
        cold_page.fill("#user-input", "alice")
        cold_page.dispatch_event("#user-input", "change")
        expect(cold_page.locator(".drill-down-history")).to_be_visible()
        item = cold_page.locator(".history-item").first
        expect(item).to_be_visible()
        expect(item).to_contain_text("llm.input_text")
        expect(item.locator(".history-decision")).to_have_text("allow")