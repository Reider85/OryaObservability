# Nightly Golden Eval Runbook (PP13)

## Overview

The nightly golden eval is a regression testing system that evaluates agent answers against a curated set of test cases (golden set) using an LLM judge. It runs daily at 03:00 UTC and provides alerting when answer quality regresses by more than 10% from the 7-day moving average.

## Architecture

- **Cron Job** (`scripts/nightly_golden_eval.py`): Runs at 03:00 UTC, loads golden set, evaluates each case, writes results to ClickHouse
- **Golden Set** (`eval/golden/*.yaml`): Curated test cases with questions, expected answers, and evaluation criteria
- **LLM Judge**: Scores agent answers using RAGAS-style prompts (faithfulness, answer_relevancy, completeness)
- **Metrics**: `agent_obs_eval_golden_pass_rate` gauge (published after each run)
- **Alerting**: `GoldenEvalRegression` (fires when pass_rate < 7d_avg - 0.10), `GoldenEvalJobStale` (fires if no success in 48h)

## Golden Set Structure

Each golden set file contains test cases in YAML format:

```yaml
version: 1
metadata:
  name: capitals
  description: "Capital city questions"
  created: "2025-01-01"
  updated: "2025-01-15"
test_cases:
  - id: "capitals-001"
    question: "What is the capital of France?"
    expected_answer: "Paris"
    metrics:
      - name: faithfulness
        threshold: 0.8
      - name: answer_relevancy
        threshold: 0.8
      - name: completeness
        threshold: 0.9
  - id: "capitals-002"
    question: "What is the capital of Japan?"
    expected_answer: "Tokyo"
    metrics:
      - name: faithfulness
        threshold: 0.8
      - name: answer_relevancy
        threshold: 0.8
      - name: completeness
        threshold: 0.9
```

### Fields

- `id`: Unique identifier for the test case
- `question`: The question to ask the agent
- `expected_answer`: The correct answer (used for faithfulness scoring)
- `metrics`: List of evaluation criteria with thresholds

### Supported Metrics

1. **faithfulness**: Does the agent's answer contain only information that can be verified from the expected answer? (binary: yes/no)
2. **answer_relevancy**: How relevant is the agent's answer to the question? (0-1 scale)
3. **completeness**: How complete is the agent's answer for the question? (0-1 scale)

## Adding New Test Cases

1. Create a new YAML file in `eval/golden/` or add to existing file
2. Follow the structure above, ensuring unique IDs
3. Set appropriate thresholds for each metric
4. Update the metadata `updated` field
5. Test locally with `python scripts/nightly_golden_eval.py --dry-run`

## Environment Variables

| Variable | Default | Purpose |
|---|---|---|
| `AGENT_OBS_CRON_GOLDEN_EVAL_SPEC` | `0 3 * * *` | Cron schedule for nightly eval |
| `AGENT_OBS_GOLDEN_AGENT_MODEL` | `gpt-4o` | Model for agent answer generation |
| `AGENT_OBS_GOLDEN_JUDGE_MODEL` | `gpt-4o-mini` | Model for answer evaluation |
| `LLM_JUDGE_API_BASE` | `https://api.openai.com/v1` | API base URL for judge |
| `OPENAI_API_KEY` | — | API key for both agent and judge |
| `LLM_JUDGE_API_KEY` | — | Separate API key for judge (falls back to OPENAI_API_KEY) |

## Local Development

### Dry Run Testing

```bash
# Run without API calls or ClickHouse writes
python scripts/nightly_golden_eval.py --dry-run

# Run with specific golden set
python scripts/nightly_golden_eval.py --dry-run --golden-set eval/golden/capitals.yaml
```

### Debugging a Single Case

```bash
# Test a single case interactively
python -c "
import asyncio
from scripts.nightly_golden_eval import load_golden_set, judge_case

async def debug():
    golden_set = await load_golden_set('eval/golden/capitals.yaml')
    case = golden_set[0]  # First case
    print(f'Question: {case.question}')
    print(f'Expected: {case.expected_answer}')
    # Add your agent call here for testing

asyncio.run(debug())
"
```

## Alert Interpretation

### GoldenEvalRegression

**When it fires**: The pass rate dropped more than 10% below the 7-day average

**What to check**:
1. Recent changes to agent prompts or configuration
2. Model API performance issues affecting answer quality
3. New golden set cases that are particularly difficult
4. Judge model changes affecting scoring consistency

**Debug steps**:
```sql
-- View recent failures
SELECT * FROM eval_golden_results 
WHERE pass = 0 
ORDER BY nightly_ts DESC 
LIMIT 10;

-- Check pass rate trend
SELECT 
    toStartOfDay(nightly_ts) as day,
    avg(pass) as pass_rate,
    count() as total_cases
FROM eval_golden_results 
GROUP BY day
ORDER BY day DESC;
```

### GoldenEvalJobStale

**When it fires**: No successful run in more than 48 hours

**What to check**:
1. Cron job logs: `docker logs agent-obs-cron`
2. API credentials: `OPENAI_API_KEY` and `LLM_JUDGE_API_KEY`
3. ClickHouse connectivity
4. Agent or judge model availability

## Troubleshooting

### Common Issues

**API Key Errors**
```
ERROR: openai.APIConnectionError
```
- Check `OPENAI_API_KEY` and `LLM_JUDGE_API_KEY` in cron service environment
- Verify API base URL is accessible

**ClickHouse Write Failures**
```
ERROR: clickhouse.OperationalError
```
- Check ClickHouse service health: `docker ps | grep clickhouse`
- Verify database credentials in cron service

**Empty Results**
```
WARNING: No results written to ClickHouse
```
- Check agent answer generation (may be failing silently)
- Verify judge client is not returning empty responses

### Metrics Verification

Check that the gauge is being published:
```bash
curl http://localhost:9777/metrics | grep agent_obs_eval_golden_pass_rate
```

Expected output:
```
agent_obs_eval_golden_pass_rate 0.95
```

## Maintenance

### Updating Golden Set

1. Review failed cases from recent runs
2. Add new challenging cases
3. Update existing cases if expected answers change
4. Remove obsolete cases
5. Test with dry-run before deploying

### Model Configuration

- Periodically review if agent/judge models are still appropriate
- Consider cost/performance trade-offs (e.g., switching to cheaper models)
- Test model changes with dry-run before production deployment

### Retention

- ClickHouse table `eval_golden_results` has 90-day TTL
- Historical data is automatically cleaned up
- For long-term analysis, export to cold storage before deletion

## Integration

The nightly golden eval integrates with:

- **Prometheus**: Long-lived gauge for scraping by alerting rules
- **ClickHouse**: Results storage for analysis and debugging
- **Langfuse**: Optional integration for detailed answer evaluation
- **Grafana**: Dashboard for pass rate trends and failure analysis

See the main observability dashboards for golden eval metrics and alert status.