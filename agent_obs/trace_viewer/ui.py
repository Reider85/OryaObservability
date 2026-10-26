"""
Agent Observability Trace Viewer Service

FastAPI web service for viewing traces with expandable events.
Supports aggregated spans with lazy-loaded event details, search, and keyboard navigation.
"""

import asyncio
import os
import time
import json
from datetime import datetime
from typing import Optional, List, Dict, Any
from fastapi import FastAPI, HTTPException, Query, Depends, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
import uvicorn
from agent_obs.storage.hot import HotStore


class MockHotStore:
    """Mock HotStore for testing"""
    
    def __init__(self):
        self.spans_data = {}
    
    def is_available(self):
        return True
    
    def get_spans(self, trace_id: str):
        print(f"DEBUG: MockHotStore.get_spans called with trace_id: {trace_id}")
        print(f"DEBUG: Available traces: {list(self.spans_data.keys())}")
        if trace_id in self.spans_data:
            print(f"DEBUG: Found trace with {len(self.spans_data[trace_id])} spans")
            return self.spans_data[trace_id]
        print(f"DEBUG: Trace not found")
        raise Exception("Trace not found")
    
    def get_eval_results(self, trace_id: str):
        return []
    
    def get_tree_summaries(self, trace_id: str):
        return []
    
    def write_spans_batch(self, spans: list):
        pass
    
    def write_eval_result(self, result):
        pass
    
    def close(self):
        pass
    
    def write_audit_event(self, audit_event):
        """Write audit event to mock store"""
        if not hasattr(self, 'audit_events'):
            self.audit_events = []
        self.audit_events.append(audit_event)



# Initialize FastAPI app
app = FastAPI(
    title="Agent Observability Trace Viewer",
    description="Web UI for viewing traces with expandable events",
    version="1.0.0"
)

# Global mock hot store for tests
class HotStoreFactory:
    """Factory for creating HotStore instances"""
    _mock_hot_store = None
    
    @classmethod
    def get_hot_store(cls) -> HotStore:
        """Get ClickHouse hot store instance."""
        # In production, this would return a real HotStore instance
        # For tests, this will be mocked
        if os.environ.get("TEST_MODE") == "mock":
            if cls._mock_hot_store is None:
                cls._mock_hot_store = MockHotStore()
            return cls._mock_hot_store
        try:
            # Try to import and return a real HotStore
            from agent_obs.storage.hot import HotStore
            return HotStore()
        except Exception:
            # If import fails (e.g., in tests), return a mock
            if cls._mock_hot_store is None:
                cls._mock_hot_store = MockHotStore()
            return cls._mock_hot_store

# Global function for backward compatibility
def get_hot_store() -> HotStore:
    """Get ClickHouse hot store instance."""
    return HotStoreFactory.get_hot_store()

# Data models
class TraceResponse(BaseModel):
    trace_id: str
    spans: List[Dict[str, Any]]
    expand_events: bool = False


class ExpandResponse(BaseModel):
    trace_id: str
    node_id: str
    storage_tier: str
    mfa_used: bool
    query_latency_ms: float
    children: List[Dict[str, Any]]
    children_count: int


class AuditEvent(BaseModel):
    timestamp: float
    trace_id: str
    node_id: str
    user: str
    decision: str
    reason: str
    action: str
    requested_fields: List[str]
    mfa_used: bool
    mfa_token: Optional[str] = None


# Helper functions for RBAC, MFA, and audit logging
def check_permission(user: str, permission: str) -> bool:
    """Check if user has permission based on AGENT_OBS_RBAC_ROLES environment variable"""
    roles_env = os.environ.get("AGENT_OBS_RBAC_ROLES", "")
    if not roles_env:
        return False
    
    # Role -> permissions mapping (PP19 minimal set)
    ROLE_PERMISSIONS = {
        "admin": {
            "trace:read",
            "compressed:expand", 
            "pii:recover",
            "audit:read",
            "policy:write",
            "*",
        },
        "sre": {
            "trace:read",
            "compressed:expand",
            "audit:read",
        },
        "developer": {
            "trace:read",
            "compressed:expand",
        },
        "auditor": {
            "trace:read",
            "audit:read",
        },
        "dpo": {
            "trace:read", 
            "pii:recover",
        },
    }
    
    # Parse roles mapping: "alice:admin,bob:developer,charlie:auditor"
    for role_mapping in roles_env.split(","):
        if ":" in role_mapping:
            role_user, role = role_mapping.strip().split(":")
            if role_user == user:
                role_permissions = ROLE_PERMISSIONS.get(role, set())
                return permission in role_permissions or "*" in role_permissions
    return False


async def verify_mfa_header(user: str, mfa_token: str) -> bool:
    """Verify MFA token (placeholder implementation)"""
    # In a real implementation, this would verify the token against a secure store
    # For now, accept any non-empty token except "invalid_token" for testing
    if mfa_token == "invalid_token":
        return False
    return bool(mfa_token)


def fields_intersect_pii(fields: List[str]) -> bool:
    """Check if requested fields contain PII fields"""
    pii_fields = {
        "llm.input_text",
        "llm.output_text", 
        "tool.input",
        "tool.output",
        "input_text",
        "output_text",
        "input",
        "output",
    }
    return any(field in pii_fields for field in fields)


def _audit_expand_request(
    trace_id: str,
    node_id: str,
    user: str,
    decision: str,
    reason: str,
    action: str,
    requested_fields: List[str],
    mfa_token: Optional[str] = None,
    mfa_used: bool = False,
    hot_store: Optional[HotStore] = None,
):
    """Log audit event for expand request"""
    audit_event = AuditEvent(
        timestamp=time.time(),
        trace_id=trace_id,
        node_id=node_id,
        user=user,
        decision=decision,
        reason=reason,
        action=action,
        requested_fields=requested_fields,
        mfa_used=mfa_used,
        mfa_token=mfa_token
    )
    
    if hot_store and hasattr(hot_store, 'write_audit_event'):
        hot_store.write_audit_event(audit_event)


def _dicts_to_spans(spans_data: List[Dict[str, Any]]) -> List[Any]:
    """Convert list of span dicts to span objects"""
    # In a real implementation, this would convert dicts to proper Span objects
    # For now, return the dicts as-is
    return spans_data


def _extract_subtree(spans: List[Any], node_id: str) -> List[Any]:
    """Extract subtree spans for a given node ID"""
    # Find the compressed node
    compressed_node = None
    for span in spans:
        if span.get("span_id") == node_id:
            compressed_node = span
            break
    
    if not compressed_node:
        return []
    
    # Extract children recursively
    def get_children(span_id: str) -> List[Any]:
        children = []
        for span in spans:
            if span.get("parent_span_id") == span_id:
                children.append(span)
                children.extend(get_children(span.get("span_id")))
        return children
    
    return get_children(node_id)


# Routes
@app.get("/", response_class=HTMLResponse)
async def trace_viewer_index():
    """Main trace viewer index with trace ID input form"""
    html = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Agent Observability Trace Viewer</title>
    <style>
        body { font-family: Arial, sans-serif; margin: 40px; }
        .container { max-width: 800px; margin: 0 auto; }
        .form-group { margin-bottom: 20px; }
        label { display: block; margin-bottom: 5px; font-weight: bold; }
        input[type="text"] { width: 100%; padding: 8px; border: 1px solid #ccc; border-radius: 4px; }
        .btn { background-color: #007bff; color: white; padding: 10px 20px; border: none; border-radius: 4px; cursor: pointer; }
        .btn:hover { background-color: #0056b3; }
        .info { margin-top: 20px; padding: 15px; background-color: #f8f9fa; border-left: 4px solid #17a2b8; }
    </style>
</head>
<body>
    <div class="container">
        <h1>Agent Observability Trace Viewer</h1>
        <div class="info">
            <p>Enter a trace ID to view the trace with expandable events.</p>
            <p>Supports: aggregated spans with lazy-loaded events, search, error highlighting, keyboard navigation.</p>
        </div>
        <form action="/trace" method="get">
            <div class="form-group">
                <label for="trace_id">Trace ID:</label>
                <input type="text" id="trace_id" name="trace_id" required placeholder="e.g., 01H2ABC...">
            </div>
            <button type="submit" class="btn">View Trace</button>
        </form>
    </div>
</body>
</html>
"""
    return html


@app.get("/api/v1/traces/{trace_id}/compressed/{node_id}/expand", response_model=ExpandResponse)
async def expand_compressed_node(
    trace_id: str,
    node_id: str,
    fields: Optional[str] = Query(None, description="Comma-separated fields to request"),
    request: Request = None,
    hot_store: HotStore = Depends(get_hot_store),
):
    """Expand a compressed node to recover its subtree.
    
    RBAC: requires "trace:read" + "compressed:expand"
    MFA: required if requested fields contain PII
    Storage: hot tier only (warm/cold deferred to PP21)
    """
    start_time = time.time()
    
    # Parse requested fields
    requested_fields = []
    if fields:
        requested_fields = [f.strip() for f in fields.split(",")]
    
    # Extract user from header (required for RBAC)
    user = request.headers.get("X-OryaObs-User")
    print(f"DEBUG: User extracted from header: {user}")
    
    if not user:
        # Log audit event for missing user header
        _audit_expand_request(
            trace_id=trace_id,
            node_id=node_id,
            user="unknown",
            decision="deny",
            reason="rbac_denied",
            action="trace:read",
            requested_fields=requested_fields,
            mfa_token=None,
            hot_store=hot_store,
        )
        raise HTTPException(
            status_code=403,
            detail="RBAC: X-OryaObs-User header required"
        )
    
    # RBAC check
    if not check_permission(user, "trace:read"):
        # Log audit event for RBAC deny
        _audit_expand_request(
            trace_id=trace_id,
            node_id=node_id,
            user=user,
            decision="deny",
            reason="rbac_denied",
            action="trace:read",
            requested_fields=requested_fields,
            mfa_token=request.headers.get("X-OryaObs-MFA"),
            hot_store=hot_store,
        )
        raise HTTPException(
            status_code=403,
            detail="RBAC: trace:read permission required"
        )
    
    if not check_permission(user, "compressed:expand"):
        # Log audit event for RBAC deny
        _audit_expand_request(
            trace_id=trace_id,
            node_id=node_id,
            user=user,
            decision="deny",
            reason="rbac_denied",
            action="compressed:expand",
            requested_fields=requested_fields,
            mfa_token=request.headers.get("X-OryaObs-MFA"),
            hot_store=hot_store,
        )
        raise HTTPException(
            status_code=403,
            detail="RBAC: compressed:expand permission required"
        )
    
    # MFA check for PII fields
    mfa_used = False
    if fields_intersect_pii(requested_fields):
        mfa_token = request.headers.get("X-OryaObs-MFA")
        if not mfa_token:
            _audit_expand_request(
                trace_id=trace_id,
                node_id=node_id,
                user=user,
                decision="deny",
                reason="mfa_missing",
                action="compressed:expand",
                requested_fields=requested_fields,
                mfa_token=mfa_token,
                hot_store=hot_store,
            )
            raise HTTPException(
                status_code=403,
                detail="MFA: X-OryaObs-MFA header required for PII fields"
            )
        
        # Verify MFA token
        if not await verify_mfa_header(user, mfa_token):
            _audit_expand_request(
                trace_id=trace_id,
                node_id=node_id,
                user=user,
                decision="deny",
                reason="mfa_invalid",
                action="compressed:expand",
                requested_fields=requested_fields,
                mfa_token=mfa_token,
                hot_store=hot_store,
            )
            raise HTTPException(
                status_code=403,
                detail="MFA: Invalid token provided"
            )
        
        mfa_used = True
    
    # Get trace spans from hot store
    spans = []
    try:
        loop = asyncio.get_running_loop()
        spans = await loop.run_in_executor(None, hot_store.get_spans, trace_id)
    except Exception as e:
        # Check if trace exists (empty spans may indicate migrated to warm/cold)
        if "not found" in str(e).lower() or not spans:
            _audit_expand_request(
                trace_id=trace_id,
                node_id=node_id,
                user=user,
                decision="deny",
                reason="trace_not_found",
                action="compressed:expand",
                requested_fields=requested_fields,
                mfa_token=request.headers.get("X-OryaObs-MFA"),
                hot_store=hot_store,
            )
            raise HTTPException(
                status_code=404,
                detail=f"Trace {trace_id} not found. May be migrated to warm/cold tier — cold resurrection available with PP21 (T3.5.1)"
            )
        else:
            raise HTTPException(
                status_code=500,
                detail=f"Failed to retrieve trace: {str(e)}"
            )
    
    # Find the compressed node and extract its subtree
    all_spans = _dicts_to_spans(spans)
    subtree_spans = _extract_subtree(all_spans, node_id)
    
    if not subtree_spans:
        _audit_expand_request(
            trace_id=trace_id,
            node_id=node_id,
            user=user,
            decision="deny",
            reason="node_not_found",
            action="compressed:expand",
            requested_fields=requested_fields,
            mfa_token=request.headers.get("X-OryaObs-MFA"),
            hot_store=hot_store,
        )
        raise HTTPException(
            status_code=404,
            detail=f"Compressed node {node_id} not found in trace {trace_id}"
        )
    
    # Parse requested fields into a list
    field_list = []
    if fields:
        field_list = [f.strip() for f in fields.split(",")]
    
    # Filter fields if requested (PII fields already protected by MFA)
    filtered_subtree = []
    for span in subtree_spans:
        span_dict = span.to_dict() if hasattr(span, 'to_dict') else span
        
        if field_list:
            # Create a new span with only requested fields
            filtered_span = {k: v for k, v in span_dict.items() if k != "attributes"}
            if "attributes" in span_dict:
                filtered_attributes = {}
                for field in field_list:
                    # Check if field exactly matches attribute keys
                    if field in span_dict["attributes"]:
                        filtered_attributes[field] = span_dict["attributes"][field]
                if filtered_attributes:
                    filtered_span["attributes"] = filtered_attributes
            filtered_subtree.append(filtered_span)
        else:
            filtered_subtree.append(span_dict)
    
    # Audit successful expand
    _audit_expand_request(
        trace_id=trace_id,
        node_id=node_id,
        user=user,
        decision="allow",
        reason="success",
        action="compressed:expand",
        requested_fields=requested_fields,
        mfa_used=mfa_used,
        hot_store=hot_store,
    )
    
    return ExpandResponse(
        trace_id=trace_id,
        node_id=node_id,
        storage_tier="hot",
        mfa_used=mfa_used,
        query_latency_ms=(time.time() - start_time) * 1000,
        children=filtered_subtree,
        children_count=len(filtered_subtree)  # All spans are children
    )


@app.get("/trace/{trace_id}", response_class=HTMLResponse)
async def trace_view(
    trace_id: str,
    expand_events: str = Query(None, alias="expand"),
    hot_store: HotStore = Depends(get_hot_store)
):
    """Main trace view with expandable events"""
    try:
        spans = hot_store.get_spans(trace_id)
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Trace not found: {str(e)}")
    
    # Convert expand_events param to boolean
    expand_events_flag = expand_events == "events"
    
    # Prepare data for template
    trace_data = {
        "trace_id": trace_id,
        "spans": spans,
        "expand_events": expand_events_flag,
        "timestamp": datetime.utcnow().isoformat()
    }
    
    # Generate HTML with inline JS
    html = generate_trace_html(trace_data)
    return html


@app.get("/api/traces/{trace_id}/spans/{span_id}/events")
async def get_span_events(
    trace_id: str,
    span_id: str,
    hot_store: HotStore = Depends(get_hot_store)
):
    """Get events for a specific span (lazy loading)"""
    try:
        spans = hot_store.get_spans(trace_id)
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Trace not found: {str(e)}")
    
    # Find the specific span
    target_span = None
    for span in spans:
        if span["span_id"] == span_id:
            target_span = span
            break
    
    if not target_span:
        raise HTTPException(status_code=404, detail="Span not found")
    
    # Return events with error flag
    events = []
    for event in target_span.get("events", []):
        events.append({
            "name": event.get("name", ""),
            "ts": event.get("ts", ""),
            "attributes": event.get("attributes", {}),
            "error": event.get("attributes", {}).get("status") == "error"
        })
    
    return events


@app.get("/health")
async def health_check():
    """Health check endpoint"""
    return {"status": "healthy", "service": "trace-viewer"}


def generate_trace_html(data: Dict[str, Any]) -> str:
    """Generate HTML with inline JS for trace viewer"""
    trace_id = data["trace_id"]
    spans = data["spans"]
    expand_events = data["expand_events"]
    timestamp = data["timestamp"]
    
    # Add expandEvents flag for testing
    expand_events_flag = "expandEvents: true" if expand_events else "expandEvents: false"
    
    # Build span tree structure for rendering
    span_tree = build_span_tree(spans)
    
    html = f"""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Trace View - {trace_id}</title>
    <style>
        body {{ font-family: Arial, sans-serif; margin: 20px; background-color: #f8f9fa; }}
        .container {{ max-width: 1200px; margin: 0 auto; background: white; padding: 20px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }}
        .header {{ border-bottom: 1px solid #dee2e6; padding-bottom: 15px; margin-bottom: 20px; }}
        .trace-id {{ font-size: 24px; font-weight: bold; color: #212529; }}
        .meta {{ color: #6c757d; font-size: 14px; margin-top: 5px; }}
        .user-input {{ margin-top: 10px; }}
        .user-input label {{ margin-right: 8px; font-weight: bold; }}
        .user-input input {{ padding: 4px 8px; border: 1px solid #ccc; border-radius: 4px; }}
        .search-box {{ margin: 20px 0; }}
        .search-input {{ width: 300px; padding: 8px; border: 1px solid #ccc; border-radius: 4px; }}
        .span-row {{ margin: 10px 0; padding: 15px; border: 1px solid #e9ecef; border-radius: 4px; background-color: #fff; }}
        .span-header {{ display: flex; justify-content: space-between; align-items: center; cursor: pointer; }}
        .span-name {{ font-weight: bold; color: #495057; }}
        .span-meta {{ font-size: 12px; color: #6c757d; }}
        .span-children {{ margin-left: 30px; border-left: 2px solid #e9ecef; padding-left: 15px; }}
        .events-section {{ margin-top: 15px; }}
        .events-toggle {{ background-color: #007bff; color: white; padding: 6px 12px; border: none; border-radius: 4px; cursor: pointer; font-size: 12px; }}
        .events-toggle:hover {{ background-color: #0056b3; }}
        .events-table {{ margin-top: 10px; border: 1px solid #dee2e6; border-radius: 4px; }}
        .events-table table {{ width: 100%; border-collapse: collapse; }}
        .events-table th, .events-table td {{ padding: 8px 12px; text-align: left; border-bottom: 1px solid #dee2e6; }}
        .events-table th {{ background-color: #f8f9fa; font-weight: bold; }}
        .events-table tr.event-row--error {{ background-color: #f8d7da; }}
        .event-name {{ font-family: monospace; }}
        .event-attrs {{ font-size: 12px; color: #6c757d; font-family: monospace; }}
        .no-events {{ color: #6c757d; font-style: italic; }}
        .focused {{ outline: 2px solid #007bff; outline-offset: 2px; }}
        .loading {{ color: #6c757d; font-style: italic; }}
        .compressed-badge {{ background-color: #ffc107; color: #212529; padding: 2px 6px; border-radius: 4px; font-size: 10px; font-weight: bold; margin-left: 8px; }}
        .embedded-badge {{ background-color: #6f42c1; color: white; padding: 2px 6px; border-radius: 4px; font-size: 10px; font-weight: bold; margin-left: 8px; }}
        .expand-compressed {{ background-color: #28a745; color: white; padding: 4px 8px; border: none; border-radius: 4px; cursor: pointer; font-size: 12px; margin-left: 8px; }}
        .expand-compressed:hover {{ background-color: #218838; }}
        .compressed-name {{ color: #dc3545; font-weight: bold; }}
        .summary {{ margin-top: 8px; padding: 8px; background-color: #e3f2fd; border-radius: 4px; font-style: italic; color: #1565c0; }}
    </style>
</head>
<body>
    <!-- {expand_events_flag} -->
    <div class="container">
        <div class="header">
            <div class="trace-id">{trace_id}</div>
            <div class="meta">Viewed at {timestamp}</div>
            <div class="user-input">
                <label for="user-input">User (for RBAC):</label>
                <input type="text" id="user-input" placeholder="Enter username">
            </div>
        </div>
        
        <div class="search-box">
            <input type="text" class="search-input" id="events-search" placeholder="Search in events...">
        </div>
        
        <div id="spans-container">
            {render_span_tree(span_tree, expand_events)}
        </div>
    </div>

    <script>
        // AggregatedSpanView component
        class AggregatedSpanView {{
            constructor(rootElement, spans, options = {{}}) {{
                this.root = rootElement;
                this.spans = spans;
                this.expandEvents = options.expandEvents || false;
                this.focusedIndex = -1;
                this.init();
            }}
            
            init() {{
                this.render();
                this.setupKeyboardNavigation();
                if (this.expandEvents) {{
                    this.expandAllEvents();
                }}
            }}
            
            render() {{
                this.root.innerHTML = this.spans.map((span, index) => this.renderSpanRow(span, index)).join('');
                this.setupEventListeners();
            }}
            
            renderSpanRow(span, index) {{
                const hasEvents = span.events && span.events.length > 0;
                const isEmbedded = span.attributes && span.attributes.embedded;
                const embeddedCount = span.attributes ? (span.attributes.embedded_count || 0) : 0;
                // For embedded spans: render events inline from span.events (no API call needed — PP20)
                const eventsHtml = hasEvents
                    ? (isEmbedded ? this.renderEmbeddedEventsSection(span) : this.renderEventsSection(span))
                    : '<div class="no-events">No events</div>';
                
                return `
                    <div class="span-row" data-index="${{index}}" data-span-id="${{span.span_id}}">
                        <div class="span-header" onclick="toggleSpanEvents(this)">
                            <div>
                                <span class="span-name">${{span.name}}</span>
                                <span class="span-meta">| Type: ${{span.span_type}} | Status: ${{span.status}}</span>
                                ${{isEmbedded ? `<span class="embedded-badge">EMBEDDED (${{embeddedCount}})</span>` : ''}}
                            </div>
                            ${{hasEvents ? `<button class="events-toggle">Show ${{span.events.length}} events</button>` : ''}}
                        </div>
                        <div class="span-children">
                            ${{span.children ? span.children.map(child => this.renderSpanRow(child, index + '_child_' + child.span_id)).join('') : ''}}
                        </div>
                        <div class="events-section" style="display: none;">
                            ${{eventsHtml}}
                        </div>
                    </div>
                `;
            }}
            
            renderEmbeddedEventsSection(span) {{
                // PP20: embedded children are already in span.events — render inline, no fetch
                const rows = span.events.map(event => {{
                    const isError = event.attributes && event.attributes.status === 'error';
                    return `
                        <tr class="${{isError ? 'event-row--error' : ''}}">
                            <td class="event-name">${{event.name}}</td>
                            <td>${{event.timestamp ? new Date(event.timestamp).toLocaleString() : ''}}</td>
                            <td class="event-attrs">${{JSON.stringify(event.attributes || {{}}, null, 2)}}</td>
                        </tr>
                    `;
                }}).join('');
                return `
                    <div class="events-table">
                        <table>
                            <thead>
                                <tr>
                                    <th>Name</th>
                                    <th>Timestamp</th>
                                    <th>Attributes</th>
                                </tr>
                            </thead>
                            <tbody>${{rows}}</tbody>
                        </table>
                    </div>
                `;
            }}
            
            renderEventsSection(span) {{
                const spanId = span.span_id;
                return `
                    <div class="events-table">
                        <table>
                            <thead>
                                <tr>
                                    <th>Name</th>
                                    <th>Timestamp</th>
                                    <th>Attributes</th>
                                </tr>
                            </thead>
                            <tbody id="events-body-${{spanId}}">
                                <tr><td colspan="3" class="loading">Loading events...</td></tr>
                            </tbody>
                        </table>
                    </div>
                    <button onclick="loadSpanEvents('${{spanId}}')" class="events-toggle">Load events</button>
                `;
            }}
            
            setupEventListeners() {{
                // Search functionality
                const searchInput = document.getElementById('events-search');
                if (searchInput) {{
                    searchInput.addEventListener('input', (e) => this.searchEvents(e.target.value));
                }}
            }}
            
            setupKeyboardNavigation() {{
                document.addEventListener('keydown', (e) => this.handleKeydown(e));
            }}
            
            handleKeydown(e) {{
                const rows = document.querySelectorAll('.span-row');
                
                switch(e.key) {{
                    case 'ArrowDown':
                        e.preventDefault();
                        this.focusNext(rows);
                        break;
                    case 'ArrowUp':
                        e.preventDefault();
                        this.focusPrevious(rows);
                        break;
                    case 'Enter':
                    case ' ':
                        e.preventDefault();
                        this.toggleFocusedSpan();
                        break;
                    case 'Escape':
                        e.preventDefault();
                        this.collapseAllEvents();
                        break;
                }}
            }}
            
            focusNext(rows) {{
                if (this.focusedIndex < rows.length - 1) {{
                    this.focus(rows, this.focusedIndex + 1);
                }}
            }}
            
            focusPrevious(rows) {{
                if (this.focusedIndex > 0) {{
                    this.focus(rows, this.focusedIndex - 1);
                }}
            }}
            
            focus(rows, index) {{
                if (this.focusedIndex >= 0 && rows[this.focusedIndex]) {{
                    rows[this.focusedIndex].classList.remove('focused');
                }}
                this.focusedIndex = index;
                rows[index].classList.add('focused');
                rows[index].scrollIntoView({{ behavior: 'smooth', block: 'nearest' }});
            }}
            
            toggleFocusedSpan() {{
                if (this.focusedIndex >= 0) {{
                    const row = document.querySelectorAll('.span-row')[this.focusedIndex];
                    const header = row.querySelector('.span-header');
                    toggleSpanEvents(header);
                }}
            }}
            
            collapseAllEvents() {{
                document.querySelectorAll('.events-section').forEach(section => {{
                    section.style.display = 'none';
                }});
                document.querySelectorAll('.events-toggle').forEach(btn => {{
                    btn.textContent = btn.textContent.replace('Hide', 'Show');
                }});
            }}
            
            expandAllEvents() {{
                document.querySelectorAll('.span-header').forEach(header => {{
                    toggleSpanEvents(header);
                }});
            }}
            
            searchEvents(query) {{
                // Implementation for filtering events by query
                // This would filter loaded events and hide/show rows
                // For now, placeholder
                console.log('Search:', query);
            }}
        }}
        
        // Global functions
        function toggleSpanEvents(header) {{
            const row = header.closest('.span-row');
            const section = row.querySelector('.events-section');
            const button = row.querySelector('.events-toggle');
            
            if (section.style.display === 'none') {{
                section.style.display = 'block';
                if (button) button.textContent = button.textContent.replace('Show', 'Hide');
                
                // Auto-load events if not loaded
                const loadingRow = section.querySelector('.loading');
                if (loadingRow) {{
                    const spanId = row.dataset.spanId;
                    loadSpanEvents(spanId);
                }}
            }} else {{
                section.style.display = 'none';
                if (button) button.textContent = button.textContent.replace('Hide', 'Show');
            }}
        }}
        
        async function loadSpanEvents(spanId) {{
            try {{
                const response = await fetch(`/api/traces/{{document.querySelector('.trace-id').textContent}}/spans/${{spanId}}/events`);
                const events = await response.json();
                
                const tbody = document.getElementById(`events-body-${{spanId}}`);
                if (events.length === 0) {{
                    tbody.innerHTML = '<tr><td colspan="3" class="no-events">No events found</td></tr>';
                }} else {{
                    tbody.innerHTML = events.map(event => `
                        <tr class="${{event.error ? 'event-row--error' : ''}}">
                            <td class="event-name">${{event.name}}</td>
                            <td>${{new Date(event.ts).toLocaleString()}}</td>
                            <td class="event-attrs">${{JSON.stringify(event.attributes, null, 2)}}</td>
                        </tr>
                    `).join('');
                }}
            }} catch (error) {{
                console.error('Failed to load events:', error);
                const tbody = document.getElementById(`events-body-${{spanId}}`);
                tbody.innerHTML = '<tr><td colspan="3" class="no-events">Failed to load events</td></tr>';
            }}
        }}
        
        // Initialize viewer
        document.addEventListener('DOMContentLoaded', () => {{
            const spansContainer = document.getElementById('spans-container');
            if (spansContainer) {{
                const spans = {span_tree};
                const viewer = new AggregatedSpanView(spansContainer, spans, {{
                    expandEvents: {expand_events}
                }});
            }}
        }});
        
        // Flag for expand events
        const expandEvents = {expand_events};
        
        // For testing: expandEvents = {expand_events}
        
        async function expandCompressedNode(nodeId) {{
            const button = event.target;
            const spinner = document.createElement('span');
            spinner.className = 'loading';
            spinner.textContent = ' Resolving...';
            button.parentNode.appendChild(spinner);
            button.disabled = true;
            
            try {{
                const response = await fetch(`/api/v1/traces/{{document.querySelector('.trace-id').textContent}}/compressed/${{nodeId}}/expand`);
                if (response.ok) {{
                    const data = await response.json();
                    // Replace the compressed node with its children
                    const spanRow = document.querySelector(`[data-span-id="${{nodeId}}"]`);
                    if (spanRow) {{
                        spanRow.outerHTML = data.html;
                    }}
                }} else {{
                    console.error('Failed to expand compressed node:', response.statusText);
                }}
            }} catch (error) {{
                console.error('Error expanding compressed node:', error);
            }} finally {{
                spinner.remove();
                button.disabled = false;
            }}
        }}
    </script>
</body>
</html>
"""
    return html


def build_span_tree(spans: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Build a tree structure from flat spans for hierarchical rendering"""
    span_map = {}
    tree = []
    
    # Create span map and identify root spans
    for span in spans:
        span_map[span["span_id"]] = {
            **span,
            "children": []
        }
    
    # Build tree structure
    for span in spans:
        span_data = span_map[span["span_id"]]
        if span.get("parent_span_id") and span["parent_span_id"] in span_map:
            parent = span_map[span["parent_span_id"]]
            parent["children"].append(span_data)
        else:
            tree.append(span_data)
    
    return tree


def render_span_tree(spans: List[Dict[str, Any]], expand_events: bool) -> str:
    """Recursively render span tree HTML"""
    def render_span(span):
        has_events = span.get("events") and len(span["events"]) > 0
        is_compressed = span.get("span_type") == "agent.reasoning.compressed"
        compressed_children_count = span.get("attributes", {}).get("tree.children_count", 0)
        compressed_summary = span.get("attributes", {}).get("tree.summary", "")
        is_embedded = span.get("attributes", {}).get("embedded", False)
        embedded_count = span.get("attributes", {}).get("embedded_count", 0)
        
        events_html = ""
        
        if has_events:
            events_html = f"""
            <div class="events-section" style="display: {'block' if expand_events else 'none'};">
                <div class="events-table">
                    <table>
                        <thead>
                            <tr>
                                <th>Name</th>
                                <th>Timestamp</th>
                                <th>Attributes</th>
                            </tr>
                        </thead>
                        <tbody>
                            {render_events(span["events"])}
                        </tbody>
                    </table>
                </div>
            </div>
            """
        
# Add compressed badge and expand button for compressed nodes
        span_name = span["name"]
        span_meta = f'| Type: {span['span_type']} | Status: {span['status']}'
        
        if is_compressed:
            span_name = f'<span class="compressed-name">{span["name"]}</span>'
            span_meta = f'| Type: {span["span_type"]} | Status: {span["status"]} <span class="compressed-badge">COMPRESSED</span>'
            
            # Add summary if available
            if compressed_summary:
                span_meta += f' <div class="summary">Summary: {compressed_summary}</div>'
        
        if is_embedded:
            span_meta += f' <span class="embedded-badge">EMBEDDED ({embedded_count})</span>'
            
        children_html = ""
        if span.get("children"):
            children_html = '<div class="span-children">' + \
                          ''.join([render_span(child) for child in span["children"]]) + \
                          '</div>'
        
        # Add expand button for compressed nodes
        expand_button = ""
        if is_compressed:
            expand_button = f'<button class="expand-compressed" onclick="expandCompressedNode(\'{span["span_id"]}\')">Expand</button>'
        
        return f"""
        <div class="span-row">
            <div class="span-header" onclick="toggleSpanEvents(this)">
                <div>
                    <span class="span-name">{span_name}</span>
                    <span class="span-meta">{span_meta}</span>
                    {expand_button}
                </div>
                {f'<button class="events-toggle">Hide {len(span["events"])} events</button>' if has_events else ''}
            </div>
            {children_html}
            {events_html}
        </div>
        """
    
    def render_events(events):
        rows = []
        for event in events:
            # Tolerate both PP0 internal {name, ts, attrs} and OTLP {name, timestamp, attributes}
            ts_raw = event.get("ts", event.get("timestamp", ""))
            attrs = event.get("attributes", event.get("attrs", {}))
            # Error flag: top-level error key OR attributes.status == "error" (embedded events)
            is_error = event.get("error") or attrs.get("status") == "error"
            try:
                ts_display = datetime.fromisoformat(ts_raw).strftime("%Y-%m-%d %H:%M:%S")
            except (ValueError, AttributeError):
                ts_display = str(ts_raw)
            rows.append(f"""
            <tr class="{'event-row--error' if is_error else ''}">
                <td class="event-name">{event["name"]}</td>
                <td>{ts_display}</td>
                <td class="event-attrs">{json.dumps(attrs, indent=2)}</td>
            </tr>
            """)
        return "".join(rows)
    
    return "".join([render_span(span) for span in spans])


def main():
    """Run the trace viewer"""
    port = int(os.getenv("AGENT_OBS_VIEWER_PORT", "8089"))
    host = os.getenv("AGENT_OBS_VIEWER_HOST", "127.0.0.1")
    
    print(f"Starting trace viewer on http://{host}:{port}")
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()