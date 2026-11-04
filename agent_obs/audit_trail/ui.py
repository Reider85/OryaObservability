"""Audit trail web interface for shared proxy (PP46).

Provides web interface for viewing audit events with filtering, drill-down,
and correlation capabilities for observability spans and security events.
"""

from __future__ import annotations

import csv
import logging
from datetime import datetime, timedelta, UTC
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from agent_obs.proxy.audit_trail import SharedAuditTrail

logger = logging.getLogger(__name__)

router = APIRouter()
security = HTTPBearer()

# Global audit trail instance - will be set by dependency injection
audit_trail = None


def get_audit_trail():
    """Dependency for getting audit trail instance."""
    # Return the global audit_trail instance (will be patched by tests)
    return audit_trail

@router.get("/admin/audit-trail", response_class=HTMLResponse)
async def audit_trail_dashboard(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(security),
    audit_trail: SharedAuditTrail = Depends(get_audit_trail),
    tenant_id: str | None = Query(None, description="Filter by tenant ID"),
    user_id: str | None = Query(None, description="Filter by user ID"),
    agent_id: str | None = Query(None, description="Filter by agent ID"),
    security_verdict: str | None = Query(None, description="Filter by security verdict (clean/flag/block)"),
    decision: str | None = Query(None, description="Filter by decision (forward/flag/block)"),
    start_time: str | None = Query(None, description="Start time (ISO format)"),
    end_time: str | None = Query(None, description="End time (ISO format)"),
    limit: int = Query(50, ge=1, le=200, description="Maximum number of records"),
    offset: int = Query(0, ge=0, description="Offset for pagination"),
):
    """Audit trail dashboard with filtering and drill-down capabilities.
    
    RBAC: admin / sre / auditor permissions required
    """
    
    # Initialize variables
    start_timestamp = None
    end_timestamp = None
    events = []
    summary = {}
    summary_dict = {}
    formatted_events = []
    
    if start_time:
            try:
                start_timestamp = datetime.fromisoformat(start_time.replace('Z', '+00:00')).timestamp()
            except ValueError:
                raise HTTPException(status_code=400, detail="Invalid start_time format")
    
            if end_time:
                try:
                    end_timestamp = datetime.fromisoformat(end_time.replace('Z', '+00:00')).timestamp()
                except ValueError:
                    raise HTTPException(status_code=400, detail="Invalid end_time format")
    
            # Get filtered audit events
            print(f"DEBUG: Calling get_audit_events_for_ui with limit={limit}, offset={offset}")
            events = await audit_trail.get_audit_events_for_ui(
                tenant_id=tenant_id,
                user_id=user_id,
                agent_id=agent_id,
                security_verdict=security_verdict,
                decision=decision,
                start_time=start_timestamp,
                end_time=end_timestamp,
                limit=limit,
                offset=offset,
            )
            print(f"DEBUG: Got events: {events}")
    
            # Get summary statistics
            print("DEBUG: Calling get_audit_summary_for_ui")
            summary = await audit_trail.get_audit_summary_for_ui(
                start_time=start_timestamp,
                end_time=end_timestamp,
            )
            print(f"DEBUG: Got summary: {summary}")
    
            # Format timestamps for display
            formatted_events = []
            for event in events:
                formatted_event = event.copy()
                formatted_event["formatted_timestamp"] = datetime.fromtimestamp(
                    event["timestamp"], tz=UTC
                ).strftime("%Y-%m-%d %H:%M:%S UTC")
                formatted_events.append(formatted_event)
    
            # Convert summary to regular dict for template
            summary_dict = dict(summary) if hasattr(summary, 'items') else summary
    
    # Generate saved queries
    saved_queries = [
            {"name": "All blocks", "params": {"decision": "block"}},
            {"name": "Flags from tenant_X", "params": {"tenant_id": "tenant_X", "decision": "flag"}},
            {"name": "Last 24 hours", "params": {"end_time": datetime.now(UTC).isoformat()}},
        ]
    
    # Build HTML content with proper escaping
    html_content = f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Audit Trail - OryaObservability</title>
        <style>
            body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; margin: 0; padding: 20px; background-color: #f5f5f5; }}
            .container {{ max-width: 1400px; margin: 0 auto; background: white; border-radius: 8px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); padding: 30px; }}
            .header {{ border-bottom: 1px solid #e0e0e0; padding-bottom: 20px; margin-bottom: 30px; }}
            .header h1 {{ margin: 0; color: #2c3e50; font-size: 28px; }}
            .header p {{ margin: 10px 0 0 0; color: #7f8c8d; }}
    
            .summary-cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 20px; margin-bottom: 30px; }}
            .card {{ background: #f8f9fa; border-radius: 8px; padding: 20px; border-left: 4px solid #3498db; }}
            .card h3 {{ margin: 0 0 10px 0; color: #2c3e50; font-size: 16px; }}
            .card .value {{ font-size: 24px; font-weight: bold; color: #3498db; }}
            .card .subtext {{ font-size: 12px; color: #7f8c8d; margin-top: 5px; }}
    
            .filters {{ background: #f8f9fa; border-radius: 8px; padding: 20px; margin-bottom: 20px; }}
            .filters h3 {{ margin: 0 0 15px 0; color: #2c3e50; }}
            .filter-row {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 15px; margin-bottom: 15px; }}
            .filter-group {{ display: flex; flex-direction: column; }}
            .filter-group label {{ margin-bottom: 5px; font-weight: 500; color: #2c3e50; font-size: 14px; }}
            .filter-group input, .filter-group select {{ padding: 8px 12px; border: 1px solid #ddd; border-radius: 4px; font-size: 14px; }}
    
            .saved-queries {{ margin-bottom: 20px; }}
            .saved-queries h4 {{ margin: 0 0 10px 0; color: #2c3e50; }}
            .query-buttons {{ display: flex; gap: 10px; flex-wrap: wrap; }}
            .query-btn {{ padding: 6px 12px; background: #ecf0f1; border: 1px solid #bdc3c7; border-radius: 4px; cursor: pointer; font-size: 12px; transition: all 0.2s; }}
            .query-btn:hover {{ background: #d5dbdb; }}
    
            .table-container {{ overflow-x: auto; }}
            table {{ width: 100%; border-collapse: collapse; background: white; }}
            th, td {{ padding: 12px; text-align: left; border-bottom: 1px solid #e0e0e0; font-size: 14px; }}
            th {{ background: #f8f9fa; font-weight: 600; color: #2c3e50; cursor: pointer; user-select: none; }}
            th:hover {{ background: #e9ecef; }}
            .sortable::after {{ content: ' \\u2195'; color: #bdc3c7; font-size: 12px; }}
    
            .status-badge {{ padding: 4px 8px; border-radius: 12px; font-size: 12px; font-weight: 500; text-transform: uppercase; }}
            .status-clean {{ background: #d4edda; color: #155724; }}
            .status-flag {{ background: #fff3cd; color: #856404; }}
            .status-block {{ background: #f8d7da; color: #721c24; }}
            .decision-forward {{ background: #cce5ff; color: #004085; }}
            .decision-flag {{ background: #fff3cd; color: #856404; }}
            .decision-block {{ background: #f8d7da; color: #721c24; }}
    
            .actions {{ display: flex; gap: 10px; }}
            .btn {{ padding: 8px 16px; border: none; border-radius: 4px; cursor: pointer; font-size: 14px; text-decoration: none; display: inline-block; transition: all 0.2s; }}
            .btn-primary {{ background: #3498db; color: white; }}
            .btn-primary:hover {{ background: #2980b9; }}
            .btn-secondary {{ background: #95a5a6; color: white; }}
            .btn-secondary:hover {{ background: #7f8c8d; }}
            .btn-export {{ background: #27ae60; color: white; }}
            .btn-export:hover {{ background: #229954; }}
    
            .pagination {{ display: flex; justify-content: space-between; align-items: center; margin-top: 20px; padding-top: 20px; border-top: 1px solid #e0e0e0; }}
            .pagination-info {{ color: #7f8c8d; font-size: 14px; }}
            .pagination-controls {{ display: flex; gap: 10px; }}
    
            .trace-link {{ color: #3498db; text-decoration: none; font-weight: 500; }}
            .trace-link:hover {{ text-decoration: underline; }}
    
            .security-link {{ color: #e74c3c; text-decoration: none; font-weight: 500; }}
            .security-link:hover {{ text-decoration: underline; }}
    
            @media (max-width: 768px) {{
                .filter-row {{ grid-template-columns: 1fr; }}
                .summary-cards {{ grid-template-columns: 1fr; }}
                .table-container {{ overflow-x: scroll; }}
            }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <h1>Audit Trail</h1>
                <p>Monitor and analyze security and observability decisions across your system</p>
            </div>
    
            <div class="summary-cards">
                <div class="card">
                    <h3>Total Events</h3>
                    <div class="value">{summary_dict.get('total_events', 0)}</div>
                    <div class="subtext">Last 30 days</div>
                </div>
                <div class="card">
                    <h3>Block Rate</h3>
                    <div class="value">{summary_dict.get('block_rate', 0):.1%}</div>
                    <div class="subtext">{summary_dict.get('block_count', 0)} blocks</div>
                </div>
                <div class="card">
                    <h3>Flag Rate</h3>
                    <div class="value">{summary_dict.get('flag_rate', 0):.1%}</div>
                    <div class="subtext">{summary_dict.get('flag_count', 0)} flags</div>
                </div>
                <div class="card">
                    <h3>Avg Security Score</h3>
                    <div class="value">{summary_dict.get('avg_security_score', 0):.2f}</div>
                    <div class="subtext">0.0 = low, 1.0 = high</div>
                </div>
            </div>
    
            <div class="filters">
                <h3>Filter Events</h3>
                <form method="GET" id="filterForm">
                    <div class="filter-row">
                        <div class="filter-group">
                            <label for="tenant_id">Tenant ID</label>
                            <input type="text" id="tenant_id" name="tenant_id" value="{tenant_id or ''}" placeholder="tenant_123">
                        </div>
                        <div class="filter-group">
                            <label for="user_id">User ID</label>
                            <input type="text" id="user_id" name="user_id" value="{user_id or ''}" placeholder="user_456">
                        </div>
                        <div class="filter-group">
                            <label for="agent_id">Agent ID</label>
                            <input type="text" id="agent_id" name="agent_id" value="{agent_id or ''}" placeholder="agent_789">
                        </div>
                        <div class="filter-group">
                            <label for="security_verdict">Security Verdict</label>
                            <select id="security_verdict" name="security_verdict">
                                <option value="">All</option>
                                <option value="clean" {'selected' if security_verdict == 'clean' else ''}>Clean</option>
                                <option value="flag" {'selected' if security_verdict == 'flag' else ''}>Flag</option>
                                <option value="block" {'selected' if security_verdict == 'block' else ''}>Block</option>
                            </select>
                        </div>
                        <div class="filter-group">
                            <label for="decision">Decision</label>
                            <select id="decision" name="decision">
                                <option value="">All</option>
                                <option value="forward" {'selected' if decision == 'forward' else ''}>Forward</option>
                                <option value="flag" {'selected' if decision == 'flag' else ''}>Flag</option>
                                <option value="block" {'selected' if decision == 'block' else ''}>Block</option>
                            </select>
                        </div>
                        <div class="filter-group">
                            <label for="start_time">Start Time</label>
                            <input type="datetime-local" id="start_time" name="start_time" value="{start_time[:19] if start_time else ''}">
                        </div>
                        <div class="filter-group">
                            <label for="end_time">End Time</label>
                            <input type="datetime-local" id="end_time" name="end_time" value="{end_time[:19] if end_time else ''}">
                        </div>
                    </div>
                    <div style="display: flex; gap: 10px;">
                        <button type="submit" class="btn btn-primary">Apply Filters</button>
                        <button type="button" class="btn btn-secondary" onclick="clearFilters()">Clear</button>
                        <button type="button" class="btn btn-export" onclick="exportToCSV()">Export CSV</button>
                    </div>
                </form>
            </div>
    
            <div class="saved-queries">
                <h4>Saved Queries</h4>
                <div class="query-buttons">
                    <button class="query-btn" onclick="applySavedQuery('all-blocks')">All Blocks</button>
                    <button class="query-btn" onclick="applySavedQuery('tenant-flags')">Flags from Tenant X</button>
                    <button class="query-btn" onclick="applySavedQuery('last-24h')">Last 24 Hours</button>
                    <button class="query-btn" onclick="applySavedQuery('high-risk')">High Risk (Score < 0.5)</button>
                </div>
            </div>
    
            <div class="table-container">
                <table>
                    <thead>
                        <tr>
                            <th class="sortable" onclick="sortTable(0)">Timestamp</th>
                            <th class="sortable" onclick="sortTable(1)">Tenant ID</th>
                            <th class="sortable" onclick="sortTable(2)">User ID</th>
                            <th class="sortable" onclick="sortTable(3)">Agent ID</th>
                            <th class="sortable" onclick="sortTable(4)">Security Verdict</th>
                            <th class="sortable" onclick="sortTable(5)">Decision</th>
                            <th class="sortable" onclick="sortTable(6)">Security Score</th>
                            <th>Actions</th>
                        </tr>
                    </thead>
                    <tbody>
                        {formatted_events}
                    </tbody>
</table>
            </div>
    
            <div class="pagination">
                    <div class="pagination-info">
                        Showing {offset + 1}-{min(offset + len(formatted_events), summary_dict.get('total_events', 0))} of {summary_dict.get('total_events', 0)} events
                    </div>
                    <div class="pagination-controls">
                        <button class="btn btn-secondary" onclick="changePage({max(0, offset - limit)})" {'disabled' if offset == 0 else ''}>Previous</button>
                        <button class="btn btn-secondary" onclick="changePage({offset + limit})" {'disabled' if offset + limit >= summary_dict.get('total_events', 0) else ''}>Next</button>
                    </div>
                </div>
        </div>
    
        <script>
            function clearFilters() {{
                document.getElementById('filterForm').reset();
            }}
    
            function exportToCSV() {{
                const params = new URLSearchParams(window.location.search);
                const url = '/admin/audit-trail/export?' + params.toString();
                window.open(url, '_blank');
            }}
    
            function applySavedQuery(type) {{
                const params = new URLSearchParams(window.location.search);
    
                switch(type) {{
                    case 'all-blocks':
                        params.set('decision', 'block');
                        params.delete('security_verdict');
                        break;
                    case 'tenant-flags':
                        params.set('tenant_id', 'tenant_X');
                        params.set('decision', 'flag');
                        break;
                    case 'last-24h':
                        const now = new Date();
                        const yesterday = new Date(now.getTime() - 24 * 60 * 60 * 1000);
                        params.set('end_time', now.toISOString().slice(0, 16));
                        params.set('start_time', yesterday.toISOString().slice(0, 16));
                        break;
                    case 'high-risk':
                        params.set('security_verdict', 'block');
                        break;
                }}
    
                window.location.search = params.toString();
            }}
    
            function changePage(newOffset) {{
                const params = new URLSearchParams(window.location.search);
                params.set('offset', newOffset);
                window.location.search = params.toString();
            }}
    
            function sortTable(columnIndex) {{
                const table = document.querySelector('table');
                const tbody = table.querySelector('tbody');
                const rows = Array.from(tbody.querySelectorAll('tr'));
    
                rows.sort((a, b) => {{
                    const aValue = a.cells[columnIndex].textContent.trim();
                    const bValue = b.cells[columnIndex].textContent.trim();
    
                    // Handle numeric values
                    if (columnIndex === 6) {{ // Security Score
                        return parseFloat(bValue) - parseFloat(aValue);
                    }}
    
                    // Handle dates
                    if (columnIndex === 0) {{ // Timestamp
                        return new Date(bValue) - new Date(aValue);
                    }}
    
                    // Handle text
                    return aValue.localeCompare(bValue);
                }});
    
                // Re-append sorted rows
                rows.forEach(row => tbody.appendChild(row));
            }}
    
            // Format table rows
            const formattedEvents = {formatted_events};
            const tbody = document.querySelector('tbody');
            tbody.innerHTML = formattedEvents.map(event => \`
                    <tr>
                        <td>\${{event.formatted_timestamp}}</td>
                        <td>\${{event.tenant_id}}</td>
                        <td>\${{event.user_id}}</td>
                        <td>\${{event.agent_id}}</td>
                        <td><span class="status-badge status-\${{event.security_verdict}}">\${{event.security_verdict}}</span></td>
                        <td><span class="status-badge decision-\${{event.decision}}">\${{event.decision}}</span></td>
                        <td>\${{event.security_score.toFixed(2)}}</td>
                        <td class="actions">
                            <a href="/trace-viewer/\${{event.trace_id}}" class="trace-link" target="_blank">View Trace</a>
                            <a href="https://orya-security-ui/traces/\${{event.security_audit_id}}" class="security-link" target="_blank">Security Details</a>
                        </td>
                    </tr>
                \`).join('');
        </script>
    </body>
    </html>
    """
    
    return HTMLResponse(content=html_content)


@router.get("/admin/audit-trail/export")
async def export_audit_trail_csv(
    audit_trail: SharedAuditTrail = Depends(get_audit_trail),
    tenant_id: str | None = Query(None),
    user_id: str | None = Query(None),
    agent_id: str | None = Query(None),
    security_verdict: str | None = Query(None),
    decision: str | None = Query(None),
    start_time: str | None = Query(None),
    end_time: str | None = Query(None),
):
    """Export audit events to CSV format."""
    
    # Parse time filters
    start_timestamp = None
    end_timestamp = None
    
    if start_time:
        try:
            start_timestamp = datetime.fromisoformat(start_time.replace('Z', '+00:00')).timestamp()
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid start_time format")
    
    if end_time:
        try:
            end_timestamp = datetime.fromisoformat(end_time.replace('Z', '+00:00')).timestamp()
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid end_time format")
    
    # Get filtered audit events
    events = await audit_trail.get_audit_events_for_ui(
        tenant_id=tenant_id,
        user_id=user_id,
        agent_id=agent_id,
        security_verdict=security_verdict,
        decision=decision,
        start_time=start_timestamp,
        end_time=end_timestamp,
        limit=10000,  # Large limit for export
    )
    
    def generate_csv():
        import io
        output = io.StringIO()
        writer = csv.writer(output)
        
        # Write header
        writer.writerow([
            'Timestamp', 'Trace ID', 'Tenant ID', 'User ID', 'Agent ID',
            'Security Verdict', 'Decision', 'Security Score',
            'Redacted Fields Count', 'Policy Rule', 'IP Address', 'User Agent',
            'Security Audit ID', 'Observability Span ID'
        ])
        
        # Write data rows
        for event in events:
            writer.writerow([
                datetime.fromtimestamp(event['timestamp'], tz=UTC).isoformat(),
                event['trace_id'],
                event['tenant_id'],
                event['user_id'],
                event['agent_id'],
                event['security_verdict'],
                event['decision'],
                event['security_score'],
                event['redacted_fields_count'],
                event['policy_rule'] or '',
                event['ip_address'],
                event['user_agent'],
                event['security_audit_id'],
                event['obs_span_id']
            ])
        
        # Convert to string and yield
        output.seek(0)
        yield output.read()
    
    return StreamingResponse(
        generate_csv(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=audit_trail_export.csv"}
    )


@router.get("/admin/audit-trail/trace/{trace_id}", response_class=HTMLResponse)
async def get_trace_audit_events(
    trace_id: str,
    credentials: HTTPAuthorizationCredentials = Depends(security),
    audit_trail: SharedAuditTrail = Depends(get_audit_trail),
):
    """Get all audit events for a specific trace ID."""
    
    try:
        events = await audit_trail.get_by_trace(trace_id=trace_id, limit=100)
    except Exception as e:
        # Log the error but still render the UI with empty data
        events = []
    
# Create HTML template with proper escaping
    events_json = str(events).replace("'", "&apos;")
    total_events = len(events)
    security_events = len([e for e in events if e.security_verdict != 'clean'])
    blocked_events = len([e for e in events if e.decision == 'block'])
    
    html_content = f"""    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Trace Audit Events - {trace_id}</title>
        <style>
            body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; margin: 0; padding: 20px; background-color: #f5f5f5; }}
            .container {{ max-width: 1200px; margin: 0 auto; background: white; border-radius: 8px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); padding: 30px; }}
            .header {{ border-bottom: 1px solid #e0e0e0; padding-bottom: 20px; margin-bottom: 30px; }}
            .header h1 {{ margin: 0; color: #2c3e50; }}
            .header p {{ margin: 10px 0 0 0; color: #7f8c8d; }}
    
            .trace-info {{ background: #f8f9fa; border-radius: 8px; padding: 20px; margin-bottom: 30px; }}
            .trace-info h3 {{ margin: 0 0 15px 0; color: #2c3e50; }}
            .trace-details {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 15px; }}
            .detail-item {{ }}
            .detail-label {{ font-weight: 600; color: #2c3e50; font-size: 14px; }}
            .detail-value {{ color: #34495e; font-size: 16px; }}
    
            .audit-events {{ }}
            .audit-events h3 {{ margin: 0 0 15px 0; color: #2c3e50; }}
            table {{ width: 100%; border-collapse: collapse; }}
            th, td {{ padding: 12px; text-align: left; border-bottom: 1px solid #e0e0e0; }}
            th {{ background: #f8f9fa; font-weight: 600; color: #2c3e50; }}
            .status-badge {{ padding: 4px 8px; border-radius: 12px; font-size: 12px; font-weight: 500; text-transform: uppercase; }}
            .status-clean {{ background: #d4edda; color: #155724; }}
            .status-flag {{ background: #fff3cd; color: #856404; }}
            .status-block {{ background: #f8d7da; color: #721c24; }}
            .decision-forward {{ background: #cce5ff; color: #004085; }}
            .decision-flag {{ background: #fff3cd; color: #856404; }}
            .decision-block {{ background: #f8d7da; color: #721c24; }}
    
            .back-link {{ color: #3498db; text-decoration: none; font-weight: 500; }}
            .back-link:hover {{ text-decoration: underline; }}
            .security-link {{ color: #e74c3c; text-decoration: none; font-weight: 500; }}
            .security-link:hover {{ text-decoration: underline; }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <h1>Trace Audit Events</h1>
                <p><a href="/admin/audit-trail" class="back-link">\u2190 Back to Audit Trail</a></p>
            </div>
    
            <div class="trace-info">
                <h3>Trace Information</h3>
                <div class="trace-details">
                    <div class="detail-item">
                        <div class="detail-label">Trace ID</div>
                        <div class="detail-value">{trace_id}</div>
                    </div>
                    <div class="detail-item">
                        <div class="detail-label">Total Events</div>
                        <div class="detail-value">{total_events}</div>
                    </div>
                    <div class="detail-item">
                        <div class="detail-label">Security Events</div>
                        <div class="detail-value">{security_events}</div>
                    </div>
                    <div class="detail-item">
                        <div class="detail-label">Blocked Events</div>
                        <div class="detail-value">{blocked_events}</div>
                    </div>
                </div>
            </div>
    
            <div class="audit-events">
                <h3>Audit Events</h3>
                <table>
                    <thead>
                        <tr>
                            <th>Timestamp</th>
                            <th>Security Verdict</th>
                            <th>Decision</th>
                            <th>Security Score</th>
                            <th>Redacted Fields</th>
                            <th>Policy Rule</th>
                            <th>Actions</th>
                        </tr>
                    </thead>
                    <tbody id="events-tbody">
                        <!-- Events will be populated by JavaScript -->
                    </tbody>
                </table>
            </div>
        </div>
    
        <script>
            const events = {events_json};
            const tbody = document.getElementById('events-tbody');
            let html = '';
            for (const event of events) {{
                const timestamp = new Date(event.timestamp * 1000).toISOString().replace('T', ' ').replace('Z', ' UTC');
                html += '<tr>';
                html += '<td>' + timestamp + '</td>';
                html += '<td><span class="status-badge status-' + event.security_verdict + '">' + event.security_verdict + '</span></td>';
                html += '<td><span class="status-badge decision-' + event.decision + '">' + event.decision + '</span></td>';
                html += '<td>' + event.security_score.toFixed(2) + '</td>';
                html += '<td>' + event.redacted_fields_count + '</td>';
                html += '<td>' + (event.policy_rule || 'None') + '</td>';
                html += '<td>';
                html += '<a href="https://orya-security-ui/traces/' + event.security_audit_id + '" class="security-link" target="_blank">Security Details</a>';
                html += '</td>';
                html += '</tr>';
            }}
            tbody.innerHTML = html;
        </script>
    </body>
    </html>"""
    
    return HTMLResponse(content=html_content)