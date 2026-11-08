# Golden Signals Runbook (PP59)

This runbook provides operational guidance for the Google SRE Four Golden Signals implemented in PP59. When golden signal alerts fire, follow these procedures to maintain system reliability and performance.

## Overview

The Four Golden Signals provide a comprehensive view of system health:

- **LATENCY**: User experience and system responsiveness
- **TRAFFIC**: Service load and capacity planning  
- **ERRORS**: Service reliability and error rates
- **SATURATION**: Resource pressure and capacity headroom

## Alert Triage

### Severity Levels

- **PAGE** (Critical): Fast burn rates (>14.4x) affecting multiple windows - immediate action required
- **TICKET** (Warning): Slow burn rates (>6.0x) - next business day remediation
- **CRITICAL**: Multiple signals unhealthy - widespread issues requiring attention

### Alert Response Matrix

| Signal | Page Alert | Ticket Alert | Common Causes |
|--------|------------|--------------|---------------|
| **LATENCY** | p99 > 2s for >2min | p99 degrading >6x | LLM provider latency, network issues, hot paths |
| **TRAFFIC** | Rate spikes >14.4x | Rate trends >6x | DDoS, misconfigured clients, viral features |
| **ERRORS** | Error rate >0.5% | Error rate increasing | Code changes, dependency failures, config issues |
| **SATURATION** | Utilization >80% | Resource pressure | Scaling limits, memory leaks, capacity planning |

## Signal-Specific Procedures

### LATENCY Signal

#### Page Alert: GoldenSignalLatencyBurnRateHigh

**What it means**: p99 latency has exceeded 2 seconds for an extended period, affecting user experience significantly.

**Immediate Actions**:
1. **Check LLM Provider Latency**:
   ```bash
   # Check OpenAI/Anthropic provider status
   curl -s https://status.openai.com/api/v2/status.json
   curl -s https://status.anthropic.com/
   
   # Check provider latency from metrics
   curl -s "http://prometheus:9090/api/v1/query?query=histogram_quantile(0.99,rate(agent_obs_span_duration_seconds_bucket[5m]))"
   ```

2. **Identify Hot Paths**:
   ```bash
   # Query slow traces
   curl -s "http://prometheus:9090/api/v1/query?query=topk(10,sum(rate(agent_obs_spans_total{status=\"ok\"}[5m])) by (span_name))"
   
   # Check specific slow operations
   curl -s "http://prometheus:9090/api/v1/query?query=sum(rate(agent_obs_span_duration_seconds_bucket[5m])) by (span_name,le)"
   ```

3. **Check Network Latency**:
   ```bash
   # Check ClickHouse query latency
   curl -s "http://clickhouse:8123/?query=SELECT avg(duration_ms) FROM spans_hot WHERE span_type='agent.loop' AND start_time > now() - 3600"
   
   # Check network connectivity
   ping clickhouse
   ping langfuse
   ```

4. **Check System Resources**:
   ```bash
   # Check CPU utilization
   curl -s "http://prometheus:9090/api/v1/query?query=avg(rate(node_cpu_seconds_total{mode=\"idle\"}[5m]))"
   
   # Check memory usage
   curl -s "http://prometheus:9090/api/v1/query?query=(1 - avg(node_memory_MemAvailable_bytes) / avg(node_memory_MemTotal_bytes)) * 100"
   ```

**Escalation**: If LLM provider is healthy and local resources are adequate, escalate to SRE team for infrastructure investigation.

#### Ticket Alert: GoldenSignalLatencyBurnRateSlowBurn

**What it means**: p99 latency is gradually degrading but hasn't reached critical levels yet.

**Actions**:
1. **Review recent changes**: Check deployments, configuration changes, or model updates
2. **Monitor trends**: Watch for continued degradation over the next few days
3. **Capacity planning**: Consider scaling resources if degradation continues
4. **Performance tuning**: Review agent loops for optimization opportunities

### TRAFFIC Signal

#### Page Alert: GoldenSignalTrafficBurnRateHigh

**What it means**: Request rate has deviated significantly (>20%) from baseline, potentially indicating abnormal traffic patterns.

**Immediate Actions**:
1. **Check for Traffic Spikes**:
   ```bash
   # Check request rate patterns
   curl -s "http://prometheus:9090/api/v1/query?query=rate(agent_obs_spans_total[5m]) by (span_type)"
   
   # Check for unusual user agents or patterns
   curl -s "http://clickhouse:8123/?query=SELECT count(), any(user_agent) FROM spans_hot WHERE start_time > now() - 3600 GROUP BY user_agent ORDER BY count() DESC LIMIT 10"
   ```

2. **Check for DDoS or Abuse**:
   ```bash
   # Check request patterns by IP
   curl -s "http://clickhouse:8123/?query=SELECT count(), remote_addr FROM spans_hot WHERE start_time > now() - 3600 GROUP BY remote_addr ORDER BY count() DESC LIMIT 10"
   
   # Check for rapid-fire requests
   curl -s "http://clickhouse:8123/?query=SELECT trace_id, count() as req_count FROM spans_hot WHERE start_time > now() - 300 GROUP BY trace_id HAVING req_count > 100 ORDER BY req_count DESC LIMIT 10"
   ```

3. **Check Client Configuration**:
   - Verify client applications aren't in retry loops
   - Check for misconfigured load balancers
   - Review API rate limiting settings

4. **Coordinate with Security Team**: If malicious traffic is suspected, engage security team for investigation

#### Ticket Alert: GoldenSignalTrafficBurnRateSlowBurn

**What it means**: Traffic patterns are gradually changing, requiring capacity planning attention.

**Actions**:
1. **Review capacity forecasts**: Update scaling predictions based on trends
2. **Check auto-scaling**: Verify Kubernetes HPA or similar scaling mechanisms
3. **Plan capacity**: Schedule resource increases if sustained growth is expected
4. **Monitor for anomalies**: Watch for unexpected changes in traffic patterns

### ERRORS Signal

#### Page Alert: GoldenSignalErrorsBurnRateHigh

**What it means**: Error rate has exceeded 0.5%, indicating significant reliability issues.

**Immediate Actions**:
1. **Check Recent Deployments**:
   ```bash
   # Check recent deployments
   curl -s "http://prometheus:9090/api/v1/query?query=changes(agent_obs_spans_total{status=\"error\"}[1h])"
   
   # Check deployment timestamps
   curl -s "http://clickhouse:8123/?query=SELECT formatDateTime(start_time, '%Y-%m-%d %H:%M:%S'), agent_version FROM spans_hot WHERE status='error' AND start_time > now() - 3600 ORDER BY start_time DESC LIMIT 10"
   ```

2. **Check Dependencies**:
   ```bash
   # Check upstream service health
   curl -s "http://prometheus:9090/api/v1/query?query=sum(rate(exporter_errors_total[5m])) by (exporter)"
   
   # Check database connectivity
   curl -s "http://prometheus:9090/api/v1/query?query=rate(clickhouse_http_requests_total[5m])"
   ```

3. **Check Error Patterns**:
   ```bash
   # Check error types
   curl -s "http://clickhouse:8123/?query=SELECT status, count() FROM spans_hot WHERE status='error' AND start_time > now() - 3600 GROUP BY status ORDER BY count() DESC"
   
   # Check error messages
   curl -s "http://clickhouse:8123/?query=SELECT any(error_message) FROM spans_hot WHERE status='error' AND start_time > now() - 3600 GROUP by trace_id LIMIT 10"
   ```

4. **Check System Resources**:
   ```bash
   # Check for resource exhaustion
   curl -s "http://prometheus:9090/api/v1/query?query=(1 - avg(node_memory_MemAvailable_bytes) / avg(node_memory_MemTotal_bytes)) * 100"
   curl -s "http://prometheus:9090/api/v1/query?query=avg(rate(node_cpu_seconds_total{mode=\"idle\"}[5m]))"
   ```

**Escalation**: If errors are widespread and not deployment-related, escalate to SRE team for infrastructure investigation.

#### Ticket Alert: GoldenSignalErrorsBurnRateSlowBurn

**What it means**: Error rate is gradually increasing, requiring attention before it becomes critical.

**Actions**:
1. **Review error trends**: Analyze error patterns and identify potential causes
2. **Check monitoring**: Verify that error tracking is working properly
3. **Review recent changes**: Look for configuration or code changes that might be affecting reliability
4. **Proactive remediation**: Address potential issues before they escalate

### SATURATION Signal

#### Page Alert: GoldenSignalSaturationBurnRateHigh

**What it means**: System utilization has exceeded 80% for extended periods, indicating resource constraints.

**Immediate Actions**:
1. **Check Resource Utilization**:
   ```bash
   # Check CPU usage
   curl -s "http://prometheus:9090/api/v1/query?query=(1 - avg(rate(node_cpu_seconds_total{mode=\"idle\"}[5m]))) * 100"
   
   # Check memory usage
   curl -s "http://prometheus:9090/api/v1/query?query=(1 - avg(node_memory_MemAvailable_bytes) / avg(node_memory_MemTotal_bytes)) * 100"
   
   # Check disk usage
   curl -s "http://prometheus:9090/api/v1/query?query=(1 - avg(node_filesystem_avail_bytes) / avg(node_filesystem_size_bytes)) * 100"
   ```

2. **Check Concurrent Operations**:
   ```bash
   # Check concurrent spans
   curl -s "http://clickhouse:8123/?query=SELECT avg(max_concurrent), max(max_concurrent) FROM (SELECT trace_id, max(concurrent_spans) as max_concurrent FROM (SELECT trace_id, count() OVER (PARTITION BY trace_id ORDER BY start_time) as concurrent_spans FROM spans_hot WHERE start_time > now() - 3600) GROUP BY trace_id)"
   
   # Check worker queue sizes
   curl -s "http://prometheus:9090/api/v1/query?query=sum(agent_obs_worker_queue_size)"
   ```

3. **Check Database Performance**:
   ```bash
   # Check ClickHouse performance
   curl -s "http://clickhouse:8123/?query=SELECT avg(query_duration_ms) FROM system.query_log WHERE query_start_time > now() - 3600"
   
   # Check connection pool usage
   curl -s "http://prometheus:9090/api/v1/query?query=sum(rate(clickhouse_connections[5m]))"
   ```

4. **Immediate Scaling**:
   - Scale up Kubernetes deployments if using auto-scaling
   - Increase database connection pools
   - Consider horizontal scaling for CPU-bound operations

**Escalation**: If scaling doesn't resolve the issue, escalate to SRE team for infrastructure investigation.

#### Ticket Alert: GoldenSignalSaturationBurnRateSlowBurn

**What it means**: Resource utilization is gradually increasing, requiring capacity planning.

**Actions**:
1. **Review capacity trends**: Analyze utilization patterns over time
2. **Plan scaling**: Schedule resource increases based on growth projections
3. **Optimize resource usage**: Look for opportunities to reduce resource consumption
4. **Monitor for bottlenecks**: Watch for specific resource constraints that might limit scalability

## Multi-Signal Response

### Critical Alert: GoldenSignalsMultipleUnhealthy

**What it means**: Multiple golden signals are unhealthy simultaneously, indicating widespread system issues.

**Immediate Actions**:
1. **Declare Incident**: Follow the standard incident declaration process
2. **Assemble Incident Response Team**: Include SRE, development, and operations teams
3. **Check Overall System Health**:
   ```bash
   # Check all major components
   curl -s "http://prometheus:9090/api/v1/query?query=sum(rate(dependency_health{dependency=~\"langfuse|ch|pg|vault|redis\"}[5m]))"
   
   # Check overall system metrics
   curl -s "http://prometheus:9090/api/v1/query?query=sum(rate(agent_obs_spans_total[5m])) by (status)"
   ```

4. **Prioritize Critical Services**: Focus on services affecting the most users
5. **Communicate Status**: Provide regular updates to stakeholders

### Cross-Signal Correlation

Signals often indicate related issues:

- **LATENCY + SATURATION**: Usually indicates resource constraints
- **ERRORS + TRAFFIC**: May indicate capacity issues or abusive behavior
- **ALL SIGNALS**: Often indicates systemic issues or major outages

## Troubleshooting Commands

### System Health Checks

```bash
# Overall system status
curl -s "http://prometheus:9090/api/v1/query?query=sum(rate(agent_obs_spans_total[5m])) by (status)"

# Resource utilization
curl -s "http://prometheus:9090/api/v1/query?query=(1 - avg(rate(node_cpu_seconds_total{mode=\"idle\"}[5m]))) * 100"

# Database health
curl -s "http://clickhouse:8123/?query=SELECT count() FROM system.metrics WHERE metric = 'Query'"
```

### Golden Signal Metrics

```bash
# Check current SLI values
curl -s "http://prometheus:9090/api/v1/query?query=golden_signal_*_sli"

# Check burn rates
curl -s "http://prometheus:9090/api/v1/query?query=golden_signal_burn_rate"

# Check signal health
curl -s "http://prometheus:9090/api/v1/query?query=golden_signal_healthy"
```

### Trace Investigation

```bash
# Check slow traces
curl -s "http://clickhouse:8123/?query=SELECT trace_id, avg(duration_ms) FROM spans_hot WHERE duration_ms > 2000 AND start_time > now() - 3600 GROUP BY trace_id ORDER BY avg(duration_ms) DESC LIMIT 10"

# Check error traces
curl -s "http://clickhouse:8123/?query=SELECT trace_id, any(error_message) FROM spans_hot WHERE status='error' AND start_time > now() - 3600 GROUP BY trace_id LIMIT 10"
```

## Escalation Procedures

### When to Escalate

- **PAGE alerts not resolved within 15 minutes**
- **Multiple signals unhealthy simultaneously**
- **Infrastructure issues requiring vendor support**
- **Security incidents requiring specialized response**

### Escalation Contacts

- **SRE On-call**: [Contact Information]
- **Development Team**: [Contact Information]
- **Security Team**: [Contact Information]
- **Vendor Support**: [Contact Information for key vendors]

## Post-Incident Review

After resolving any golden signal alert:

1. **Document the incident**: Record timeline, actions taken, and resolution
2. **Analyze root cause**: Determine the underlying issue that triggered the alert
3. **Implement preventive measures**: Add monitoring or automation to prevent recurrence
4. **Update runbook**: Document lessons learned and update procedures as needed

## Monitoring and Prevention

### Proactive Monitoring

- **Regular capacity reviews**: Monthly reviews of resource utilization trends
- **Performance baselines**: Establish and monitor performance baselines
- **Load testing**: Regular load testing to validate capacity planning
- **Dependency monitoring**: Monitor upstream service health and SLAs

### Automation Opportunities

- **Auto-scaling**: Implement horizontal and vertical auto-scaling
- **Capacity alerts**: Set up alerts for capacity planning triggers
- **Self-healing**: Implement automated remediation for common issues
- **Canary deployments**: Use canary deployments to reduce risk of changes

## References

- [Google SRE Four Golden Signals](https://sre.google/sre-book/monitoring-distributed-systems/#xtable-monitoring-distributed-systems)
- [PP59: Golden Signals SLI Implementation](../PRODUCTION-PROMPTS.md#pp59---improve-9-golden-signals-sli-латентность-трафик-ошибки-saturation)
- [SLO Alert Runbook](../slo-alerts-runbook.md)
- [Error Budget Runbook](../error-budget-runbook.md)