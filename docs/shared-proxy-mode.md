# Shared Proxy Mode (PP44)

Shared proxy mode (Mode B) combines observability and security scanning in a single HTTP proxy. This mode integrates with OryaSecurity service to provide threat detection, PII scanning, and security policy evaluation while maintaining observability capabilities.

## Overview

Mode B (shared proxy) addresses the requirement for unified security and observability processing:

- **Single transaction**: Security scanning and observability enrichment happen in parallel
- **Performance isolation**: Independent timeouts and circuit breakers for security/obs
- **Fail-open semantics**: Security timeouts allow requests to proceed with audit warnings
- **Cross-system correlation**: Unified trace_id links security events and observability spans
- **Decision priority**: Security decisions override observability decisions

## Configuration

### Environment Variables

```bash
# Enable shared proxy mode
SHARED_PROXY_ENABLED=on

# OryaSecurity service URL
SHARED_PROXY_SECURITY_URL=http://orya-security:8000/scan

# Security scanning timeout (50ms p99)
SHARED_PROXY_SECURITY_TIMEOUT=0.05

# Fail-open behavior: allow requests when security fails
SHARED_PROXY_SECURITY_FAIL_OPEN=true
```

### Proxy Configuration

```python
from agent_obs.proxy import ProxyConfig

config = ProxyConfig(
    mode="on",                    # Enable proxy mode
    enabled=True,                 # Enable proxy
    shared_proxy_enabled=True,    # Enable shared mode
    shared_proxy_security_url="http://orya-security:8000",
    shared_proxy_security_timeout=0.05,
    shared_proxy_security_fail_open=True,
)
```

## Architecture

### Components

1. **SecurityClient**: HTTP client for OryaSecurity integration
   - Handles security scanning with timeout and circuit breakers
   - Implements fail-open semantics
   - Returns SecurityScanResult with verdict, score, and metadata

2. **SharedPipeline**: Coordinates security + obs processing
   - Parallel execution with independent timeouts
   - Performance isolation with circuit breakers
   - Handles fail-open scenarios

3. **DecisionRouter**: Priority-based decision making
   - Security > Policy > Observability priority
   - Record-then-block semantics
   - Detailed decision metadata for audit trail

4. **SharedAuditTrail**: Cross-system correlation
   - Unified audit logging for security and obs events
   - Trace_id-based correlation
   - Query interface for audit events

### Processing Flow

```
Request → Security Scan (50ms) → Obs Enrichment (100ms) → Decision Router → Response
    ↓            ↓                    ↓                    ↓
  Security   Obs Span            Span Attributes      Final Decision
  Result     Enrichment          with Security         (Forward/Flag/Block)
```

## Security Verdicts

### Clean
- **Action**: Forward request to upstream
- **Span attributes**: `security.decision=clean`, `security.verdict=clean`
- **Response**: Normal HTTP 200 response

### Flag
- **Action**: Forward request with security metadata
- **Span attributes**: `security.decision=flag`, `security.verdict=flag`, `security.flag=true`
- **Response**: Normal HTTP 200 response with security warnings

### Block
- **Action**: Block request with security details
- **Span attributes**: `security.decision=block`, `security.verdict=block`
- **Response**: HTTP 403 with security violation details

## Performance Characteristics

### Timeouts
- **Security scanning**: 50ms p99 (configurable)
- **Obs enrichment**: 100ms p99
- **Total processing**: < 100ms p99 (parallel execution)

### Circuit Breakers
- **Security**: Opens after 3 consecutive failures, 30s recovery
- **Observability**: Opens after 10 consecutive failures, 60s recovery
- **Audit trail**: Independent circuit breaker for logging

### SLA Targets
- **Total latency**: p99 < 100ms
- **Availability**: 99.9% (with fail-open)
- **Data loss**: < 0.1% (with audit trail)

## Monitoring and Observability

### Metrics

```python
# Security metrics
shared_proxy_security_latency_seconds          # Security scan latency
shared_proxy_security_scans_total             # Total security scans
shared_proxy_security_failures_total          # Security failures
shared_proxy_security_timeouts_total          # Security timeouts
shared_proxy_security_circuit_open_total      # Circuit breaker opens

# Obs metrics
shared_proxy_obs_latency_seconds              # Obs enrichment latency
shared_proxy_obs_circuit_open_total           # Obs circuit breaker opens

# Decision metrics
decisions_total{action="forward|flag|block"}  # Total decisions
decision_router_latency_seconds               # Decision latency

# Audit metrics
shared_proxy_audit_events_total               # Total audit events
shared_proxy_audit_write_latency_seconds      # Audit write latency
```

### Alerts

```yaml
# Security-related alerts
- alert: SharedProxyBlockRateHigh
  expr: rate(shared_proxy_security_blocks_total[5m]) > 0.05
  for: 5m
  labels:
    severity: warning
  annotations:
    summary: High security block rate
    description: Security block rate > 5% for 5 minutes

- alert: SharedProxySecurityTimeoutsHigh
  expr: rate(shared_proxy_security_timeouts_total[5m]) > 0.1
  for: 5m
  labels:
    severity: warning
  annotations:
    summary: High security timeout rate
    description: Security timeout rate > 10% for 5 minutes

# Performance alerts
- alert: SharedProxyLatencyHigh
  expr: histogram_quantile(0.99, sum(rate(shared_proxy_total_latency_seconds_bucket[5m]))) by (le) > 0.1
  for: 5m
  labels:
    severity: warning
  annotations:
    summary: High shared proxy latency
    description: p99 latency > 100ms for 5 minutes
```

## Testing

### Unit Tests

```bash
# Run shared proxy tests
python -m pytest tests/test_shared_proxy.py -q

# Run with coverage
python -m pytest tests/test_shared_proxy.py --cov=agent_obs.proxy --cov-report=html
```

### Integration Tests

```bash
# Test with mock OryaSecurity service
python -m pytest tests/test_shared_proxy.py::TestIntegrationScenarios -q

# Test end-to-end flow
python -m pytest tests/test_shared_proxy.py::TestEndToEndFlow -q
```

### Load Testing

```bash
# Load test with locust
locust -f tests/locust/shared_proxy.py --users=100 --spawn-rate=10 --run-time=5m
```

## Troubleshooting

### Common Issues

1. **Security Service Unavailable**
   - Check OryaSecurity service connectivity
   - Verify `SHARED_PROXY_SECURITY_URL` is correct
   - Check network connectivity and firewall rules

2. **High Security Latency**
   - Monitor `shared_proxy_security_latency_seconds` metric
   - Check OryaService performance
   - Consider adjusting `SHARED_PROXY_SECURITY_TIMEOUT`

3. **Circuit Breaker Open**
   - Check `shared_proxy_security_circuit_open_total` metric
   - Monitor OryaService health
   - Wait for automatic recovery (30s)

4. **Audit Trail Failures**
   - Check audit trail connectivity
   - Monitor disk space for audit storage
   - Verify audit permissions

### Debug Mode

Enable debug logging for detailed troubleshooting:

```bash
export AGENT_OBS_LOG_LEVEL=DEBUG
export SHARED_PROXY_SECURITY_URL=http://orya-security-debug:8000
```

### Health Checks

The shared proxy provides health endpoints:

```bash
# Check proxy health
curl http://localhost:8765/health

# Check security service connectivity
curl http://localhost:8765/debug/security-health
```

## Migration from Mode A

To migrate from Mode A (observability-only) to Mode B (shared proxy):

1. **Enable shared mode**:
   ```bash
   SHARED_PROXY_ENABLED=on
   SHARED_PROXY_SECURITY_URL=http://orya-security:8000/scan
   ```

2. **Deploy OryaSecurity service**:
   - Deploy OryaSecurity scanner
   - Configure service discovery
   - Test connectivity

3. **Update monitoring**:
   - Add security-related metrics
   - Configure security alerts
   - Update dashboards

4. **Test migration**:
   - Test with clean/flag/block scenarios
   - Verify performance targets
   - Check audit trail correlation

## Best Practices

1. **Security Service Configuration**
   - Deploy OryaSecurity in HA mode
   - Monitor service health and performance
   - Configure appropriate timeouts and circuit breakers

2. **Performance Optimization**
   - Use independent timeouts for security/obs
   - Monitor parallel execution performance
   - Consider caching for repeated requests

3. **Audit Trail Management**
   - Configure appropriate retention policies
   - Monitor audit storage usage
   - Enable export for compliance reporting

4. **Monitoring and Alerting**
   - Set up comprehensive metrics collection
   - Configure meaningful alerts
   - Regular performance reviews

5. **Security and Compliance**
   - Configure appropriate security policies
   - Monitor for security violations
   - Maintain audit trail for compliance