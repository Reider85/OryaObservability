#!/usr/bin/env python3
"""Adaptive OTLP sampler proxy (PC30, ticket T2.7.2).

Why a proxy at all: the OTel Collector interpolates
``${env:TAIL_SAMPLER_NORMAL_RATE}`` once at process start and exposes no admin
API for reloading it, so the normal-trace rate cannot be changed without a
container restart.  The proxy is the "mini-proxy sampler" (MVP P20 option B):
it sits in the trace path, applies the current rate, and forwards the traces it
keeps.

    SDK ──OTLP/HTTP──▶ proxy ──OTLP/HTTP──▶ Langfuse (or the Collector)
                          ▲
                          │ rate changes pushed by
                    PolicyEngine (30s tick, reads PC29 gauges)

The SDK ships OTLP/HTTP JSON, so the HTTP receiver is the primary path and the
gRPC receiver is an optional extra that is only imported when
``--grpc``/``SAMPLER_PROXY_GRPC`` is enabled — the base image then does not need
grpcio.

Decisions are per *trace*, not per span: a batch from ``_export_worker`` mixes
spans of many traces, and dropping a trace's root span while keeping its child
would leave an orphan.  A trace containing an error, an over-budget cost or a
security incident is always kept (the Collector's own P20 policies do the same,
and PC0 rule 1 keeps the contract intact).  The probabilistic branch hashes the
trace id rather than drawing a random number, so a retried batch gets the same
verdict instead of flipping.

``--mode file`` writes the rate to a shared file for a Collector that is
restarted out-of-band; ``--mode proxy`` (default) needs no restart at all.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import logging
import os
import signal
import struct
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import httpx
from prometheus_client import start_http_server

from agent_obs.guardrail.audit import SAMPLER_RATES
from agent_obs.metrics import tail_sampler_current_rate, tail_sampler_traces_sampled_total
from scripts.sampler.policy_engine import PolicyDecision, PolicyEngine

logger = logging.getLogger(__name__)

DEFAULT_HTTP_PORT = 4321
DEFAULT_GRPC_PORT = 4320
# 9095, not 9091: Prometheus already publishes 9091 in infra/docker-compose.yml.
DEFAULT_METRICS_PORT = 9095
MAX_BODY_BYTES = 16 * 1024 * 1024  # 16 MiB, matches Collector otlp receiver

# Span attributes that make a trace unconditionally interesting. Same keys the
# Collector's tail_sampling policies match on, so behaviour is identical
# whichever path a trace takes.
ALWAYS_KEEP_ATTRIBUTES = ("cost.over_budget", "security.incident")
ALWAYS_KEEP_ATTRIBUTE_VALUE = "true"

# OTLP status code for ERROR (opentelemetry.proto.trace.v1.Status.STATUS_CODE_ERROR)
OTLP_STATUS_ERROR = 2


def extract_spans(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten every span in an OTLP ExportTraceServiceRequest payload.

    Walks ``resourceSpans[*].scopeSpans[*].spans[*]``.  Malformed entries are
    skipped rather than raising: one bad span must not drop a whole batch.
    """
    spans: list[dict[str, Any]] = []
    for resource_span in payload.get("resourceSpans") or []:
        if not isinstance(resource_span, dict):
            continue
        for scope_span in resource_span.get("scopeSpans") or []:
            if not isinstance(scope_span, dict):
                continue
            for span in scope_span.get("spans") or []:
                if isinstance(span, dict):
                    spans.append(span)
    return spans


def _attribute_map(span: dict[str, Any]) -> dict[str, Any]:
    """Collapse an OTLP ``attributes`` array into a plain dict.

    Only the value's first populated field is read, which is all the sampler
    needs — the attributes it inspects are all string-typed.
    """
    result: dict[str, Any] = {}
    for attribute in span.get("attributes") or []:
        if not isinstance(attribute, dict):
            continue
        key = attribute.get("key")
        if key is None:
            continue
        value = attribute.get("value")
        if isinstance(value, dict):
            for field in ("stringValue", "intValue", "boolValue", "doubleValue"):
                if field in value:
                    result[key] = value[field]
                    break
        else:
            result[key] = value
    return result


def is_interesting_span(span: dict[str, Any]) -> bool:
    """True when a single span must be kept regardless of the sampling rate.

    Matches the Collector's P20 policies: an error status, an over-budget cost
    or a security incident.  ``status.code`` is compared numerically as well as
    by name because the OTLP JSON encoding accepts both.
    """
    status = span.get("status")
    if isinstance(status, dict):
        code = status.get("code")
        if code == OTLP_STATUS_ERROR or code == "STATUS_CODE_ERROR":
            return True

    attributes = _attribute_map(span)
    for key in ALWAYS_KEEP_ATTRIBUTES:
        value = attributes.get(key)
        if isinstance(value, bool):
            if value:
                return True
        elif str(value).lower() == ALWAYS_KEEP_ATTRIBUTE_VALUE:
            return True
    return False


def group_spans_by_trace(spans: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Group OTLP spans by trace id.

    Spans with no trace id are grouped under ``""`` so they are still decided
    together rather than being silently dropped.
    """
    grouped: dict[str, list[dict[str, Any]]] = {}
    for span in spans:
        grouped.setdefault(span.get("traceId", "") or "", []).append(span)
    return grouped


class AdaptiveSampler:
    """Holds the current rate and decides per-trace keep/drop.

    Thread-safe: the rate is written by the policy engine's event loop and read
    by the HTTP handler threads.
    """

    def __init__(self, initial_rate: float = SAMPLER_RATES["default"], reason: str = "default") -> None:
        self._lock = threading.Lock()
        self._rate = initial_rate
        self._reason = reason
        self._last_changed_at = time.time()
        self._last_change_reason = "initial"
        tail_sampler_current_rate.labels(policy_reason=reason).set(initial_rate)

    @property
    def current_rate(self) -> float:
        with self._lock:
            return self._rate

    @property
    def current_reason(self) -> str:
        with self._lock:
            return self._reason

    def set_rate(self, rate: float, reason: str) -> None:
        with self._lock:
            if rate == self._rate and reason == self._reason:
                return
            self._rate = rate
            self._reason = reason
            self._last_changed_at = time.time()
            self._last_change_reason = reason
        tail_sampler_current_rate.labels(policy_reason=reason).set(rate)

    def status(self) -> dict[str, Any]:
        """Snapshot for the ``/sampler/status`` endpoint (PC32 adds the rest)."""
        with self._lock:
            return {
                "current_rate": self._rate,
                "policy_reason": self._reason,
                "last_changed_at": self._last_changed_at,
                "last_change_reason": self._last_change_reason,
            }

    def decide_trace(self, trace_id: str, spans: list[dict[str, Any]]) -> str:
        """Return ``"kept_interesting"``, ``"kept_probabilistic"`` or ``"dropped"``."""
        if any(is_interesting_span(span) for span in spans):
            return "kept_interesting"
        if self._probabilistic_keep(trace_id):
            return "kept_probabilistic"
        return "dropped"

    def _probabilistic_keep(self, trace_id: str) -> bool:
        """Deterministic rate decision keyed on the trace id.

        Hashing the trace id (rather than sampling randomly) means a retried or
        duplicated batch reaches the same verdict — a trace must not be able to
        appear because a retry happened to roll differently.
        """
        rate = self.current_rate
        if rate >= 1.0:
            return True
        if rate <= 0.0:
            return False
        digest = hashlib.sha256(trace_id.encode("utf-8")).digest()
        # 64-bit slice → uniform in [0, 1).
        value = struct.unpack(">Q", digest[:8])[0] / float(1 << 64)
        return value < rate


class RateFileWriter:
    """Writes the current rate to a shared file for out-of-band consumers.

    Used by ``--mode file``: the rate is written atomically (temp file plus
    rename) so a reader never observes a truncated value.
    """

    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, rate: float, reason: str) -> None:
        payload = {"rate": rate, "reason": reason, "updated_at": time.time()}
        temp = self.path.with_suffix(self.path.suffix + ".tmp")
        temp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(temp, self.path)
        logger.info("Wrote sampling rate %.2f (%s) to %s", rate, reason, self.path)


class SamplerProxy:
    """OTLP receiver that samples normal traces and forwards the rest."""

    def __init__(
        self,
        downstream_url: str,
        public_key: str = "",
        secret_key: str = "",
        sampler: Optional[AdaptiveSampler] = None,
        timeout_s: float = 5.0,
        max_retries: int = 3,
        tls_ca: Optional[str] = None,
        client: Optional[httpx.Client] = None,
    ) -> None:
        self.downstream_url = downstream_url.rstrip("/")
        self._url = f"{self.downstream_url}/api/public/otel/v1/traces"
        self.sampler = sampler or AdaptiveSampler()
        self._timeout_s = timeout_s
        self._max_retries = max_retries
        self._tls_ca = tls_ca
        self._client = client
        self._own_client = client is None

        self._public_key = public_key
        self._secret_key = secret_key

    # --- downstream forwarding --------------------------------------------

    def _ensure_client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                timeout=self._timeout_s, verify=self._tls_ca or True
            )
            self._own_client = True
        return self._client

    def close(self) -> None:
        if self._client is not None and self._own_client:
            self._client.close()
            self._client = None

    def _auth_header(self) -> dict[str, str]:
        if not self._public_key or not self._secret_key:
            return {}
        token = base64.b64encode(
            f"{self._public_key}:{self._secret_key}".encode("utf-8")
        ).decode("ascii")
        return {"Authorization": f"Basic {token}"}

    def forward(self, payload: dict[str, Any]) -> bool:
        """POST an OTLP payload downstream, retrying transient failures.

        Returns True on success.  Mirrors the SDK exporter's retry posture: 429
        and 5xx are transient, other 4xx are not, so a malformed batch cannot
        spin for the full retry budget.
        """
        client = self._ensure_client()
        headers = {"Content-Type": "application/json", **self._auth_header()}
        body = json.dumps(payload).encode("utf-8")
        for attempt in range(self._max_retries + 1):
            try:
                response = client.post(self._url, content=body, headers=headers)
            except Exception as exc:
                if attempt >= self._max_retries:
                    logger.error("Downstream unreachable after retries: %s", exc)
                    return False
                time.sleep(min(2**attempt * 0.1, 2.0))
                continue
            if response.status_code < 400:
                return True
            if response.status_code < 500 and response.status_code != 429:
                logger.error(
                    "Downstream rejected batch permanently: HTTP %s",
                    response.status_code,
                )
                return False
            if attempt >= self._max_retries:
                logger.error(
                    "Downstream failed after retries: HTTP %s", response.status_code
                )
                return False
            time.sleep(min(2**attempt * 0.1, 2.0))
        return False

    # --- sampling ----------------------------------------------------------

    def sample_payload(self, payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, int]]:
        """Split a payload into kept traces and a decision tally.

        Returns ``(filtered_payload, counts)`` where counts holds
        ``kept_interesting`` / ``kept_probabilistic`` / ``dropped``.  The
        filtered payload preserves the resourceSpans nesting so it can be
        forwarded verbatim.
        """
        counts = {"kept_interesting": 0, "kept_probabilistic": 0, "dropped": 0}
        kept_by_resource: dict[int, list[dict[str, Any]]] = {}

        resource_spans = payload.get("resourceSpans") or []
        for resource_index, resource_span in enumerate(resource_spans):
            if not isinstance(resource_span, dict):
                continue
            for scope_span in resource_span.get("scopeSpans") or []:
                if not isinstance(scope_span, dict):
                    continue
                spans = [s for s in (scope_span.get("spans") or []) if isinstance(s, dict)]
                for trace_id, trace_spans in group_spans_by_trace(spans).items():
                    decision = self.sampler.decide_trace(trace_id, trace_spans)
                    counts[decision] += 1
                    tail_sampler_traces_sampled_total.labels(decision=decision).inc()
                    if decision == "dropped":
                        continue
                    kept_by_resource.setdefault(resource_index, []).extend(trace_spans)

        if not kept_by_resource:
            return {"resourceSpans": []}, counts

        filtered_spans: list[dict[str, Any]] = []
        for resource_index, resource_span in enumerate(resource_spans):
            if not isinstance(resource_span, dict):
                continue
            kept = kept_by_resource.get(resource_index)
            if not kept:
                continue
            # One ScopeSpans carries every kept span from this resource: the
            # SDK emits a single scope, and merging keeps the output valid OTLP
            # regardless of how spans were distributed on the way in.
            original_scopes = [
                s
                for s in (resource_span.get("scopeSpans") or [])
                if isinstance(s, dict)
            ]
            scope = original_scopes[0].get("scope") if original_scopes else None
            entry: dict[str, Any] = {
                "resource": resource_span.get("resource", {"attributes": []}),
                "scopeSpans": [{"spans": kept}],
            }
            if scope:
                entry["scopeSpans"][0]["scope"] = scope
            filtered_spans.append(entry)

        return {"resourceSpans": filtered_spans}, counts

    def handle_payload(self, payload: dict[str, Any]) -> dict[str, int]:
        """Sample then forward.  Returns the decision counts."""
        filtered, counts = self.sample_payload(payload)
        if filtered.get("resourceSpans"):
            self.forward(filtered)
        return counts


def make_handler(proxy: SamplerProxy):
    """Build the OTLP/HTTP request handler bound to ``proxy``."""

    class OtlpTraceHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "agent-obs-sampler/1.0"

        def do_POST(self) -> None:  # noqa: N802 — stdlib naming
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                self._respond(400, {"error": "empty body"})
                return
            if length > MAX_BODY_BYTES:
                # Do not drain a huge body; the client will see the rejection.
                self._respond(413, {"error": "payload too large"})
                return
            raw = self.rfile.read(length)
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError as exc:
                self._respond(400, {"error": f"invalid json: {exc}"})
                return

            counts = proxy.handle_payload(payload)
            # 200 even when everything was dropped: the proxy consumed the
            # request correctly, and the SDK exporter must not retry a batch
            # that was sampled out on purpose.
            self._respond(200, {"status": "ok", "counts": counts})

        def do_GET(self) -> None:  # noqa: N802 — stdlib naming
            if self.path.rstrip("/") in ("/sampler/status", ""):
                self._respond(200, proxy.sampler.status())
            else:
                self._respond(404, {"error": "not found"})

        def _respond(self, status: int, body: dict[str, Any]) -> None:
            encoded = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, fmt: str, *args: Any) -> None:
            logger.debug("sampler-proxy %s", fmt % args)

    return OtlpTraceHandler


def run_policy_engine_loop(
    engine: PolicyEngine,
    sampler: AdaptiveSampler,
    rate_file: Optional[RateFileWriter],
    stop_event: threading.Event,
) -> None:
    """Run the async policy engine on its own event loop in a daemon thread.

    The HTTP handlers run in their own threads, so the engine needs a loop of
    its own; a thread is simpler than sharing one and keeps the proxy
    startable from a plain ``python sampler_proxy.py``.  The interval is slept
    in short slices so a shutdown signal is observed promptly instead of after
    a full poll period.
    """
    import asyncio

    async def on_change(decision: PolicyDecision) -> None:
        sampler.set_rate(decision.rate, decision.reason)
        if rate_file is not None:
            rate_file.write(decision.rate, decision.reason)

    async def main() -> None:
        logger.info(
            "Policy engine started: prometheus=%s interval=%.0fs initial_rate=%.2f",
            engine.prometheus_url,
            engine.poll_interval,
            engine.current_rate,
        )
        while not stop_event.is_set():
            try:
                await engine.run_once(on_change)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Policy engine tick failed")
            waited = 0.0
            while waited < engine.poll_interval and not stop_event.is_set():
                await asyncio.sleep(0.25)
                waited += 0.25
        logger.info("Policy engine stopped at rate=%.2f", engine.current_rate)

    asyncio.run(main())
    # Make sure whatever the engine settled on is what the samplers publish.
    sampler.set_rate(engine.current_rate, engine.current_reason)
    if rate_file is not None:
        rate_file.write(engine.current_rate, engine.current_reason)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--downstream-url",
        default=os.environ.get("DOWNSTREAM_URL", "http://langfuse:3000"),
        help="Langfuse base URL, or the Collector when routing through it",
    )
    parser.add_argument(
        "--http-port", type=int, default=int(os.environ.get("SAMPLER_PROXY_PORT_HTTP", DEFAULT_HTTP_PORT))
    )
    parser.add_argument(
        "--grpc-port", type=int, default=int(os.environ.get("SAMPLER_PROXY_PORT_GRPC", DEFAULT_GRPC_PORT))
    )
    parser.add_argument(
        "--metrics-port", type=int, default=int(os.environ.get("SAMPLER_PROXY_METRICS_PORT", DEFAULT_METRICS_PORT))
    )
    parser.add_argument(
        "--prometheus-url", default=os.environ.get("PROMETHEUS_URL", "http://localhost:9090")
    )
    parser.add_argument(
        "--poll-interval", type=float, default=float(os.environ.get("AGENT_OBS_SAMPLER_POLL_INTERVAL", "30"))
    )
    parser.add_argument(
        "--mode",
        choices=("proxy", "file"),
        default=os.environ.get("AGENT_OBS_SAMPLER_MODE", "proxy"),
        help="proxy: apply the rate in-process; file: publish it for a Collector restart",
    )
    parser.add_argument(
        "--rate-file", default=os.environ.get("SAMPLER_RATE_FILE", "/shared/sampler_rate.json")
    )
    parser.add_argument("--grpc", action="store_true", help="also start the OTLP/gRPC receiver")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=os.environ.get("AGENT_OBS_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    sampler = AdaptiveSampler()
    proxy = SamplerProxy(
        downstream_url=args.downstream_url,
        public_key=os.environ.get("LANGFUSE_PUBLIC_KEY", ""),
        secret_key=os.environ.get("LANGFUSE_SECRET_KEY", ""),
        sampler=sampler,
        tls_ca=os.environ.get("AGENT_OBS_TLS_CA") or None,
    )
    rate_file = RateFileWriter(args.rate_file) if args.mode == "file" else None
    if rate_file is not None:
        rate_file.write(sampler.current_rate, sampler.current_reason)

    engine = PolicyEngine(
        prometheus_url=args.prometheus_url, poll_interval=args.poll_interval
    )

    start_http_server(args.metrics_port)
    httpd = ThreadingHTTPServer(("0.0.0.0", args.http_port), make_handler(proxy))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    logger.info(
        "Sampler proxy listening on :%d (metrics :%d) -> %s, mode=%s",
        args.http_port,
        args.metrics_port,
        args.downstream_url,
        args.mode,
    )

    if args.grpc:
        start_grpc_receiver(proxy, args.grpc_port)

    stop_event = threading.Event()

    def _handle_signal(signum: int, _frame: Any) -> None:
        logger.info("Received signal %s, shutting down", signum)
        stop_event.set()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    policy_thread = threading.Thread(
        target=run_policy_engine_loop,
        args=(engine, sampler, rate_file, stop_event),
        daemon=True,
    )
    policy_thread.start()

    try:
        while not stop_event.wait(timeout=1.0):
            pass
    except KeyboardInterrupt:
        stop_event.set()

    httpd.shutdown()
    proxy.close()
    logger.info("Sampler proxy stopped at rate=%.2f", sampler.current_rate)
    return 0


def start_grpc_receiver(proxy: SamplerProxy, port: int) -> None:
    """Start the optional OTLP/gRPC receiver.

    grpcio and opentelemetry-proto are imported lazily so the base image does
    not have to carry them for the HTTP path to work.
    """
    try:
        from opentelemetry.proto.collector.trace.v1 import trace_service_pb2_grpc
    except ImportError:
        logger.error(
            "grpcio/opentelemetry-proto not installed; skipping gRPC receiver. "
            "Install scripts/sampler/requirements-grpc.txt to enable it."
        )
        return

    from concurrent import futures

    import grpc

    class TraceService(trace_service_pb2_grpc.TraceServiceServicer):
        def Export(self, request: Any, context: Any) -> Any:
            from google.protobuf import json_format

            payload = json_format.MessageToDict(request)
            proxy.handle_payload(payload)
            return trace_service_pb2.ExportTraceServiceResponse()

    server = grpc.server(futures.ThreadPoolExecutor(max_workers=8))
    trace_service_pb2_grpc.add_TraceServiceServicer_to_server(TraceService(), server)
    server.add_insecure_port(f"0.0.0.0:{port}")
    server.start()
    logger.info("OTLP/gRPC receiver listening on :%d", port)


if __name__ == "__main__":
    raise SystemExit(main())
