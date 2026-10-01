"""
Agent Observability Compliance UI Service

FastAPI web service for compliance catalog management and GDPR exports.
Provides web interface for DPO to view PII data, filter, and export GDPR reports.
"""

import asyncio
import os
from datetime import datetime, timedelta
from typing import Optional, List
import pandas as pd
from fastapi import FastAPI, HTTPException, Query, Depends
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel
import uvicorn
from agent_obs.compliance.catalog import ComplianceCatalog, ComplianceRow
from agent_obs.compliance.gdpr_export import export_data_map, GDPRDataRow
from agent_obs.compliance.aggregate import aggregate_compliance_catalog, ComplianceAggregationResult
from agent_obs.storage.warm import WarmStore

# Initialize FastAPI app
app = FastAPI(
    title="Agent Observability Compliance UI",
    description="Web UI for compliance catalog management and GDPR exports",
    version="1.0.0"
)

# Basic auth for development
security = HTTPBasic()

# Global dependencies
async def get_compliance_catalog() -> ComplianceCatalog:
    """Get compliance catalog instance"""
    from agent_obs.compliance.catalog import compliance_catalog
    return compliance_catalog

async def get_warm_store() -> WarmStore:
    """Get warm store instance"""
    from agent_obs.storage.warm import warm_store
    return warm_store

# Data models
class ComplianceCatalogResponse(BaseModel):
    total_rows: int
    rows: List[ComplianceRow]
    filters: dict

class ExportResponse(BaseModel):
    export_id: str
    format: str
    status: str
    download_url: Optional[str] = None
    created_at: datetime

class DashboardSummary(BaseModel):
    total_pii_events: int
    unique_agents: int
    unique_tools: int
    unique_pii_types: int
    recent_events: List[ComplianceRow]
    top_pii_types: List[tuple]
    top_agents: List[tuple]

# Basic auth dependency
async def get_current_user(credentials: HTTPBasicCredentials = Depends(security)):
    """Basic authentication for development"""
    import base64
    correct_username = os.getenv("COMPLIANCE_UI_USERNAME", "admin")
    correct_password = os.getenv("COMPLIANCE_UI_PASSWORD", "admin123")
    
    try:
        decoded = base64.b64encode(f"{credentials.username}:{credentials.password}".encode()).decode()
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid authentication credentials")
    
    expected = base64.b64encode(f"{correct_username}:{correct_password}".encode()).decode()
    
    if decoded != expected:
        raise HTTPException(status_code=401, detail="Invalid authentication credentials")
    
    return credentials.username

# API Endpoints

@app.get("/", response_class=HTMLResponse)
async def compliance_dashboard(
    username: str = Depends(get_current_user),
    agent_id: Optional[str] = Query(None, description="Filter by agent ID"),
    tool: Optional[str] = Query(None, description="Filter by tool name"),
    pii_type: Optional[str] = Query(None, description="Filter by PII type"),
    start_date: Optional[str] = Query(None, description="Start date (YYYY-MM-DD)"),
    end_date: Optional[str] = Query(None, description="End date (YYYY-MM-DD)"),
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(50, ge=1, le=500, description="Items per page")
):
    """Main compliance dashboard with catalog table"""
    
    # Get compliance catalog
    catalog = await get_compliance_catalog()
    
    # Build filters
    filters = {
        "agent_id": agent_id,
        "tool": tool,
        "pii_type": pii_type,
        "start_date": start_date,
        "end_date": end_date
    }
    
    # Get filtered compliance data
    compliance_rows = catalog.get_filtered_rows(
        agent_id=agent_id,
        tool=tool,
        pii_type=pii_type,
        start_date=start_date,
        end_date=end_date
    )
    
    # Pagination
    total_rows = len(compliance_rows)
    start_idx = (page - 1) * page_size
    end_idx = start_idx + page_size
    paginated_rows = compliance_rows[start_idx:end_idx]
    
    # Get dashboard summary
    summary = await get_dashboard_summary(catalog)
    
    # Render HTML template
    html_template = """
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Compliance Data Map - Agent Observability</title>
    <style>
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            margin: 0;
            padding: 20px;
            background-color: #f5f5f5;
        }
        .container {
            max-width: 1200px;
            margin: 0 auto;
            background: white;
            padding: 20px;
            border-radius: 8px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }
        .header {
            border-bottom: 2px solid #007bff;
            padding-bottom: 20px;
            margin-bottom: 30px;
        }
        .header h1 {
            margin: 0;
            color: #007bff;
        }
        .summary-cards {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 20px;
            margin-bottom: 30px;
        }
        .card {
            background: #f8f9fa;
            padding: 20px;
            border-radius: 6px;
            border-left: 4px solid #007bff;
        }
        .card h3 {
            margin: 0 0 10px 0;
            color: #333;
            font-size: 14px;
            text-transform: uppercase;
        }
        .card .value {
            font-size: 24px;
            font-weight: bold;
            color: #007bff;
        }
        .filters {
            background: #f8f9fa;
            padding: 20px;
            border-radius: 6px;
            margin-bottom: 20px;
        }
        .filters h3 {
            margin-top: 0;
        }
        .filter-row {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 15px;
            margin-bottom: 15px;
        }
        .filter-group {
            display: flex;
            flex-direction: column;
        }
        .filter-group label {
            margin-bottom: 5px;
            font-weight: 500;
            font-size: 14px;
        }
        .filter-group input, .filter-group select {
            padding: 8px;
            border: 1px solid #ddd;
            border-radius: 4px;
            font-size: 14px;
        }
        .export-section {
            margin-bottom: 30px;
            padding: 20px;
            background: #e7f3ff;
            border-radius: 6px;
            border: 1px solid #b3d9ff;
        }
        .export-button {
            background: #007bff;
            color: white;
            padding: 10px 20px;
            border: none;
            border-radius: 4px;
            cursor: pointer;
            font-size: 14px;
            margin-right: 10px;
        }
        .export-button:hover {
            background: #0056b3;
        }
        .table-container {
            overflow-x: auto;
        }
        table {
            width: 100%;
            border-collapse: collapse;
            margin-top: 20px;
        }
        th, td {
            padding: 12px;
            text-align: left;
            border-bottom: 1px solid #ddd;
        }
        th {
            background: #f8f9fa;
            font-weight: 600;
            position: sticky;
            top: 0;
        }
        tr:hover {
            background: #f5f5f5;
        }
        .pagination {
            display: flex;
            justify-content: center;
            margin-top: 20px;
            gap: 10px;
        }
        .pagination button {
            padding: 8px 12px;
            border: 1px solid #ddd;
            background: white;
            cursor: pointer;
            border-radius: 4px;
        }
        .pagination button:hover {
            background: #f5f5f5;
        }
        .pagination button.active {
            background: #007bff;
            color: white;
            border-color: #007bff;
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>Compliance Data Map</h1>
            <p>Where PII is processed in the Agent Observability system</p>
        </div>
        
        <!-- Summary Cards -->
        <div class="summary-cards">
            <div class="card">
                <h3>Total PII Events</h3>
                <div class="value">{{ summary.total_pii_events }}</div>
            </div>
            <div class="card">
                <h3>Unique Agents</h3>
                <div class="value">{{ summary.unique_agents }}</div>
            </div>
            <div class="card">
                <h3>Unique Tools</h3>
                <div class="value">{{ summary.unique_tools }}</div>
            </div>
            <div class="card">
                <h3>PII Types</h3>
                <div class="value">{{ summary.unique_pii_types }}</div>
            </div>
        </div>
        
        <!-- Export Section -->
        <div class="export-section">
            <h3>GDPR Data Export</h3>
            <p>Export compliance data for GDPR reporting</p>
            <button class="export-button" onclick="exportData('csv')">Export CSV</button>
            <button class="export-button" onclick="exportData('xlsx')">Export XLSX</button>
            <p style="margin-top: 10px; font-size: 14px; color: #666;">
                <strong>Note:</strong> Exports include all PII events from the last 90 days by default
            </p>
        </div>
        
        <!-- Filters -->
        <div class="filters">
            <h3>Filter Data</h3>
            <form method="GET" id="filterForm">
                <div class="filter-row">
                    <div class="filter-group">
                        <label for="agent_id">Agent ID</label>
                        <input type="text" id="agent_id" name="agent_id" value="{{ filters.agent_id or '' }}">
                    </div>
                    <div class="filter-group">
                        <label for="tool">Tool Name</label>
                        <input type="text" id="tool" name="tool" value="{{ filters.tool or '' }}">
                    </div>
                    <div class="filter-group">
                        <label for="pii_type">PII Type</label>
                        <select id="pii_type" name="pii_type">
                            <option value="">All Types</option>
                            <option value="email" {% if filters.pii_type == 'email' %}selected{% endif %}>Email</option>
                            <option value="phone" {% if filters.pii_type == 'phone' %}selected{% endif %}>Phone</option>
                            <option value="inn" {% if filters.pii_type == 'inn' %}selected{% endif %}>INN</option>
                            <option value="passport" {% if filters.pii_type == 'passport' %}selected{% endif %}>Passport</option>
                            <option value="payment" {% if filters.pii_type == 'payment' %}selected{% endif %}>Payment</option>
                        </select>
                    </div>
                    <div class="filter-group">
                        <label for="start_date">Start Date</label>
                        <input type="date" id="start_date" name="start_date" value="{{ filters.start_date or '' }}">
                    </div>
                    <div class="filter-group">
                        <label for="end_date">End Date</label>
                        <input type="date" id="end_date" name="end_date" value="{{ filters.end_date or '' }}">
                    </div>
                </div>
                <button type="submit" class="export-button">Apply Filters</button>
                <button type="button" class="export-button" onclick="clearFilters()">Clear Filters</button>
            </form>
        </div>
        
        <!-- Data Table -->
        <div class="table-container">
            <h3>PII Processing Events</h3>
            <table>
                <thead>
                    <tr>
                        <th>Agent ID</th>
                        <th>Tool</th>
                        <th>Field</th>
                        <th>PII Type</th>
                        <th>Frequency</th>
                        <th>Last Seen</th>
                    </tr>
                </thead>
                <tbody>
                    {% for row in paginated_rows %}
                    <tr>
                        <td>{{ row.agent_id }}</td>
                        <td>{{ row.tool }}</td>
                        <td>{{ row.field }}</td>
                        <td>{{ row.pii_type }}</td>
                        <td>{{ row.frequency }}</td>
                        <td>{{ row.frequency }} events</td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
        </div>
        
        <!-- Pagination -->
        {% if total_rows > page_size %}
        <div class="pagination">
            {% if page > 1 %}
            <button onclick="window.location.href='/?page={{ page-1 }}{% for k, v in filters.items() %}{% if v %}&{{ k }}={{ v }}{% endif %}{% endfor %}'">Previous</button>
            {% endif %}
            
            {% for p in range(max(1, page-2), min(total_pages + 1, page + 3)) %}
            <button {% if p == page %}class="active"{% endif %} onclick="window.location.href='/?page={{ p }}{% for k, v in filters.items() %}{% if v %}&{{ k }}={{ v }}{% endif %}{% endfor %}'">{{ p }}</button>
            {% endfor %}
            
            {% if page < total_pages %}
            <button onclick="window.location.href='/?page={{ page+1 }}{% for k, v in filters.items() %}{% if v %}&{{ k }}={{ v }}{% endif %}{% endfor %}'">Next</button>
            {% endif %}
        </div>
        {% endif %}
    </div>
    
    <script>
        function exportData(format) {
            const params = new URLSearchParams(window.location.search);
            params.set('fmt', format);
            params.set('export', 'true');
            
            const exportUrl = `/compliance/export?${params.toString()}`;
            window.open(exportUrl, '_blank');
        }
        
        function clearFilters() {
            document.getElementById('agent_id').value = '';
            document.getElementById('tool').value = '';
            document.getElementById('pii_type').value = '';
            document.getElementById('start_date').value = '';
            document.getElementById('end_date').value = '';
            document.getElementById('filterForm').submit();
        }
    </script>
</body>
</html>
    """
    
    # Calculate total pages
    total_pages = (total_rows + page_size - 1) // page_size
    
    # Render template with context
    from jinja2 import Template
    template = Template(html_template)
    
    return template.render(
        summary=summary.model_dump(),
        paginated_rows=paginated_rows,
        total_rows=total_rows,
        page=page,
        total_pages=total_pages,
        page_size=page_size,
        filters=filters
    )

@app.get("/compliance/export")
async def export_gdpr_data(
    username: str = Depends(get_current_user),
    fmt: str = Query("csv", pattern="^(csv|xlsx)$", description="Export format"),
    as_of: Optional[str] = Query(None, description="Export as of date (YYYY-MM-DD)"),
    agent_id: Optional[str] = Query(None, description="Filter by agent ID"),
    tool: Optional[str] = Query(None, description="Filter by tool name"),
    pii_type: Optional[str] = Query(None, description="Filter by PII type")
):
    """Export GDPR Data Map in CSV or XLSX format"""
    
    try:
        # Parse as_of date if provided
        export_date = datetime.strptime(as_of, "%Y-%m-%d").date() if as_of else datetime.now().date()
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD")
    
    # Generate export filename
    export_id = f"gdpr_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    filename = f"gdpr_data_map_{export_date.strftime('%Y%m%d')}.{fmt}"
    
    # Get compliance catalog
    catalog = await get_compliance_catalog()
    
    # Get filtered data
    compliance_rows = catalog.get_filtered_rows(
        agent_id=agent_id,
        tool=tool,
        pii_type=pii_type,
        end_date=export_date.strftime('%Y-%m-%d')
    )
    
    # Convert to GDPR data rows
    gdpr_rows = [
            GDPRDataRow(
                agent_id=row.agent_id,
                tool=row.tool,
                field=row.field,
                pii_type=row.pii_type,
                frequency=row.frequency,
                data_subject_category="employee",  # Default, can be configured
                lawful_basis="legitimate_interest",  # Default, can be configured
                retention_period="90d",  # Default, can be configured
                recipient="internal"  # Default, can be configured
            )
            for row in compliance_rows
        ]
    
    # Generate export
    if fmt == "csv":
        csv_data = export_data_map(gdpr_rows, fmt="csv")
        return StreamingResponse(
            iter([csv_data]),
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )
    else:  # xlsx
        # Generate XLSX in memory
        output = export_data_map(gdpr_rows, fmt="xlsx")
        return StreamingResponse(
            iter([output]),
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )
    
    return ExportResponse(
        export_id=export_id,
        format=fmt,
        status="completed",
        created_at=datetime.now()
    )

@app.get("/compliance/api/summary")
async def get_dashboard_summary_api(
    username: str = Depends(get_current_user)
):
    """Get dashboard summary data"""
    catalog = await get_compliance_catalog()
    summary = await get_dashboard_summary(catalog)
    return summary

@app.get("/compliance/api/catalog")
async def get_compliance_catalog_api(
    username: str = Depends(get_current_user),
    agent_id: Optional[str] = Query(None, description="Filter by agent ID"),
    tool: Optional[str] = Query(None, description="Filter by tool name"),
    pii_type: Optional[str] = Query(None, description="Filter by PII type"),
    start_date: Optional[str] = Query(None, description="Start date (YYYY-MM-DD)"),
    end_date: Optional[str] = Query(None, description="End date (YYYY-MM-DD)"),
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(50, ge=1, le=500, description="Items per page")
):
    """Get compliance catalog data with filtering and pagination"""
    
    catalog = await get_compliance_catalog()
    
    # Get filtered data
    compliance_rows = catalog.get_filtered_rows(
        agent_id=agent_id,
        tool=tool,
        pii_type=pii_type,
        start_date=start_date,
        end_date=end_date
    )
    
    # Pagination
    total_rows = len(compliance_rows)
    start_idx = (page - 1) * page_size
    end_idx = start_idx + page_size
    paginated_rows = compliance_rows[start_idx:end_idx]
    
    return ComplianceCatalogResponse(
        total_rows=total_rows,
        rows=paginated_rows,
        filters={
            "agent_id": agent_id,
            "tool": tool,
            "pii_type": pii_type,
            "start_date": start_date,
            "end_date": end_date
        }
    )

@app.get("/compliance/api/health")
async def health_check():
    """Health check endpoint"""
    return {"status": "healthy", "service": "compliance-ui"}

# Helper functions

async def get_dashboard_summary(catalog: ComplianceCatalog) -> DashboardSummary:
    """Generate dashboard summary statistics"""
    
    # Get all compliance data
    all_rows = catalog.get_filtered_rows()
    
    if not all_rows:
        return DashboardSummary(
            total_pii_events=0,
            unique_agents=0,
            unique_tools=0,
            unique_pii_types=0,
            recent_events=[],
            top_pii_types=[],
            top_agents=[]
        )
    
    # Calculate basic statistics
    total_pii_events = len(all_rows)
    unique_agents = len(set(row.agent_id for row in all_rows))
    unique_tools = len(set(row.tool for row in all_rows))
    unique_pii_types = len(set(row.pii_type for row in all_rows))
    
# Get recent events (last 24 hours) - using current time as proxy since ComplianceRow doesn't have timestamp
    recent_cutoff = datetime.now() - timedelta(hours=24)
    recent_events = all_rows[:10]  # Take first 10 as "recent" since we don't have timestamps
    
    # Get top PII types
    pii_type_counts = {}
    for row in all_rows:
        pii_type_counts[row.pii_type] = pii_type_counts.get(row.pii_type, 0) + row.frequency
    
    top_pii_types = sorted(pii_type_counts.items(), key=lambda x: x[1], reverse=True)[:5]
    
    # Get top agents
    agent_counts = {}
    for row in all_rows:
        agent_counts[row.agent_id] = agent_counts.get(row.agent_id, 0) + row.frequency
    
    top_agents = sorted(agent_counts.items(), key=lambda x: x[1], reverse=True)[:5]
    
    return DashboardSummary(
        total_pii_events=total_pii_events,
        unique_agents=unique_agents,
        unique_tools=unique_tools,
        unique_pii_types=unique_pii_types,
        recent_events=recent_events[:10],  # Top 10 recent
        top_pii_types=top_pii_types,
        top_agents=top_agents
    )

# CLI entry point

def main():
    """Run the compliance UI service"""
    host = os.getenv("COMPLIANCE_UI_HOST", "0.0.0.0")
    port = int(os.getenv("COMPLIANCE_UI_PORT", "8088"))
    reload = os.getenv("COMPLIANCE_UI_RELOAD", "false").lower() == "true"
    
    uvicorn.run(
        "agent_obs.compliance.ui:app",
        host=host,
        port=port,
        reload=reload,
        log_level="info"
    )

if __name__ == "__main__":
    main()