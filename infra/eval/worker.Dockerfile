FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
RUN pip install --no-cache-dir \
    rq \
    httpx \
    pyyaml \
    prometheus-client \
    presidio-analyzer \
    transformers \
    pyotp \
    hvac \
    clickhouse-driver \
    psycopg \
    boto3 \
    pyarrow \
    torch

# Copy agent_obs package
COPY ../agent_obs/ /app/agent_obs/

# Copy requirements if available
COPY ../pyproject.toml /app/
COPY ../README.md /app/

# Install the package
RUN pip install -e /app/

# Expose metrics port
EXPOSE 8777

# Health check endpoint (simple HTTP server for health checks)
CMD ["sh", "-c", "python -m http.server 8777 --bind 0.0.0.0 & rq worker eval-queue --url redis://eval-redis:6379/1 --worker-class 'rq.worker.SimpleWorker' --name eval-worker-1"]