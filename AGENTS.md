# AGENTS.md

## Project

`agent-obs` — Python 3.11+ async observability SDK for LLM agents. Instruments agent loops, LLM calls, and tool calls with traces, cost tracking, PII guardrails, and content sampling.

## Commands

```bash
# Install (editable)
pip install -e ".[dev]"

# Run all tests
pytest

# Run single test file
pytest tests/test_span.py

# Run single test class or method
pytest tests/test_guardrail_engine.py::TestScenarioBlock
pytest tests/test_guardrail_engine.py::TestScenarioBlock::test_injection_blocked

# Load / performance tests
pytest tests/load/

# Start infra stack (PowerShell)
./scripts/start-all.ps1

# Stop infra stack
./scripts/stop-all.ps1
```

## Test conventions

- Framework: pytest + pytest-asyncio. `asyncio_mode = "auto"` in `pyproject.toml` means async tests run without `@pytest.mark.asyncio` — but many existing tests still have it. Either works.
- Tests mock heavy ML components (Presidio, DeBERTa) via `unittest.mock.MagicMock`. Do not import real models in unit tests.
- `tests/load/test_overhead.py` tests p99 latency and event-loop lag — these are slow and may fail on low-resource machines.
- `tests/fixtures/ca.pem` is a self-signed CA for TLS tests.

## Architecture — key invariants

- **SDK is the only in-process component.** Exporters ship spans out-of-process. Observability must never block business logic.
- **Fire-and-forget export.** `asyncio.Queue(maxsize=100_000)` ring buffer between instrumentation and exporters. Full buffer = drop span + increment `dropped_spans_total`. Never block.
- **PII masking happens in-process before export.** `_apply_guardrail()` runs in `_enqueue()`, not in the collector. Plaintext PII must never leave the process boundary (architectural anti-pattern §8.3).
- **Guardrail is fail-closed for masking, fail-open for classification.** If PII masking fails, text is replaced with `[REDACTED:guardrail_unavailable]`. If injection classifier is unavailable, it returns `benign` (score 0.0).
- **Content sampling is deterministic by `trace_id`.** All spans sharing a trace agree on whether to store full text or just sha256 + char count. Rate controlled by `AGENT_OBS_CONTENT_RATE` (default 10%).
- **Guardrail verdicts are cached on spans** (`span.guardrail_verdicts`) so pre-call hooks and `_enqueue` don't re-detect the same text. The `guardrail_verdicts` dict is excluded from `to_dict()` serialization.
- **Compliance catalog is a free side-product of masking (PC33).** `GuardrailEngine.check_input()` calls `ComplianceCatalog.record()` (sync dict increment, no I/O) for every `redacted_fields` entry. Persistence is a background `asyncio` task batching UPSERTs to Postgres `compliance_catalog` once per minute (`flush_interval`). On flush failure the buffer is kept and retried; `asyncpg` stays optional via `AGENT_OBS_COMPLIANCE_PG` (NullCatalogWriter fallback). `tool_name` for catalog rows comes from `SpanContext.tool_name`, set only in `tool_call`.

## Span types (only 3 in MVP)

| Type | Value |
|---|---|
| Root loop | `agent.loop` |
| LLM call | `llm.call` |
| Tool call | `tool.call` |

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

## Code style

- No linter/formatter config exists in `pyproject.toml` beyond `[tool.pytest.ini_options]`. `.ruff_cache` and `.mypy_cache` directories exist but no config files — likely run ad-hoc.
- Existing code uses `from __future__ import annotations` in most modules.
- Prefer async-first. Guardrail engine methods are `async def` even though current work is CPU-bound (future-proofing for real Vault HTTP calls).
- `PIIMatch` is defined in two places: `pii_detector.py` and `pii_types.py`. The canonical import is `from agent_obs.guardrail.pii_types import PIIMatch`.

## Infrastructure

Docker Compose stack in `infra/` provides: Langfuse v3, OTel Collector, Prometheus, Vault, ClickHouse, Redis, MinIO, Postgres. The stack does **not** expose Postgres/Redis/ClickHouse on host ports (they conflict with local installs). Connect to Langfuse on `:3000` and OTel on `:4317`/`:4318`.

Langfuse v3 requires `CLICKHOUSE_URL`, `ENCRYPTION_KEY`, `NEXTAUTH_SECRET`, and `LANGFUSE_SALT` in `infra/.env`. Copy from `.env.example` and set real values before first boot.

## Gotchas

- `_atexit_handler` is defined twice in `observability.py` (lines 720 and 734) — the second silently overwrites the first.
- `PilotAgent` in `pilot_agent/agent.py` calls `compute_cost(usage, model=config.model, book=self.price_book)` with keyword `book=`, while `llm_call()` uses positional `price_book=`. These are different call sites — be careful when changing the `compute_cost` signature.
- Tests in `tests/test_guardrail_engine.py` use a `PIIMatch` from `pii_detector.py` (local re-definition), not from `pii_types.py`. The two classes are structurally identical but not the same type — don't mix imports in tests without checking.
- The injection classifier loads the HuggingFace model lazily on first `classify()` call. First call is slow (cold-start). In tests, always mock `InjectionClassifier` — never let it download real weights.
- The historical "test_cron_jobs.py / test_cron_cold_migration.py hang on Windows" issue was a test-mock bug, not an environment issue: `get_traces_for_partition` mocks returned rows unconditionally, so the warm→cold delete phase's keyset loop never terminated. Fixed (mocks now return `[]` once `last_trace_id` is set). Both files run green.
- ML PII detection (when `AGENT_OBS_PII_ML_ENABLED=true`) currently uses MockAnalyzer (keyword stub) with hardcoded ФИО/addresses/medical conditions. Real Presidio integration requires `presidio-analyzer` and `spacy` dependencies (currently declared but unused).
