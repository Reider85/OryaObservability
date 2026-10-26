# AGENTS.md

## Project

`agent-obs` — Python 3.11+ async observability SDK for LLM agents. Instruments agent loops, LLM calls, and tool calls with traces, cost tracking, PII guardrails, and content sampling.

## Commands

```bash
# Install (editable)
pip install -e ".[dev]"

# Fast full suite (~90s, 1344 tests) — the default verification run
python -m pytest --ignore=tests/ha --ignore=tests/load -q --tb=short 2>&1

# Single test file / class / method
python -m pytest tests/test_span.py -q
python -m pytest tests/test_guardrail_engine.py::TestScenarioBlock -q
python -m pytest tests/test_guardrail_engine.py::TestScenarioBlock::test_injection_blocked -q

# Chaos tests (tests/ha): 30 tests, bring up docker-compose.ha.yml and
# time.sleep(60..120) per fixture — never run without the HA stack.
python -m pytest tests/ha/

# Load / performance tests (tests/load): slow p99 overhead tests
python -m pytest tests/load/

# Start/stop infra stack (PowerShell)
./scripts/start-all.ps1
./scripts/stop-all.ps1
```

**Do not run bare `pytest`:** `testpaths = ["tests"]` collects `tests/ha`
(Docker chaos + minute-long sleeps) and `tests/load`. Collection alone takes
~25–30s (torch/spacy imports) — that is normal, not a hang. In PowerShell
always append `2>&1` or stderr output is lost. `pytest-timeout` is **not
installed** — `--timeout=N` fails with "unrecognized arguments".

## Test conventions

- Framework: pytest + pytest-asyncio. `asyncio_mode = "auto"` in `pyproject.toml` means async tests run without `@pytest.mark.asyncio` — but many existing tests still have it. Either works.
- Tests mock heavy ML components (Presidio, DeBERTa) via `unittest.mock.MagicMock`. Do not import real models in unit tests.
- Tests that read the wall clock (day/night sampling modes) must pin time via `detect_mode(now=...)` or mock `detect_mode` — see `test_eval_adaptive_sampler.py`.
- Tests patching names that `scripts/cron/scheduler.py` binds via `from ... import ...` must patch `scripts.cron.scheduler.<name>`, not the source module.
- `tests/fixtures/ca.pem` is a self-signed CA for TLS tests.

## New test suites

- `tests/test_langfuse_event_mapping.py`: Unit tests for PP10 Langfuse event mapping functionality
- `tests/test_trace_viewer.py`: TestClient tests for trace viewer UI (requires ClickHouse mocking for full functionality)
- `tests/e2e/test_trace_viewer_e2e.py`: Playwright e2e tests for trace viewer (requires `playwright install chromium` once)
- `tests/test_compressed_expand_api.py`: Unit tests for PP19 compressed node expand API with RBAC/MFA support

## Lint / typecheck

```bash
python -m ruff check agent_obs tests   # baseline: ~830 errors, ~565 autofixable
python -m mypy agent_obs               # baseline: ~116 errors
```

The baseline is dirty by design — fix only findings in files you touched; do
not mass-refactor. There is no ruff/mypy config; both run with defaults.

## Architecture — key invariants

- **SDK is the only in-process component.** Exporters ship spans out-of-process. Observability must never block business logic.
- **Fire-and-forget export.** `asyncio.Queue(maxsize=100_000)` ring buffer between instrumentation and exporters. Full buffer = drop span + increment `dropped_spans_total`. Never block.
- **PII masking happens in-process before export.** `_apply_guardrail()` runs in `_enqueue()`, not in the collector. Plaintext PII must never leave the process boundary (architectural anti-pattern §8.3).
- **Guardrail is fail-closed for masking, fail-open for classification.** If PII masking fails, text is replaced with `[REDACTED:guardrail_unavailable]`. If injection classifier is unavailable, it returns `benign` (score 0.0).
- **Content sampling is deterministic by `trace_id`.** All spans sharing a trace agree on whether to store full text or just sha256 + char count. Rate controlled by `AGENT_OBS_CONTENT_RATE` (default 10%).
- **Guardrail verdicts are cached on spans** (`span.guardrail_verdicts`) so pre-call hooks and `_enqueue` don't re-detect the same text. The `guardrail_verdicts` dict is excluded from `to_dict()` serialization.
- **`agent.reasoning` spans are aggregated in-process (PP09).** `_enqueue` routes them through `SpanAggregator` (buffer 50 spans / 1s window) instead of the ring buffer; flushed aggregates re-enter normal export. Aggregation is enabled by default (`AGENT_OBS_AGGREGATION_ENABLED=true`).
- **Compliance catalog is a free side-product of masking (PC33).** `GuardrailEngine.check_input()` calls `ComplianceCatalog.record()` (sync dict increment, no I/O) for every `redacted_fields` entry. Persistence is a background `asyncio` task batching UPSERTs to Postgres `compliance_catalog` once per minute (`flush_interval`). On flush failure the buffer is kept and retried; `asyncpg` stays optional via `AGENT_OBS_COMPLIANCE_PG` (NullCatalogWriter fallback). `tool_name` for catalog rows comes from `SpanContext.tool_name`, set only in `tool_call`.

## Span types (5 in MVP after PP17)

| Type | Value |
|---|---|
| Root loop | `agent.loop` |
| LLM call | `llm.call` |
| Tool call | `tool.call` |
| Reasoning micro-span | `agent.reasoning` (PP09: routed through SpanAggregator when aggregation is enabled) |
| Compressed reasoning | `agent.reasoning.compressed` (PP17: tree compression summary nodes, tree-only, never exported) |

Level-2 types (`tool.input`, `llm.output`, etc.) are placeholders — do not use.

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `AGENT_OBS_ENABLED` | `true` | Master on/off. When false, decorators are no-op. |
| `AGENT_OBS_CONTENT_RATE` | `10` | Percent of traces keeping full prompt/response text. |
| `AGENT_OBS_GUARDRAIL_ENABLED` | `true` | Enable guardrail when engine is supplied. |
| `AGENT_OBS_PII_DETECTORS` | `email,phone,inn,passport,payment` | Comma-separated PII detector types. |
| `AGENT_OBS_PII_ML_ENABLED` | `false` | Enable ML-based PII detection. Currently uses MockAnalyzer (keyword stub) for ФИО/addresses/medical conditions. Real Presidio integration deferred. |
| `AGENT_OBS_INJECTION_MODEL` | `deepset/deberta-v3-base-prompt-injection` | HuggingFace model for injection classification. |
| `AGENT_OBS_FAIL_ON_CONFIG` | `1` | 1 = fail fast on missing Langfuse config; 0 = degrade to stdout exporter. |
| `TAIL_SAMPLER_COST_THRESHOLD` | `0.05` | USD threshold for keeping traces 100%. |
| `TAIL_SAMPLER_NORMAL_RATE` | `10` | Percent of normal traces kept by tail sampler. |
| `AGENT_OBS_COMPLIANCE_PG` | `auto` | Compliance catalog Postgres writer: `on`/`off`/`auto`. `auto` = Postgres only if asyncpg importable **and** `COMPLIANCE_PG_DSN` set, else in-memory no-op. |
| `COMPLIANCE_PG_DSN` | — | DSN for compliance catalog Postgres writes. Default when `AGENT_OBS_COMPLIANCE_PG=on`: warm-tier DSN (`postgresql://warm:warm@localhost:5433/warm_store`). |
| `AGENT_OBS_AGGREGATION_AUDIT` | `false` | Enable writing span aggregation flush audit events to ClickHouse audit_events_hot table (PP11). Best-effort: never blocks business logic. |
| `AGENT_OBS_AGGREGATION_ENABLED` | `true` | Route `agent.reasoning` spans through SpanAggregator (PP09). Priority: config > env > default. |
| `AGENT_OBS_AGGREGATION_MAX_COUNT` | `50` | Micro-spans buffered before an aggregate flush. |
| `AGENT_OBS_AGGREGATION_WINDOW_SECONDS` | `1.0` | Max age of buffered micro-spans before flush. |
| `AGENT_OBS_COMPLIANCE_TIER` | `dual` | `dual`/`warm_only`/`hot_only` — which catalog tiers `build_compliance_catalog()` wires (PC34). |
| `AGENT_OBS_INJECTION_THRESHOLD_LOW` / `_HIGH` | `0.5` / `0.85` | Injection classifier verdict bands: below low = benign, at-or-above high = block. |
| `AGENT_OBS_LOG_LEVEL` | `INFO` | Root logging level for SDK and cron scheduler. |
| `AGENT_OBS_USER_HASH_SALT` | `""` | Salt for user hashing (empty = no salt). |
| `AGENT_OBS_TLS_CA` / `_CERT` / `_KEY` | — | mTLS material for exporters (PP02). |
| `AGENT_OBS_EVAL_ADAPTIVE_SAMPLER` | `false` | Enable adaptive eval sampler in LLM judge when a judge is supplied. PP14. |
| `TREE_COMPRESSION_THRESHOLD` | `20` | PP17: Max subtree size before compression to summary node (tree compression). |
| `TREE_EMBEDDED_SIZE` | `10` | PP20: Max leaf-children embedded into parent events (matreshka). |
| `AGENT_OBS_TREE_SUMMARY_ENABLED` | `true` | Enable async LLM summary generation for compressed nodes (PP18). |
| `AGENT_OBS_TREE_SUMMARY_CACHE_TTL` | `604800` | Redis TTL for tree summary cache in seconds (PP18). |
| `AGENT_OBS_EVAL_INCIDENT_RATE_LIMIT` | `100` | Max incident eval jobs per minute (Redis-based rate limiter). PP14. |
| `AGENT_OBS_RBAC_ROLES` | — | PP19: User→role mapping for compressed expand API (e.g., "alice:admin,bob:developer"). Unknown users fail-closed. |
| `X-OryaObs-User` | — | PP19: HTTP header with user login for RBAC in compressed expand API. |
| `X-OryaObs-MFA` | — | PP19: HTTP header with MFA token for PII field requests in compressed expand API. |

Full list: grep `AGENT_OBS_` under `agent_obs/` and `scripts/`. The
`AGENT_OBS_CRON_*` family (~30 vars: retention days, specs, batch sizes,
dry-run) lives in `scripts/cron/` and `infra/.env.example` — not listed here.

## Code style

- No linter/formatter config exists in `pyproject.toml` beyond `[tool.pytest.ini_options]` — see "Lint / typecheck" above for the commands and dirty baseline.
- Existing code uses `from __future__ import annotations` in most modules.
- Prefer async-first. Guardrail engine methods are `async def` even though current work is CPU-bound (future-proofing for real Vault HTTP calls).
- `PIIMatch` is defined in two places: `pii_detector.py` and `pii_types.py`. The canonical import is `from agent_obs.guardrail.pii_types import PIIMatch`.

## Infrastructure

Docker Compose stack in `infra/` provides: Langfuse v3, OTel Collector, Prometheus, Vault, ClickHouse, Redis, MinIO, Postgres. The stack does **not** expose Postgres/Redis/ClickHouse on host ports (they conflict with local installs). Connect to Langfuse on `:3000` and OTel on `:4317`/`:4318`.

Langfuse v3 requires `CLICKHOUSE_URL`, `ENCRYPTION_KEY`, `NEXTAUTH_SECRET`, and `LANGFUSE_SALT` in `infra/.env`. Copy from `.env.example` and set real values before first boot.

## Gotchas

- `_atexit_handler` is defined twice in `agent_obs/observability.py` (lines 1635 and 1649) — the second silently overwrites the first.
- `PilotAgent` in `pilot_agent/agent.py` (line 82) calls `compute_cost(usage, model=config.model, book=self.price_book)` with keyword `book=`, while `llm_call()` in `agent_obs/observability.py` (line 1769) uses positional `price_book=`. These are different call sites — be careful when changing the `compute_cost` signature.
- Tests in `tests/test_guardrail_engine.py` use a `PIIMatch` from `pii_detector.py` (local re-definition), not from `pii_types.py`. The two classes are structurally identical but not the same type — don't mix imports in tests without checking.
- The injection classifier loads the HuggingFace model lazily on first `classify()` call. First call is slow (cold-start). In tests, always mock `InjectionClassifier` — never let it download real weights.
- ML PII detection (when `AGENT_OBS_PII_ML_ENABLED=true`) currently uses MockAnalyzer (keyword stub) with hardcoded ФИО/addresses/medical conditions. Real Presidio integration requires `presidio-analyzer` and `spacy` dependencies (currently declared but unused).
- **PC24: Embedding storage sidecar.** Embeddings are stored in `span_embeddings` table, not `spans_hot.response_embedding`. The queue tuple is 5 elements: `(trace_id, span_id, agent_id, tenant_id, embedding)`. `embeddings_stored_total` increments only on successful ClickHouse writes; `embeddings_storage_failed_total` increments on queue-full or CH failure; `embeddings_redis_fallback_total` increments on Redis fallback. HotStore `get_spans()` returns `response_embedding` from spans_hot for compatibility, but drift/Phoenix query `span_embeddings` directly.

- **PP10: Trace viewer e2e tests.** Run with `python -m pytest tests/e2e/ -q 2>&1`. Requires `playwright install chromium` once. Tests automatically skip if Playwright unavailable.
