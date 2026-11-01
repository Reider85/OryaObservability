"""PP40: LLMProxyServer — FastAPI LLM proxy with span interception."""

from __future__ import annotations

import asyncio
import json
import time

import pytest

httpx = pytest.importorskip("httpx")
pytest.importorskip("fastapi")

import respx
from httpx import ASGITransport

from agent_obs.observability import (
    InvalidSpanTypeError,
    ObservabilitySDK,
    Span,
    SpanContext,
    SpanType,
)
from agent_obs.proxy import ProxyConfig
from agent_obs.proxy.server import (
    LLMProxyServer,
    extract_headers,
    forward_headers,
)

UPSTREAM = "https://api.openai.com/v1/chat/completions"

_SSE_CHUNKS = [
    b'data: {"choices":[{"delta":{"content":"he"}}]}\n\n',
    b'data: {"choices":[{"delta":{"content":"llo"}}]}\n\n',
    b"data: [DONE]\n\n",
]

# PP41: realistic stream with finish_reason + usage in the trailing chunks
# (OpenAI reports usage only in the final chunk, with include_usage=true).
_SSE_CHUNKS_WITH_USAGE = [
    b'data: {"choices":[{"delta":{"content":"He"}}]}\n\n',
    b'data: {"choices":[{"delta":{"content":"llo"}}]}\n\n',
    b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n',
    b'data: {"choices":[],"usage":{"prompt_tokens":7,"completion_tokens":3}}\n\n',
    b"data: [DONE]\n\n",
]


class SSEStream(httpx.AsyncByteStream):
    """AsyncByteStream yielding SSE chunks separately (realistic streaming)."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def __aiter__(self):
        for chunk in self._chunks:
            yield chunk

    async def aclose(self) -> None:  # pragma: no cover - nothing to close
        pass


class SlowSSEStream(SSEStream):
    """SSE stream with a per-chunk delay — guarantees a measurable ttft."""

    def __init__(self, chunks: list[bytes], delay: float = 0.02) -> None:
        super().__init__(chunks)
        self._delay = delay

    async def __aiter__(self):
        for chunk in self._chunks:
            await asyncio.sleep(self._delay)
            yield chunk


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

_PROXY_ENV_VARS = [
    "AGENT_OBS_PROXY_MODE",
    "AGENT_OBS_PROXY_HOST",
    "AGENT_OBS_PROXY_PORT",
    "AGENT_OBS_PROXY_UPSTREAM_PROVIDER",
    "AGENT_OBS_PROXY_UPSTREAM_BASE_URL",
    "AGENT_OBS_PROXY_UPSTREAM_API_KEY_ENV",
    "AGENT_OBS_PROXY_REQUEST_TIMEOUT_SECONDS",
    "AGENT_OBS_PROXY_STREAMING_ENABLED",
    "AGENT_OBS_PROXY_LOG_BODIES",
    "AGENT_OBS_PROXY_LOG_BODIES_MAX_BYTES",
    "SHARED_PROXY_ENABLED",
    "AGENT_OBS_TENANT_ID",
]


@pytest.fixture(autouse=True)
def clean_proxy_env(monkeypatch):
    for var in _PROXY_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AGENT_OBS_ENABLED", "true")
    # Deterministic full-text bodies in span assertions.
    monkeypatch.setenv("AGENT_OBS_CONTENT_RATE", "100")


@pytest.fixture
def proxy_config(monkeypatch) -> ProxyConfig:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-upstream-secret")
    return ProxyConfig(
        mode="on",
        enabled=True,
        upstream_base_url="https://api.openai.com/v1",
        upstream_api_key_env="OPENAI_API_KEY",
    )


@pytest.fixture
def sdk() -> ObservabilitySDK:
    return ObservabilitySDK([])


@pytest.fixture
def proxy(proxy_config, sdk) -> LLMProxyServer:
    return LLMProxyServer(proxy_config, sdk=sdk)


def client_for(proxy: LLMProxyServer) -> httpx.AsyncClient:
    """In-process ASGI client — only the proxy's outbound calls hit respx."""
    return httpx.AsyncClient(
        transport=ASGITransport(app=proxy.app), base_url="http://proxy"
    )


def proxy_spans(sdk: ObservabilitySDK) -> list[Span]:
    return [s for s in sdk.last_spans if s.span_type == SpanType.LLM_PROXY_CALL]


def chat_body(**overrides) -> dict:
    body = {"model": "gpt-4o", "messages": [{"role": "user", "content": "hello"}]}
    body.update(overrides)
    return body


# ---------------------------------------------------------------------------
# SpanType integration
# ---------------------------------------------------------------------------


class TestProxySpanType:
    def test_llm_proxy_call_in_enum(self):
        assert SpanType.LLM_PROXY_CALL.value == "llm.proxy.call"
        assert len(SpanType) == 6

    def test_span_accepts_string_type(self):
        span = Span(
            name="llm.proxy.call",
            span_type="llm.proxy.call",
            context=SpanContext.new(agent_id="proxy"),
        )
        assert span.span_type is SpanType.LLM_PROXY_CALL

    def test_invalid_type_still_raises(self):
        with pytest.raises(InvalidSpanTypeError):
            Span(
                name="bad",
                span_type="llm.nope",
                context=SpanContext.new(agent_id="proxy"),
            )


# ---------------------------------------------------------------------------
# Header helpers
# ---------------------------------------------------------------------------


class TestHeaderHelpers:
    def test_extract_headers_full(self):
        headers = {
            "X-OryaObs-Trace-Id": "T-fixed",
            "X-OryaObs-User-Id": "alice",
            "X-OryaObs-Tenant-Id": "ten-a",
            "X-OryaObs-Agent-Id": "agent-1",
        }
        assert extract_headers(headers) == ("T-fixed", "alice", "ten-a", "agent-1")

    def test_extract_headers_defaults(self):
        trace_id, user_id, tenant_id, agent_id = extract_headers({})
        assert len(trace_id) == 26  # generated ULID
        assert user_id == tenant_id == agent_id == ""

    def test_forward_headers_strips_auth_and_hop_by_hop(self):
        headers = {
            "Authorization": "Bearer dummy",
            "Content-Type": "application/json",
            "Host": "proxy.local",
            "Connection": "keep-alive",
            "Content-Length": "42",
            "X-OryaObs-Trace-Id": "T1",
            "X-Custom": "keep-me",
        }
        fwd = forward_headers(headers)
        assert fwd == {"Content-Type": "application/json", "X-Custom": "keep-me"}


# ---------------------------------------------------------------------------
# Non-streaming
# ---------------------------------------------------------------------------


class TestNonStreaming:
    async def test_ok_span_created_with_bodies(self, proxy, sdk):
        upstream_resp = {"choices": [{"message": {"content": "hi"}}]}
        with respx.mock:
            respx.post(UPSTREAM).mock(
                return_value=httpx.Response(200, json=upstream_resp)
            )
            async with client_for(proxy) as client:
                resp = await client.post(
                    "/llm/v1/chat/completions", json=chat_body()
                )
        assert resp.status_code == 200
        assert resp.json() == upstream_resp

        spans = proxy_spans(sdk)
        assert len(spans) == 1
        span = spans[0]
        assert span.name.startswith("llm.proxy.call")
        assert span.attributes["status"] == "ok"
        assert span.attributes["llm.provider"] == "openai"
        assert span.attributes["llm.model"] == "gpt-4o"
        assert span.attributes["llm.stream"] is False
        assert span.attributes["http.response.status_code"] == 200
        assert "hello" in span.attributes["llm.input_text"]
        assert "hi" in span.attributes["llm.output_text"]
        assert span.end_time is not None and span.end_time >= span.start_time

    async def test_upstream_gets_real_key_not_client_dummy(self, proxy):
        with respx.mock:
            route = respx.post(UPSTREAM).mock(
                return_value=httpx.Response(200, json={"ok": True})
            )
            async with client_for(proxy) as client:
                await client.post(
                    "/llm/v1/chat/completions",
                    json=chat_body(),
                    headers={"Authorization": "Bearer dummy"},
                )
        upstream_auth = route.calls[0].request.headers.get("authorization")
        assert upstream_auth == "Bearer sk-upstream-secret"

    async def test_upstream_4xx_forwarded_with_error_span(self, proxy, sdk):
        with respx.mock:
            respx.post(UPSTREAM).mock(
                return_value=httpx.Response(429, json={"error": "rate limited"})
            )
            async with client_for(proxy) as client:
                resp = await client.post(
                    "/llm/v1/chat/completions", json=chat_body()
                )
        assert resp.status_code == 429
        assert resp.json() == {"error": "rate limited"}

        span = proxy_spans(sdk)[0]
        assert span.attributes["status"] == "error"
        assert span.attributes["error.type"] == "upstream_http_429"


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------


class TestStreaming:
    async def test_sse_forwarded_and_span_closes_after_last_chunk(
        self, proxy, sdk
    ):
        with respx.mock:
            respx.post(UPSTREAM).mock(
                return_value=httpx.Response(
                    200,
                    stream=SSEStream(_SSE_CHUNKS),
                    headers={"content-type": "text/event-stream"},
                )
            )
            async with client_for(proxy) as client, client.stream(
                "POST",
                "/llm/v1/chat/completions",
                json=chat_body(stream=True),
            ) as resp:
                assert resp.status_code == 200
                assert "text/event-stream" in resp.headers["content-type"]
                received = b""
                async for chunk in resp.aiter_bytes():
                    received += chunk

        assert received == b"".join(_SSE_CHUNKS)

        spans = proxy_spans(sdk)
        assert len(spans) == 1
        span = spans[0]
        assert span.attributes["llm.stream"] is True
        assert span.attributes["status"] == "ok"
        assert span.attributes["llm.stream_chunks"] == len(_SSE_CHUNKS)
        chunk_events = [e for e in span.events if e.name == "stream.chunk"]
        assert len(chunk_events) == len(_SSE_CHUNKS)
        assert [e.attributes["index"] for e in chunk_events] == [1, 2, 3]
        # Span is closed (end_time set) only after the final chunk.
        assert span.end_time is not None

    async def test_stream_options_injected_for_usage(self, proxy):
        with respx.mock:
            route = respx.post(UPSTREAM).mock(
                return_value=httpx.Response(
                    200,
                    stream=SSEStream(_SSE_CHUNKS),
                    headers={"content-type": "text/event-stream"},
                )
            )
            async with client_for(proxy) as client, client.stream(
                "POST",
                "/llm/v1/chat/completions",
                json=chat_body(stream=True),
            ) as resp:
                async for _ in resp.aiter_bytes():
                    pass
        sent = json.loads(route.calls[0].request.content)
        assert sent["stream_options"] == {"include_usage": True}

    async def test_client_stream_options_not_overridden(self, proxy):
        with respx.mock:
            route = respx.post(UPSTREAM).mock(
                return_value=httpx.Response(
                    200,
                    stream=SSEStream(_SSE_CHUNKS),
                    headers={"content-type": "text/event-stream"},
                )
            )
            async with client_for(proxy) as client, client.stream(
                "POST",
                "/llm/v1/chat/completions",
                json=chat_body(
                    stream=True,
                    stream_options={"include_usage": False},
                ),
            ) as resp:
                async for _ in resp.aiter_bytes():
                    pass
        sent = json.loads(route.calls[0].request.content)
        assert sent["stream_options"] == {"include_usage": False}

    async def test_streaming_disabled_falls_back_to_non_stream(
        self, sdk, monkeypatch
    ):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-upstream-secret")
        cfg = ProxyConfig(
            mode="on",
            enabled=True,
            upstream_api_key_env="OPENAI_API_KEY",
            streaming_enabled=False,
        )
        proxy = LLMProxyServer(cfg, sdk=sdk)
        with respx.mock:
            route = respx.post(UPSTREAM).mock(
                return_value=httpx.Response(200, json={"ok": True})
            )
            async with client_for(proxy) as client:
                resp = await client.post(
                    "/llm/v1/chat/completions", json=chat_body(stream=True)
                )
        assert resp.status_code == 200
        sent = json.loads(route.calls[0].request.content)
        assert sent["stream"] is False  # body rewritten for the upstream

        span = proxy_spans(sdk)[0]
        assert span.attributes["llm.stream"] is False
        assert span.attributes["llm.stream_disabled_fallback"] is True


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestErrors:
    async def test_upstream_timeout_returns_504(self, proxy, sdk):
        with respx.mock:
            respx.post(UPSTREAM).mock(side_effect=httpx.TimeoutException("boom"))
            async with client_for(proxy) as client:
                resp = await client.post(
                    "/llm/v1/chat/completions", json=chat_body()
                )
        assert resp.status_code == 504
        body = resp.json()
        assert body["error"] == "upstream timeout"
        assert body["request_id"]

        span = proxy_spans(sdk)[0]
        assert span.attributes["status"] == "error"
        assert span.attributes["error.type"] == "upstream_timeout"

    async def test_upstream_connect_error_returns_502(self, proxy, sdk):
        with respx.mock:
            respx.post(UPSTREAM).mock(
                side_effect=httpx.ConnectError("refused")
            )
            async with client_for(proxy) as client:
                resp = await client.post(
                    "/llm/v1/chat/completions", json=chat_body()
                )
        assert resp.status_code == 502

        span = proxy_spans(sdk)[0]
        assert span.attributes["status"] == "error"
        assert span.attributes["error.type"] == "upstream_connection_error"

    async def test_internal_error_returns_500(self, proxy, sdk, monkeypatch):
        async def boom(*args, **kwargs):
            raise RuntimeError("kaboom")

        monkeypatch.setattr(proxy, "forward_to_upstream", boom)
        async with client_for(proxy) as client:
            resp = await client.post(
                "/llm/v1/chat/completions", json=chat_body()
            )
        assert resp.status_code == 500
        assert resp.json()["error"] == "proxy internal error"

        span = proxy_spans(sdk)[0]
        assert span.attributes["status"] == "error"
        assert span.attributes["error.type"] == "proxy_internal_error"

    async def test_stream_timeout_mid_stream_marks_span(self, proxy, sdk):
        class FailingStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'data: {"choices":[{"delta":{"content":"x"}}]}\n\n'
                raise httpx.ReadTimeout("mid-stream timeout")

            async def aclose(self) -> None:
                pass

        with respx.mock:
            respx.post(UPSTREAM).mock(
                return_value=httpx.Response(
                    200,
                    stream=FailingStream(),
                    headers={"content-type": "text/event-stream"},
                )
            )
            async with client_for(proxy) as client, client.stream(
                "POST",
                "/llm/v1/chat/completions",
                json=chat_body(stream=True),
            ) as resp:
                received = b""
                async for chunk in resp.aiter_bytes():
                    received += chunk
        # First chunk was delivered; the stream then aborted.
        assert b"delta" in received

        span = proxy_spans(sdk)[0]
        assert span.attributes["status"] == "error"
        assert span.attributes["error.type"] == "upstream_timeout"
        assert span.attributes["llm.stream_chunks"] == 1


# ---------------------------------------------------------------------------
# Header propagation into the span
# ---------------------------------------------------------------------------


class TestHeaderPropagation:
    async def test_client_trace_id_reused(self, proxy, sdk):
        with respx.mock:
            respx.post(UPSTREAM).mock(
                return_value=httpx.Response(200, json={"ok": True})
            )
            async with client_for(proxy) as client:
                await client.post(
                    "/llm/v1/chat/completions",
                    json=chat_body(),
                    headers={
                        "X-OryaObs-Trace-Id": "01HZZZFIXEDTRACEID000000AA",
                        "X-OryaObs-User-Id": "alice",
                        "X-OryaObs-Tenant-Id": "ten-a",
                        "X-OryaObs-Agent-Id": "agent-9",
                    },
                )
        span = proxy_spans(sdk)[0]
        assert span.context.trace_id == "01HZZZFIXEDTRACEID000000AA"
        assert span.context.user_id == "alice"
        assert span.context.tenant_id == "ten-a"
        assert span.context.agent_id == "agent-9"

    async def test_defaults_when_headers_missing(self, proxy, sdk):
        with respx.mock:
            respx.post(UPSTREAM).mock(
                return_value=httpx.Response(200, json={"ok": True})
            )
            async with client_for(proxy) as client:
                await client.post("/llm/v1/chat/completions", json=chat_body())
        span = proxy_spans(sdk)[0]
        assert len(span.context.trace_id) == 26
        assert span.context.trace_id != ""  # generated
        assert span.context.user_id == ""
        assert span.context.tenant_id == ""
        assert span.context.agent_id == "proxy"  # default agent id

    async def test_tenant_stamped_from_context(self, proxy, sdk):
        with respx.mock:
            respx.post(UPSTREAM).mock(
                return_value=httpx.Response(200, json={"ok": True})
            )
            async with client_for(proxy) as client:
                await client.post(
                    "/llm/v1/chat/completions",
                    json=chat_body(),
                    headers={"X-OryaObs-Tenant-Id": "ten-x"},
                )
        span = proxy_spans(sdk)[0]
        # PP32: _enqueue stamps tenant_id into attributes from the context.
        assert span.attributes.get("tenant_id") == "ten-x"


# ---------------------------------------------------------------------------
# Body-logging policy
# ---------------------------------------------------------------------------


class TestBodyPolicy:
    async def test_log_bodies_false_stores_hashes_only(self, sdk, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-upstream-secret")
        cfg = ProxyConfig(
            mode="on",
            enabled=True,
            upstream_api_key_env="OPENAI_API_KEY",
            log_bodies=False,
        )
        proxy = LLMProxyServer(cfg, sdk=sdk)
        with respx.mock:
            respx.post(UPSTREAM).mock(
                return_value=httpx.Response(200, json={"ok": True})
            )
            async with client_for(proxy) as client:
                await client.post("/llm/v1/chat/completions", json=chat_body())
        span = proxy_spans(sdk)[0]
        assert "llm.input_text" not in span.attributes
        assert "llm.output_text" not in span.attributes
        assert len(span.attributes["llm.input_sha256"]) == 64
        assert span.attributes["llm.input_chars"] > 0
        assert len(span.attributes["llm.output_sha256"]) == 64

    async def test_oversized_body_truncated(self, sdk, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-upstream-secret")
        cfg = ProxyConfig(
            mode="on",
            enabled=True,
            upstream_api_key_env="OPENAI_API_KEY",
            log_bodies_max_bytes=64,
        )
        proxy = LLMProxyServer(cfg, sdk=sdk)
        big = chat_body()
        big["messages"] = [{"role": "user", "content": "x" * 5000}]
        with respx.mock:
            respx.post(UPSTREAM).mock(
                return_value=httpx.Response(200, json={"ok": True})
            )
            async with client_for(proxy) as client:
                await client.post("/llm/v1/chat/completions", json=big)
        span = proxy_spans(sdk)[0]
        assert span.attributes["llm.input_truncated"] is True
        assert len(span.attributes["llm.input_text"].encode()) <= 64


# ---------------------------------------------------------------------------
# Real-socket lifecycle (uvicorn on an ephemeral port)
# ---------------------------------------------------------------------------


class _UpstreamStub:
    """Tiny stdlib HTTP server standing in for the LLM provider.

    Used by the real-socket tests: respx intercepts ALL httpx traffic in
   -process (including the inbound request to the proxy socket), so those
    tests talk to a real local upstream instead of a respx route.
    """

    def __init__(self, payload: dict | None = None, status: int = 200) -> None:
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        self.payload = payload if payload is not None else {"ok": True}
        self.status = status
        self.requests: list[dict] = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("content-length", 0))
                body = self.rfile.read(length)
                stub.requests.append(
                    {
                        "path": self.path,
                        "auth": self.headers.get("authorization"),
                        "body": body,
                    }
                )
                data = json.dumps(stub.payload).encode()
                self.send_response(stub.status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):  # silence
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        self.base_url = f"http://127.0.0.1:{self.port}/v1"

    def __enter__(self):
        import threading

        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._server.shutdown()
        self._server.server_close()
        return False


class TestLifecycle:
    async def test_start_serve_stop(self, sdk, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-upstream-secret")
        with _UpstreamStub(payload={"choices": []}) as stub:
            cfg = ProxyConfig(
                mode="on",
                enabled=True,
                listen_port=0,  # ephemeral
                upstream_base_url=stub.base_url,
                upstream_api_key_env="OPENAI_API_KEY",
            )
            proxy = LLMProxyServer(cfg, sdk=sdk)
            assert proxy.bound_port == 0
            await proxy.start()
            try:
                assert proxy.bound_port != 0  # actually bound
                # start() is idempotent.
                await proxy.start()
                async with httpx.AsyncClient() as client:
                    resp = await client.post(
                        f"http://127.0.0.1:{proxy.bound_port}"
                        "/llm/v1/chat/completions",
                        json=chat_body(),
                    )
                assert resp.status_code == 200
                assert resp.json() == {"choices": []}
            finally:
                await proxy.stop()

        # Real upstream received the forwarded request with the injected key.
        assert len(stub.requests) == 1
        assert stub.requests[0]["path"] == "/v1/chat/completions"
        assert stub.requests[0]["auth"] == "Bearer sk-upstream-secret"
        assert b"hello" in stub.requests[0]["body"]

        # Port is released after stop.
        with pytest.raises(httpx.ConnectError):
            async with httpx.AsyncClient() as client:
                await client.post(
                    f"http://127.0.0.1:{proxy.bound_port}"
                    "/llm/v1/chat/completions",
                    json=chat_body(),
                )
        # The real-socket request produced a span too.
        assert len(proxy_spans(sdk)) == 1

    async def test_stop_without_start_is_noop(self, proxy):
        await proxy.stop()  # must not raise


# ---------------------------------------------------------------------------
# SDK wiring (real server module replaces the PP39 stub contract)
# ---------------------------------------------------------------------------


class TestSDKIntegration:
    def test_sdk_constructs_real_server(self, monkeypatch):
        monkeypatch.setenv("AGENT_OBS_PROXY_MODE", "on")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-upstream-secret")
        monkeypatch.setenv("AGENT_OBS_PROXY_PORT", "0")
        sdk = ObservabilitySDK([])
        assert isinstance(sdk._proxy, LLMProxyServer)
        assert sdk._proxy.sdk is sdk
        assert sdk._proxy.config.mode == "on"

    async def test_async_context_manager_starts_and_stops(self, monkeypatch):
        monkeypatch.setenv("AGENT_OBS_PROXY_MODE", "on")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-upstream-secret")
        monkeypatch.setenv("AGENT_OBS_PROXY_PORT", "0")
        async with ObservabilitySDK([]) as sdk:
            proxy = sdk._proxy
            assert isinstance(proxy, LLMProxyServer)
            assert proxy.bound_port != 0
        # shutdown() stopped the proxy — port is closed.
        with pytest.raises(httpx.ConnectError):
            async with httpx.AsyncClient() as client:
                await client.post(
                    f"http://127.0.0.1:{proxy.bound_port}"
                    "/llm/v1/chat/completions",
                    json=chat_body(),
                )

    async def test_end_to_end_through_sdk(self, monkeypatch):
        monkeypatch.setenv("AGENT_OBS_PROXY_MODE", "on")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-upstream-secret")
        monkeypatch.setenv("AGENT_OBS_PROXY_PORT", "0")
        with _UpstreamStub(
            payload={"choices": [{"message": {"content": "pong"}}]}
        ) as stub:
            monkeypatch.setenv(
                "AGENT_OBS_PROXY_UPSTREAM_BASE_URL", stub.base_url
            )
            async with ObservabilitySDK([]) as sdk:
                port = sdk._proxy.bound_port
                async with httpx.AsyncClient() as client:
                    resp = await client.post(
                        f"http://127.0.0.1:{port}/llm/v1/chat/completions",
                        json=chat_body(),
                        headers={"X-OryaObs-Trace-Id": "E2E-TRACE"},
                    )
                assert resp.status_code == 200
                assert resp.json()["choices"][0]["message"]["content"] == "pong"
                # Give the enqueue a tick to land in last_spans.
                deadline = time.monotonic() + 2.0
                while not proxy_spans(sdk) and time.monotonic() < deadline:
                    await asyncio.sleep(0.02)
                span = proxy_spans(sdk)[0]
                assert span.context.trace_id == "E2E-TRACE"
                assert span.attributes["status"] == "ok"


# ---------------------------------------------------------------------------
# PP41: ObsPipeline enrichment through the endpoint
# ---------------------------------------------------------------------------


class TestSpanEnrichment:
    async def test_non_streaming_tokens_cost_and_timing(self, proxy, sdk):
        upstream_resp = {
            "choices": [
                {"message": {"content": "hi"}, "finish_reason": "stop"}
            ],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "prompt_tokens_details": {"cached_tokens": 2},
            },
        }
        with respx.mock:
            respx.post(UPSTREAM).mock(
                return_value=httpx.Response(200, json=upstream_resp)
            )
            async with client_for(proxy) as client:
                resp = await client.post(
                    "/llm/v1/chat/completions",
                    json=chat_body(temperature=0.3),
                )
        assert resp.status_code == 200

        span = proxy_spans(sdk)[0]
        assert span.attributes["tokens.input"] == 10
        assert span.attributes["tokens.output"] == 5
        assert span.attributes["tokens.cached"] == 2
        assert span.attributes["cost.usd"] > 0
        assert span.attributes["cost.price_book_version"]
        assert span.attributes["llm.finish_reason"] == "stop"
        assert span.attributes["llm.temperature"] == 0.3
        assert span.attributes["llm.input_messages_count"] == 1
        assert span.attributes["llm.output_messages_count"] == 1
        assert span.attributes["llm.input_chars"] == len(
            json.dumps([{"role": "user", "content": "hello"}])
        )
        assert span.attributes["llm.output_chars"] == len("hi")
        assert span.attributes["llm.ttft_ms"] == 0.0  # N/A for non-streaming
        assert span.attributes["llm.latency_ms"] >= 0
        assert span.attributes["llm.stream"] is False
        assert "hello" in span.attributes["llm.input_text"]
        assert span.attributes["llm.output_text"] == "hi"

    async def test_streaming_ttft_usage_and_content(self, proxy, sdk):
        with respx.mock:
            respx.post(UPSTREAM).mock(
                return_value=httpx.Response(
                    200,
                    stream=SlowSSEStream(_SSE_CHUNKS_WITH_USAGE),
                    headers={"content-type": "text/event-stream"},
                )
            )
            async with client_for(proxy) as client, client.stream(
                "POST",
                "/llm/v1/chat/completions",
                json=chat_body(stream=True),
            ) as resp:
                assert resp.status_code == 200
                async for _ in resp.aiter_bytes():
                    pass

        span = proxy_spans(sdk)[0]
        assert span.attributes["llm.stream"] is True
        assert span.attributes["llm.ttft_ms"] > 0
        assert span.attributes["llm.latency_ms"] >= span.attributes["llm.ttft_ms"]
        assert span.attributes["tokens.input"] == 7
        assert span.attributes["tokens.output"] == 3
        assert span.attributes["llm.finish_reason"] == "stop"
        assert span.attributes["llm.output_text"] == "Hello"
        assert span.attributes["llm.stream_chunks"] == len(_SSE_CHUNKS_WITH_USAGE)
        assert span.attributes["cost.usd"] > 0

    async def test_usage_chunk_survives_body_cap(self, sdk, monkeypatch):
        """The trailing usage chunk must be parseable even after the body cap."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-upstream-secret")
        cfg = ProxyConfig(
            mode="on",
            enabled=True,
            upstream_api_key_env="OPENAI_API_KEY",
            log_bodies_max_bytes=64,
        )
        proxy = LLMProxyServer(cfg, sdk=sdk)
        chunks = [
            b'data: {"choices":[{"delta":{"content":"' + b"x" * 300 + b'"}}]}\n\n',
            b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n',
            (
                b'data: {"choices":[],"usage":{"prompt_tokens":11,'
                b'"completion_tokens":6}}\n\n'
            ),
            b"data: [DONE]\n\n",
        ]
        with respx.mock:
            respx.post(UPSTREAM).mock(
                return_value=httpx.Response(
                    200,
                    stream=SSEStream(chunks),
                    headers={"content-type": "text/event-stream"},
                )
            )
            async with client_for(proxy) as client, client.stream(
                "POST",
                "/llm/v1/chat/completions",
                json=chat_body(stream=True),
            ) as resp:
                async for _ in resp.aiter_bytes():
                    pass

        span = proxy_spans(sdk)[0]
        assert span.attributes["tokens.input"] == 11
        assert span.attributes["tokens.output"] == 6
        assert span.attributes["llm.finish_reason"] == "stop"
        assert span.attributes["llm.output_truncated"] is True

    async def test_content_sampling_zero_keeps_hashes_only(
        self, proxy, sdk, monkeypatch
    ):
        monkeypatch.setenv("AGENT_OBS_CONTENT_RATE", "0")
        with respx.mock:
            respx.post(UPSTREAM).mock(
                return_value=httpx.Response(
                    200,
                    json={"choices": [{"message": {"content": "hi"}}]},
                )
            )
            async with client_for(proxy) as client:
                await client.post("/llm/v1/chat/completions", json=chat_body())

        span = proxy_spans(sdk)[0]
        assert "llm.input_text" not in span.attributes
        assert "llm.output_text" not in span.attributes
        assert len(span.attributes["llm.input_sha256"]) == 64
        assert len(span.attributes["llm.output_sha256"]) == 64
        assert span.attributes["trace.content_sampled"] is False
        assert span.attributes["llm.input_chars"] == len(
            json.dumps([{"role": "user", "content": "hello"}])
        )
        # Structured attributes are independent of sampling.
        assert span.attributes["llm.input_messages_count"] == 1
        assert span.attributes["llm.output_messages_count"] == 1
