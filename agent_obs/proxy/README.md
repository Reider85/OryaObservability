# agent_obs.proxy — LLM-proxy observability mode (PP39)

Level-3 mode (PRODUCTION-PROMPTS §0.4): the SDK runs an in-process HTTP proxy
in front of the LLM provider, so agents you do not instrument directly still
produce `llm.call` spans and cost attributes. The FastAPI server that forwards
traffic and emits spans is PP40 (`agent_obs/proxy/server.py`); this package
currently ships the configuration layer only.

## Modes

| `AGENT_OBS_PROXY_MODE` | Behaviour |
|---|---|
| `off` (default) | In-process SDK, as in MVP/CRITICAL. Agent instruments via `sdk.llm_call()`. |
| `on` | **Mode A:** SDK proxies all LLM traffic. |
| `auto` | Failover: proxy comes up only if an SDK dependency's circuit breaker (PP38) is open. |
| — | `SHARED_PROXY_ENABLED=on` + `AGENT_OBS_PROXY_MODE=on` = **Mode B** (PP44, shared obs+security proxy). |
| — | `SHARED_PROXY_ENABLED=on` + `AGENT_OBS_PROXY_MODE=off` = **invalid**, fails fast at startup. |

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
`openai_api_base`, LiteLLM `api_base`, etc.).

## Configuration

See the `# --- LLM proxy mode (PP39 ...)` block in `infra/.env.example`. Key
vars: `AGENT_OBS_PROXY_MODE`, `AGENT_OBS_PROXY_UPSTREAM_PROVIDER`
(`openai`/`anthropic`/`azure_openai`/`custom`), `AGENT_OBS_PROXY_UPSTREAM_BASE_URL`,
`AGENT_OBS_PROXY_UPSTREAM_API_KEY_ENV`. Invalid combinations (unknown mode,
unsupported provider, missing upstream key env, `off`+shared) raise
`agent_obs.proxy.ConfigError` at SDK construction — fail-fast, before any
traffic is proxied.
