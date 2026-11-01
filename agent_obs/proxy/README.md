# agent_obs.proxy — LLM-proxy observability mode (PP39 + PP40)

Level-3 mode (PRODUCTION-PROMPTS §0.4): the SDK runs an in-process HTTP proxy
in front of the LLM provider, so agents you do not instrument directly still
produce `llm.proxy.call` spans and cost attributes. PP39 ships the
configuration layer (`ProxyConfig`); PP40 ships the FastAPI server
(`agent_obs/proxy/server.py`).

## Modes

| `AGENT_OBS_PROXY_MODE` | Behaviour |
|---|---|
| `off` (default) | In-process SDK, as in MVP/CRITICAL. Agent instruments via `sdk.llm_call()`. |
| `on` | **Mode A:** SDK proxies all LLM traffic. |
| `auto` | Failover: proxy comes up only if an SDK dependency's circuit breaker (PP38) is open. |
| — | `SHARED_PROXY_ENABLED=on` + `AGENT_OBS_PROXY_MODE=on` = **Mode B** (PP44, shared obs+security proxy). |
| — | `SHARED_PROXY_ENABLED=on` + `AGENT_OBS_PROXY_MODE=off` = **invalid**, fails fast at startup. |

## Running the server (PP40)

The proxy is constructed by the SDK when the mode resolves to enabled; it is
started/stopped with the SDK lifecycle. The constructor does **not** bind the
port — you must `await sdk.start()` (or use `async with`):

```python
import asyncio
from agent_obs.observability import ObservabilitySDK

async def main():
    # AGENT_OBS_PROXY_MODE=on + OPENAI_API_KEY set in the environment
    async with ObservabilitySDK([]) as sdk:  # start() binds the proxy port
        ...  # proxy serves /llm/v1/chat/completions on 127.0.0.1:8765
    # shutdown() drains spans and stops the proxy

asyncio.run(main())
```

Requirements: `pip install "agent-obs[api]"` (FastAPI + uvicorn are an
optional extra). If the extra is missing the SDK logs a warning and runs
in-process only (`sdk._proxy is None`) — it never fails startup over a
missing proxy dependency.

## Switching an agent onto the proxy

Point the agent's OpenAI-compatible client at the proxy instead of the real
provider. The agent sends `"api_key": "dummy"`; the proxy injects the real
upstream key from its own env and never forwards the client's key.

```python
import openai

client = openai.Client(
    base_url="http://localhost:8765/llm/v1",  # proxy, not api.openai.com
    api_key="dummy",  # real key lives in the proxy's env, injected upstream
)

resp = client.chat.completions.create(
    model="gpt-4o",
    messages=[{"role": "user", "content": "hello"}],
)
```

The same pattern works for any OpenAI-SDK-compatible framework (LangChain
`openai_api_base`, LiteLLM `api_base`, etc.). Streaming (`stream=True`) is
forwarded as SSE; when it finishes the span closes and records the chunk
events.

## Header propagation

The client may pass observability context as headers; they land on the
`llm.proxy.call` span's `SpanContext`:

| Header | Span field | If missing |
|---|---|---|
| `X-OryaObs-Trace-Id` | `context.trace_id` | new ULID generated |
| `X-OryaObs-User-Id` | `context.user_id` | empty |
| `X-OryaObs-Tenant-Id` | `context.tenant_id` (PP32 stamping applies) | empty |
| `X-OryaObs-Agent-Id` | `context.agent_id` | `"proxy"` |

`Authorization` is **never** forwarded — the proxy replaces it with the
upstream key. Hop-by-hop headers are stripped.

## Error handling

| Condition | Client gets | Span |
|---|---|---|
| Upstream timeout | `504` + `{"error": "upstream timeout", ...}` | `status=error`, `error.type=upstream_timeout` |
| Upstream connection error | `502` + `{"error": "upstream connection error", ...}` | `status=error`, `error.type=upstream_connection_error` |
| Upstream 4xx/5xx | forwarded verbatim (status + body) | `status=error`, `error.type=upstream_http_<code>` |
| Proxy internal error | `500` + `{"error": "proxy internal error", ...}` | `status=error`, `error.type=proxy_internal_error` |

Span creation/enqueue is fire-and-forget (`sdk._enqueue`) — a broken
observability pipeline never fails the proxied request.

## Configuration

See the `# --- LLM proxy mode (PP39 ...)` block in `infra/.env.example`. Key
vars: `AGENT_OBS_PROXY_MODE`, `AGENT_OBS_PROXY_HOST`/`_PORT`,
`AGENT_OBS_PROXY_UPSTREAM_PROVIDER`
(`openai`/`anthropic`/`azure_openai`/`custom`), `AGENT_OBS_PROXY_UPSTREAM_BASE_URL`,
`AGENT_OBS_PROXY_UPSTREAM_API_KEY_ENV`, `AGENT_OBS_PROXY_REQUEST_TIMEOUT_SECONDS`,
`AGENT_OBS_PROXY_STREAMING_ENABLED`, `AGENT_OBS_PROXY_LOG_BODIES`(+`_MAX_BYTES`).
Invalid combinations (unknown mode, unsupported provider, missing upstream key
env, `off`+shared) raise `agent_obs.proxy.ConfigError` at SDK construction —
fail-fast, before any traffic is proxied.

Bodies are stored on the span as `llm.input_text` / `llm.output_text` (when
`AGENT_OBS_PROXY_LOG_BODIES=true`, the default) so the in-process guardrail
masks PII before export and content sampling (`AGENT_OBS_CONTENT_RATE`)
applies. With `log_bodies=false` only sha256 + char counts are stored.

Not yet supported: mTLS for internal-only access (`AGENT_OBS_PROXY_TLS`) —
planned with the shared-proxy work (PP44).
