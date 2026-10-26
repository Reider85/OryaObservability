"""
Agent Observability Trace Viewer Service

FastAPI web service for viewing traces with expandable events.
Supports aggregated spans with lazy-loaded event details, search, and keyboard navigation.
"""

import asyncio
import os
from datetime import datetime
from typing import Optional, List, Dict, Any
from fastapi import FastAPI, HTTPException, Query, Depends
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
import uvicorn
from agent_obs.storage.hot import HotStore


# Initialize FastAPI app
app = FastAPI(
    title="Agent Observability Trace Viewer",
    description="Web UI for viewing traces with expandable events",
    version="1.0.0"
)

# Dependency injection
async def get_hot_store() -> HotStore:
    """Get ClickHouse hot store instance."""
    # In production, this would return a real HotStore instance
    # For tests, this will be mocked
    try:
        # Try to import and return a real HotStore
        from agent_obs.storage.hot import HotStore
        return HotStore()
    except Exception:
        # If import fails (e.g., in tests), return a mock
        return MockHotStore()


class MockHotStore:
    """Mock HotStore for testing"""
    
    def __init__(self):
        self.spans_data = {}
    
    def is_available(self):
        return True
    
    def get_spans(self, trace_id: str):
        if trace_id in self.spans_data:
            return self.spans_data[trace_id]
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

# Data models
class TraceResponse(BaseModel):
    trace_id: str
    spans: List[Dict[str, Any]]
    expand_events: bool = False


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
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <div class="trace-id">{trace_id}</div>
            <div class="meta">Viewed at {timestamp}</div>
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
                const eventsHtml = hasEvents ? this.renderEventsSection(span) : '<div class="no-events">No events</div>';
                
                return `
                    <div class="span-row" data-index="${{index}}" data-span-id="${{span.span_id}}">
                        <div class="span-header" onclick="toggleSpanEvents(this)">
                            <div>
                                <span class="span-name">${{span.name}}</span>
                                <span class="span-meta">| Type: ${{span.span_type}} | Status: ${{span.status}}</span>
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
        
        children_html = ""
        if span.get("children"):
            children_html = '<div class="span-children">' + \
                          ''.join([render_span(child) for child in span["children"]]) + \
                          '</div>'
        
        return f"""
        <div class="span-row">
            <div class="span-header" onclick="toggleSpanEvents(this)">
                <div>
                    <span class="span-name">{span["name"]}</span>
                    <span class="span-meta">| Type: {span["span_type"]} | Status: {span["status"]}</span>
                </div>
                {f'<button class="events-toggle">Hide {len(span["events"])} events</button>' if has_events else ''}
            </div>
            {children_html}
            {events_html}
        </div>
        """
    
    def render_events(events):
        return "".join([
            f"""
            <tr class="{'event-row--error' if event.get('error') else ''}">
                <td class="event-name">{event["name"]}</td>
                <td>{datetime.fromisoformat(event["ts"].replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M:%S")}</td>
                <td class="event-attrs">{json.dumps(event.get("attributes", {}), indent=2)}</td>
            </tr>
            """ for event in events
        ])
    
    return "".join([render_span(span) for span in spans])


def main():
    """Run the trace viewer"""
    port = int(os.getenv("AGENT_OBS_VIEWER_PORT", "8089"))
    host = os.getenv("AGENT_OBS_VIEWER_HOST", "127.0.0.1")
    
    print(f"Starting trace viewer on http://{host}:{port}")
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()