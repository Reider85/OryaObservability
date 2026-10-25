# Eval Budget Runbook (PP15)

## Overview

The eval budget system provides cost control for the LLM judge evaluation pipeline. It enforces daily spending limits with automatic rate reduction (soft limit at 80% → 5% sampling rate) and hard pause (100% → all evals stopped). The system tracks costs by mode (day/night/incident) and provides real-time monitoring via Grafana dashboard.

## Architecture

- **Budget Enforcer** (`agent_obs/eval/adaptive_sampler.py`): Core logic that reads daily cost from Redis, determines state (ok/soft/paused), and enforces sampling rules
- **Cost Tracking** (`agent_obs/eval/llm_judge_worker.py`): Worker computes cost using PriceBook or fallback rate, writes to Redis daily hashes
- **Metrics**: 
  - `eval_cost_today_usd{mode}`: Daily cost by mode (gauge, resets 00:00 UTC)
  - `eval_count_by_mode{mode}`: Daily eval count by mode (counter)
  - `eval_cost_per_eval_usd`: Average cost per eval (gauge)
  - `budget_utilization_ratio`: Current cost/budget ratio (0..1.2)
  - `eval_budget_state`: Current state (0=ok, 1=soft, 2=paused)
- **Dashboard**: `infra/grafana/dashboards/eval-cost.json` - 4 panels showing utilization, cost per eval, eval counts, and daily cost by mode
- **Alerting**: `EvalBudgetExhausted` (fires when budget ≥100% for 1m)

## Environment Variables

| Variable | Default | Purpose |
|---|---|---|
| `EVAL_DAILY_BUDGET_USD` | `20.0` | Daily budget limit in USD |
| `LLM_JUDGE_COST_PER_1M_TOKENS_USD` | `5.0` | Fallback cost per 1M tokens if PriceBook unavailable |
| `EVAL_REDIS_URL` | `redis://localhost:6379/1` | Redis URL for cost storage and budget state |

## Local Development

### Dry Run Testing

Test budget logic without real costs:

```bash
# Set low budget for testing
export EVAL_DAILY_BUDGET_USD=1.0

# Run eval tests with budget enforcement
python -m pytest tests/test_eval_adaptive_sampler.py -k "budget" -v

# Simulate cost accumulation by writing to Redis directly
redis-cli HINCRBYFLOAT agent_obs:eval:cost:2024-01-01 day 15.0
redis-cli HINCRBYFLOAT agent_obs:eval:cost:2024-01-01 night 5.0
```

### Manual Budget Override

Temporarily disable budget enforcement for debugging:

```bash
# Set budget to very high value
export EVAL_DAILY_BUDGET_USD=10000.0

# Or disable via sampler (if testing without env)
sampler = AdaptiveEvalSampler(budget_usd=float('inf'))
```

## Alert Interpretation

### When EvalBudgetExhausted fires

**What it means:**
- Eval budget has reached or exceeded 100% for 1+ minute
- All eval jobs are paused (hard stop) to prevent further spending
- Existing eval jobs continue to completion but no new ones start

**What to check:**
1. **Current costs**: View `eval-cost` dashboard, panel "Daily Cost by Mode"
   - Check which mode (day/night/incident) drove costs over budget
   - Look for unexpected spikes in incident mode costs
2. **Budget setting**: Current `EVAL_DAILY_BUDGET_USD` value
3. **Recent changes**: Any model changes, increased eval frequency, or incident activity

**Debug steps:**

1. **Check dashboard first**:
   ```bash
   curl http://localhost:3001/d/eval-cost
   ```
   - Look at "Budget Utilization Ratio" gauge (should be ≥1.0)
   - Check "Daily Cost by Mode" to see cost distribution
   - Review "Eval Count by Mode" to see if incident evals are driving costs

2. **Inspect recent audit events**:
   ```sql
   -- In ClickHouse audit_events_hot table
   SELECT 
     timestamp,
     actor,
     resource
   FROM audit_events_hot 
   WHERE action = 'eval_budget.state_change'
     AND timestamp > now() - INTERVAL '1 hour'
   ORDER BY timestamp DESC
     LIMIT 10;
   ```

3. **Check Redis cost data**:
   ```bash
   # Get today's costs
   DATE=$(date -u +%Y-%m-%d)
   redis-cli HGETALL "agent_obs:eval:cost:$DATE"
   
   # Example output:
   # 1) "day"
   # 2) "18.50"
   # 3) "night"
   # 4) "1.25"
   # 5) "incident"
   # 6) "0.75"
   ```

4. **Verify budget setting**:
   ```bash
   # Check current environment
   echo $EVAL_DAILY_BUDGET_USD
   
   # Or check in app (if running)
   curl http://localhost:9777/metrics | grep eval_budget_state
   ```

### Recovery Actions

1. **Immediate recovery** (if within budget):
   - No action needed - system automatically recovers when cost drops below 50% of budget
   - Monitor `eval_budget_state` gauge returning to 0

2. **Adjust budget** (temporary increase):
   ```bash
   # Increase budget for today
   export EVAL_DAILY_BUDGET_USD=50.0
   
   # For production, update deployment and restart services
   ```

3. **Reduce eval frequency** (permanent solution):
   - Check which mode is driving costs
   - For day mode: consider reducing `AGENT_OBS_LLM_JUDGE_RATE` below default 10%
   - For incident mode: investigate root cause of frequent security incidents

4. **Model optimization** (long-term):
   - If incident mode costs are high, review prompts for efficiency
   - Consider cheaper models for judge calls if quality allows

## Troubleshooting

### Common Issues

**Issue: Budget exhausted but no eval jobs are running**

- **Check**: Are there stuck eval jobs in RQ queue?
- **Fix**: Clear stuck jobs or increase RQ worker concurrency
- **Command**: `rq info` and `rq clear <queue_name>`

**Issue: Costs not updating in dashboard**

- **Check**: Is Redis accessible from both app and worker?
- **Fix**: Verify `EVAL_REDIS_URL` is consistent across services
- **Command**: `redis-cli ping` and check connectivity

**Issue: Budget state stuck in soft mode**

- **Check**: Has cost been above 80% for extended period?
- **Fix**: Wait for cost to drop below 50% for recovery, or adjust budget
- **Debug**: Manually reset Redis cost data for testing:
  ```bash
  DATE=$(date -u +%Y-%m-%d)
  redis-cli DEL "agent_obs:eval:cost:$DATE" "agent_obs:eval:count:$DATE"
  ```

**Issue: Worker failing to write costs to Redis**

- **Check**: Worker logs for Redis connection errors
- **Fix**: Ensure Redis is running and `EVAL_REDIS_URL` is correct
- **Impact**: Budget enforcement will continue using cached values (fail-open)

### Metrics Verification

Verify metrics are being exported correctly:

```bash
# Check budget metrics
curl http://localhost:9777/metrics | grep -E "eval_cost|budget"

# Expected output:
# eval_cost_today_usd{mode="day"} 18.5
# eval_cost_today_usd{mode="night"} 1.25
# eval_cost_today_usd{mode="incident"} 0.75
# eval_cost_per_eval_usd 10.1
# budget_utilization_ratio 1.025
# eval_budget_state 2
# eval_count_by_mode{mode="day"} 2
# eval_count_by_mode{mode="night"} 1
# eval_count_by_mode{mode="incident"} 1
```

### Cost Calculation Issues

If costs seem incorrect:

1. **Check PriceBook pricing**:
   ```python
   from agent_obs.cost.price_book import PriceBook
   book = PriceBook.load()
   print(f"Price book version: {book.version}")
   print(f"Model gpt-4 price: {book.price_for('gpt-4')}")
   ```

2. **Verify worker cost computation**:
   - Check worker logs for `_compute_judge_cost` output
   - Look for fallback warnings if PriceBook fails

3. **Manual cost calculation**:
   - Use `LLM_JUDGE_COST_PER_1M_TOKENS_USD` for manual verification
   - Example: 1M tokens × $5.0 = $5.00 cost

## Maintenance

### Daily Reset

- Cost metrics automatically reset at 00:00 UTC when Redis keys expire
- Budget state transitions logged in audit trail
- No manual intervention required

### Budget Planning

1. **Historical cost analysis**:
   ```sql
   -- In ClickHouse eval_results table (if cost tracking added there)
   SELECT 
     toStartOfDay(timestamp) as day,
     sum(cost_usd) as total_cost,
     count() as eval_count
   FROM eval_results
   WHERE timestamp > now() - INTERVAL '30 day'
   GROUP BY day
   ORDER BY day;
   ```

2. **Seasonal adjustments**:
   - Increase budget during incident-heavy periods
   - Reduce budget during low-activity periods

### Integration Notes

- Budget enforcement applies to all eval sources (LLM judge, embedding eval, rule-based)
- Incident evals are subject to budget limits (hard pause stops everything)
- Cost attribution by mode helps identify expensive patterns
- Audit trail tracks all budget state changes for compliance

## Testing

Run the full test suite to verify budget functionality:

```bash
# Run budget-specific tests
python -m pytest tests/test_eval_adaptive_sampler.py -k "budget" -q --tb=short 2>&1

# Run worker cost tests
python -m pytest tests/test_llm_judge_worker.py -k "cost" -q --tb=short 2>&1

# Run all eval tests (excluding HA/Load)
python -m pytest --ignore=tests/ha --ignore=tests/load -q --tb=short 2>&1
```