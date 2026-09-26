# Async Eval Workers Infrastructure

This directory contains infrastructure for async LLM judge evaluations using Redis + RQ workers.

## Overview

The async eval system consists of:
- **eval-redis**: Dedicated Redis instance for eval jobs (DB 1, separate from Langfuse)
- **eval-worker**: Python workers that process LLM judge jobs from the queue
- **RQ (Redis Queue)**: Simple job queue for async processing

## Services

### eval-redis
- Redis server instance dedicated to eval jobs
- Uses database 1 to separate from main Langfuse data
- Authenticated with `EVAL_REDIS_AUTH` password

### eval-worker
- Python 3.11 workers with agent_obs dependencies
- Scale: 2 replicas by default
- Processes jobs from `eval-queue`
- Exposes HTTP server on port 8777 for health checks

## Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `EVAL_REDIS_URL` | Redis connection URL | `redis://localhost:6379/1` |
| `EVAL_QUEUE_NAME` | Queue name for jobs | `eval-queue` |
| `EVAL_WORKER_CONCURRENCY` | Concurrent jobs per worker | `4` |
| `LLM_JUDGE_MODEL` | LLM model for evaluation | `gpt-4o-mini` |
| `LLM_JUDGE_API_KEY` | OpenAI API key | (required) |
| `EMBEDDING_MODEL` | Embedding model | `text-embedding-3-small` |
| `OPENAI_API_KEY` | OpenAI API for embeddings | (required) |

## Usage

### Start Services

Using Docker Compose:
```bash
docker-compose up -d eval-redis eval-worker
```

### Manual Worker Management

If you need to run workers manually (for debugging):

```bash
# Connect to eval-redis
redis-cli -h localhost -p 6379 -a eval-redis-secret

# Check queue status
rq info eval-queue

# Enqueue a test job
rq eval-queue 'your.module:job_function' --args 'arg1' 'arg2'

# Cancel a stuck job
rq cancel eval-queue <job_id>
```

### Debugging

1. **View logs**:
   ```bash
   docker-compose logs -f eval-worker
   ```

2. **Check worker health**:
   ```bash
   curl http://localhost:8777
   ```

3. **Monitor queue**:
   ```bash
   docker-compose exec eval-redis redis-cli -a eval-redis-secret
   > eval-queue llen
   ```

4. **Cancel stuck jobs**:
   ```bash
   docker-compose exec eval-worker rq cancel eval-queue <job_id>
   ```

### Testing

1. **Test queue connectivity**:
   ```bash
   docker-compose exec eval-redis redis-cli -a eval-redis-secret ping
   ```

2. **Test worker availability**:
   ```bash
   docker-compose ps eval-worker
   ```

## Dependencies

The eval-worker includes:
- `rq` - Redis Queue
- `httpx` - Async HTTP client for LLM API calls
- `presidio-analyzer` - PII detection
- `transformers` - ML models
- `clickhouse-driver` - ClickHouse client
- `psycopg` - PostgreSQL client
- `boto3` - AWS S3 client
- `pyarrow` - Parquet file format

## Development Notes

- Workers are configured with `fail-open` policy - if dependencies fail, they continue processing
- Health checks are available on port 8777
- Logs are written to stdout for Docker log collection
- Workers auto-restart on failure (max 3 attempts)