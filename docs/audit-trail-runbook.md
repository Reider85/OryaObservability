# Audit Trail UI - PP48

## Overview

The Audit Trail UI provides a unified interface for monitoring and analyzing security and observability decisions across the OryaObservability system. This interface is available in **MODE B** (Shared proxy OryaObservability + OryaSecurity) and allows SRE/DPO/auditor personnel to view all decisions (forward/flag/block) with comprehensive filtering and drill-down capabilities.

## Access

The audit trail UI is available at:
```
https://<proxy-host>:<proxy-port>/admin/audit-trail
```

**RBAC Requirements:**
- `admin` - Full access to all audit functions
- `sre` - Full access for operational monitoring
- `developer` - Read-only access for debugging
- `dpo` - Access for compliance monitoring
- `auditor` - Read-only access for audit purposes

## Features

### 1. Dashboard Overview
- **Summary Cards**: Total events, block rate, flag rate, average security score
- **Real-time Statistics**: Shows current state of the system
- **Time Range**: Displays data from the last 30 days by default

### 2. Filtering Capabilities
The dashboard provides comprehensive filtering options:

- **Tenant ID**: Filter by specific tenant
- **User ID**: Filter by specific user
- **Agent ID**: Filter by specific agent
- **Security Verdict**: 
  - `clean` - No security issues detected
  - `flag` - Security issues detected but allowed
  - `block` - Request blocked due to security concerns
- **Decision**: 
  - `forward` - Request allowed to proceed
  - `flag` - Request flagged but allowed
  - `block` - Request blocked
- **Time Range**: Filter by custom time range
- **Pagination**: 50 records per page with navigation

### 3. Saved Queries
Quick access to common filter combinations:

- **All Blocks**: Shows all blocked requests
- **Flags from Tenant X**: Shows flagged requests from a specific tenant
- **Last 24 Hours**: Shows events from the last 24 hours
- **High Risk**: Shows events with security score < 0.5

### 4. Drill-down Capabilities

#### Trace Viewer Integration
- Click any trace ID to view detailed trace information
- Direct link to the trace viewer with security audit panel highlighted
- Shows correlation between security decisions and observability spans

#### OryaSecurity Integration
- Direct link to OryaSecurity UI for detailed security analysis
- Deep links to specific security audit events
- Provides security-specific context and analysis

### 5. Export Functionality
- **CSV Export**: Export filtered results to CSV format
- **Compliance Ready**: Includes all required fields for audit reports
- **Filtered Exports**: Export respects all applied filters

### 6. Alerting Integration

The dashboard integrates with the following alerting rules:

#### SharedProxyBlockRateHigh
- **Trigger**: Block rate > 5% for 5+ minutes
- **Severity**: Critical
- **Action**: Investigate potential attacks or policy issues

#### SharedProxySecurityTimeoutsHigh  
- **Trigger**: Security timeout rate > 10% for 5+ minutes
- **Severity**: Warning
- **Action**: Check OryaHealth service health and network connectivity

## Usage Examples

### 1. Security Incident Investigation
```
1. Navigate to /admin/audit-trail
2. Set time range to incident duration
3. Filter by decision = "block"
4. Sort by timestamp (newest first)
5. Click trace IDs to investigate specific incidents
6. Use security links for detailed analysis
```

### 2. Compliance Monitoring
```
1. Navigate to /admin/audit-trail
2. Set time range to compliance period
3. Export to CSV for audit records
4. Filter by tenant_id for tenant-specific compliance
```

### 3. Performance Analysis
```
1. Use saved queries "Last 24 Hours"
2. Monitor block rate trends
3. Check security score distributions
4. Investigate unusual patterns
```

## Integration Architecture

### MODE B Required
The audit trail UI is only available in **MODE B** configuration:
```bash
AGENT_OBS_PROXY_MODE=on
SHARED_PROXY_ENABLED=on
SHARED_PROXY_SECURITY_URL=https://orya-security:8443
```

### Data Flow
```
Agent Request → LLM Proxy → Security Scan → Decision Router → Audit Trail
                                    ↓
                              Observability Span
```

### Storage
- **Primary**: ClickHouse `audit_events_hot` table
- **Backup**: In-memory storage for testing/fallback
- **Retention**: Configurable (default 7 days)

## API Endpoints

### REST Endpoints
- `GET /admin/audit-trail` - Main dashboard
- `GET /admin/audit-trail/export` - CSV export
- `GET /admin/audit-trail/trace/{trace_id}` - Trace-specific view

### Query Parameters
All endpoints support the same filtering parameters:
- `tenant_id`, `user_id`, `agent_id`
- `security_verdict` (clean/flag/block)
- `decision` (forward/flag/block)
- `start_time`, `end_time` (ISO format)
- `limit` (1-200, default 50)
- `offset` (default 0)

## Monitoring and Metrics

### Key Metrics
- `shared_proxy_audit_total` - Total audit events
- `shared_proxy_audit_latency_seconds` - Processing latency
- `shared_proxy_security_timeouts_total` - Security timeouts
- `shared_proxy_security_circuit_open_total` - Circuit breaker opens

### Dashboard Integration
The audit trail metrics are integrated into:
- **Grafana Dashboards**: Real-time monitoring
- **Prometheus Alerting**: Automated alerting
- **Trace Viewer**: Correlation with observability data

## Troubleshooting

### Common Issues

#### 1. Access Denied
- **Issue**: 401/403 errors when accessing dashboard
- **Solution**: Verify RBAC configuration and user permissions
- **Check**: Ensure user has `audit:read` permission

#### 2. No Data Displayed
- **Issue**: Dashboard shows empty results
- **Solution**: Check time range and filters
- **Check**: Verify proxy is running in MODE B

#### 3. Slow Performance
- **Issue**: Dashboard loads slowly with large datasets
- **Solution**: Apply time range filters
- **Check**: Monitor ClickHouse query performance

#### 4. Export Failures
- **Issue**: CSV export fails or produces empty files
- **Solution**: Verify data exists for selected filters
- **Check**: Check file permissions and storage space

### Debug Mode
Enable debug logging:
```bash
AGENT_OBS_LOG_LEVEL=DEBUG
```

## Security Considerations

### Access Control
- All endpoints require authentication
- RBAC enforced at the API level
- Audit trail tracks all access attempts

### Data Protection
- PII fields are masked in the UI
- Export functionality respects content sampling policies
- No sensitive data logged in plain text

### Performance Impact
- Read-only operations with no impact on proxy performance
- Efficient indexing on ClickHouse for fast queries
- Pagination prevents memory issues with large datasets

## Related Components

### OryaSecurity Integration
- **URL**: `SHARED_PROXY_SECURITY_URL`
- **Authentication**: Automatic token handling
- **Timeout**: 50ms with fail-open semantics

### Observability SDK Integration
- **Span Correlation**: Links audit events to observability spans
- **Cost Tracking**: Integrates with cost attribution
- **Content Sampling**: Respects sampling policies

### RBAC System
- **Role Store**: Postgres or in-memory mapping
- **Permissions**: `audit:read` required for access
- **User Mapping**: `AGENT_OBS_RBAC_ROLES` environment variable

## Performance Optimization

### Query Optimization
- Use time range filters for large datasets
- Filter by tenant when possible
- Avoid wildcards in text searches

### Memory Management
- Pagination limits to 50 records per page
- Efficient JSON serialization
- Connection pooling for database queries

### Caching
- Prometheus metrics cached locally
- Query results cached for repeated requests
- Static assets browser-cached

## Future Enhancements

### Planned Features
- [ ] Real-time event streaming
- [ ] Advanced search with full-text capabilities
- [ ] Custom dashboard creation
- [ ] Multi-tenant isolation improvements
- [ ] Machine learning anomaly detection

### Integration Opportunities
- [ ] SIEM integration for security events
- [ ] Compliance reporting automation
- [ ] Incident response workflows
- [ ] Performance analytics dashboards

---

**Note**: This documentation is for PP48 - Unified Audit Trail UI. For more information about the overall architecture, see [PRODUCTION-PROMPTS.md](../../analytics/PRODUCTION-PROMPTS.md).