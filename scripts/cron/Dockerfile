FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install Python dependencies
COPY pyproject.toml .
COPY requirements-dev.txt .  # If you have one
RUN pip install --no-cache-dir -e .

# Copy application code
COPY scripts/cron/drift_detection.py .
COPY agent_obs/ ./agent_obs/

# Create non-root user
RUN useradd -m -u 1000 driftuser && chown -R driftuser:driftuser /app
USER driftuser

# Expose metrics port if needed
EXPOSE 9090

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import sys; sys.exit(0)" || exit 1

# Run the drift detector
CMD ["python", "scripts/cron/drift_detection.py"]