"""LLMProxyServer — FastAPI proxy for OpenAI-compatible chat completions (PP40).

The proxy sits between an agent's LLM client and the upstream provider
(OpenAI / Anthropic / Azure / custom).  The agent points its client at
``http://<host>:<port>/llm/v1`` with a dummy API key; the proxy injects the
real upstream key from ``config.upstream_api_key_env``, forwards the request,
and records an ``llm.proxy.call`` span (fire-and-forget via ``sdk._enqueue``).

Design notes
------------
* FastAPI / uvicorn are an *optional* dependency (``pip install "agent-obs[api]"``).
  The SDK imports this module lazily inside a ``try/except ImportError``
  (``ObservabilitySDK.__init__``), so a missing extra degrades to a warning.
* Bodies, tokens, cost and streaming timing are extracted by ``ObsPipeline``
  (PP41, ``agent_obs/proxy/obs_pipeline.py``): bodies are stored on the span
  under ``llm.input_text`` / ``llm.output_text`` so the in-process guardrail
  masks PII before anything leaves the process (architectural anti-pattern
  §8.3), and content sampling (``attach_llm_content``) applies the
  ``AGENT_OBS_CONTENT_RATE`` policy.  Enrichment failures are swallowed by
  ``_enrich_span`` — they can never fail a proxied request.
* ``forward_to_upstream`` / ``build_proxy_span`` are separate functions so
  PP43 (failover/retries) can slot in without rewriting ``chat_completions``.
* mTLS (``AGENT_OBS_PROXY_TLS``) is not yet in ``ProxyConfig`` — out of scope
  for PP40.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import AsyncIterator, Mapping
from typing import TYPE_CHECKING, Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from agent_obs.cost.attribution import (
    BUDGET_APPROACHING,
    BUDGET_EXHAUSTED,
    BudgetEnforcer,
    CostAttributionTracker,
    attribution_from_headers,
)
from agent_obs.guardrail.audit import ProxyFallbackAuditEvent
from agent_obs.ha.health_checks import CircuitBreaker
from agent_obs.observability import (
    Span,
    SpanContext,
    SpanType,
    _new_ulid,
    _now,
)
from agent_obs.proxy import ProxyConfig
from agent_obs.proxy.obs_pipeline import ObsPipeline, Timing
from agent_obs.proxy.shared_pipeline import SharedPipeline
from agent_obs.proxy.security_client import SecurityClient
from agent_obs.proxy.decision_router import DecisionRouter

from agent_obs.proxy.semantic_cache import get_semantic_cache

if TYPE_CHECKING:  # pragma: no cover
    from agent_obs.observability import ObservabilitySDK

logger = logging.getLogger(__name__)

# Headers that must not be forwarded to the upstream (RFC 9110 hop-by-hop +
# the proxy's own auth injection).  X-OryaObs-* headers are proxy metadata and
# are stripped too — the upstream has no use for them.
_HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "host",
        "content-length",
        "authorization",
    }
)

# Cap on stream-chunk span events so a pathological stream cannot balloon
# span memory (events beyond the cap are still counted in llm.stream_chunks).
_MAX_STREAM_EVENTS = 1000

# Raw SSE chunks carrying usage / finish_reason are retained separately from
# the body-cap buffer so PP41 enrichment can always parse them — the final
# usage chunk may arrive after ``log_bodies_max_bytes`` was already reached.
_MAX_USAGE_CHUNK_BYTES = 64 * 1024

_UPSTREAM_TIMEOUT = "upstream_timeout"
_UPSTREAM_CONNECTION = "upstream_connection_error"
_PROXY_INTERNAL = "proxy_internal_error"


def _is_usage_chunk(chunk: bytes) -> bool:
    """True if an SSE chunk carries the fields PP41 must parse."""
    return b'"usage"' in chunk or b'"finish_reason"' in chunk


def extract_headers(headers: Mapping[str, str]) -> tuple[str, str, str, str]:
    """Pull the X-OryaObs-* propagation headers (PP40 §4).

    Returns ``(trace_id, user_id, tenant_id, agent_id)``.  A client-provided
    ``X-OryaObs-Trace-Id`` is reused so the proxy span joins the caller's
    trace; otherwise a fresh id is generated.
    """
    trace_id = headers.get("X-OryaObs-Trace-Id", "").strip() or _new_ulid()
    user_id = headers.get("X-OryaObs-User-Id", "").strip()
    tenant_id = headers.get("X-OryaObs-Tenant-Id", "").strip()
    agent_id = headers.get("X-OryaObs-Agent-Id", "").strip()
    return trace_id, user_id, tenant_id, agent_id


def forward_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Client headers safe to forward upstream (auth is replaced, not passed)."""
    out: dict[str, str] = {}
    for key, value in headers.items():
        lower = key.lower()
        if lower in _HOP_BY_HOP or lower.startswith("x-oryaobs-"):
            continue
        out[key] = value
    return out


def _parse_body(raw: bytes) -> dict[str, Any]:
    """Best-effort JSON parse of the request body (invalid JSON → {})."""
    try:
        parsed = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


class LLMProxyServer:
    """FastAPI-based LLM proxy with span interception (PP40).

    Lifecycle: construct with the resolved ``ProxyConfig`` (and optionally the
    SDK for span export), then ``await start()`` / ``await stop()``.  The SDK
    wires this automatically when ``AGENT_OBS_PROXY_MODE`` resolves to
    enabled and calls start/stop from ``ObservabilitySDK.start()/shutdown()``.
    """

    def __init__(self, config: ProxyConfig, sdk: ObservabilitySDK | None = None) -> None:
        self.config = config
        self.sdk = sdk
        # PP42: tenant-aware cost attribution + budget enforcement. The
        # tracker's background flush cycle starts in start() (needs a loop).
        self.cost_tracker = CostAttributionTracker()
        self.budget = BudgetEnforcer(self.cost_tracker)
        
        # PP44: shared proxy mode detection
        self.shared_enabled = getattr(config, 'shared_proxy_enabled', False)
        self.shared_security_url = getattr(config, 'shared_proxy_security_url', '')
        
        # Initialize pipelines based on mode
        if self.shared_enabled and self.shared_security_url:
            # Mode B: shared obs + security proxy
            self.security_client = SecurityClient(
                base_url=self.shared_security_url,
                timeout=getattr(config, 'shared_proxy_security_timeout', 0.05),
                fail_open=getattr(config, 'shared_proxy_security_fail_open', True),
            )
            self.shared_pipeline = SharedPipeline(
                security_client=self.security_client,
                obs_pipeline=ObsPipeline(config, cost_tracker=self.cost_tracker),
                sdk=sdk,
            )
            self.decision_router = DecisionRouter(
                audit_logger=getattr(sdk, "_audit_trail", None) if sdk else None,
            )
            self.pipeline = self.shared_pipeline  # Use shared pipeline
        else:
            # Mode A: observability-only proxy
            self.pipeline = ObsPipeline(config, cost_tracker=self.cost_tracker)
            self.security_client = None
            self.shared_pipeline = None
            self.decision_router = None
        
        self._client: httpx.AsyncClient | None = None
        self._server: Any = None  # uvicorn.Server
        self._server_task: asyncio.Task | None = None

        # PP43: upstream failover
        self.upstream_breaker = CircuitBreaker("llm_upstream")
        self.cache = get_semantic_cache(config)
        # Initialize semantic cache if enabled
        if self.cache is not None:
            asyncio.create_task(self.cache.initialize())
        # For testability: allow sleep injection to avoid real delays
        self._sleep = asyncio.sleep

        self.app = FastAPI(title="OryaObservability LLM Proxy")
        
        # Add main proxy route
        self.app.add_api_route(
            "/llm/v1/chat/completions",
            self.chat_completions,
            methods=["POST"],
        )
        
        # PP48: Add audit trail UI routes (only in shared proxy mode)
        if self.shared_enabled and self.shared_security_url:
            from agent_obs.audit_trail.ui import router as audit_trail_router
            self.app.include_router(audit_trail_router, prefix="/admin")

    # Sentinel for upstream unavailable
    class _UpstreamUnavailable(Exception):
        def __init__(self, reason: str):
            self.reason = reason
            super().__init__(f"Upstream unavailable: {reason}")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Bind the listen socket in a background task (never blocks the caller's flow).

        Waits until uvicorn has actually bound the port so ``stop()`` and
        tests see a ready server.  A bind failure (port in use) raises so the
        operator notices instead of silently running without a proxy.
        """
        if self._server_task is not None:
            return
        import uvicorn

        uv_config = uvicorn.Config(
            self.app,
            host=self.config.listen_host,
            port=self.config.listen_port,
            log_level="warning",
        )
        self._server = uvicorn.Server(uv_config)
        self._server_task = asyncio.create_task(self._server.serve())
        # PP42: begin the cost-attribution flush cycle alongside the server.
        self.cost_tracker.start()
        while not self._server.started:
            if self._server_task.done():
                # Propagate the bind/serve error.
                await self._server_task
                self._server_task = None
                self._server = None
                raise RuntimeError(
                    "LLM proxy server failed to start "
                    f"({self.config.listen_host}:{self.config.listen_port})"
                )
            await asyncio.sleep(0.01)
        logger.info(
            "LLM proxy listening on %s:%s (upstream=%s)",
            self.config.listen_host,
            self.bound_port,
            self.config.upstream_base_url,
        )

    async def stop(self) -> None:
        """Gracefully stop the proxy: drain in-flight requests, close the client."""
        # PP42: stop the cost-attribution worker first so the final flush
        # lands before the process tears down its stores.
        try:
            await self.cost_tracker.stop()
        except Exception:  # pragma: no cover - best-effort teardown
            logger.warning("LLM proxy cost tracker stop failed", exc_info=True)
        
        # PP44: close shared proxy resources
        if self.shared_pipeline is not None:
            try:
                await self.shared_pipeline.close()
            except Exception:  # pragma: no cover - best-effort teardown
                logger.warning("Shared pipeline stop failed", exc_info=True)
        
        if self._server is not None:
            self._server.should_exit = True
        if self._server_task is not None:
            try:
                await asyncio.wait_for(self._server_task, timeout=10.0)
            except Exception:
                logger.warning("LLM proxy server stop did not finish cleanly", exc_info=True)
                self._server_task.cancel()
            self._server_task = None
            self._server = None
        # PP54: close semantic cache
        if self.cache is not None:
            try:
                await self.cache.close()
            except Exception:  # pragma: no cover - best-effort teardown
                logger.warning("Semantic cache close failed", exc_info=True)
            self.cache = None
            
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:  # pragma: no cover - best-effort teardown
                logger.warning("LLM proxy httpx client close failed", exc_info=True)
            self._client = None

    @property
    def bound_port(self) -> int:
        """The port actually bound (useful when ``listen_port=0`` in tests)."""
        if self._server is not None and self._server.servers:
            try:
                return int(self._server.servers[0].sockets[0].getsockname()[1])
            except (IndexError, OSError):  # pragma: no cover - defensive
                pass
        return self.config.listen_port

    # ------------------------------------------------------------------
    # HTTP client
    # ------------------------------------------------------------------

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.config.upstream_base_url.rstrip("/"),
                timeout=httpx.Timeout(self.config.request_timeout_seconds),
            )
        return self._client

    def _upstream_auth_headers(self) -> dict[str, str]:
        """Inject the real upstream key; the client's key is never forwarded."""
        api_key = os.environ.get(self.config.upstream_api_key_env, "")
        return {"Authorization": f"Bearer {api_key}"}

    async def forward_to_upstream(
        self,
        body: bytes,
        headers: Mapping[str, str],
        request_id: str,
    ) -> httpx.Response:
        """Forward a non-streaming request to the upstream provider."""
        client = self._get_client()
        fwd = forward_headers(headers)
        fwd.update(self._upstream_auth_headers())
        fwd["X-Request-Id"] = request_id
        return await client.post("/chat/completions", headers=fwd, content=body)

    # ------------------------------------------------------------------
    # Span building
    # ------------------------------------------------------------------

    async def _enrich_span(
        self,
        span: Span,
        *,
        request_body: bytes,
        response_body: bytes | None,
        stream_chunks: list[bytes] | None,
        headers: Mapping[str, str],
        timing: Timing,
    ) -> None:
        """Run ObsPipeline enrichment, never letting it fail the request (PP41)."""
        try:
            await self.pipeline.enrich_span(
                span,
                request_body=request_body,
                response_body=response_body,
                stream_chunks=stream_chunks,
                headers=headers,
                timing=timing,
            )
        except Exception:  # pragma: no cover - defensive
            logger.warning(
                "Proxy span enrichment failed (span=%s)",
                span.context.span_id,
                exc_info=True,
            )

    def build_proxy_span(
        self,
        *,
        request_body: bytes,
        trace_id: str,
        user_id: str,
        tenant_id: str,
        agent_id: str,
        stream: bool,
        start_time: float,
    ) -> Span:
        """Create the ``llm.proxy.call`` span (bodies attached later)."""
        parsed = _parse_body(request_body)
        model = parsed.get("model", "")
        context = SpanContext(
            trace_id=trace_id,
            span_id=_new_ulid(),
            agent_id=agent_id or "proxy",
            user_id=user_id,
            tenant_id=tenant_id,
        )
        span = Span(
            name=f"llm.proxy.call:{model}" if model else "llm.proxy.call",
            span_type=SpanType.LLM_PROXY_CALL,
            context=context,
            attributes={
                "llm.provider": self.config.upstream_provider,
                "llm.stream": stream,
            },
        )
        if model:
            span.attributes["llm.model"] = model
        span.start_time = start_time
        return span

    async def _enqueue_span(self, span: Span) -> None:
        """Fire-and-forget: enqueue must never break the proxied request."""
        if self.sdk is None:
            return
        try:
            await self.sdk._enqueue(span)
        except Exception:  # pragma: no cover - defensive
            logger.warning("Failed to enqueue proxy span", exc_info=True)

    # ------------------------------------------------------------------
    # PP43: Failover and retry helpers
    # ------------------------------------------------------------------

    def _record_failure(self, reason: str, request_id: str, upstream_url: str) -> None:
        """Record a failure to the circuit breaker and emit audit/metric."""
        self.upstream_breaker.record_failure()
        from agent_obs.metrics import proxy_upstream_failures_total
        proxy_upstream_failures_total.labels(reason=reason).inc()
        
        # Emit audit event
        try:
            event = ProxyFallbackAuditEvent.for_fallback(
                request_id=request_id,
                upstream_url=upstream_url,
                reason=reason,
                fallback_type=reason,
            )
            if self.sdk is not None:
                hot = self.sdk._resolve_hot_store()
                if hot is not None:
                    hot.write_audit_event(event)
        except Exception:  # pragma: no cover - defensive
            logger.debug("Failed to write proxy fallback audit event", exc_info=True)

    async def _forward_with_retry(
        self,
        body: bytes,
        headers: Mapping[str, str],
        request_id: str,
        *,
        stream: bool = False,
    ) -> tuple[httpx.Response, list[bytes]]:
        """Forward to upstream with PP43 retry/circuit breaker logic.
        
        Returns: (upstream_response, stream_chunks) or raises _UpstreamUnavailable.
        """
        upstream_url = f"{self.config.upstream_base_url.rstrip('/')}/chat/completions"
        timeout = self.config.stream_timeout_seconds if stream else self.config.upstream_timeout_seconds
        
        # Check circuit breaker before first attempt
        if not self.upstream_breaker.allow_request():
            self._record_failure("circuit_open", request_id, upstream_url)
            raise self._UpstreamUnavailable("circuit_open")
        
        # Retry loop for 429 and 5xx
        attempt = 0
        max_attempts = 4  # 1 initial + 3 retries for 429
        while attempt < max_attempts:
            try:
                # Apply timeout per attempt
                client = self._get_client()
                fwd = forward_headers(headers)
                fwd.update(self._upstream_auth_headers())
                fwd["X-Request-Id"] = request_id
                
                if stream:
                    # For streaming, timeout applies to getting response headers
                    upstream_req = client.build_request(
                        "POST", "/chat/completions", headers=fwd, content=body
                    )
                    # Note: httpx.AsyncClient.send() doesn't support timeout parameter
                    upstream_resp = await client.send(upstream_req, stream=True)
                    # For streaming, return the response and empty chunks (streaming handles it)
                    return upstream_resp, []
                else:
                    # For non-streaming, apply timeout to the full request
                    upstream_resp = await client.post(
                        "/chat/completions", headers=fwd, content=body, timeout=timeout
                    )
                    return upstream_resp, []
                    
            except httpx.TimeoutException:
                reason = "timeout"
                self._record_failure(reason, request_id, upstream_url)
                if attempt == max_attempts - 1:
                    raise self._UpstreamUnavailable(reason)
                # No retry on timeout - go to fallback immediately
                raise self._UpstreamUnavailable(reason)
                
            except httpx.TransportError:
                reason = "connection"
                self._record_failure(reason, request_id, upstream_url)
                if attempt == max_attempts - 1:
                    raise self._UpstreamUnavailable(reason)
                # No retry on connection error - go to fallback immediately
                raise self._UpstreamUnavailable(reason)
            
            # Handle upstream responses
            if upstream_resp.status_code == 429:
                reason = "429"
                self._record_failure(reason, request_id, upstream_url)
                if attempt < max_attempts - 1:  # Retry 429
                    delay = 1.0 * (2 ** attempt)  # 1s, 2s, 4s
                    await self._sleep(delay)
                    attempt += 1
                    continue
                # Last attempt: return the 429 as-is
                return upstream_resp, []
                
            elif 500 <= upstream_resp.status_code < 600:
                reason = "5xx"
                self._record_failure(reason, request_id, upstream_url)
                if attempt == 0:  # Retry 5xx once
                    attempt += 1
                    continue
                # Second 5xx: return as-is
                return upstream_resp, []
                
            else:
                # Success or other 4xx: return immediately
                if upstream_resp.status_code < 400:
                    self.upstream_breaker.record_success()
                return upstream_resp, []

    async def _check_semantic_cache(self, request_body: bytes, span: Span, tenant_id: str = "default", agent_id: str = "unknown") -> Optional[Response]:
        """Check semantic cache before upstream call."""
        if self.cache is None:
            return None
            
        try:
            cached = await self.cache.get(request_body, tenant_id, agent_id)
            if cached is not None:
                # Cache hit: return cached response
                span.attributes["cache.hit"] = True
                span.attributes["cache.hit_type"] = "semantic"
                span.attributes["llm.cost_usd"] = 0.0  # No cost for cache hit
                span.attributes["llm.input_tokens"] = 0  # No tokens for cache hit
                span.attributes["llm.output_tokens"] = 0
                span.end_time = _now()
                await self._enqueue_span(span)
                
                # Record cost savings
                await self.cache.record_cost_savings(cached.cost_usd, tenant_id, agent_id)
                
                from fastapi.responses import Response
                return Response(
                    content=cached.payload,
                    media_type=cached.media_type,
                    headers={
                        "X-OryaObs-Cache-Hit": "true",
                        "X-OryaObs-Cache-Hit-Type": "semantic",
                        "X-OryaOrig-Model": cached.original_model,
                    }
                )
        except Exception as e:
            logger.warning("Semantic cache lookup error: %s", e)
            
        return None

    async def _fallback_to_cache_or_503(self, reason: str, request_id: str, span: Span) -> Response:
        """Handle fallback: cache lookup or 503 response."""
        upstream_url = f"{self.config.upstream_base_url.rstrip('/')}/chat/completions"
        
        # Try semantic cache first if enabled
        if self.cache is not None:
            # Extract tenant_id and agent_id from span for accurate metrics
            tenant_id = span.attributes.get("context.tenant_id", "default")
            agent_id = span.attributes.get("context.agent_id", "unknown")
            
            request_body = span.attributes.get("llm.input_text", "").encode("utf-8")
            cached = await self.cache.get(request_body, tenant_id, agent_id)
            if cached is not None:
                # Cache hit: return cached response
                span.attributes["cache.hit"] = True
                span.attributes["cache.hit_type"] = "semantic"
                span.attributes["status"] = "ok"
                span.attributes["llm.cost_usd"] = 0.0  # No cost for cache hit
                span.end_time = _now()
                await self._enqueue_span(span)
                
                # Record cost savings
                await self.cache.record_cost_savings(cached.cost_usd, tenant_id, agent_id)
                
                from fastapi.responses import Response
                return Response(
                    content=cached.payload,
                    media_type=cached.media_type,
                    headers={
                        "X-OryaObs-Cache-Hit": "true",
                        "X-OryaObs-Cache-Hit-Type": "semantic",
                        "X-OryaOrig-Model": cached.original_model,
                    }
                )
        
        # Cache miss or no cache: return 503
        span.attributes["status"] = "error"
        span.attributes["error.type"] = _UPSTREAM_CONNECTION if reason == "connection" else f"upstream_{reason}"
        span.attributes["http.response.status_code"] = 503
        span.end_time = _now()
        await self._enqueue_span(span)
        
        # Emit audit event
        try:
            event = ProxyFallbackAuditEvent.for_fallback(
                request_id=request_id,
                upstream_url=upstream_url,
                reason=reason,
                fallback_type=reason,
                status_code=503,
            )
            if self.sdk is not None:
                hot = self.sdk._resolve_hot_store()
                if hot is not None:
                    hot.write_audit_event(event)
        except Exception:  # pragma: no cover - defensive
            logger.debug("Failed to write proxy fallback audit event", exc_info=True)
        
        from fastapi.responses import JSONResponse
        retry_after = 30
        return JSONResponse(
            {"error": "upstream_timeout" if reason == "timeout" else "upstream_unavailable",
             "retry_after": retry_after,
             "request_id": request_id},
            status_code=503,
            headers={"Retry-After": str(retry_after)}
        )

    # ------------------------------------------------------------------
    # Endpoint
    # ------------------------------------------------------------------

    async def chat_completions(self, request: Request) -> Response:
        """POST /llm/v1/chat/completions — forward, intercept, span, respond."""
        start_time = _now()
        request_id = _new_ulid()
        body = await request.body()
        trace_id, user_id, tenant_id, agent_id = extract_headers(request.headers)
        parsed = _parse_body(body)
        stream = bool(parsed.get("stream")) and self.config.streaming_enabled

        span = self.build_proxy_span(
            request_body=body,
            trace_id=trace_id,
            user_id=user_id,
            tenant_id=tenant_id,
            agent_id=agent_id,
            stream=stream,
            start_time=start_time,
        )

        # PP54: Check semantic cache before any processing
        if self.cache is not None:
            cached_response = await self._check_semantic_cache(body, span, tenant_id, agent_id)
            if cached_response is not None:
                return cached_response

        # PP42 §3: tenant-level daily budget gate, checked before the upstream
        # call.  Budget resolution mirrors enrich_span's attribution defaults
        # (missing tenant header -> "default") so the gate and the rollup
        # agree on which bucket the request belongs to.
        attr_tenant, _attr_agent, _attr_user = attribution_from_headers(request.headers)
        decision = self.budget.check(attr_tenant)
        if decision != "ok":
            return await self._reject_over_budget(
                span, decision, attr_tenant, request_id
            )

        if bool(parsed.get("stream")) and not self.config.streaming_enabled:
            # Client asked for a stream but streaming is disabled: rewrite the
            # body so the upstream answers with plain JSON, and record the
            # fallback on the span.
            span.attributes["llm.stream_disabled_fallback"] = True
            parsed["stream"] = False
            body = json.dumps(parsed).encode("utf-8")

        try:
            # PP44: shared proxy mode
            if self.shared_enabled and self.shared_pipeline:
                return await self._handle_shared_proxy(request, span, body, request_id, start_time)
            else:
                # Mode A: observability-only proxy
                if stream:
                    return await self._handle_streaming(request, span, body, request_id, start_time)
                return await self._handle_non_streaming(request, span, body, request_id, start_time)
        except self._UpstreamUnavailable as e:
            # PP43: Fallback to cache or 503
            return await self._fallback_to_cache_or_503(e.reason, request_id, span)
        except httpx.TimeoutException:
            # PP43: Timeout now handled in _forward_with_retry, this is safety net
            span.attributes["status"] = "error"
            span.attributes["error.type"] = _UPSTREAM_TIMEOUT
            span.end_time = _now()
            await self._enqueue_span(span)
            return JSONResponse(
                status_code=503,
                content={"error": "upstream_timeout", "request_id": request_id},
            )
        except httpx.TransportError:
            # PP43: Connection errors now handled in _forward_with_retry, this is safety net
            span.attributes["status"] = "error"
            span.attributes["error.type"] = _UPSTREAM_CONNECTION
            span.end_time = _now()
            await self._enqueue_span(span)
            return JSONResponse(
                status_code=503,
                content={"error": "upstream_connection_error", "request_id": request_id},
            )
        except Exception:
            logger.exception("LLM proxy internal error (request_id=%s)", request_id)
            span.attributes["status"] = "error"
            span.attributes["error.type"] = _PROXY_INTERNAL
            span.end_time = _now()
            await self._enqueue_span(span)
            return JSONResponse(
                status_code=500,
                content={"error": "proxy internal error", "request_id": request_id},
            )

    async def _reject_over_budget(
        self,
        span: Span,
        decision: str,
        tenant_id: str,
        request_id: str,
    ) -> Response:
        """Answer 429/403 for a tenant over budget, recording why (PP42 §3).

        The refusal still produces and exports a span (``status=blocked``)
        so cost-denied traffic is observable, but the upstream is never
        called.  ``approaching`` (>= 80% of the daily budget) rate-limits
        with 429 + Retry-After; ``exhausted`` (>= 100%) refuses with 403.
        """
        if decision == BUDGET_EXHAUSTED:
            status_code = 403
            error = "budget exhausted"
            error_type = "budget_exhausted"
            headers = None
        else:  # BUDGET_APPROACHING — the only other non-ok decision
            assert decision == BUDGET_APPROACHING
            status_code = 429
            error = "budget approaching"
            error_type = "budget_approaching"
            headers = {"Retry-After": "60"}

        # The refusal span mirrors enrich_span's attribution so the blocked
        # request is queryable under the same resolved tenant bucket.
        span.context.tenant_id = tenant_id
        span.attributes["cost.attribution.tenant_id"] = tenant_id
        span.attributes["status"] = "blocked"
        span.attributes["error.type"] = error_type
        span.attributes["cost.budget.decision"] = decision
        span.attributes["cost.budget.tenant_id"] = tenant_id
        span.attributes["http.response.status_code"] = status_code
        span.end_time = _now()
        await self._enqueue_span(span)
        logger.info(
            "LLM proxy budget %s for tenant=%s (request_id=%s)",
            decision,
            tenant_id,
            request_id,
        )
        return JSONResponse(
            status_code=status_code,
            content={
                "error": error,
                "hint": error,
                "tenant_id": tenant_id,
                "request_id": request_id,
            },
            headers=headers,
        )

    async def _handle_non_streaming(
        self,
        request: Request,
        span: Span,
        body: bytes,
        request_id: str,
        start_time: float,
    ) -> Response:
        # PP43: Use resilient forward with retry/circuit breaker logic
        try:
            upstream, _ = await self._forward_with_retry(body, request.headers, request_id, stream=False)
        except self._UpstreamUnavailable as e:
            # Fallback to cache or 503 - need to await the async method
            fallback_response = await self._fallback_to_cache_or_503(e.reason, request_id, span)
            return fallback_response
        t_upstream_completed = _now()

        span.attributes["http.response.status_code"] = upstream.status_code
        if upstream.status_code >= 400:
            span.attributes["status"] = "error"
            span.attributes["error.type"] = f"upstream_http_{upstream.status_code}"
        else:
            span.attributes["status"] = "ok"
        await self._enrich_span(
            span,
            request_body=body,
            response_body=upstream.content,
            stream_chunks=None,
            headers=request.headers,
            timing=Timing(
                t_request_received=start_time,
                t_upstream_started=start_time,  # Approximate since we don't track start of forward
                t_upstream_completed=t_upstream_completed,
            ),
        )
        span.end_time = _now()
        await self._enqueue_span(span)

        media_type = upstream.headers.get("content-type", "application/json")
        
        # PP54: Cache successful response if semantic cache is enabled
        if self.cache is not None and upstream.status_code < 400:
            try:
                # Extract cost information from span for caching
                cost_usd = span.attributes.get("llm.cost_usd", 0.0)
                original_model = span.attributes.get("llm.model", "")
                
                await self.cache.set(
                    request_body=body,
                    response_payload=upstream.content,
                    media_type=media_type,
                    cost_usd=cost_usd,
                    tenant_id=tenant_id,
                    agent_id=agent_id,
                    original_model=original_model,
                    headers=dict(upstream.headers),
                )
                logger.debug("Cached successful response for tenant=%s, agent=%s", tenant_id, agent_id)
            except Exception as e:
                logger.warning("Failed to cache response: %s", e)
        
        return Response(
            content=upstream.content,
            status_code=upstream.status_code,
            media_type=media_type,
        )

    async def _handle_streaming(
        self,
        request: Request,
        span: Span,
        body: bytes,
        request_id: str,
        start_time: float,
    ) -> Response:
        client = self._get_client()
        fwd = forward_headers(request.headers)
        fwd.update(self._upstream_auth_headers())
        fwd["X-Request-Id"] = request_id

        # OpenAI only reports usage in the final stream chunk when
        # stream_options.include_usage=true — inject it so PP41 can parse it
        # (only when the body is valid JSON we can safely re-serialize).
        try:
            parsed = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            parsed = None
        if isinstance(parsed, dict) and "stream_options" not in parsed:
            parsed["stream_options"] = {"include_usage": True}
            body = json.dumps(parsed).encode("utf-8")

        output_parts: list[bytes] = []
        output_bytes = 0
        chunk_count = 0
        # Retained past the body cap so PP41 can always parse usage/finish.
        usage_parts: list[bytes] = []
        usage_bytes = 0
        t_first_token: float | None = None

        # PP43: Use resilient forward with retry/circuit breaker logic for headers phase
        try:
            upstream_resp, _ = await self._forward_with_retry(body, request.headers, request_id, stream=True)
            t_upstream_started = _now()
        except self._UpstreamUnavailable as e:
            # Fallback to cache or 503 - need to await the async method
            fallback_response = await self._fallback_to_cache_or_503(e.reason, request_id, span)
            return fallback_response
        span.attributes["http.response.status_code"] = upstream_resp.status_code
        if upstream_resp.status_code >= 400:
            span.attributes["status"] = "error"
            span.attributes["error.type"] = f"upstream_http_{upstream_resp.status_code}"
        media_type = upstream_resp.headers.get("content-type", "text/event-stream")

        async def sse_aiter() -> AsyncIterator[bytes]:
            nonlocal output_bytes, chunk_count, usage_bytes, t_first_token
            try:
                async for chunk in upstream_resp.aiter_bytes():
                    if t_first_token is None:
                        t_first_token = _now()
                    chunk_count += 1
                    if chunk_count <= _MAX_STREAM_EVENTS:
                        span.add_event(
                            "stream.chunk",
                            {"index": chunk_count, "bytes": len(chunk)},
                        )
                    if output_bytes < self.config.log_bodies_max_bytes:
                        output_parts.append(chunk)
                        output_bytes += len(chunk)
                    elif _is_usage_chunk(chunk) and usage_bytes < _MAX_USAGE_CHUNK_BYTES:
                        usage_parts.append(chunk)
                        usage_bytes += len(chunk)
                    yield chunk
                if "status" not in span.attributes:
                    span.attributes["status"] = "ok"
            except httpx.TimeoutException:
                span.attributes["status"] = "error"
                span.attributes["error.type"] = _UPSTREAM_TIMEOUT
                logger.warning(
                    "Upstream timeout mid-stream (request_id=%s)", request_id
                )
            except httpx.TransportError:
                span.attributes["status"] = "error"
                span.attributes["error.type"] = _UPSTREAM_CONNECTION
                logger.warning(
                    "Upstream connection error mid-stream (request_id=%s)", request_id
                )
            except asyncio.CancelledError:  # pragma: no cover - client disconnect
                span.attributes["status"] = "error"
                span.attributes["error.type"] = "client_disconnect"
                raise
            finally:
                try:
                    await upstream_resp.aclose()
                except Exception:  # pragma: no cover - best-effort teardown
                    logger.debug("Upstream stream close failed", exc_info=True)
                span.attributes["llm.stream_chunks"] = chunk_count
                await self._enrich_span(
                    span,
                    request_body=body,
                    response_body=None,
                    stream_chunks=output_parts + usage_parts,
                    headers=request.headers,
                    timing=Timing(
                        t_request_received=start_time,
                        t_upstream_started=t_upstream_started,
                        t_first_token=t_first_token,
                        t_upstream_completed=_now(),
                    ),
                )
                span.end_time = _now()
                await self._enqueue_span(span)

        return StreamingResponse(
            sse_aiter(), media_type=media_type, status_code=upstream_resp.status_code
        )

    # PP44: Shared proxy mode handling
    async def _handle_shared_proxy(
        self,
        request: Request,
        span: Span,
        body: bytes,
        request_id: str,
        start_time: float,
    ) -> Response:
        """Handle request through shared obs + security pipeline (PP44)."""
        try:
            # Process through shared pipeline
            result = await self.shared_pipeline.process(
                request_body=body,
                headers=dict(request.headers),
                trace_id=span.trace_id,
                request_id=request_id,
            )
            
            # Make final decision
            final_decision = await self.decision_router.decide(
                security_result=result.security_result,
                obs_span=result.obs_span,
                request_id=request_id,
                trace_id=span.trace_id,
            )
            
            # Update span with decision metadata (record-then-block)
            span.attributes.update(final_decision.span_attributes)
            
            # Handle different decisions (accept both old and new action names for compatibility)
            is_block = final_decision.action in ("block", "block_response")
            is_flag = final_decision.action in ("flag", "forward_with_flag")
            
            if is_block:
                # Block response with security details (record-then-block)
                span.attributes["status"] = "blocked"
                span.attributes["security.block_reason"] = final_decision.reason
                span.end_time = _now()
                await self._enqueue_span(span)
                return JSONResponse(
                    status_code=final_decision.status_code,
                    content=final_decision.body,
                )
            
            elif is_flag:
                # Forward with flag metadata
                span.attributes["status"] = "forwarded"
                span.attributes["security.flag"] = True
                span.end_time = _now()
                await self._enqueue_span(span)
                # Forward to upstream with masked body
                return await self._forward_to_upstream(
                    request, result.masked_request_body, span, request_id, start_time
                )
            
            else:  # forward
                # Normal forward
                span.attributes["status"] = "forwarded"
                span.end_time = _now()
                await self._enqueue_span(span)
                # Forward to upstream with masked body
                return await self._forward_to_upstream(
                    request, result.masked_request_body, span, request_id, start_time
                )
                
        except Exception as e:
            logger.error("Shared proxy processing failed: %s", e)
            span.attributes["status"] = "error"
            span.attributes["error.type"] = "shared_proxy_error"
            span.end_time = _now()
            await self._enqueue_span(span)
            return JSONResponse(
                status_code=500,
                content={"error": "shared proxy error", "request_id": request_id},
            )
