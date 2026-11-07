# Chaos Engineering Runbook

This document provides operational guidance for the chaos engineering system implemented for PP52 (Weekly Chaos Engineering Game-Day).

## Overview

The chaos engineering system automatically tests the resilience of the Orya Observability HA stack by randomly stopping one component per week and verifying that the system recovers within SLO thresholds.

## Architecture

### Components

1. **Chaos Scenarios**: Individual tests for each HA component
   - ClickHouse nodes (clickhouse1-clickhouse4)
   - PostgreSQL primary (pg-primary)
   - Vault nodes (vault-1, vault-2, vault-3)
   - Redis master (redis-master)
   - MinIO nodes (minio1-minio4)
   - OTel Collectors (otel-collector-1-3)
   - Langfuse nodes (langfuse-1-3)

2. **SLO Validator**: Monitors service levels during chaos
   - Availability (>99.5%)
   - Latency p95 (<5s)
   - Error budget (<20%)

3. **Chaos Engine**: Orchestrates test execution
   - Manages scenario lifecycle
   - Handles failures and recovery
   - Generates reports

4. **Reporter**: Generates test reports
   - JSON reports with detailed metrics
   - Markdown summaries for human readability

5. **Notifier**: Sends notifications
   - Webhook support
   - Slack integration
   - Telegram notifications

## SLO Thresholds

| Metric | Warning | Critical | Unit |
|--------|---------|----------|------|
| Availability | 99.5% | 99.0% | % |
| Latency p95 | 5000 | 10000 | ms |
| Error Budget | 20% | 50% | % |

## Target Services (PP52)

The system focuses on these critical services:
- clickhouse-1 (ClickHouse node 1)
- ch-1 (ClickHouse node 1, alias)
- pg-primary (PostgreSQL primary)
- vault-1 (Vault node 1)
- redis-1 (Redis master)
- minio-1 (MinIO node 1)
- otel-collector-1 (OTel Collector 1)
- langfuse-1 (Langfuse node 1)

## Manual Testing

### Running Individual Scenarios

```bash
# Run a specific scenario
python -m scripts.chaos.weekly_chaos --scenario clickhouse1_chaos

# Run with verbose logging
python -m scripts.chaos.weekly_chaos --scenario clickhouse1_chaos --verbose

# Run with custom output directory
python -m scripts.chaos.weekly_chaos --scenario clickhouse1_chaos --output-dir /custom/path
```

### Running All Scenarios

```bash
# Run all PP52 target scenarios
python -m scripts.chaos.weekly_chaos

# Run in random order
python -m scripts.chaos.weekly_chaos --random

# Run with verbose logging
python -m scripts.chaos.weekly_chaos --verbose
```

### Running Scheduled Tests

```bash
# Run the scheduled chaos test
python -m scripts.chaos.cron_weekly_chaos
```

## Configuration

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| CHAOS_ENGINEERING_ENABLED | true | Enable chaos engineering |
| CHAOS_OUTPUT_DIR | docs/chaos-reports | Output directory for reports |
| CHAOS_LOG_LEVEL | INFO | Logging level |
| CHAOS_DEFAULT_DURATION_SECONDS | 60 | Default chaos duration |
| CHAOS_RECOVERY_TIMEOUT_SECONDS | 300 | Recovery timeout |
| CHAOS_WEBHOOK_URL | - | Webhook URL for notifications |
| CHAOS_SLACK_WEBHOOK_URL | - | Slack webhook URL |
| CHAOS_TELEGRAM_BOT_TOKEN | - | Telegram bot token |
| CHAOS_TELEGRAM_CHAT_ID | - | Telegram chat ID |

### SLO Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| CHAOS_SLO_AVAILABILITY_WARNING | 99.5 | Availability warning threshold |
| CHAOS_SLO_AVAILABILITY_CRITICAL | 99.0 | Availability critical threshold |
| CHAOS_SLO_LATENCY_WARNING | 5000 | Latency warning threshold (ms) |
| CHAOS_SLO_LATENCY_CRITICAL | 10000 | Latency critical threshold (ms) |
| CHAOS_SLO_ERROR_BUDGET_WARNING | 20 | Error budget warning (%) |
| CHAOS_SLO_ERROR_BUDGET_CRITICAL | 50 | Error budget critical (%) |

### Schedule Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| CHAOS_SCHEDULE_ENABLED | true | Enable scheduled tests |
| CHAOS_WEEKLY_CRON | "0 2 * * 0" | Cron schedule for weekly tests |
| CHAOS_RANDOM_ORDER | true | Run scenarios in random order |
| CHAOS_INCLUDE_ALL_SERVICES | true | Include all services in tests |
| CHAOS_EXCLUDE_SERVICES | - | Comma-separated list of services to exclude |

## Service Health Checks

### ClickHouse Health

```bash
# Check ClickHouse node health
curl -s http://localhost:8123/ping

# Check cluster status
clickhouse-client --query "SELECT * FROM system.clusters"
```

### PostgreSQL Health

```bash
# Check PostgreSQL primary
pg_isready -h localhost -p 5432

# Check replication status
psql -h localhost -p 5432 -U langfuse -c "SELECT * FROM repmgr.nodes"
```

### Redis Health

```bash
# Check Redis master
redis-cli -h localhost -p 6379 ping

# Check sentinel status
redis-cli -h localhost -p 26379 info sentinel
```

### Vault Health

```bash
# Check Vault status
vault status

# Check Vault health
vault health
```

### MinIO Health

```bash
# Check MinIO node
mc ready local

# Check MinIO cluster status
mc admin info local
```

### OTel Collector Health

```bash
# Check OTel Collector health
curl -s http://localhost:18888/debug/health

# Check metrics
curl -s http://localhost:18888/metrics
```

### Langfuse Health

```bash
# Check Langfuse health
curl -s http://localhost:3000/api/public/health

# Check database connections
curl -s http://localhost:3000/api/debug/health
```

## Emergency Procedures

### Test Failure Response

1. **Immediate Actions**
   - Check service logs for errors
   - Verify service health using the health checks above
   - If service is down, manually restart if necessary

2. **Investigation**
   - Review the chaos engineering report
   - Check SLO violations in the report
   - Examine service metrics during the test

3. **Recovery**
   - If automatic recovery failed, manually restore services
   - Update SLO thresholds if needed
   - Re-run the scenario after recovery

### Service Recovery Commands

```bash
# Restart ClickHouse node
docker restart clickhouse1

# Restart PostgreSQL primary
docker restart pg-primary

# Restart Vault node
docker restart vault-1

# Restart Redis master
docker restart redis-master

# Restart MinIO node
docker restart minio1

# Restart OTel Collector
docker restart otel-collector-1

# Restart Langfuse node
docker restart langfuse-1
```

## Monitoring and Alerting

### Metrics to Monitor

1. **Service Availability**
   - ClickHouse cluster status
   - PostgreSQL replication lag
   - Redis master status
   - Vault cluster health
   - MinIO cluster status
   - OTel Collector health
   - Langfuse database connections

2. **Performance Metrics**
   - Query latency across all services
   - Memory and CPU usage
   - Network latency
   - Disk I/O

3. **Chaos Engineering Metrics**
   - Test success rate
   - Recovery time
   - SLO violations
   - Error rates

### Alerting Rules

1. **Critical Alerts**
   - Service availability < 99.0%
   - Recovery time > 5 minutes
   - Multiple SLO violations
   - Test failure rate > 20%

2. **Warning Alerts**
   - Service availability < 99.5%
   - Recovery time > 2 minutes
   - Single SLO violation
   - Test failure rate > 10%

## Troubleshooting

### Common Issues

1. **Docker Connection Issues**
   ```bash
   # Check Docker daemon
   docker info
   
   # Restart Docker if needed
   sudo systemctl restart docker
   ```

2. **Service Health Check Failures**
   ```bash
   # Check service logs
   docker logs <service_name>
   
   # Check service status
   docker ps | grep <service_name>
   ```

3. **SLO Validation Issues**
   ```bash
   # Check metrics collection
   curl -s http://localhost:18888/metrics | grep chaos
   
   # Verify SLO thresholds
   echo $CHAOS_SLO_AVAILABILITY_WARNING
   ```

### Debug Mode

Enable debug logging for troubleshooting:

```bash
export CHAOS_LOG_LEVEL=DEBUG
python -m scripts.chaos.weekly_chaos --verbose
```

## Best Practices

1. **Test Timing**
   - Run tests during low-traffic periods
   - Avoid running during maintenance windows
   - Consider time zones for scheduled tests

2. **Service Dependencies**
   - Test services in dependency order
   - Allow sufficient time for recovery
   - Monitor dependent services during tests

3. **Configuration Management**
   - Keep SLO thresholds realistic
   - Regularly review configuration
   - Document changes to configuration

4. **Documentation**
   - Keep this runbook updated
   - Document any changes to services
   - Share results with the team

## Reporting

### Report Locations

- **Detailed Reports**: `docs/chaos-reports/chaos_report_YYYYMMDD_HHMMSS.json`
- **Summary Reports**: `docs/chaos-reports/chaos_summary_YYYYMMDD_HHMMSS.md`

### Report Content

1. **Summary Section**
   - Total scenarios
   - Success/failure counts
   - Success rate
   - Average recovery time

2. **Detailed Results**
   - Individual scenario results
   - SLO violations
   - Error messages
   - Recovery times

3. **Metrics Section**
   - Service availability during tests
   - Latency metrics
   - Error rates
   - Resource usage

### Report Analysis

1. **Success Criteria**
   - Success rate ≥ 80%
   - All services recover within 5 minutes
   - No critical SLO violations

2. **Failure Criteria**
   - Success rate < 80%
   - Any service fails to recover
   - Critical SLO violations detected

3. **Trend Analysis**
   - Compare results over time
   - Identify recurring issues
   - Track improvement initiatives

## Maintenance

### Regular Maintenance Tasks

1. **Monthly Review**
   - Review test results
   - Update SLO thresholds if needed
   - Adjust test schedules

2. **Quarterly Updates**
   - Update chaos scenarios for new services
   - Review configuration
   - Update documentation

3. **Annual Review**
   - Comprehensive system review
   - Plan major updates
   - Training and documentation updates

### Backup and Recovery

1. **Configuration Backup**
   ```bash
   # Backup configuration
   cp scripts/chaos/config.py /backup/path/
   
   # Backup reports
   cp -r docs/chaos-reports /backup/path/
   ```

2. **Service Recovery**
   - Follow emergency procedures above
   - Document recovery process
   - Update runbook if needed

## Contact Information

- **Chaos Engineering Team**: chaos-team@example.com
- **On-call Engineer**: oncall@example.com
- **Slack Channel**: #chaos-engineering
- **Emergency Contact**: +1-555-0123