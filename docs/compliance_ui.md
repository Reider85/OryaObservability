# Compliance UI Service

**Service:** oraya-compliance-ui  
**URL:** http://localhost:8088  
**Purpose:** Web interface for compliance catalog management and GDPR exports  
**Target Audience:** Data Protection Officer (DPO) and compliance teams

## Overview

The Compliance UI provides a simple web interface for viewing PII processing events, filtering data, and generating GDPR Data Map exports. It's built with FastAPI and provides a 5-minute time-to-GDPR-report capability.

## Features

### 📊 Dashboard View
- **Summary Cards**: Total PII events, unique agents, tools, and PII types
- **Recent Events**: PII processing from the last 24 hours
- **Top PII Types**: Most frequently detected PII types
- **Top Agents**: Agents with most PII activity

### 🔍 Filtering & Search
- **Agent ID**: Filter by specific agent
- **Tool Name**: Filter by tool type (llm_call, tool_call, etc.)
- **PII Type**: Filter by PII category (email, phone, inn, passport, payment)
- **Date Range**: Filter by start and end dates
- **Pagination**: Navigate through large datasets

### 📤 GDPR Data Export
- **CSV Export**: Comma-separated values with BOM for Excel compatibility
- **XLSX Export**: Excel format with multiple sheets
- **Date Filtering**: Export data as of specific date
- **Filtered Exports**: Apply all dashboard filters to exports

### 🔐 Security
- **Basic Authentication**: Development-only authentication
- **Read-Only Access**: No editing capabilities (audit trail protection)
- **RBAC Ready**: Framework for future role-based access control

## Quick Start

### 1. Start the Service

```bash
# Development mode with auto-reload
COMPLIANCE_UI_HOST=0.0.0.0 \
COMPLIANCE_UI_PORT=8088 \
COMPLIANCE_UI_USERNAME=admin \
COMPLIANCE_UI_PASSWORD=admin123 \
python -m agent_obs.compliance.ui
```

### 2. Access the Dashboard

Open your browser and navigate to:
```
http://localhost:8088
```

Login with:
- Username: `admin`
- Password: `admin123`

### 3. Using Docker Compose

Add to your `docker-compose.yml`:

```yaml
services:
  compliance-ui:
    image: python:3.11-slim
    container_name: compliance-ui
    ports:
      - "8088:8088"
    environment:
      - COMPLIANCE_UI_HOST=0.0.0.0
      - COMPLIANCE_UI_PORT=8088
      - COMPLIANCE_UI_USERNAME=admin
      - COMPLIANCE_UI_PASSWORD=admin123
      - COMPLIANCE_UI_RELOAD=false
    volumes:
      - .:/app
    command: >
      sh -c "pip install -e .[api] && 
             python -m agent_obs.compliance.ui"
    depends_on:
      - compliance-postgres  # If using Postgres compliance catalog
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8088/compliance/api/health"]
      interval: 30s
      timeout: 10s
      retries: 3
```

## API Endpoints

### Health Check
```
GET /compliance/api/health
```

### Dashboard Summary
```
GET /compliance/api/summary
```

### Compliance Catalog
```
GET /compliance/api/catalog?agent_id=...&tool=...&pii_type=...&start_date=...&end_date=...&page=...&page_size=...
```

### GDPR Export
```
GET /compliance/export?fmt=csv|xlsx&as_of=YYYY-MM-DD&agent_id=...&tool=...&pii_type=...
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `COMPLIANCE_UI_HOST` | `0.0.0.0` | Host to bind the service |
| `COMPLIANCE_UI_PORT` | `8088` | Port to bind the service |
| `COMPLIANCE_UI_USERNAME` | `admin` | Basic auth username |
| `COMPLIANCE_UI_PASSWORD` | `admin123` | Basic auth password |
| `COMPLIANCE_UI_RELOAD` | `false` | Enable auto-reload for development |

## GDPR Data Map Features

### Export Formats

#### CSV Export
- UTF-8 with BOM for Excel compatibility
- Single sheet with all compliance data
- Filename: `gdpr_data_map_YYYYMMDD.csv`

#### XLSX Export  
- Multi-sheet workbook:
  - **Data Map Sheet**: Detailed compliance events
  - **Summary Sheet**: Aggregated metrics and totals
- Filename: `gdpr_data_map_YYYYMMDD.xlsx`

### GDPR Compliance Data

The export includes all required GDPR data fields:

| Field | Description |
|-------|-------------|
| **Data Subject** | Employee/Customer/User/Partner |
| **Lawful Basis** | Consent/Contract/Legitimate Interest |
| **Retention Period** | 90 days by default |
| **Recipients** | Internal/External/Third-party |
| **PII Categories** | Email, Phone, INN, Passport, Payment |
| **Processing Tools** | LLM calls, Tool calls |
| **Frequency** | Number of PII detections |
| **Last Seen** | Most recent processing timestamp |

## Integration with Existing Systems

### Compliance Catalog
The UI integrates with the existing compliance catalog system:

- **Data Source**: `ComplianceCatalog` from `agent_obs.compliance.catalog`
- **Automatic Updates**: Real-time data from PII masking events
- **Postgres Storage**: Optional persistent storage via `PostgresCatalogWriter`

### Guardrail Integration
- **PII Detection**: Events automatically recorded from guardrail engine
- **Audit Trail**: All PII processing events logged and timestamped
- **Field Masking**: Original PII values replaced with `[TYPE:hash]` masks

## Security Considerations

### Development Environment
- Basic authentication is sufficient for development
- No SSL/TLS required for local development
- All data is read-only

### Production Deployment
For production deployment:

1. **Authentication**: Replace basic auth with proper OAuth2/JWT
2. **SSL/TLS**: Enable HTTPS with valid certificates
3. **Network Security**: Restrict access to authorized networks only
4. **Audit Logging**: Enable comprehensive audit logging
5. **Rate Limiting**: Implement API rate limiting

### Data Protection
- **No PII Storage**: UI only displays masked PII data
- **Read-Only**: No capabilities to modify compliance data
- **Export Encryption**: Consider encrypting exported files
- **Access Controls**: Implement proper RBAC (Level 3 feature)

## Performance Considerations

### Pagination
- Default page size: 50 records
- Maximum page size: 500 records
- Efficient database queries with proper indexing

### Export Performance
- Large datasets exported asynchronously
- Memory-efficient streaming for CSV exports
- Temporary file handling for XLSX exports

### Caching
- Dashboard summary cached for 5 minutes
- Recent events updated in real-time
- Export status tracked in memory

## Monitoring & Observability

### Metrics
The service exposes Prometheus metrics:

- `compliance_ui_requests_total` - Total API requests
- `compliance_ui_request_duration_seconds` - Request latency
- `compliance_ui_exports_total` - Export operations
- `compliance_ui_auth_failures_total` - Authentication failures

### Health Checks
- `/compliance/api/health` - Service health
- Database connectivity checks
- Compliance catalog availability

### Logging
- Structured JSON logging
- Request/response logging
- Error tracking and debugging

## Troubleshooting

### Common Issues

#### Service Won't Start
- Check if port 8088 is available
- Verify environment variables are set correctly
- Ensure all dependencies are installed (`pip install -e .[api]`)

#### Authentication Failed
- Verify username/password in environment variables
- Check basic auth configuration
- Ensure proper HTTP headers are sent

#### No Data Displayed
- Verify compliance catalog is populated
- Check guardrail engine is working
- Ensure database connection is active

#### Export Fails
- Check disk space for temporary files
- Verify compliance catalog has data
- Check export format parameters

### Debug Mode

Enable debug logging:

```bash
export PYTHONPATH=.
python -m agent_obs.compliance.ui 2>&1 | tee compliance-ui.log
```

## Testing

Run the test suite:

```bash
# Run all compliance UI tests
pytest tests/test_compliance_ui.py -v

# Run with coverage
pytest tests/test_compliance_ui.py --cov=agent_obs.compliance.ui --cov-report=html
```

### Test Coverage
- API endpoint testing
- Authentication testing
- Export functionality testing
- Integration with compliance catalog
- Error handling scenarios

## Future Enhancements

### Level 3 Features (Production Ready)
- **RBAC**: Role-based access control
- **SSO Integration**: Single sign-on support
- **Advanced Filtering**: Complex query builders
- **Real-time Updates**: WebSocket for live data
- **Export Scheduling**: Automated periodic exports
- **Multi-tenancy**: Support for multiple tenants

### UI Enhancements
- **Interactive Charts**: Visual representation of PII trends
- **Advanced Search**: Full-text search capabilities
- **Export Templates**: Pre-configured export formats
- **Alert Management**: Compliance violation notifications
- **Audit Trail**: Detailed user action logging

## Support

For issues and questions:
1. Check the troubleshooting section
2. Review existing GitHub issues
3. Create new issue with detailed description
4. Include logs and environment information

## Related Documentation

- [Compliance Catalog](../agent_obs/compliance/README.md)
- [GDPR Export](../agent_obs/compliance/gdpr_export.py)
- [Guardrail Engine](../agent_obs/guardrail/README.md)
- [Agent Observability Architecture](../../docs/architecture.md)