# Chaos Engineering for Orya Observability PP52

A comprehensive chaos engineering system for testing the resilience of the Orya Observability HA stack. Implements PP52 (Weekly Chaos Engineering Game-Day) with automated testing, SLO validation, and reporting.

## 🎯 Overview

This system automatically tests the resilience of your HA infrastructure by:

- **Randomly stopping** one HA component per week
- **Monitoring SLOs** during chaos (availability >99.5%, latency p95 <5s, error budget <20%)
- **Automated recovery** and validation
- **Comprehensive reporting** with JSON and Markdown outputs
- **Notification support** for Slack/Telegram

## 🏗️ Architecture

```
┌─────────────────┐    ┌─────────────────┐    ┌─────────────────┐
│   Chaos Engine  │    │   SLO Validator  │    │   Reporter       │
│                 │    │                 │    │                 │
│ - Orchestrates  │    │ - Monitors SLOs │    │ - JSON Reports   │
│ - Manages       │    │ - Tracks        │    │ - Markdown Summs │
│   Scenarios     │    │   Violations    │    │ - Metrics        │
└─────────────────┘    └─────────────────┘    └─────────────────┘
         │                       │                       │
         ▼                       ▼                       ▼
┌─────────────────┐    ┌─────────────────┐    ┌─────────────────┐
│  Chaos Scenarios│    │   Notifier      │    │   Configuration │
│                 │    │                 │    │                 │
│ - ClickHouse    │    │ - Webhook       │    │ - SLO Thresholds │
│ - PostgreSQL    │    │ - Slack         │    │ - Schedule       │
│ - Vault         │    │ - Telegram      │    │ - Services      │
│ - Redis         │    │                 │    │                 │
│ - MinIO         │    │                 │    │                 │
│ - OTel          │    │                 │    │                 │
│ - Langfuse      │    │                 │    │                 │
└─────────────────┘    └─────────────────┘    └─────────────────┘
```

## 🚀 Quick Start

### 1. Setup

```bash
# Install dependencies
python scripts/chaos/setup.py

# Generate sample configuration
python scripts/chaos/setup.py --generate-config

# Validate setup
python scripts/chaos/setup.py --validate-only
```

### 2. Configure

```bash
# Copy and edit configuration
cp config/chaos/.env.example .env

# Edit .env with your settings
# CHAOS_ENGINEERING_ENABLED=true
# CHAOS_WEBHOOK_URL=https://hooks.slack.com/services/YOUR/WEBHOOK/URL
# CHAOS_TELEGRAM_BOT_TOKEN=bot123456789:YOUR_TOKEN_HERE
```

### 3. Run Tests

```bash
# Run all PP52 target scenarios
python -m scripts.chaos.weekly_chaos

# Run specific scenario
python -m scripts.chaos.weekly_chaos --scenario clickhouse1_chaos

# Run with verbose logging
python -m scripts.chaos.weekly_chaos --verbose

# Run in random order
python -m scripts.chaos.weekly_chaos --random
```

### 4. Schedule Tests

```bash
# Add to crontab for weekly tests
0 2 * * 0 cd /path/to/orya-observability && python scripts/chaos/cron_weekly_chaos.py
```

## 📊 Target Services (PP52)

The system focuses on these critical HA components:

| Service | Node | Purpose |
|---------|------|---------|
| ClickHouse | clickhouse-1 | Time-series data storage |
| PostgreSQL | pg-primary | Primary database |
| Vault | vault-1 | Secrets management |
| Redis | redis-1 | Cache/session storage |
| MinIO | minio-1 | Object storage |
| OTel Collector | otel-collector-1 | Telemetry collection |
| Langfuse | langfuse-1 | Observability platform |

## 🎯 SLO Thresholds

| Metric | Warning | Critical | Unit |
|--------|---------|----------|------|
| Availability | 99.5% | 99.0% | % |
| Latency p95 | 5000 | 10000 | ms |
| Error Budget | 20% | 50% | % |

## 📈 Monitoring

### Health Checks

```bash
# ClickHouse
curl -s http://localhost:8123/ping

# PostgreSQL
pg_isready -h localhost -p 5432

# Redis
redis-cli -h localhost -p 6379 ping

# Vault
vault status

# MinIO
mc ready local

# OTel Collector
curl -s http://localhost:18888/debug/health

# Langfuse
curl -s http://localhost:3000/api/public/health
```

### Metrics

The system tracks these key metrics:

- **Service Availability**: Real-time availability during tests
- **Recovery Time**: Time to restore service after chaos
- **SLO Compliance**: Percentage of SLO thresholds met
- **Error Rates**: Error rates during and after tests
- **Resource Usage**: CPU, memory, network usage

## 📋 Reports

### Report Locations

- **Detailed Reports**: `docs/chaos-reports/chaos_report_YYYYMMDD_HHMMSS.json`
- **Summary Reports**: `docs/chaos-reports/chaos_summary_YYYYMMDD_HHMMSS.md`

### Report Content

1. **Executive Summary**
   - Success rate
   - Failed scenarios
   - Average recovery time

2. **Detailed Results**
   - Individual scenario outcomes
   - SLO violations
   - Error messages
   - Recovery metrics

3. **Metrics Analysis**
   - Service availability trends
   - Performance metrics
   - Resource utilization

## 🔧 Configuration

### Environment Variables

```bash
# General settings
CHAOS_ENGINEERING_ENABLED=true
CHAOS_OUTPUT_DIR=docs/chaos-reports
CHAOS_LOG_LEVEL=INFO

# SLO thresholds
CHAOS_SLO_AVAILABILITY_WARNING=99.5
CHAOS_SLO_AVAILABILITY_CRITICAL=99.0
CHAOS_SLO_LATENCY_WARNING=5000
CHAOS_SLO_LATENCY_CRITICAL=10000
CHAOS_SLO_ERROR_BUDGET_WARNING=20
CHAOS_SLO_ERROR_BUDGET_CRITICAL=50

# Chaos settings
CHAOS_DEFAULT_DURATION_SECONDS=60
CHAOS_RECOVERY_TIMEOUT_SECONDS=300
CHAOS_RANDOM_ORDER=true

# Notifications
CHAOS_WEBHOOK_URL=https://hooks.slack.com/services/YOUR/WEBHOOK/URL
CHAOS_TELEGRAM_BOT_TOKEN=bot123456789:YOUR_TOKEN_HERE
CHAOS_TELEGRAM_CHAT_ID=123456789

# Schedule
CHAOS_SCHEDULE_ENABLED=true
CHAOS_WEEKLY_CRON="0 2 * * 0"
```

### Custom Scenarios

Create custom chaos scenarios by extending the base class:

```python
from scripts.chaos.base import ChaosScenario
from scripts.chaos.chaos_scenarios import ClickHouseChaosScenario

class CustomChaosScenario(ChaosScenario):
    def __init__(self):
        super().__init__(
            name="custom_scenario",
            services=["custom-service"],
            duration_seconds=60
        )
    
    async def execute_chaos(self):
        # Implement your chaos logic
        pass
    
    async def restore_service(self):
        # Implement restoration logic
        pass
    
    async def validate_service_health(self):
        # Implement health validation
        return True
```

## 🧪 Testing

### Unit Tests

```bash
# Run unit tests
python -m pytest tests/test_chaos_engineering -v

# Run with coverage
python -m pytest tests/test_chaos_engineering --cov=scripts/chaos -v
```

### Integration Tests

```bash
# Run integration tests (requires HA stack)
python -m pytest tests/test_chaos_engineering::TestIntegration -v
```

### Manual Testing

```bash
# Test individual scenarios
python -m scripts.chaos.weekly_chaos --scenario clickhouse1_chaos

# Test with mocked dependencies
python -m pytest tests/test_chaos_engineering -k "mock" -v
```

## 🚨 Emergency Procedures

### Test Failure Response

1. **Check service health**
   ```bash
   # Check affected service
   curl -s http://localhost:8123/ping  # ClickHouse
   pg_isready -h localhost -p 5432    # PostgreSQL
   ```

2. **Manual recovery**
   ```bash
   # Restart failed service
   docker restart clickhouse1
   ```

3. **Investigate**
   ```bash
   # Check logs
   docker logs clickhouse1
   
   # Review reports
   cat docs/chaos-reports/chaos_report_*.json
   ```

### Common Issues

1. **Docker Connection Issues**
   ```bash
   # Check Docker daemon
   docker info
   
   # Restart Docker
   sudo systemctl restart docker
   ```

2. **Service Health Check Failures**
   ```bash
   # Check service logs
   docker logs <service_name>
   
   # Verify service status
   docker ps | grep <service_name>
   ```

## 📚 Documentation

- **Runbook**: [docs/chaos-runbook.md](docs/chaos-runbook.md)
- **API Documentation**: [scripts/chaos/](scripts/chaos/)
- **Configuration**: [config/chaos/.env.example](config/chaos/.env.example)

## 🤝 Contributing

1. Fork the repository
2. Create a feature branch
3. Add tests for new scenarios
4. Update documentation
5. Submit a pull request

## 📞 Support

- **Documentation**: [docs/chaos-runbook.md](docs/chaos-runbook.md)
- **Issues**: GitHub Issues
- **Slack**: #chaos-engineering channel

## 🔒 Security

- All chaos operations are performed in isolated environments
- Service credentials are stored securely
- Access controls are enforced
- Audit logging is enabled

## 📈 Roadmap

- [ ] Add more chaos scenarios
- [ ] Implement machine learning-based chaos
- [ ] Add multi-region testing
- [ ] Enhance reporting with ML insights
- [ ] Add automated remediation

---

**Note**: This chaos engineering system is designed for production use and includes comprehensive error handling, monitoring, and recovery mechanisms. Always test in a non-production environment first.