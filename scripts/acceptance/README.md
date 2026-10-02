# PC37 Critical Acceptance Tests

This directory contains measurement harnesses for PC37 - CRITICAL level acceptance verification per `analytics/CRITICAL-PROMPTS.md:1650-1735`.

## Overview

PC37 requires measuring 14 exit criteria and producing `docs/critical-acceptance.md`. The tests follow the principle: "Ничего не дорабатываешь — только измеряешь и фиксируешь" (no development, only measure and record).

## Test Structure

- `pc37_harness.py` - Main harness that runs all 14 tests
- `run_test.py` - Individual test runner
- `run_tests.bat` - Windows batch runner for all tests
- `config.json` - Configuration and thresholds
- `README.md` - This file

## Running Tests

### All Tests
```bash
# Run all tests and generate report
python scripts/acceptance/pc37_harness.py

# Or use the batch script
scripts/acceptance/run_tests.bat
```

### Individual Tests
```bash
# Run specific test (e.g., C1)
python scripts/acceptance/run_test.py c1

# Available tests: c1, c2, c3, c4, c5, c6, c7, c8, c9, c10, c11, c12, c13, c14
```

## Test Criteria

| ID | Description | Target | Method |
|----|-------------|--------|--------|
| C1 | SLO covered by alerting | 100% | promtool check rules + grep |
| C2 | MTTR of incidents | < 30 min | Vault stop/start measurement |
| C3 | PII leakage in logs | 0 cases | Log scanning for PII patterns |
| C4 | Drift detection latency | < 15 min | Drift simulation with timing |
| C5 | GDPR Data Map readiness | < 5 min | Data map access and export timing |
| C6 | Guardrail latency | < 5 ms p99 | Prometheus guardrail metrics |
| C7 | PII recall on guardrail | > 95% | PII detection test |
| C8 | Injection F1 score | > 0.90 | Prompt injection detection |
| C9 | Eval latency impact | 0 ms | Agent response comparison |
| C10 | Eval annotation latency | < 30 sec p95 | Prometheus eval metrics |
| C11 | Hot query latency | < 1 sec p95 | ClickHouse hot queries |
| C12 | Storage cost | < $0.5/GB/month | Tiered storage calculation |
| C13 | Overhead reduction | > 50% | Dropped spans comparison |
| C14 | Ideality | >= 1.5 | Ideality calculation |

## Prerequisites

1. Infrastructure must be running:
   ```bash
   docker compose up -d
   ```

2. Required Python packages:
   ```bash
   pip install prometheus-api-client clickhouse-driver pandas numpy requests
   ```

3. Ensure all services are healthy:
   - Prometheus: http://localhost:9091
   - Langfuse: http://localhost:3000
   - ClickHouse: localhost:8123
   - PostgreSQL: localhost:5432
   - Vault: http://localhost:8201

## Output

The main harness generates `docs/critical-acceptance.md` with:
- Overall verdict: "CRITICAL ПРИНЯТ" or "CRITICAL НЕ ПРИНЯТ"
- Detailed results for all 14 criteria
- Pass/fail status for each criterion

## Troubleshooting

1. **Connection errors**: Ensure all infrastructure services are running
2. **Missing metrics**: Check that services are properly instrumented
3. **Permission errors**: Ensure Docker services are accessible
4. **Timeout errors**: Increase timeout values in harness if needed

## Integration

This harness is designed to be integrated into CI/CD pipelines for automated CRITICAL level acceptance testing.