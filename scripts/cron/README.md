# Drift Detection Cron Script

PC25: KL-divergence detector comparing last hour vs 7-day baseline.
PC26: monthly calibration of the alert threshold as p99 of the 30-day KL history.

## Overview

This script runs drift detection every 15 minutes for configured agents. It compares the distribution of LLM response embeddings from the last hour against a 7-day baseline using KL-divergence analysis.

The alert threshold is **not** hardcoded. It is calibrated monthly (PC26) as the p99 of each agent's own 30-day KL history and stored in `configs/drift_thresholds.yaml`, which the detector re-reads on every run — a calibration takes effect without restarting the agent or the cron service.

## Configuration

Environment variables:

| Variable | Default | Description |
|---|---|---|
| `DRIFT_INTERVAL_SECONDS` | `900` | Run interval in seconds (15 minutes) |
| `DRIFT_BASELINE_HOURS` | `168` | Baseline window in hours (7 days) |
| `DRIFT_LAST_WINDOW_HOURS` | `1` | Current window in hours (1 hour) |
| `DRIFT_KL_THRESHOLD` | *(unset)* | Optional operator pin. When unset, the calibrated per-agent value is used |
| `DRIFT_AGENT_IDS` | `default_agent` | Comma-separated list of agent IDs to monitor |
| `CLICKHOUSE_HOST` | `localhost` | ClickHouse host |
| `CLICKHOUSE_PORT` | `9000` | ClickHouse port |
| `CLICKHOUSE_DB` | `observability` | ClickHouse database |
| `CLICKHOUSE_USER` | `observability_user` | ClickHouse username |
| `CLICKHOUSE_PASSWORD` | `observability_password` | ClickHouse password |

The same knobs are available under the `AGENT_OBS_CRON_DRIFT_*` names when the job runs from `scheduler.py` — see `infra/.env.example`.

## Running

```bash
# Development mode (foreground)
python scripts/cron/drift_detection.py

# Production mode (background)
nohup python scripts/cron/drift_detection.py > drift_detection.log 2>&1 &
```

## Behavior

### Sample Size Threshold
The detector skips analysis if fewer than 100 embeddings are found in the last hour window to ensure statistical significance.

### Drift Detection
- **KL-divergence score** > threshold → drift detected
- **Severity levels**:
  - `info`: KL < threshold
  - `warning`: 2× ≤ KL < 3× threshold
  - `critical`: KL ≥ 3× threshold

### Data Storage
- Drift reports are written to `drift_history` table in ClickHouse
- Prometheus metrics are emitted for monitoring
- Failed writes are logged but don't stop detection

### Logging
- INFO: Normal operation, no drift detected
- WARNING: Drift detected
- ERROR: Failed operations

## Threshold Calibration (PC26)

`scripts/cron/calibrate_drift_threshold.py` runs monthly via `scheduler.py`.

```bash
# Preview the new thresholds without writing them
python scripts/cron/calibrate_drift_threshold.py --dry-run

# Calibrate specific agents
python scripts/cron/calibrate_drift_threshold.py --agents default_agent,chat_agent

# Calibrate from a partial window (skips the 30-day completeness guard)
python scripts/cron/calibrate_drift_threshold.py --allow-partial-window
```

**How it works.** For each agent it reads the KL scores from the `drift_history` table over the trailing 30 days, takes the p99, and writes the result to `configs/drift_thresholds.yaml`. p99 puts the threshold at the edge of observed normal behaviour, so only a genuinely unusual hour alerts.

**Why `drift_history` and not the `drift_kl_score` metric.** The metric is a gauge: it holds the latest value and each scrape overwrites it. There is no history to take a p99 over, so the table written by PC25 on every run is the source of truth.

**Refuses to calibrate on partial data.** If history spans less than the full window, or has fewer than 100 usable samples, the agent keeps the 0.1 default and the job logs why. A p99 derived from three days of data is worse than no calibration: it would permanently blind the detector to real drift.

**Quarterly baseline sanity check.** Each calibration also samples 100 known-good runs (scored, never flagged as drift) and counts how many now exceed the new threshold. If more than 10% do, the job logs `BASELINE STALE` — the baseline window has itself drifted, and a p99 taken from it would ratchet the threshold upward every month until nothing ever alerts. That condition needs a human to rebuild the baseline from verified-good traces; the job deliberately does not do it automatically.

**Atomic writes.** The thresholds file is written via temp file + rename, so a detector reading it concurrently never sees a half-written document. Re-running on unchanged history is idempotent.

### Calibration Metrics
- `agent_obs_drift_threshold_value{agent_id}`: Active threshold in force (gauge)
- `agent_obs_drift_threshold_calibrated_at{agent_id}`: Unix timestamp of the last successful calibration (gauge)
- `agent_obs_drift_calibration_runs_total{agent_id,outcome}`: Runs by outcome — `calibrated`, `insufficient_history`, `no_data`, `error` (counter)
- `agent_obs_drift_sanity_check_total{agent_id,result}`: Baseline check results — `ok`, `stale` (counter)

## Integration with Docker Compose

Add to your `docker-compose.yml`:

```yaml
services:
  drift-detector:
    build:
      context: .
      dockerfile: scripts/cron/Dockerfile
    environment:
      - CLICKHOUSE_HOST=clickhouse
      - CLICKHOUSE_PORT=9000
      - CLICKHOUSE_DB=observability
      - CLICKHOUSE_USER=observability_user
      - CLICKHOUSE_PASSWORD=observability_password
      - DRIFT_AGENT_IDS=default_agent,agent1,agent2
    depends_on:
      - clickhouse
    restart: unless-stopped
```

## Monitoring

### Prometheus Metrics
- `agent_obs_drift_kl_score`: Current KL-divergence score (gauge)
- `agent_obs_drift_runs_total`: Total detection runs (counter)
- `agent_obs_drift_alerts_total`: Alerts generated by severity (counter)
- `agent_obs_drift_threshold_value`: Active calibrated threshold (gauge)

### Alert Rules
Add to `infra/prometheus-rules.yml`. Compare against the calibrated threshold
gauge rather than a hardcoded constant, so the rule follows calibration:

```yaml
groups:
  - name: drift-detection
    rules:
      - alert: DriftDetected
        expr: agent_obs_drift_kl_score > on(agent_id) agent_obs_drift_threshold_value
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "Drift detected for agent {{ $labels.agent_id }}"
          description: "KL={{ $value }}"
```

## Testing

Run tests:
```bash
python -m pytest tests/test_drift_detection.py tests/test_drift_threshold_calibration.py -v
```

## Exit Conditions

- Normal: `Ctrl+C` (KeyboardInterrupt)
- Error: Continues running with exponential backoff
- Configuration errors: Logs and continues