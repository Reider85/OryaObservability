# PC30 — adaptive tail-sampler proxy.
#
# Default target builds the HTTP-only proxy: the SDK ships OTLP/HTTP JSON, so
# grpcio is not needed and the image stays small. Build the `sampler-grpc`
# target to also enable the OTLP/gRPC receiver.
FROM python:3.11-slim AS sampler-base

WORKDIR /app

COPY scripts/sampler/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# agent_obs provides the metrics and the audit event; copy the package rather
# than installing it so the proxy always runs the working tree.
COPY agent_obs /app/agent_obs
COPY scripts /app/scripts
COPY pyproject.toml /app/pyproject.toml

EXPOSE 4320 4321 9095

CMD ["python", "-m", "scripts.sampler.sampler_proxy"]


FROM sampler-base AS sampler-grpc

COPY scripts/sampler/requirements-grpc.txt /tmp/requirements-grpc.txt
RUN pip install --no-cache-dir -r /tmp/requirements-grpc.txt
