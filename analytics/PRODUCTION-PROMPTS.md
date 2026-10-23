# PRODUCTION-PROMPTS.md — Промпты для ИИ-агента: выход на Уровень 3 (PRODUCTION-READY)

**Назначение:** готовая серия промптов для ИИ-кодинг-агента (Claude Code, Cursor, Codex, Aider или любой LLM-агент), которая доводит проект до **Уровня 3 PRODUCTION-READY** observability-стека для LLM-агента. После этого уровня система соответствует всем нефункциональным требованиям из §1.4 `ARCHITECT.md`: overhead < 2 мс p99, потеря трейсов < 0.1%, доступность 99.9%, retention 14д / 90д / 1г.

**Источники:** `ARCHITECT.md` v1.0 (§1.4, §3, §6, §7, §8, §10, §11.1 — ТРИЗ-улучшения #1, #2, #4, #8, #9, #10), `ROADMAP.md` v1.0 (§6 — Уровень 3 production-ready, эпики E3.1–E3.8, задачи T3.x.x, exit criteria §6.5), `MVP-PROMPT.md` v1.0 (контракты P0+P02..P06), `CRITICAL-PROMPTS.md` v1.0 (контракты PC0+PC01..PC36).

**Формат:** универсальные промпты без привязки к среде. Каждый промпт самодостаточен при условии, что в сессию сначала отправлен мастер-промпт **PP0**. Промпты опираются на готовый MVP- и CRITICAL-код (SDK, guardrail, eval, drift, tiered storage, adaptive sampling base — 256/256+ тестов PASS).

**Принцип non-contradiction (§1.2 ROADMAP):** ни одна задача PRODUCTION-READY не отменяет решения MVP и CRITICAL. Контракты SDK (декораторы `@agent_observed`, context managers `llm_call()` / `tool_call()`, `BaseExporter`, `Span` / `SpanContext`, `GuardrailEngine`, `BaseEvaluator`, `BaseStorage`) сохраняются. PRODUCTION-READY добавляет новые слои (adaptive sampling v2, tree-compression, drill-down, SLO, RBAC, HA) и **два новых архитектурных режима прокси** (см. §0.4).

---

## 0. Как пользоваться

### 0.1. Правила скармливания

1. **Каждая новая сессия** агента начинается с промпта **PP0** (мастер-контекст PRODUCTION-READY), затем отправляется **один** тикет-промпт (PP01…PP60).
2. **Одна сессия = один тикет = один коммит.** Не склеивайте несколько тикетов в одну сессию — падает качество и теряется контроль.
3. После выполнения агента **обязательно** дождитесь от него: (а) список сделанного, (б) результат запуска тестов, (в) заполненный чек-лист DoD из промпта. Нет отчёта — тикет не принят.
4. Если агент предлагает сделать больше, чем указано в промпте, — **отказывайте**: расширение scope — Уровень 4 (Улучшения, ИКР-направленные: sidecar-SDK, federation, cost-aware routing). НЕ РЕАЛИЗУЕМ.
5. Промпты с пометкой **[ПАРАЛЛЕЛЬ]** можно выполнять одновременно в отдельных сессиях/субагентах — они не пересекаются по файлам.
6. Промпты со скоупом MVP/CRITICAL-файлов (`agent_obs/observability.py`, `agent_obs/metrics.py`, `agent_obs/storage/`, `agent_obs/exporters/`) — строгая последовательность внутри волны.
7. **Сквозная нумерация:** промпты нумеруются без пропусков — `PP0 → PP01 → PP02 → … → PP60`. Префикс `PP` = «Production Prompt». Не путать с `P` (MVP) и `PC` (CRITICAL).

### 0.2. Карта волн выполнения (критический путь для скорости)

| Волна | Промпты | Тикеты | Можно параллельно с | Зависимости |
|---|---|---|---|---|
| **0** | PP01 → PP02 | T3.0.1, T3.0.2 (production-инфра: HA, mTLS) | Волны 1–10 | MVP+CRITICAL |
| **1** | PP03 → PP04 → PP05 → PP06 → PP07 | T3.1.1–T3.1.5 (adaptive sampling v2) | Волна 2 (после PP07) | PC29–PC32 (CRITICAL adaptive base) |
| **2** | PP08 → PP09 → PP10 → PP11 | T3.2.1–T3.2.4 (span-aggregation в SDK) | Волна 3 (после PP11) | MVP SDK каркас (P04–P05) |
| **3** | PP12 → PP13 → PP14 → PP15 | T3.3.1–T3.3.4 (adaptive eval-sampling день/ночь/инцидент) | Волна 4 (после PP15) | PC09–PC13 (CRITICAL eval base) |
| **4** | PP16 → PP17 → PP18 → PP19 → PP20 | T3.4.1–T3.4.5 (tree + compression длинных трейсов) | Волна 5 (после PP20) | PP08–PP11 (span-aggregation) |
| **5** | PP21 → PP22 → PP23 → PP24 | T3.5.1–T3.5.4 (on-demand drill-down cold storage) | Волна 6 (после PP24) | PC22–PC23 (CRITICAL tiered storage) |
| **6** | PP25 → PP26 → PP27 → PP28 → PP29 | T3.6.1–T3.6.5 (SLO/SLA enforcement + alerting) | Волна 7 (после PP29) | PP03–PP07 (sampling v2), MVP metrics |
| **7** | PP30 → PP31 → PP32 → PP33 → PP34 | T3.7.1–T3.7.5 (RBAC + multi-tenant изоляция) | Волна 8 (после PP34) | PP02 (mTLS), CRITICAL Vault (PC02) |
| **8** | PP35 → PP36 → PP37 → PP38 | T3.8.1–T3.8.4 (HA 99.9%, репликация, failover) | Волна 9 (после PP38) | PP01 (HA-инфра) |
| **9** | PP39 → PP40 → PP41 → PP42 → PP43 | **NEW MODE A:** OryaObservability как LLM-proxy (настройкой) | Волна 10 (после PP43) | MVP SDK, CRITICAL guardrail |
| **10** | PP44 → PP45 → PP46 → PP47 → PP48 | **NEW MODE B:** Shared proxy OryaObservability + OryaSecurity (настройкой) | Волна 11 (после PP48) | PP39–PP43 (Mode A) |
| **11** | PP49 | Верификация PRODUCTION-READY (все exit-criteria §6.5) | после всех | все тикеты |
| **12** | PP50 (шаблон) | Цикл фиксов по проваленным критериям | итеративно | — |
| **13** | PP51 → PP52 → PP53 → PP54 → PP55 → PP56 → PP57 → PP58 → PP59 → PP60 | **80/20 улучшения:** meta-obs, chaos, prompt-versioning, semantic cache, cost-routing-preview, hot-reload, drift-explainability, e2e-encryption, golden-signals-sli, obs-cost-budget | после PP49 | все тикеты |

**Быстрая последовательность для одного агента (без параллели):**
`PP0 → PP01 → PP02 → PP03 → PP04 → PP05 → PP06 → PP07 → PP08 → PP09 → PP10 → PP11 → PP12 → PP13 → PP14 → PP15 → PP16 → PP17 → PP18 → PP19 → PP20 → PP21 → PP22 → PP23 → PP24 → PP25 → PP26 → PP27 → PP28 → PP29 → PP30 → PP31 → PP32 → PP33 → PP34 → PP35 → PP36 → PP37 → PP38 → PP39 → PP40 → PP41 → PP42 → PP43 → PP44 → PP45 → PP46 → PP47 → PP48 → PP49 → (PP50 при провалах) → PP51 → … → PP60`

### 0.3. Стримы для параллельной команды (рекомендация §9.2 ROADMAP)

**Стрим A (Performance-стек, 1 подкоманда):** PP01 → PP03→PP07 → PP08→PP11 → PP16→PP20 → PP35→PP38
**Стрим B (Storage & drill-down, 1 подкоманда):** PP02 → PP21→PP24 → PP25→PP29
**Стрим C (Security & multi-tenant, 1 подкоманда):** PP30→PP34 → PP44→PP48
**Стрим D (Eval & adaptive, 1 подкоманда):** PP12→PP15 → PP51→PP55
**Стрим E (Proxy modes, 1 подкоманда):** PP39→PP43 → PP44→PP48
**Завершение:** интеграция всех пяти стримов в общую систему, затем PP49 + 80/20 улучшения PP51–PP60.

### 0.4. Два новых архитектурных режима (главная инновация Уровня 3)

> **MODE A — OryaObservability как LLM-proxy.** SDK поднимает in-process (или side-by-side) HTTP-прокси, через который агент обращается к LLM-провайдеру. Включается флагом `AGENT_OBS_PROXY_MODE=on`. Прокси: (1) перехватывает request/response bodies, (2) выставляет cost-атрибуты автоматически (без ручного кода в агенте), (3) генерирует span `llm.call` сам — агент пишет «обычный» `openai.chat.completions.create(...)`, но URL указывает на `localhost:8765/llm/v1`. Триггер Уровня 3: «observability должна работать на agent'ах, которые мы не контролируем».

> **MODE B — Shared proxy OryaObservability + OryaSecurity.** Один HTTP-прокси обслуживает и observability-tap (cost, spans, latency), и security-scan (PII, injection, secrets — из соседнего репо `OryaSecurity`). Включается флагом `SHARED_PROXY_ENABLED=on` + `SHARED_PROXY_SECURITY_URL`. Single audit-trail, single decision-point, single latency-budget. Триггер: «observability и security — две стороны одного потока agent→LLM; двойной прокси = двойной latency».

**Совместимость режимов:**
| Конфиг | Поведение |
|---|---|
| `AGENT_OBS_PROXY_MODE=off`, `SHARED_PROXY_ENABLED=off` | **Default:** SDK в in-process режиме (как MVP/CRITICAL). Агент сам инструментирует вызовы через `sdk.llm_call()`. |
| `AGENT_OBS_PROXY_MODE=on`, `SHARED_PROXY_ENABLED=off` | **Mode A:** OryaObservability проксирует LLM-трафик. |
| `AGENT_OBS_PROXY_MODE=on`, `SHARED_PROXY_ENABLED=on` | **Mode B:** Shared proxy = OryaObservability + OryaSecurity вместе. |
| `AGENT_OBS_PROXY_MODE=off`, `SHARED_PROXY_ENABLED=on` | **Invalid** (запрещено на старте, fail-fast). |

---

## PP0 — МАСТЕР-ПРОМПТ: контекст PRODUCTION-READY уровня

> Отправлять **первым** в каждой новой сессии агента. Все дальнейшие промпты опираются на этот контекст. Промпт расширяет PC0 из `CRITICAL-PROMPTS.md` новыми слоями (adaptive sampling v2, tree-compression, drill-down, SLO, RBAC, HA) и двумя новыми архитектурными режимами прокси (см. §0.4).

```text
РОЛЬ
Ты — senior Python-инженер, внедряешь PRODUCTION-READY (Уровень 3) observability-стека для LLM-агента.
Работаешь строго в рамках одного тикета из ROADMAP Уровня 3 (PRODUCTION-READY). Не расширяешь scope
на Уровень 4 (Улучшения, ИКР): sidecar-SDK, federation, cost-aware model routing, LLM-intrinsic trace,
prompt-template codegen — НЕ РЕАЛИЗУЕМ.

ПРОЕКТ
Пакет SDK: agent_obs (Python 3.11+, asyncio-only). MVP реализован: SpanContext, Span, ObservabilitySDK с
декоратором @agent_observed и context managers llm_call()/tool_call(), LangfuseExporter, ring buffer
asyncio.Queue(maxsize=100_000) с _export_worker (drain 50ms, batch ≤512, fan-out), _compute_cost() с
price_book.yaml, tail-sampler с фиксированной политикой, content-sampling по trace_id. CRITICAL реализован:
GuardrailEngine (PII + injection), FieldMasker, VaultClient с MFA, async eval-pipeline (rule + llm-judge +
embedding + late annotation), DriftDetector (KL), Hot/Warm/Cold tiered storage, ComplianceCatalog, adaptive
sampling v1 (cpu/error-rate driven), 4 новых span-типа (agent.reasoning, eval.judge, guardrail.check, audit.event).
256/256 + тесты CRITICAL PASS.

Цель PRODUCTION-READY: довести observability до полноценного production-grade — adaptive sampling v2
(eval-aware + dynamic rate), span-aggregation для длинных reasoning-трейсов, tree + compression,
on-demand drill-down в cold storage, SLO/SLA enforcement с alerting, RBAC + multi-tenant изоляция,
HA 99.9% всех компонентов. + два новых режима прокси (MODE A — OryaObs как LLM-proxy; MODE B — shared
proxy OryaObs + OryaSecurity).

СТРУКТУРА РЕПОЗИТОРИЯ (расширение CRITICAL — поддерживай её)
agent-obs/                                # OryaObservability
├── agent_obs/
│   ├── __init__.py
│   ├── observability.py                  # ObservabilitySDK, Span, SpanContext, _enqueue
│   ├── content_sampling.py
│   ├── span_aggregator.py                # NEW на PROD: микро-span → агрегат (E3.2)
│   ├── tree_compressor.py                # NEW на PROD: tree + compression для длинных трейсов (E3.4)
│   ├── guardrail/                        # CRITICAL — без изменений контрактов
│   │   ├── engine.py
│   │   ├── pii_detector.py
│   │   ├── injection_classifier.py
│   │   ├── field_masker.py
│   │   └── vault_client.py
│   ├── eval/                             # CRITICAL — расширяем adaptive-sampling'ом (E3.3)
│   │   ├── base.py
│   │   ├── rule_based.py
│   │   ├── llm_judge.py
│   │   ├── embedding.py
│   │   ├── late_annotation.py
│   │   └── adaptive_sampler.py           # NEW на PROD: день/ночь/инцидент (T3.3.1)
│   ├── drift/                            # CRITICAL — расширяем explainability (PP57)
│   ├── storage/                          # CRITICAL — расширяем drill-down (E3.5)
│   │   ├── hot.py
│   │   ├── warm.py
│   │   ├── cold.py
│   │   ├── migrations.py
│   │   └── drill_down.py                 # NEW на PROD: on-demand cold-resurrection (T3.5.1)
│   ├── compliance/
│   ├── exporters/
│   │   ├── base.py
│   │   ├── langfuse_exporter.py
│   │   ├── otlp_mapping.py
│   │   ├── stdout_exporter.py
│   │   └── tail_sampler.py               # CRITICAL — расширяем до adaptive v2 (E3.1)
│   ├── proxy/                            # NEW на PROD: режимы прокси (E3.9, E3.10)
│   │   ├── __init__.py
│   │   ├── server.py                     # LLmProxyServer: FastAPI, перехват /llm/v1/chat/completions
│   │   ├── obs_pipeline.py               # MODE A: observability-tap (cost, span, latency)
│   │   ├── shared_pipeline.py            # MODE B: observability + security вместе
│   │   ├── security_client.py            # клиент к OryaSecurity-scanner (HTTP)
│   │   ├── decision_router.py            # единая точка policy-решения (allow/flag/block)
│   │   └── audit_trail.py                # единый audit-trail для обоих режимов
│   ├── cost/
│   │   ├── compute_cost.py
│   │   └── price_book.py
│   ├── rbac/                             # NEW на PROD: RBAC + multi-tenant (E3.7)
│   │   ├── __init__.py
│   │   ├── roles.py                      # admin / sre / developer / dpo / auditor
│   │   ├── policy_engine.py              # OPA/Rego или pydantic-based
│   │   └── tenant_isolator.py            # tenant_id → изоляция в storage + в proxy
│   ├── slo/                              # NEW на PROD: SLO/SLA enforcement (E3.6)
│   │   ├── __init__.py
│   │   ├── definitions.py                # SLO-каталог: availability, latency, quality, cost
│   │   ├── burn_rate.py                  # multi-window multi-burn-rate alerting
│   │   └── error_budget.py               # error budget tracking + policy actions
│   ├── ha/                               # NEW на PROD: HA 99.9% (E3.8)
│   │   ├── __init__.py
│   │   ├── replication.py                # ClickHouse replication, Postgres streaming
│   │   ├── failover.py                   # Vault/Redis/Langfuse failover policies
│   │   └── health_checks.py              # active health probes для всех зависимостей
│   └── metrics.py                        # MVP+CRITICAL + NEW SLO/HA/proxy-метрики
├── pilot_agent/agent.py
├── infra/
│   ├── docker-compose.yml                # + HA-реплики, multi-AZ
│   ├── docker-compose.ha.yml             # NEW: HA-стек (3 реплики каждого сервиса)
│   ├── otel-collector/config.yaml
│   ├── prometheus.yml
│   ├── prometheus-rules.yml              # +SLO/burn-rate/proxy-метрики
│   ├── vault/
│   ├── eval/
│   ├── storage/
│   ├── proxy/                            # NEW: deployment артефакты прокси
│   │   ├── Dockerfile
│   │   └── supervisor.conf
│   ├── rbac/                             # NEW: OPA policies, role mappings
│   └── ha/                               # NEW: HA configs, healthcheck probes
├── tests/
├── docs/
└── pyproject.toml

ТИПЫ SPAN'ОВ (на PROD — те же 7 + 1 NEW)
- agent.loop, llm.call, tool.call — MVP, без изменений
- agent.reasoning, eval.judge, guardrail.check, audit.event — CRITICAL, без изменений
- llm.proxy.call — NEW (только в MODE A/B: span созданный прокси-сервером, parent_span_id
  отсутствует до тех пор пока прокси не свяжет с root через X-OryaObs-Trace-Id header)

КОНТРАКТЫ PRODUCTION-READY (канонические, менять нельзя)

@dataclass
class SamplingPolicy:                      # E3.1 — adaptive sampling v2
    name: str                             # "error_keep", "eval_fail_keep", "cost_high_keep", "random"
    predicate: Callable[[Span, list[EvalResult]], bool]
    keep_rate: float                       # 0.0..1.0; 1.0 = keep 100%
    reason_label: str                     # для метрики tail_sampler_kept_ratio_by_reason

@dataclass
class SpanAggregate:                       # E3.2 — span-aggregation
    parent_span_id: str
    span_type: str                         # тип агрегирующего span'а (обычно agent.reasoning)
    events: list[dict]                     # микро-spans как events
    children_count: int
    bytes_saved: int

@dataclass
class TraceTree:                           # E3.4 — tree + compression
    root: Span
    children: list["TraceTree"]
    compressed: bool                       # True если subtree было сжато
    compression_ratio: float

@dataclass
class DrillDownRequest:                    # E3.5 — on-demand drill-down
    trace_id: str
    requested_by: str                      # user_id с RBAC-проверкой
    fields: list[str]                      # какие поля восстановить (например ["llm.input_text"])
    mfa_token: str | None = None           # для PII-полей — обязательно

class RBAC:                                 # E3.7
    @dataclass
    class Role:
        name: str                          # admin / sre / developer / dpo / auditor / agent
        permissions: frozenset[str]        # "trace:read", "pii:recover", "policy:write", ...
    def check(self, user: str, action: str, resource: str) -> bool: ...

class SLOBurnRate:                          # E3.6 — multi-window burn-rate
    def is_burned(self, window: str, slo: str) -> bool: ...  # windows: "5m", "1h", "6h", "1d"

class LLMProxyServer:                      # E3.9, E3.10 — proxy modes
    async def handle_chat_completions(self, request: Request) -> Response: ...

ЖЕЛЕЗНЫЕ ПРАВИЛА PRODUCTION-READY (нарушение = тикет не принят)
1. Все контракты MVP и CRITICAL сохраняются: SDK fire-and-forget, async-only, cost в атрибутах span,
   PII-маскинг в процессе, LLM-as-judge async, late annotation, Vault MFA, tiered storage.
2. Adaptive sampling v2 НЕ уменьшает observability критических трейсов: 100% для error/security/
   eval-fail/cost-high — без исключений. Динамическая rate применяется только к нормальным трейсам.
3. Span-aggregation обратим: из агрегированного span'а можно восстановить дерево (events → микро-spans).
   Сжатие не должно уничтожать диагностическую ценность.
4. Drill-down в cold storage — по RBAC + MFA для PII-полей. Аудит каждого drill-down в audit-trail.
5. SLO violation > 5 минут = alert. Multi-window burn-rate (5m + 1h) — для быстрого обнаружения.
   Single-window alerts запрещены (слишком много ложных срабатываний).
6. RBAC: tenant-изоляция в storage (ClickHouse tenant_id partitioning, Postgres row-level security).
   Агент не должен видеть трейсы другого tenant'а — даже при прямом запросе по trace_id.
7. HA: каждый компонент переживает потерю 1 реплики без деградации SLO. RPO ≤ 5 минут, RTO ≤ 1 минута.
8. MODE A (proxy): агент работает с обычным OpenAI-совместимым API — никаких изменений в коде агента
   не требуется (только base_url поменять). Прокси = прозрачная прослойка.
9. MODE B (shared proxy): security-scan и observability-tap выполняются в одной транзакции с единым
   decision-router. Решение security (allow/flag/block) приоритетнее observability — если security
   block, observability-tap всё равно пишет span + audit-event (record-then-block).
10. Не вводить Уровень 4: sidecar-SDK (отдельный процесс вместо in-process SDK), federation
    (tenant-level trace map между инстансами), cost-aware model routing (динамический выбор модели
    по budget+eval), LLM-intrinsic trace (модель сама генерирует trace-токены), prompt-template codegen.
11. Код и комментарии — на английском; ответы мне — на русском.
12. После каждого тикета: прогнать pytest, сделать один коммит (conventional commits:
    feat(sampling): ..., feat(tree): ..., feat(proxy): ..., feat(rbac): ...), отчитаться по чек-листу DoD.

СТЕК
Python 3.11+, asyncio, dataclasses, pytest + pytest-asyncio, prometheus-client, pyyaml, httpx.
CRITICAL-зависимости: presidio-analyzer, transformers, pyotp, hvac, redis+rq, clickhouse-driver,
psycopg, boto3, pyarrow, arize-phoenix-client.
NEW на PROD: fastapi + uvicorn (proxy server), opa-python (RBAC policy), asyncpg (Postgres streaming),
clickhouse-driver-replica (CH cluster), pyjwt (proxy auth), websockets (proxy streaming), locust (HA chaos).
```

---

## ВОЛНА 0: Production-ready инфраструктура (HA + mTLS)

> Два инфра-промпта, расширяют docker-compose до HA-конфигурации и включают mTLS между сервисами.
> [ПАРАЛЛЕЛЬ] с Волнами 1–10 — файлы в `infra/ha/` и `infra/rbac/`.

### PP01 — [T3.0.1] HA-инфраструктура: multi-AZ реплики для Langfuse, ClickHouse, Vault, Redis, Postgres

> PP0. Запускать можно параллельно с Волнами 1–10 — файлы в `infra/ha/`.

```text
ЗАДАЧА (тикет T3.0.1)
Развернуть HA-стек всех observability-компонентов в docker-compose: 3 реплики каждого сервиса,
репликация данных, автоматический failover.

ЧТО СДЕЛАТЬ
1. infra/docker-compose.ha.yml — отдельный compose-файл для HA-режима:
- langfuse: 3 реплики (через docker-compose `deploy.replicas: 3`), балансировка через nginx
  (сервис `langfuse-lb`), shared ClickHouse/Postgres/Redis.
- clickhouse: 2 реплики + Keeper (Zookeeper-подобный) для coordination; шардирование по tenant_id,
  репликация по agent_id. Preset `infra/ha/clickhouse-cluster.xml`.
- postgres: 17 + repmgr (streaming replication, 1 primary + 2 replicas) + pgBouncer (connection pool).
- redis: 7 + redis-sentinel (3 sentinel'а, automatic failover) для queue и cache.
- vault: 2 реплики с raft storage (не file storage, как в CRITICAL PC02), auto-unseal через transit.
- minio: distributed mode (4 узла, erasure coding), buckets с репликацией.
- otel-collector: 3 реплики, load-balanced через OTLP `load_balancing` exporter.

2. infra/ha/healthchecks/ — health-probes для каждого сервиса:
- /health для langfuse (HTTP 200 = ok)
- SELECT 1 для clickhouse / postgres
- redis-cli ping для redis
- v1/sys/health для vault
- mc admin info для minio

3. .env.example.ha — все переменные HA-режима:
- LANGFUSE_HA_MODE=true, CLICKHOUSE_CLUSTER=observability_cluster
- POSTGRES_PRIMARY_HOST, POSTGRES_REPLICA_HOSTS (comma-separated)
- REDIS_SENTINEL_HOSTS, VAULT_RAFT_NODES
- MINIO_DISTRIBUTED_MODE=true, MINIO_ENDPOINTS

4. README в infra/ha/:
- как поднять HA-стек (docker compose -f docker-compose.ha.yml up -d)
- как сэмулировать падение узла (docker stop <service>-1)
- как проверить, что failover произошёл (sentinel logs, repmgr switchover, vault raft state)
- RPO/RTO метрики после теста

5. Скрипт scripts/ha-chaos-test.sh:
- поднимает HA-стек
- убивает по одной реплике каждого сервиса
- проверяет, что observability продолжает работать (отправка тестового span'а проходит)
- замеряет время восстановления (RTO)

DoD
- [ ] docker compose -f docker-compose.ha.yml up -d поднимает 3× сервисы без ошибок
- [ ] При остановке 1 реплики любого сервиса SDK продолжает отправлять span'ы без потерь
- [ ] Failover происходит автоматически < 60 секунд для redis, < 30 секунд для postgres, < 10 секунд для vault
- [ ] RPO ≤ 5 минут (по логам репликации), RTO ≤ 1 минута (по тесту chaos-test)
- [ ] .env.example.ha заполнен, README в infra/ha/ создан
- [ ] pytest tests/ha/ зелёный (smoke-тесты HA-режима)

Коммит: feat(infra): add HA stack with multi-az replicas and failover for all observability services
```

---

### PP02 — [T3.0.2] mTLS / cert-management для всех внутренних сервисов

> PP0 + PP01. Файлы в `infra/rbac/` и `infra/ha/certs/`.

```text
ЗАДАЧА (тикет T3.0.2)
Включить mutual TLS между всеми внутренними сервисами observability-стека: SDK ↔ OTel Collector ↔
Langfuse ↔ ClickHouse ↔ Postgres ↔ Vault ↔ Redis ↔ Proxy. Internal CA, сертификаты сервисов, rotation.

ЧТО СДЕЛАТЬ
1. infra/ha/certs/internal-ca/ — internal CA (self-signed, через cfssl или openssl):
- ca.pem (CA cert), ca-key.pem (CA key, в vault, НЕ в git)
- cfssl config для выпуска сервисных сертификатов
2. Сервисные сертификаты для каждого сервиса (CN=<service-name>, SAN включает FQDN + localhost):
- langfuse.pem / langfuse-key.pem
- clickhouse.pem / clickhouse-key.pem
- postgres.pem / postgres-key.pem
- vault.pem / vault-key.pem
- redis.pem / redis-key.pem
- proxy.pem / proxy-key.pem (для MODE A/B)
- otel-collector.pem / otel-collector-key.pem
3. Конфиги каждого сервиса на использование mTLS (verify peer + CA bundle):
- Langfuse: `TLS_ENABLED=true`, `TLS_CA=/certs/ca.pem`, `TLS_CERT=/certs/langfuse.pem`
- ClickHouse: `<server> <https_port>...</https_port>`, `<client> <ssl>1</ssl>`
- Postgres: `ssl=on`, `ssl_cert_file=...`, `ssl_ca_file=...`, `pg_hba.conf: hostssl all all 0.0.0.0/0 cert clientcert=verify-full`
- Vault: `listener "tcp" { tls_cert_file=... tls_key_file=... tls_disable_client_certs=false }`
- Redis: `tls-port 6380`, `tls-cert-file`, `tls-key-file`, `tls-ca-cert-file`
- OTel Collector: `tls: cert_file, key_file, ca_file` в receiver и exporter
4. SDK-side: AGENT_OBS_TLS_CA=/path/to/ca.pem, AGENT_OBS_TLS_CERT=/path/to/sdk.pem
5. scripts/rotate-certs.sh — rotation раз в 90 дней:
- генерирует новые сертификаты
- атомарно подменяет на всех сервисах через docker cp + reload
- валидирует, что mTLS handshake работает после rotation
6. tests/test_mtls.py — SDK коннектится к OTel Collector и Langfuse только при валидном сертификате;
при отсутствии CA = connection refused; при expired сертификате = connection refused.

DoD
- [ ] Все сервисы требуют mTLS, без сертификата клиент не подключается
- [ ] SDK работает с mTLS (env AGENT_OBS_TLS_CA + AGENT_OBS_TLS_CERT)
- [ ] Скрипт rotate-certs.sh успешно ротирует сертификаты без даунтайма
- [ ] pytest tests/test_mtls.py зелёный
- [ ] README в infra/ha/certs/ с инструкцией по выпуску и ротации

Коммит: feat(infra): enable mtls for all internal observability services with cert rotation
```

---

## ВОЛНА 1: Tiered adaptive sampling v2 (eval-aware + dynamic rate)

> Эпик E3.1. Развивает CRITICAL-сэмплер (PC29–PC32) до полноценного adaptive v2: учитывает eval-результаты
> (faithfulness, hallucination), стоимость и инциденты. Динамическая rate только для нормальных трейсов.
> Промпты PP03–PP07 выполняются строго последовательно (общий файл `agent_obs/exporters/tail_sampler.py`).

### PP03 — [T3.1.1] Policy engine: условие eval.faithfulness < 0.7 → поднять content-sampling до 30%

> PP0 + CRITICAL PC29–PC32 (adaptive sampling v1: cpu/error-rate driven). Файл `agent_obs/exporters/tail_sampler.py`.

```text
ЗАДАЧА (тикет T3.1.1)
Расширить policy engine tail-сэмплера: условие на eval.faithfulness. Трейсы с низким качеством ответа
попадают в 30% content-sampling (вместо 10% по умолчанию), без ручного вмешательства.

ЧТО СДЕЛАТЬ
1. В tail_sampler.py добавить новый класс SamplingPolicyV2 (extends CRITICAL-сэмплер):
- принимает на вход не только Span, но и list[EvalResult] (late annotation, могут приходить позже)
- поддерживает policies: list[SamplingPolicy] (контракт из PP0)
2. Базовый набор policies (реализуй все 5):
- error_keep:        predicate = span.status == "ERROR";                keep_rate = 1.0
- eval_fail_keep:    predicate = any(er.scores.get("faithfulness", 1.0) < 0.7 for er in evals); keep_rate = 1.0
- cost_high_keep:     predicate = span.cost_usd > threshold (env: TAIL_SAMPLER_COST_THRESHOLD=0.05);  keep_rate = 1.0
- security_keep:      predicate = span.attributes.get("security.incident") == "true"; keep_rate = 1.0
- random:             predicate = True; keep_rate = float(env: TAIL_SAMPLER_NORMAL_RATE=0.10)
3. Content-sampling override: если сработал eval_fail_keep — content_sampling_rate = 0.30 (вместо 0.10).
4. Идемпотентность: решение по span_id стабильно во времени (хеш от span_id + policy_version).
5. Тесты: для каждой policy — recall 100% на сработавшем предикате; false positive rate < 1% на нормальных.

DoD
- [x] SamplingPolicyV2 реализует все 5 policies, контракт SamplingPolicy из PP0 соблюдён
- [x] Трейс с faithfulness < 0.7 попадает в 30% content-sampling автоматически
- [x] Идемпотентность: одно и то же решение по span_id при повторной прогоне
- [x] pytest tests/test_tail_sampler_v2.py зелёный (≥ 20 кейсов)

Коммит: feat(sampling): add v2 policy engine with eval-aware content sampling override
```

---

### PP04 — [T3.1.2] Feedback loop: метрика eval_score_avg_5m → в policy engine

> PP0 + PP03. Создаёт обратную связь: средний eval-score за последние 5 минут влияет на global rate.

```text
ЗАДАЧА (тикет T3.1.2)
Создать feedback loop: средний faithfulness-score за скользящее окно 5 минут → в policy engine.
Если средний faithfulness падает ниже 0.8, global rate растёт до 25% (деградация качества агента).

ЧТО СДЕЛАТЬ
1. agent_obs/exporters/tail_sampler.py — добавить класс FeedbackLoopPolicy:
- читает метрику eval_score_avg_5m из Prometheus (query: avg_over_time(faithfulness_score[5m]))
- если score < 0.8: global_rate = 0.25 (вместо 0.10)
- если score < 0.6: global_rate = 0.40 (тяжёлая деградация)
- если score >= 0.8: global_rate = 0.10 (normal)
2. Промежуточный кеш: metric читается не чаще 1 раза в 30 секунд (asyncio.Lock + TTL).
3. Метрика tail_sampler_current_rate для мониторинга feedback'а.
4. Логировать изменения rate в audit trail с указанием причины (eval_score_drop).
5. Тесты: simulate eval_score drop → rate растёт; simulate recovery → rate возвращается к 0.10.

DoD
- [x] FeedbackLoopPolicy корректно реагирует на средний faithfulness
- [x] Метрика tail_sampler_current_rate обновляется при изменении rate
- [x] Audit-trail содержит запись об изменении rate с причиной
- [x] pytest зелёный

Коммит: feat(sampling): add feedback loop from eval_score_avg_5m to global sampling rate
```

---

### PP05 — [T3.1.3] Логирование всех изменений политики в audit trail

> PP0 + PP03 + PP04. Файлы `agent_obs/exporters/tail_sampler.py` + `agent_obs/guardrail/audit_trail.py`.

```text
ЗАДАЧА (тикет T3.1.3)
Каждое изменение sampling policy (включая dynamic rate) логируется в audit trail с указанием:
какая policy сработала, на каком span'е, какой был old_rate → new_rate, причина срабатывания.

ЧТО СДЕЛАТЬ
1. Добавить класс AuditTrailLogger в agent_obs/guardrail/audit_trail.py (если ещё нет):
- log_sampling_decision(span_id, policy_name, decision, reason, old_rate, new_rate)
- batched writes в audit_events_hot (ClickHouse), идемпотентный по audit_id
2. В каждой policy (PP03) — после принятия решения вызвать AuditTrailLogger.
3. Уникальный audit_id = sha256(span_id + policy_version + decision_time).
4. Не блокирует основной путь (async fire-and-forget в ring buffer).
5. Метрика sampling_decisions_logged_total.
6. Тесты: каждое изменение policy логируется; replay сценария показывает историю изменений rate.

DoD
- [x] Все изменения sampling policies логируются в audit_events_hot
- [x] Audit-trail содержит old_rate → new_rate + reason для каждого решения
- [x] Идемпотентность: replay не дублирует записи
- [x] pytest зелёный

Коммит: feat(sampling): log all policy decisions to audit trail with old/new rate and reason
```

---

### PP06 — [T3.1.4] Дашборд «Sampler Policy History» — график rate по времени с аннотациями причин

> PP0 + PP05. Файлы в `infra/grafana/dashboards/sampler-policy-history.json`.

```text
ЗАДАЧА (тикет T3.1.4)
Grafana-дашборд для SRE: график sampling rate по времени с аннотациями причин (eval_drop, cpu_high,
error_spike). Каждое изменение rate — vertical annotation с причиной из audit trail.

ЧТО СДЕЛАТЬ
1. infra/grafana/dashboards/sampler-policy-history.json:
- panel 1: tail_sampler_current_rate (gauge + graph)
- panel 2: sampling_decisions_by_reason (pie + table)
- panel 3: tail_sampler_kept_ratio_by_reason (stacked bar)
- panel 4: eval_score_avg_5m overlay (для корреляции)
- panel 5: cpu + error_rate overlay (для корреляции)
2. Аннотации: запрос в ClickHouse audit_events_hot WHERE action='sampling_decision' GROUP BY timestamp.
3. Time range picker: 1h / 6h / 24h / 7d.
4. Drill-down: клик на точку rate-change → открывает audit-event в trace-viewer.
5. README в docs/sampler-dashboard.md: как читать дашборд, какие действия предпринимать при различных
сценариях (eval_drop → проверить LLM; cpu_high → scale; error_spike → page on-call).

DoD
- [x] Дашборд импортируется в Grafana без ошибок
- [x] Аннотации причин rate-change видны на графике
- [x] Drill-down в trace-viewer работает
- [x] README с runbook-инструкциями создан

Коммит: feat(grafana): add sampler policy history dashboard with reason annotations
```

---

### PP07 — [T3.1.5] Метрика tail_sampler_kept_ratio_by_reason{reason="error|cost|eval_fail|security|random"}

> PP0 + PP03..PP06. Файл `agent_obs/metrics.py`.

```text
ЗАДАЧА (тикет T3.1.5)
Prometheus-метрика с лейблом reason: показывает, по какой причине span был сохранён.

ЧТО СДЕЛАТЬ
1. agent_obs/metrics.py — Counter:
- tail_sampler_kept_spans_total{reason="error|cost|eval_fail|security|random", agent_id, tenant_id}
- tail_sampler_dropped_spans_total{agent_id, tenant_id}  (нормальные, не попавшие в random)
2. Gauge:
- tail_sampler_current_rate{policy="random|eval_feedback|cpu_driven"} — текущая rate
- tail_sampler_content_rate{policy="default|eval_fail_override"} — текущая content-rate
3. Histogram:
- tail_sampler_decision_latency_seconds — p99 < 1 мс (должно быть очень быстро)
4. PromQL-queries в docs/sampler-metrics.md для типичных сценариев:
- "доля сохранённых по eval_fail" → sum(rate(tail_sampler_kept_spans_total{reason="eval_fail"}[5m])) / sum(rate(tail_sampler_kept_spans_total[5m]))
- "current rate trend" → tail_sampler_current_rate{policy="eval_feedback"}
5. Alerting rules в infra/prometheus-rules.yml:
- alert: SamplerRateUnstable — если rate меняется > 5 раз за 10 минут (возможно, проблемы с feedback loop)
- alert: SamplerDecisionLatencyHigh — если p99 latency > 5 мс

DoD
- [ ] Метрики с лейблом reason экспортируются в Prometheus
- [ ] PromQL-queries документированы
- [ ] Alerting rules добавлены в prometheus-rules.yml
- [ ] pytest + интеграционный тест /metrics — зелёный

Коммит: feat(metrics): add tail_sampler_kept_ratio_by_reason counter with reason labels
```

---

## ВОЛНА 2: Span-aggregation в SDK (micro-spans → один span + events)

> Эпик E3.2. ТРИЗ-улучшение #2: span-aggregation. Длинные reasoning-циклы (50+ шагов) сжимаются
> в один span + массив events. Сетевой трафик сокращается на 60%. [ПАРАЛЛЕЛЬ] с Волной 1
> после PP07 (не пересекается по файлам).

### PP08 — [T3.2.1] SpanAggregator: собирает микро-spans до 50 штук или до 1 секунды

> PP0 + MVP P04–P05 (SDK каркас). Файл `agent_obs/span_aggregator.py` (NEW).

```text
ЗАДАЧА (тикет T3.2.1)
SpanAggregator в SDK: накапливает микро-spans (agent.reasoning шаги) до 50 штук или до 1 секунды,
затем сливает в один агрегированный span с events.

ЧТО СДЕЛАТЬ
1. agent_obs/span_aggregator.py (NEW):
- class SpanAggregator(max_count=50, max_window_seconds=1.0)
- async def add(self, span: Span) -> Optional[SpanAggregate]:
  - если буфер + новый span → count >= max_count ИЛИ (now - first_span_time) >= max_window_seconds:
    return flush() (возвращает агрегат)
  - иначе: добавить в буфер, вернуть None
- async def flush() -> Optional[SpanAggregate]: собрать агрегат, очистить буфер
2. SpanAggregate — контракт из PP0: parent_span_id, span_type, events (массив микро-spans как events),
   children_count, bytes_saved.
3. Агрегирующий span: name=f"agent.reasoning:aggregated:{N}", span_type="agent.reasoning",
   events[i] = {name: micro_span.name, ts: micro_span.start_time, attrs: micro_span.attributes}.
4. bytes_saved = sum(len(str(span.to_dict())) for span in micro_spans) - len(str(aggregate.to_dict()))
5. Тесты: 50 микро-spans → 1 агрегат с 50 events; 10 spans + 1.5 сек → 1 агрегат с 10 events;
   empty flush → None.

DoD
- [ ] SpanAggregator собирает микро-spans до 50 штук или 1 секунды
- [ ] SpanAggregate соответствует контракту PP0
- [ ] bytes_saved корректно считается
- [ ] pytest tests/test_span_aggregator.py зелёный (≥ 10 кейсов)

Коммит: feat(agent_obs): add SpanAggregator for micro-span aggregation to 50 or 1s
```

---

### PP09 — [T3.2.2] Embedded events как массив events: [{name, ts, attrs}, ...] в агрегированном span'е

> PP0 + PP08. Файл `agent_obs/observability.py` (serialization).

```text
ЗАДАЧА (тикет T3.2.2)
Сериализация агрегированного span'а: events — массив объектов {name, ts, attrs}. Совместимость с OTLP.

ЧТО СДЕЛАТЬ
1. В Span.to_dict(): если events непустой и span_type = "agent.reasoning" + has aggregate metadata,
   сериализовать events как массив {name, timestamp (ISO8601), attributes: dict}.
2. Совместимость с Langfuse: маппинг events в Langfuse Events API (через otlp_mapping.py).
3. Совместимость с ClickHouse: events колонка — JSON-тип, индекс по name.
4. Сохранение порядка events (по timestamp).
5. Дедупликация events при retry: идемпотентность по (span_id, event_name, event_ts).
6. Тесты: round-trip to_dict() → from_dict() сохраняет events; Langfuse принимает агрегат без ошибок;
   ClickHouse insert не падает на events с 50 элементами.

DoD
- [ ] Сериализация events совместима с OTLP и Langfuse
- [ ] ClickHouse принимает агрегат с 50 events без ошибок
- [ ] Дедупликация events работает
- [ ] pytest зелёный

Коммит: feat(agent_obs): serialize aggregated span events as otel-compatible event array
```

---

### PP10 — [T3.2.3] UI: поддержка expandable events в trace-viewer

> PP0 + PP09. Связано с Langfuse UI + custom UI если есть.

```text
ЗАДАЧА (тикет T3.2.3)
Расширить trace-viewer: агрегированные span'ы показываются с expandable секцией events. По клику на
«show N events» — разворачивается таблица с микро-span'ами.

ЧТО СДЕЛАТЬ
1. Если trace-viewer — Langfuse: маппинг events в Langfuse Event API, URL-параметр `?expand=events`.
2. Если есть custom UI (по T3.5 или уровень 4): React-компонент AggregatedSpanView с expand/collapse.
3. Поиск по events: input «search in events» — фильтрация по подстроке в name/attributes.
4. Цветовое выделение events с error (если у микро-span'а status="error" — красный фон).
5. Keyboard navigation: стрелки для перехода между events.
6. Performance: lazy render — рендерить events только при expand (для трейсов с 1000+ events).
7. Тесты (e2e): открыть trace с агрегатом → expand events → найти event по имени.

DoD
- [ ] Expandable events в trace-viewer работают
- [ ] Поиск по events фильтрует корректно
- [ ] Error-events выделены красным
- [ ] Lazy render предотвращает тормоза на больших трейсах
- [ ] e2e-тесты зелёные

Коммит: feat(ui): add expandable events view for aggregated spans in trace-viewer
```

---

### PP11 — [T3.2.4] Метрики spans_aggregated_total и bytes_saved_total

> PP0 + PP08..PP10. Файл `agent_obs/metrics.py`.

```text
ЗАДАЧА (тикет T3.2.4)
Прометей-метрики: счётчик агрегированных span'ов и сэкономленных байт. Позволяет SRE видеть пользу
агрегации.

ЧТО СДЕЛАТЬ
1. agent_obs/metrics.py:
- Counter spans_aggregated_total{agent_id, tenant_id, span_type="agent.reasoning"}
- Counter spans_micro_input_total{agent_id, tenant_id}  (входящие микро-spans)
- Counter bytes_saved_total{agent_id, tenant_id}  (общая экономия от агрегации)
- Gauge aggregation_ratio{agent_id} = spans_aggregated_total / spans_micro_input_total
2. График в Grafana: aggregation_ratio → должна быть 0.02–0.05 (1 агрегат на 20–50 микро-spans).
3. Alert: AggregationRatioTooLow — если ratio > 0.2, значит буфер flush'ится слишком часто (проблема).
4. Audit: в audit trail отдельная запись при каждом flush агрегата (children_count, bytes_saved).
5. Тесты: после 100 микро-spans → spans_aggregated_total ≥ 2; bytes_saved > 0.

DoD
- [ ] Метрики spans_aggregated_total и bytes_saved_total экспортируются
- [ ] Grafana-график aggregation_ratio добавлен
- [ ] Alert AggregationRatioTooLow работает
- [ ] pytest + интеграционный тест зелёный

Коммит: feat(metrics): add spans_aggregated_total and bytes_saved_total counters
```

---

## ВОЛНА 3: Adaptive eval-sampling (день / ночь / инцидент)

> Эпик E3.3. ТРИЗ-улучшение #4. Eval-pipeline адаптирует частоту запуска LLM-judge по режиму:
> ночь — больше eval (ресурс дешевле), день — меньше (пик нагрузки), инцидент — 100% eval для
> инцидент-трейсов. [ПАРАЛЛЕЛЬ] с Волнами 1–2 после PP11.

### PP12 — [T3.3.1] AdaptiveEvalSampler: три режима (day / night / incident)

> PP0 + CRITICAL PC09–PC13 (eval base). Файл `agent_obs/eval/adaptive_sampler.py` (NEW).

```text
ЗАДАЧА (тикет T3.3.1)
Eval-sampler с тремя режимами: day (10% LLM-judge, 30% embedding), night (50% LLM-judge, 100% embedding),
incident (100% обоих для инцидент-трейсов).

ЧТО СДЕЛАТЬ
1. agent_obs/eval/adaptive_sampler.py (NEW):
- class AdaptiveEvalSampler
- detect_mode() -> "day" | "night" | "incident":
  - "day" если current_time UTC в [08:00, 22:00) и нет активного инцидента
  - "night" если current_time UTC в [22:00, 08:00) и нет активного инцидента
  - "incident" если active_incident=true (читается из метрики alert_active)
2. should_eval(trace: Trace) -> tuple[bool, str]:
  - mode = detect_mode()
  - если mode="incident" и trace.has_security_incident: return (True, "incident_full")
  - если mode="night" и hash(trace_id) % 100 < 50: return (True, "night_50pct")
  - если mode="day" и hash(trace_id) % 100 < 10: return (True, "day_10pct")
  - иначе: return (False, "skip")
3. Кеширование alert_active: читать из Prometheus не чаще 1 раза в 30 секунд.
4. Метрика eval_sampler_mode{mode="day|night|incident"} — gauge, обновляется при смене режима.
5. Audit: смена режима логируется в audit trail.
6. Тесты: simulate "day" → 10% rate; simulate "night" → 50% rate; simulate incident → 100% для security-traces.

DoD
- [ ] AdaptiveEvalSampler поддерживает 3 режима с корректным переключением
- [ ] Metрика eval_sampler_mode обновляется при смене
- [ ] Audit-trail логирует смену режима
- [ ] pytest зелёный (≥ 15 кейсов)

Коммит: feat(eval): add adaptive eval sampler with day/night/incident modes
```

---

### PP13 — [T3.3.2] Nightly-batch golden eval: полный прогон LLM-judge на golden set

> PP0 + PP12. Файлы в `agent_obs/eval/` + `scripts/`.

```text
ЗАДАЧА (тикет T3.3.2)
Nightly batch-job: каждую ночь в 03:00 UTC полный прогон LLM-judge на golden set (100–500 кейсов).
Регрессионный контроль качества агента. Результаты — в отдельную таблицу eval_golden_results.

ЧТО СДЕЛАТЬ
1. scripts/nightly_golden_eval.py — cron-скрипт:
- читает golden set из eval/golden/*.yaml (вопрос, ожидаемый ответ, ожидаемые eval-scores)
- для каждого кейса: вызывает pilot_agent.run(question) → получаем фактический ответ
- запускает LLMJudgeEvaluator на (вопрос, фактический ответ) → faithfulness, relevancy, completeness
- сравнивает с ожидаемыми scores → pass/fail
2. Хранение: таблица eval_golden_results (nightly_ts, question_id, expected_score, actual_score, pass).
3. Метрика eval_golden_pass_rate — доля pass за ночь.
4. Alert: GoldenEvalRegression — если pass_rate упал на > 10% от 7-day moving average.
5. README в docs/eval-golden-runbook.md: как добавлять кейсы в golden set, как интерпретировать регрессию.
6. Тесты: golden set из 5 кейсов → nightly job отрабатывает; при деградации — alert срабатывает.

DoD
- [ ] Nightly batch запускается по cron в 03:00 UTC
- [ ] eval_golden_results заполняется, pass_rate экспортируется в Prometheus
- [ ] Alert GoldenEvalRegression работает
- [ ] README создан, pytest зелёный

Коммит: feat(eval): add nightly golden eval batch with regression alerting
```

---

### PP14 — [T3.3.3] Incident-mode: 100% eval для security-incident трейсов

> PP0 + PP12. Файл `agent_obs/eval/adaptive_sampler.py`.

```text
ЗАДАЧА (тикет T3.3.3)
В инцидент-режиме (когда alert_active=true) все security-incident трейсы получают 100% LLM-judge eval,
даже если обычный day-rate был бы 10%. Параллельно — embedding eval и rule-based всегда 100%.

ЧТО СДЕЛАТЬ
1. Расширить AdaptiveEvalSampler (PP12):
- если trace.attributes.get("security.incident")=="true" → should_eval всегда (True, "incident_security")
- если trace.attributes.get("error.type")=="prompt_injection" → should_eval всегда (True, "incident_injection")
2. Rate-limit guard: даже в инциденте не больше 100 eval-jobs в минуту (чтобы не положить LLM-judge API).
3. Priority queue в Redis: incident-evals идут с приоритетом 10, normal — с приоритетом 1.
4. Метрика eval_sampler_incident_evals_total{reason="security|injection|cost|other"}.
5. Audit: каждый incident-eval логируется с указанием инцидента.
6. Тесты: simulate incident → 5 security-traces получают 100% eval; non-security traces — обычный rate.

DoD
- [ ] В инцидент-режиме security-traces получают 100% LLM-judge eval
- [ ] Rate-limit guard предотвращает перегрузку LLM-judge API
- [ ] Priority queue в Redis работает
- [ ] pytest зелёный

Коммит: feat(eval): add 100pct eval for security-incident traces in incident mode
```

---

### PP15 — [T3.3.4] Eval cost dashboard + budget enforcement

> PP0 + PP13 + PP14. Файлы в `infra/grafana/` + `agent_obs/eval/`.

```text
ЗАДАЧА (тикет T3.3.4)
Дашборд стоимости eval-pipeline: сколько $ тратится на LLM-judge по режимам (day/night/incident),
с budget-enforcement: при достижении 80% дневного бюджета → rate снижается до 5%, при 100% → paused.

ЧТО СДЕЛАТЬ
1. infra/grafana/dashboards/eval-cost.json:
- panel 1: cost_total_usd (gauge + graph, day/night/incident stacked)
- panel 2: cost_per_eval (gauge) — avg по режимам
- panel 3: eval_count_by_mode (pie + bar)
- panel 4: budget_utilization_ratio (gauge, 0..1.2) с thresholds 0.8 (warn) / 1.0 (soft) / 1.2 (hard)
2. Budget enforcement в adaptive_sampler.py:
- env: EVAL_DAILY_BUDGET_USD=20.0 (default)
- gauge: eval_cost_today_usd (сброс в 00:00 UTC)
- при > 0.8 * budget → rate падает до 5% (soft)
- при > 1.0 * budget → eval paused (hard), audit warning
- recovery: при < 0.5 * budget → rate возвращается к normal
3. Alert: EvalBudgetExhausted — отправляется при hard-pause.
4. README в docs/eval-budget-runbook.md: как реагировать на alert, как изменить бюджет.
5. Тесты: simulate budget exhaustion → eval pauses; simulate cooldown → eval resumes.

DoD
- [ ] Дашборд eval-cost.json импортируется в Grafana
- [ ] Budget enforcement работает (soft + hard thresholds)
- [ ] Alert EvalBudgetExhausted настроен
- [ ] pytest зелёный

Коммит: feat(eval): add cost dashboard and budget enforcement for eval pipeline
```

---

## ВОЛНА 4: Tree + compression для длинных трейсов

> Эпик E3.4. ТРИЗ-улучшение #8. Трейсы с 50+ шагами рассуждения «расплющиваются» в линейную цепочку,
> теряя структуру reasoning-дерева. Реализация tree-структуры + compression: 50+ шагов → сжатое
> поддерево (parent + summary), доступное для drill-down. [ПАРАЛЛЕЛЬ] с Волнами 1–3 после PP11.

### PP16 — [T3.4.1] TraceTreeBuilder: построение дерева из плоского списка span'ов

> PP0 + PP08–PP11 (span-aggregation). Файл `agent_obs/tree_compressor.py` (NEW).

```text
ЗАДАЧА (тикет T3.4.1)
Построение дерева трейса из плоского списка span'ов по parent_span_id. Поддержка агрегированных span'ов
из Волны 2 (агрегат — родитель для своих events, но events не span'ы в дереве).

ЧТО СДЕЛАТЬ
1. agent_obs/tree_compressor.py (NEW):
- class TraceTreeBuilder
- build(spans: list[Span]) -> TraceTree:  # контракт из PP0
  - построение по parent_span_id, корень — agent.loop span
  - агрегированные span'ы — листья для своих events (events не входят в дерево как nodes)
  - если в списке есть span'ы без parent (orphan) → формируем виртуальный root
2. Tree traversal: depth-first, breadth-first (async iterators).
3. Tree metrics: max_depth, total_spans, leaf_count, error_leaf_count.
4. Сериализация: to_dict() → {root: dict, children: [dict, ...]}, рекурсивно.
5. Тесты: 3-level дерево из 5 span'ов; orphan-handling; цикл (parent_span_id образует цикл) → assert error.

DoD
- [ ] TraceTreeBuilder корректно строит дерево из плоского списка
- [ ] Tree-metrics считаются правильно
- [ ] Orphan и cycle cases обрабатываются (orphan → virtual root, cycle → ValueError)
- [ ] pytest зелёный (≥ 10 кейсов)

Коммит: feat(tree): add TraceTreeBuilder for tree construction from flat span list
```

---

### PP17 — [T3.4.2] Compression policy: поддерево > 20 узлов сжимается в summary-node

> PP0 + PP16. Файл `agent_obs/tree_compressor.py`.

```text
ЗАДАЧА (тикет T3.4.2)
Compression policy: если поддерево (subtree, начиная с какого-то узла) содержит > 20 узлов, оно
сжимается в один summary-node. Summary содержит: child_count, total_cost, error_count, summary_text
(LLM-сгенерированный саммари в async-режиме, см. PP18).

ЧТО СДЕЛАТЬ
1. class CompressionPolicy:
- max_subtree_size: int = 20  (env: TREE_COMPRESSION_THRESHOLD=20)
- compress(tree: TraceTree) -> TraceTree:
  - для каждого узла: count_subtree_size(node)
  - если size > max_subtree_size → replace subtree на SummaryNode
  - SummaryNode: {name: "compressed:N", span_type: "agent.reasoning.compressed",
      children_count: N, total_cost: sum, error_count: ..., summary: "..."}
- compression_ratio = compressed_spans / total_spans
2. Идемпотентность: при том же input и policy_version — тот же output.
3. Compression не должен удалять error-spans (если в поддереве есть error — оно не сжимается, чтобы
   сохранить диагностическую ценность).
4. Метрика tree_compression_ratio{agent_id} — gauge 0..1.
5. Тесты: 50 узлов → 1 summary; 50 узлов с 1 error → НЕ сжимается (error сохраняется в full tree);
   21 узел без errors → 1 summary.

DoD
- [ ] Compression policy срабатывает на поддеревьях > 20 узлов
- [ ] Error-spans не сжимаются
- [ ] Идемпотентность сохранена
- [ ] pytest зелёный

Коммит: feat(tree): add compression policy for subtrees >20 nodes with error preservation
```

---

### PP18 — [T3.4.3] Async LLM-summary для compressed-узлов

> PP0 + PP17. Файл `agent_obs/tree_compressor.py` + `agent_obs/eval/`.

```text
ЗАДАЧА (тикет T3.4.3)
LLM-judge-генерация summary для compressed-узлов. Async через очередь, не блокирует export трейса.
Summary приходит позже (late annotation), как eval-результат.

ЧТО СДЕЛАТЬ
1. В CompressionPolicy.compress() — после создания SummaryNode:
   - summary="pending" (placeholder)
   - enqueue job в Redis eval-queue (priority: low)
2. Worker (интегрируется с существующим eval-worker):
   - читает все span'ы поддерева
   - составляет LLM-prompt: "Суммируй следующие N шагов reasoning-цикла: ..."
   - записывает результат в late_annotation по trace_id + summary_node_id
3. Late annotation: после завершения job, summary подменяется в trace tree (для новых запросов в UI).
   Старые запросы в UI показывают "pending".
4. Кеш summaries: идентичные поддеревья (по hash от содержимого) → reuse summary.
5. Метрика tree_compression_summary_pending_total — счётчик ждущих summaries.
6. Тесты: 50 узлов → summary pending → через 30 сек summary готов.

DoD
- [ ] Async LLM-summary генерируется через очередь
- [ ] Late annotation подменяет summary в UI
- [ ] Кеш summaries работает (идентичные поддеревья не требуют нового LLM-call)
- [ ] pytest зелёный

Коммит: feat(tree): add async llm-summary generation for compressed nodes via queue
```

---

### PP19 — [T3.4.4] Drill-down из compressed-node: восстановление поддерева по клику

> PP0 + PP17 + PP18. UI + storage.

```text
ЗАДАЧА (тикет T3.4.4)
В UI trace-viewer: по клику на compressed-node → восстановление поддерева (с drill-down в cold storage
если поддерево уже мигрировано). RBAC-проверка + MFA для PII-полей.

ЧТО СДЕЛАТЬ
1. API endpoint GET /api/v1/traces/{trace_id}/compressed/{node_id}/expand:
- RBAC: requires "trace:read" + "compressed:expand"
- если поддерево в Hot store → SELECT * FROM spans_hot WHERE parent_span_id = node_id
- если в Warm/Cold → запрос через drill_down.py (см. PP21)
- для PII-полей — обязательный MFA-token в заголовке X-OryaObs-MFA
2. UI: клик на summary-node → API call → replace node на children в дереве.
3. Loading indicator (spiner) на время запроса (может быть до 30 сек для cold).
4. Audit: каждый drill-down логируется в audit_trail (user, trace_id, node_id, time, fields).
5. Тесты: drill-down на Hot < 1 сек; на Cold < 60 сек; без MFA → 403; без RBAC → 403.

DoD
- [ ] Drill-down API работает с RBAC + MFA для PII
- [ ] UI показывает восстановленное поддерево
- [ ] Audit-trail логирует каждый drill-down
- [ ] pytest + e2e тесты зелёные

Коммит: feat(tree): add drill-down api for compressed nodes with rbac and mfa
```

---

### PP20 — [T3.4.5] Embedded subtree (матрёшка): поддерево как embedded event массив

> PP0 + PP17 + PP19. Файл `agent_obs/tree_compressor.py`.

```text
ЗАДАЧА (тикет T3.4.5)
Embedded subtree: поддерево (до 10 узлов) — embedded как массив events в parent'е. «Матрёшка»: один
span содержит внутри дерево меньших span'ов.

ЧТО СДЕЛАТЬ
1. class EmbeddedSubtreePolicy:
- max_embedded_size: int = 10  (env: TREE_EMBEDDED_SIZE=10)
- embed(tree: TraceTree) -> TraceTree:
  - для каждого leaf-родителя с ≤ 10 детьми → embed children в events-массив родителя
  - родитель получает metadata: embedded_count=N, embedded=true
2. Drill-down из embedded — парсинг events-массива, no API call needed (всё уже в parent span).
3. Storage: embedded events хранятся в hot column events (как в PP09).
4. UI: по клику на embedded-node → раскрывается events-array (lazy render).
5. Метрика tree_embedded_ratio — доля узлов с embedded subtree.
6. Тесты: 8-children leaf → embedded в parent; drill-down из embedded работает локально.

DoD
- [ ] Embedded subtree работает для поддеревьев ≤ 10 узлов
- [ ] Drill-down из embedded не требует API call
- [ ] UI раскрывает embedded events
- [ ] pytest зелёный

Коммит: feat(tree): add embedded subtree (matreshka) for small subtrees as event arrays
```

---

## ВОЛНА 5: On-demand drill-down в cold storage

> Эпик E3.5. ТРИЗ-улучшение #9. Cold storage (S3 + Parquet) раньше 1 года — запрос < 1 минуты через
> Athena. Drill-down API позволяет «воскрешать» конкретные поля (например, llm.input_text) по запросу
> с RBAC + MFA. [ПАРАЛЛЕЛЬ] с Волнами 1–4 после PP11.

### PP21 — [T3.5.1] DrillDown API: восстановление конкретных полей trace из cold storage

> PP0 + CRITICAL PC22–PC23 (tiered storage migrations). Файл `agent_obs/storage/drill_down.py` (NEW).

```text
ЗАДАЧА (тикет T3.5.1)
DrillDown API: по запросу восстановить конкретные поля trace из cold storage (S3+Parquet). Если поля
содержат PII — обязательный MFA-token. Запрос идёт через Athena (для S3) или напрямую через Parquet
(для свежих cold-данных).

ЧТО СДЕЛАТЬ
1. agent_obs/storage/drill_down.py (NEW):
- class DrillDownService
- async def drill_down(self, request: DrillDownRequest) -> DrillDownResult:
  - validate RBAC: user has "trace:read" + (для PII-полей) "pii:recover" + MFA-token valid
  - locate trace: SELECT storage_tier FROM traces_warm WHERE trace_id = ?
    tier может быть: "hot", "warm", "cold"
  - если tier="hot": SELECT fields FROM spans_hot WHERE trace_id = ?
  - если tier="warm": SELECT fields FROM traces_warm WHERE trace_id = ?
  - если tier="cold": query Athena: SELECT fields FROM cold_traces WHERE trace_id = ?
  - собрать результат, маскировать PII если MFA не предоставлен
2. DrillDownResult: dict {field_name: value}, плюс metadata: storage_tier, query_latency_ms, mfa_used.
3. Кеширование: drill-down результат кешируется в Redis на 10 минут (key = sha256(trace_id + fields + user_id)).
4. Rate-limit: не больше 10 drill-down запросов в минуту на user (env: DRILL_DOWN_RATE_LIMIT=10).
5. Метрика drill_down_latency_seconds{tier="hot|warm|cold"} — histogram.
6. Тесты: hot tier < 50 мс; warm < 5 сек; cold < 60 сек; без MFA на PII → 403.

DoD
- [ ] DrillDown API работает с 3-tier storage
- [ ] RBAC + MFA enforcement для PII-полей
- [ ] Кеш в Redis работает (повторный запрос < 100 мс)
- [ ] Rate-limit guard работает
- [ ] pytest зелёный

Коммит: feat(storage): add drill-down api for cold storage field restoration with rbac and mfa
```

---

### PP22 — [T3.5.2] Athena integration: SQL-запросы к Parquet в S3

> PP0 + PP21. Файлы в `agent_obs/storage/` + `infra/storage/`.

```text
ЗАДАЧА (тикет T3.5.2)
Интеграция с AWS Athena для SQL-запросов к Parquet-данным в S3 (cold storage). Прогон query →
результат → DrillDownService.

ЧТО СДЕЛАТЬ
1. agent_obs/storage/athena_client.py (NEW):
- class AthenaClient
- async def query(self, sql: str) -> list[dict]:
  - start_query_execution (async, boto3)
  - poll for completion (get_query_execution)
  - fetch results (get_query_results)
  - timeout: 60 сек (env: ATHENA_QUERY_TIMEOUT=60)
2. Glue database + table for cold_traces:
  - infra/storage/athena_glue.sql: CREATE DATABASE observability;
    CREATE EXTERNAL TABLE cold_traces (...) STORED AS PARQUET
    LOCATION 's3://cold-traces/'
3. Оптимизация: partition pruning по trace_id (если S3 partitioned по tenant_id + date — Athena
   сканирует только нужный partition, экономя $$).
4. Cost guard: оценка сканируемых байт перед запросом (GetQueryExecution → Statistics), alert если
   > 1 ГБ сканирования на один drill-down (возврат ошибки с предложением сузить запрос).
5. Тесты: mock Athena (moto) для unit-тестов; integration test с реальной Athena (mark as @pytest.mark.integration).

DoD
- [ ] AthenaClient работает (async query + results)
- [ ] Glue database + table созданы, partitioning по tenant_id + date
- [ ] Cost guard предотвращает дорогие сканы
- [ ] pytest (unit + integration) зелёный

Коммит: feat(storage): integrate athena for sql queries to s3 parquet cold storage
```

---

### PP23 — [T3.5.3] Drill-down cache в Redis + 10-минутный TTL

> PP0 + PP21 + PP22. Файл `agent_obs/storage/drill_down.py`.

```text
ЗАДАЧА (тикет T3.5.3)
Кеш drill-down результатов в Redis: одинаковые запросы от одного user (по user_id + trace_id + fields)
в течение 10 минут не повторяются. Invalidation — при изменении trace (новый eval late annotation).

ЧТО СДЕЛАТЬ
1. Расширить DrillDownService (PP21):
- cache_key = sha256(user_id + trace_id + sorted(fields) + mfa_used)
- cache_set(cache_key, result, ttl=600)  # 10 минут
- cache_get(cache_key) → return cached result if exists
2. Invalidation:
- при поступлении late annotation (eval result, summary) → cache_invalidate(trace_id)
- при vault recovery (user recovered PII) → cache_invalidate(user_id + trace_id)
3. Метрика drill_down_cache_hit_ratio — gauge 0..1 (должна быть 0.3–0.7 в норме).
4. Размер кеша: gauge drill_down_cache_size_bytes — алерт если > 1 ГБ (в Redis).
5. Тесты: 1-й запрос → cache miss + writes; 2-й запрос → cache hit + returns cached.

DoD
- [ ] Кеш drill-down результатов работает (10 мин TTL)
- [ ] Invalidation при late annotation / vault recovery работает
- [ ] Метрика hit_ratio экспортируется
- [ ] pytest зелёный

Коммит: feat(storage): add redis cache for drill-down results with 10min ttl and invalidation
```

---

### PP24 — [T3.5.4] UI: «Drill-down» кнопка в trace-viewer для cold-полей

> PP0 + PP21..PP23. UI.

```text
ЗАДАЧА (тикет T3.5.4)
В trace-viewer для холодных трейсов поля с меткой "cold" показываются с кнопкой «Drill down».
По клику → модалка с MFA-prompt (если PII) → запрос к API → результат в той же модалке.

ЧТО СДЕЛАТЬ
1. UI-компонент ColdField:
- показывает [COLD] badge + "Drill down" button
- по клику открывает модалку:
  - если поле PII: форма MFA-token (TOTP)
  - индикатор загрузки (spiner) с примерным временем ожидания ("< 1 min" для cold)
  - результат (значение поля или маска если без MFA)
2. RBAC: если user не имеет "pii:recover" → button disabled с tooltip.
3. Rate-limit indication: если user превысил 10/min → button disabled с tooltip "Rate limit reached".
4. История drill-downs для user в sidebar (последние 10 запросов).
5. Тесты (e2e): открыть cold-trace → drill-down на cold-field → получить значение; rate-limit; RBAC.

DoD
- [ ] UI drill-down для cold-полей работает
- [ ] MFA-prompt для PII-полей
- [ ] RBAC + rate-limit enforcement в UI
- [ ] История drill-downs в sidebar
- [ ] e2e-тесты зелёные

Коммит: feat(ui): add drill-down button for cold fields in trace-viewer with mfa prompt
```

---

## ВОЛНА 6: SLO/SLA enforcement + multi-window alerting

> Эпик E3.6. ТРИЗ-улучшение #18 (механические колебания): tiered retention с колеблющейся детализацией.
> Multi-window multi-burn-rate alerting (Google SRE Workbook). Single-window alerts запрещены (много
> ложных срабатываний). [ПАРАЛЛЕЛЬ] с Волнами 1–5 после PP07.

### PP25 — [T3.6.1] SLO-каталог: availability, latency, quality, cost

> PP0. Файл `agent_obs/slo/definitions.py` (NEW).

```text
ЗАДАЧА (тикет T3.6.1)
SLO-каталог: 4 SLO (availability ≥ 99.9%, latency p95 ≤ 3 сек, faithfulness ≥ 90% answers > 0.7,
cost per request p99 ≤ $0.05) с target + window + burn-rate thresholds.

ЧТО СДЕЛАТЬ
1. agent_obs/slo/definitions.py (NEW):
- @dataclass class SLO:
    name: str
    target: float                  # доля успешных событий (0..1)
    window: timedelta              # 30 дней (rolling)
    burn_rate_thresholds: list     # [(short_window, long_window, threshold), ...]
                                  # пример: [("5m", "1h", 14.4), ("1h", "6h", 6.0), ("6h", "1d", 3.0)]
- SLO_CATALOG: dict[str, SLO] = {
    "availability":  SLO(target=0.999, window=30d, burn_rate=[(5m,1h,14.4), (1h,6h,6.0)]),
    "latency_p95":   SLO(target=0.95,  window=30d, burn_rate=[(5m,1h,14.4), (1h,6h,6.0)]),
    "quality":       SLO(target=0.90,  window=30d, burn_rate=[(1h,6h,6.0), (6h,1d,3.0)]),
    "cost_p99":      SLO(target=0.95,  window=30d, burn_rate=[(1h,6h,6.0), (6h,1d,3.0)]),
  }
2. Каждый SLO включает SLI-определение (как мерить):
   availability: 1 - error_traces / total_traces (rolling 30d)
   latency_p95:  доля traces с p95_latency <= 3 сек (rolling 30d)
   quality:       доля traces с faithfulness_score > 0.7 (rolling 30d, late-annotated)
   cost_p99:      доля traces с cost_per_request <= $0.05 (rolling 30d)
3. Расчёт burn_rate = (1 - actual_sli) / (1 - target)  # > 1 = burn rate exceeds
4. Метрики per-SLO:
   - slo_target{name} — gauge (target value)
   - slo_actual_sli{name} — gauge (rolling value, обновляется раз в 5 минут)
   - slo_burn_rate{name, window="short|long"} — gauge (current burn rates)
   - slo_error_budget_remaining{name} — gauge (0..1, остаток бюджета на период)
5. Тесты: каждое SLO считается корректно по тестовому набору traces.

DoD
- [ ] SLO-каталог с 4 SLO создан
- [ ] SLI-определения документированы
- [ ] Метрики per-SLO экспортируются в Prometheus
- [ ] pytest зелёный

Коммит: feat(slo): add slo catalog with availability latency quality cost sli definitions
```

---

### PP26 — [T3.6.2] Multi-window burn-rate alerting (Google SRE)

> PP0 + PP25. Файл `agent_obs/slo/burn_rate.py` (NEW) + `infra/prometheus-rules.yml`.

```text
ЗАДАЧА (тикет T3.6.2)
Multi-window multi-burn-rate alerting по образцу Google SRE Workbook. Срабатывание alert'а только
если оба окна (short + long) превышают burn-rate threshold — снижает ложные срабатывания.

ЧТО СДЕЛАТЬ
1. agent_obs/slo/burn_rate.py (NEW):
- class SLOBurnRate:
  - def is_burned(self, window: str, slo: str) -> bool: ...  # контракт из PP0
  - метод is_alert_fired(slo_name) -> bool:
    * True если любая пара (short, long) превышает свой порог одновременно
2. Alert rules в infra/prometheus-rules.yml:
- alert: SLOBurnRateHigh
  expr: |
    (slo_burn_rate{name="availability",window="short"} > 14.4)
    and
    (slo_burn_rate{name="availability",window="long"} > 14.4)
  for: 2m   # проверка что условие держится 2 минуты
  labels: { severity: page, slo: "availability" }
  annotations: { summary: "Availability SLO burn-rate high (page)" }
- ...аналогично для latency_p95, quality, cost_p99
- Дублирующие alert'ы для (1h, 6h) thresholds (severity: ticket, не page)
3. Routing: page alerts → PagerDuty/Opsgenie; ticket alerts → Jira/Linear.
4. Runbook: docs/slo-alerts-runbook.md с инструкциями для каждого SLO.
5. Тесты: simulate SLO burn → alert срабатывает только при обоих окнах превышающих threshold.

DoD
- [ ] Multi-window burn-rate alerting работает
- [ ] Single-window (только short или только long) — не вызывает alert
- [ ] Page vs ticket routing настроено
- [ ] Runbook создан
- [ ] pytest зелёный

Коммит: feat(slo): add multi-window multi-burn-rate alerting with page and ticket routing
```

---

### PP27 — [T3.6.3] Error budget tracking + policy actions

> PP0 + PP25 + PP26. Файл `agent_obs/slo/error_budget.py` (NEW).

```text
ЗАДАЧА (тикет T3.6.3)
Error budget tracking: остаток бюджета (1 - target) на rolling-окно. При исчерпании > 80% — soft
actions (увеличится sampling, снизится eval-cost); при исчерпании > 100% — hard actions (freeze
deployments через CI/CD gate, уведомление команды).

ЧТО СДЕЛАТЬ
1. agent_obs/slo/error_budget.py (NEW):
- class ErrorBudget:
  - budget_total = (1 - slo.target) * total_events_in_window
  - budget_consumed = error_events_in_window
  - remaining_ratio = (budget_total - budget_consumed) / budget_total
2. Policy actions:
  - remaining_ratio < 0.2 (80% consumed): soft
    * tail_sampler_normal_rate падает до 5% (экономия storage для освобождения ресурсов)
    * eval cost-budget сокращается до 50% (экономия $)
    * alert на email/sack: "Error budget low, soft actions taken"
  - remaining_ratio < 0.0 (100% consumed): hard
    * CI/CD gate: блокирует deploy (GitHub status check на PR: error_budget_ok = false)
    * page on-call
    * audit-event в audit_trail с действием "freeze_deploy"
3. Метрики:
  - slo_error_budget_remaining{slo} — gauge 0..1
  - slo_error_budget_actions_taken_total{action="soft|hard", slo}
4. Runbook: docs/error-budget-runbook.md — что делать при soft/hard actions.
5. Тесты: simulate 85% budget → soft actions; simulate 101% → hard actions (deploy blocked).

DoD
- [ ] Error budget считается корректно для каждого SLO
- [ ] Soft actions при 80% consumed
- [ ] Hard actions при 100% consumed (CI/CD gate)
- [ ] Runbook создан
- [ ] pytest зелёный

Коммит: feat(slo): add error budget tracking with soft and hard policy actions
```

---

### PP28 — [T3.6.4] SLO-дашборд в Grafana: SLI, burn-rate, error-budget

> PP0 + PP25..PP27. Файл `infra/grafana/dashboards/slo.json`.

```text
ЗАДАЧА (тикет T3.6.4)
Гrafana-дашборд «SLO»: для каждого SLO — SLI graph, burn-rate graph, error-budget gauge. Total —
обзорный gauge по всем SLO.

ЧТО СДЕЛАТЬ
1. infra/grafana/dashboards/slo.json:
- section "SLO: availability":
  * gauge: slo_actual_sli{name="availability"} vs slo_target{name="availability"}
  * graph: slo_burn_rate{name="availability",window="short"} + {window="long"} overlay
  * gauge: slo_error_budget_remaining{name="availability"}
- section "SLO: latency_p95" (аналогично)
- section "SLO: quality" (аналогично)
- section "SLO: cost_p99" (аналогично)
- top gauge "SLO overall health": green если все 4 в бюджете, yellow если 1-2 SLO исчерпаны, red если > 2
2. Time range picker: 1h / 6h / 24h / 7d / 30d.
3. Annotations: deploy markers, incident markers (из audit_trail).
4. Drill-down: клик на SLO → открывает detail-view с конкретными traces, вызвавшими деградацию.
5. README в docs/slo-dashboard.md: как читать дашборд, какие действия предпринимать.

DoD
- [ ] Дашборд SLO импортируется в Grafana
- [ ] 4 SLO-секции + overview gauge работают
- [ ] Drill-down в traces работает
- [ ] README создан

Коммит: feat(grafana): add slo dashboard with sli burn-rate and error-budget views
```

---

### PP29 — [T3.6.5] SLO-based canary deployments: gate на release по SLO-health

> PP0 + PP27 + PP28. Файлы в `.github/workflows/` + `scripts/`.

```text
ЗАДАЧА (тикет T3.6.5)
Canary deployment: 5% трафика на новую версию, проверка SLO-health 30 минут. Если burn-rate не
превышает порог → ramp to 100%. Если превышает → rollback автоматом.

ЧТО СДЕЛАТЬ
1. scripts/canary_check.py:
- принимает new_version и old_version
- проверяет slo_burn_rate за последние 30 минут на new_version
- если burn_rate < threshold → exit 0 (canary healthy)
- если > threshold → exit 1 (canary unhealthy, signal rollback)
2. .github/workflows/canary.yml (или GitLab/Jenkins):
- step 1: deploy to canary (5% traffic via feature flag или k8s service mesh)
- step 2: wait 30 минут
- step 3: run scripts/canary_check.py
- step 4: if exit 0 → ramp to 100%; if exit 1 → rollback + page on-call
3. Автоматический rollback: revert deploy, audit-event "canary_rollback" в audit_trail.
4. Push-уведомление в Slack/Telegram при rollback.
5. Тесты: simulate canary healthy → ramp; simulate unhealthy → rollback.

DoD
- [ ] Canary deployments с SLO-gate работают
- [ ] Автоматический rollback при SLO-burn
- [ ] Slack-уведомление при rollback
- [ ] pytest + e2e тесты зелёные

Коммит: feat(slo): add canary deployments with slo-gate and automatic rollback
```

---

## ВОЛНА 7: RBAC + multi-tenant изоляция

> Эпик E3.7. ТРИЗ-улучшение #2 (принцип 26 копирование + стандарт 5.1.1). Tenant-изоляция в storage
> (ClickHouse partitioning, Postgres RLS) + RBAC на API уровне (OPA/Rego policies). [ПАРАЛЛЕЛЬ] с
> Волнами 1–6 после PP02.

### PP30 — [T3.7.1] Role-модель: admin / sre / developer / dpo / auditor / agent

> PP0. Файл `agent_obs/rbac/roles.py` (NEW).

```text
ЗАДАЧА (тикет T3.7.1)
Role-модель с 6 ролями и дискретным набором permissions. Каждая роль — frozenset[str], no overlap
prohibited (один permission — одна роль, чтобы избежать ambiguity).

ЧТО СДЕЛАТЬ
1. agent_obs/rbac/roles.py (NEW):
- ROLES: dict[str, frozenset[str]] = {
    "admin":      frozenset({"trace:read", "pii:recover", "policy:write", "config:write",
                             "deploy:gate", "audit:read", "slo:write"}),
    "sre":        frozenset({"trace:read", "pii:recover", "config:write", "deploy:gate",
                             "audit:read", "slo:read"}),
    "developer":  frozenset({"trace:read", "audit:read", "slo:read"}),
    "dpo":        frozenset({"trace:read", "audit:read", "compliance:export"}),
    "auditor":    frozenset({"audit:read", "compliance:export"}),
    "agent":      frozenset({"trace:write", "span:write", "metrics:write"}),
  }
2. user → roles mapping (в Postgres-таблице user_roles).
3. Тест на no-overlap: assert intersection(roles) == empty.
4. Метод RBAC.check(user, action, resource) → bool (контракт из PP0).
5. Тесты: each role has access only to its permissions; cross-role access denied.

DoD
- [ ] 6 ролей с дискретными permissions
- [ ] No-overlap тест проходит
- [ ] RBAC.check() работает корректно
- [ ] pytest зелёный (≥ 30 кейсов)

Коммит: feat(rbac): add role model with 6 roles and discrete permission sets
```

---

### PP31 — [T3.7.2] OPA/Rego policy engine: declarative access rules

> PP0 + PP30. Файл `agent_obs/rbac/policy_engine.py` + `infra/rbac/policies/`.

```text
ЗАДАЧА (тикет T3.7.2)
Интеграция Open Policy Agent (OPA) с Rego-политиками для declarative access control. Политики:
- deny if user.tenant_id != trace.tenant_id (cross-tenant access)
- deny if action="pii:recover" and not user.mfa_verified
- allow if user.role in {admin, sre} and action in role.permissions

ЧТО СДЕЛАТЬ
1. infra/rbac/policies/observability.rego (NEW):
- package observability.rbac
- default allow = false
- allow if {
    input.user.role in ROLES_WITH_PERMISSION
    input.action in role_permissions[input.user.role]
    input.user.tenant_id == input.resource.tenant_id
    not requires_mfa(input.action) OR input.user.mfa_verified
  }
- requires_mfa(action) if action in {"pii:recover", "audit:read:raw"}
2. agent_obs/rbac/policy_engine.py (NEW):
- class PolicyEngine:
  - def check(user: User, action: str, resource: Resource) -> bool:
    * вызов OPA через python-opa или opa-python
    * cache result в Redis на 60 секунд (по sha256(user_id + action + resource_id))
3. OPA deployment в docker-compose: openpolicyagent/opa:latest, port 8181.
4. Hot-reload политик: OPA читает policies из volume, при изменении файла — reload без рестарта.
5. Тесты: cross-tenant deny; PII без MFA deny; валидный allow.

DoD
- [ ] OPA интегрирован с Rego-политиками
- [ ] Cross-tenant access denied
- [ ] MFA enforcement для PII-действий
- [ ] Кеш в Redis работает
- [ ] pytest зелёный

Коммит: feat(rbac): integrate opa rego policy engine with declarative access rules
```

---

### PP32 — [T3.7.3] Tenant-изоляция в storage: ClickHouse partitioning + Postgres RLS

> PP0 + PP31. Файлы в `agent_obs/storage/` + `infra/storage/`.

```text
ЗАДАЧА (тикет T3.7.3)
Tenant-изоляция на storage уровне: ClickHouse partition по tenant_id (новый partition на tenant) +
Postgres row-level-security (RLS) policy.

ЧТО СДЕЛАТЬ
1. ClickHouse:
- ALTER TABLE spans_hot MODIFY PARTITION BY (tenant_id, toYYYYMMDD(start_time));
- Каждый tenant — отдельный partition, не сканируется при чужом запросе.
- ClickHouse user-per-tenant: ch_user_tenant_a → GRANT SELECT ON spans_hot WHERE tenant_id='tenant_a'.
2. Postgres warm_store:
- ALTER TABLE traces_warm ENABLE ROW LEVEL SECURITY;
- CREATE POLICY tenant_isolation ON traces_warm
    USING (tenant_id = current_setting('app.tenant_id'));
- SET app.tenant_id = 'tenant_a' при подключении (через asyncpg setup_connection.
3. SDK: при создании connection передаёт tenant_id из SpanContext.
4. Тесты cross-tenant isolation:
- tenant_a user не может SELECT traces tenant_b (должен вернуться 0 строк или ошибка)
- tenant_a user не может drill-down на trace_id tenant_b
5. Audit: каждый cross-tenant attempted-access логируется в audit_trail (severity=warning).

DoD
- [ ] ClickHouse partitioned по tenant_id
- [ ] Postgres RLS включен, policy работает
- [ ] Cross-tenant access невозможен на storage уровне
- [ ] Audit-trail логирует attempted cross-tenant access
- [ ] pytest зелёный (≥ 10 cross-tenant кейсов)

Коммит: feat(rbac): add tenant isolation in clickhouse partitioning and postgres rls
```

---

### PP33 — [T3.7.4] Multi-tenant Vault: per-tenant encryption keys + access logs

> PP0 + CRITICAL PC02 (Vault) + PP32. Файлы в `infra/vault/`.

```text
ЗАДАЧА (тикет T3.7.4)
Multi-tenant Vault: каждый tenant имеет свой encryption key в Vault. Доступ к ключу tenant_a требует
Vault token с tenant_a claim. Audit: каждый доступ к PII-оригиналу логируется с tenant_id.

ЧТО СДЕЛАТЬ
1. infra/vault/policies/per-tenant.hcl:
- path "secret/data/pii/{{tenant_id}}/*" {
    capabilities = ["read"]
    allowed_parameters = { "tenant_id" = ["{{tenant_id}}"] }
  }
2. Vault JWT auth: agent аутентифицируется с JWT, в котором есть tenant_id claim.
  Vault проверяет, что JWT-tenant_id совпадает с path-tenant_id.
3. Per-tenant transit encryption key: vault transit create_key tenant_a_pii_key.
4. Audit backend: каждый запрос к pii/* логируется с timestamp, tenant_id, user_id, mask, action.
5. Тесты: tenant_a user не может читать pii/tenant_b/* (403); tenant_a user может читать только свои.

DoD
- [ ] Per-tenant Vault policies работают
- [ ] JWT-auth с tenant_id claim проверяется
- [ ] Per-tenant encryption keys созданы
- [ ] Audit-backend логирует с tenant_id
- [ ] pytest зелёный

Коммит: feat(rbac): add multi-tenant vault with per-tenant keys and jwt auth
```

---

### PP34 — [T3.7.5] RBAC audit-trail: каждое решение access-control логируется

> PP0 + PP30..PP33. Файл `agent_obs/guardrail/audit_trail.py` + UI.

```text
ЗАДАЧА (тикет T3.7.5)
Каждое решение RBAC (allow/deny) логируется в audit_trail с указанием user, action, resource, decision,
reason. UI: страница «RBAC decisions» для auditor-роли.

ЧТО СДЕЛАТЬ
1. agent_obs/guardrail/audit_trail.py:
- log_rbac_decision(user_id, role, action, resource_id, decision, reason)
- write в audit_events_hot (action_type="rbac_decision")
- batched async (как в PP05)
2. Метрика rbac_decisions_total{decision="allow|deny", action} — counter.
3. UI:
- GET /api/v1/audit/rbac-decisions?filters=... — для role="auditor" only
- страница «RBAC Decisions» в observability UI: таблица с фильтрами по user, action, decision, time
- export to CSV (для compliance-отчётов)
4. Alert: RBACAnomalyDetected — если deny-rate > 10% за 5 минут (возможно, атака или мисконфиг).
5. Тесты: log decision → queryable; auditor может читать, developer не может (403).

DoD
- [ ] Каждое RBAC-решение логируется в audit_events_hot
- [ ] Метрика rbac_decisions_total экспортируется
- [ ] UI-страница для auditor работает
- [ ] Alert RBACAnomalyDetected работает
- [ ] pytest + e2e зелёные

Коммит: feat(rbac): log every access-control decision to audit trail with ui for auditor
```

---

## ВОЛНА 8: High availability (99.9%) всех компонентов

> Эпик E3.8. После PP01 (HA-инфра) — реализация failover-логики в SDK и storage-клиентах.
> [ПАРАЛЛЕЛЬ] с Волнами 1–7 после PP01.

### PP35 — [T3.8.1] ClickHouse cluster replication + Keeper quorum

> PP0 + PP01. Файлы в `infra/ha/` + `agent_obs/storage/`.

```text
ЗАДАЧА (тикет T3.8.1)
ClickHouse cluster с репликацией: 2 реплики на shard + Keeper (Zookeeper) quorum 3 узла.
SDK пишет через cluster-DDL (ReplicatedMergeTree), автоматический failover при потере реплики.

ЧТО СДЕЛАТЬ
1. infra/ha/clickhouse-cluster.xml:
- <clickhouse><remote_servers>
    <observability_cluster>
      <shard><replica>ch-1</replica><replica>ch-2</replica></shard>
    </observability_cluster>
  </remote_servers>
- <zookeeper><node><host>keeper-1</host><port>2181</port></node>... (3 узла)
2. Keeper (альтернатива Zookeeper) — 3 узла, quorum 2/3.
3. DDL: CREATE TABLE spans_hot ... ENGINE = ReplicatedMergeTree(
    '/clickhouse/observability/{shard}/spans_hot',
    '{replica}'
   ) PARTITION BY (tenant_id, toYYYYMMDD(start_time));
4. SDK: clickhouse-driver-replica для записи (автоматический failover).
5. Test: kill ch-1 → SDK продолжает писать (ch-2 принимает); restart ch-1 → syncs.

DoD
- [ ] ClickHouse cluster с 2 репликами работает
- [ ] Keeper quorum 3 узла
- [ ] SDK пишет через cluster, failover автоматически < 10 сек
- [ ] pytest + integration test зелёные

Коммит: feat(ha): add clickhouse cluster with replicated mergetree and keeper quorum
```

---

### PP36 — [T3.8.2] Postgres streaming replication + pgBouncer + repmgr

> PP0 + PP01. Файлы в `infra/ha/`.

```text
ЗАДАЧА (тикет T3.8.2)
Postgres HA: 1 primary + 2 replicas (streaming replication), pgBouncer для connection pooling,
repmgr для automatic failover.

ЧТО СДЕЛАТЬ
1. infra/docker-compose.ha.yml — расширить:
- postgres-primary (порт 5433)
- postgres-replica-1, postgres-replica-2 (read-only)
- pgBouncer (порт 6432, балансировка на primary + replicas)
- repmgr (quorum witness, automatic failover при потере primary)
2. Streaming replication: primary wal_level=replica, replicas hot_standby=on.
3. Failover: repmgrd daemon monitoring, при потере primary → promote старшего replica.
4. SDK: через asyncpg с DSN "host=pgbouncer port=6432", read/write split через PgBouncer.
5. Test: kill primary → repmgr promotes replica в < 30 сек; SDK не теряет запросы (через pgBouncer retry).

DoD
- [ ] Postgres 1 primary + 2 replicas работает
- [ ] Streaming replication lag < 5 сек
- [ ] repmgr automatic failover < 30 сек
- [ ] SDK продолжит работу через pgBouncer
- [ ] pytest + integration test зелёные

Коммит: feat(ha): add postgres streaming replication with pgbouncer and repmgr failover
```

---

### PP37 — [T3.8.3] Vault raft HA + auto-unseal via transit

> PP0 + CRITICAL PC02 + PP01. Файлы в `infra/vault/`.

```text
ЗАДАЧА (тикет T3.8.3)
Vault HA: 2+ узла с raft storage (не file, как в CRITICAL PC02), auto-unseal через transit backend
(другой Vault или cloud KMS).

ЧТО СДЕЛАТЬ
1. infra/vault/vault-raft.json:
- storage "raft" { path = "/vault/data" node_id = "vault-1" }
- listener "tcp" { address = "0.0.0.0:8200" tls_cert_file = "..." }
- seal "transit" { address = "https://unseal-vault:8201" token = "..." disable_renewal = "false" }
2. infra/docker-compose.ha.yml:
- vault-1, vault-2 (raft peers)
- unseal-vault (отдельный Vault для auto-unseal, можно cloud KMS в prod)
3. Raft HA: 2 узла, quorum 1 (можно пережить потерю 1).
4. Auto-unseal: при рестарте vault-1 → автоматически распломбируется через transit.
5. SDK: hvac с cluster-DNS "vault.observability.local", failover между vault-1 и vault-2.
6. Test: kill vault-1 → SDK переключается на vault-2 < 5 сек; restart vault-1 → auto-unseal, re-joins cluster.

DoD
- [ ] Vault raft HA с 2 узлами работает
- [ ] Auto-unseal через transit backend
- [ ] SDK переключается между узлами автоматически
- [ ] pytest + integration test зелёные

Коммит: feat(ha): add vault raft ha with auto-unseal via transit backend
```

---

### PP38 — [T3.8.4] Active health-probes + circuit breaker для всех зависимостей

> PP0 + PP01..PP37. Файл `agent_obs/ha/health_checks.py` (NEW).

```text
ЗАДАЧА (тикет T3.8.4)
Active health-probes для всех зависимостей SDK (Langfuse, ClickHouse, Postgres, Vault, Redis, MinIO).
Circuit breaker: при N последовательных failures → mark dependency as unhealthy → degrade gracefully.

ЧТО СДЕЛАТЬ
1. agent_obs/ha/health_checks.py (NEW):
- class HealthCheck:
  - async def check_langfuse(self) -> Health: GET /api/public/health
  - async def check_clickhouse(self) -> Health: SELECT 1
  - async def check_postgres(self) -> Health: SELECT 1
  - async def check_vault(self) -> Health: GET v1/sys/health
  - async def check_redis(self) -> Health: redis-cli ping
  - async def check_minio(self) -> Health: mc admin info
- Health = dataclass(status: str, latency_ms: float, last_checked: float)
2. CircuitBreaker:
- class CircuitBreaker:
  - failure_threshold = 5  (после 5 последовательных failures → open)
  - recovery_time = 60  (через 60 сек → half-open, пробуем 1 запрос)
  - states: closed (норм), open (не дёргать), half-open (проба)
3. Integration в SDK: каждый exporter и storage-client использует circuit breaker.
4. Метрики:
- dependency_health{dependency="langfuse|ch|pg|vault|redis|minio"} — gauge 0/1
- circuit_breaker_state{dependency} — gauge 0/1/2 (closed/half-open/open)
- circuit_breaker_failures_total{dependency}
5. Алерт: DependencyUnhealthy — если dependency_health=0 > 5 минут.
6. Degrade gracefully:
  - Langfuse down → fire-and-forget в ring buffer, dropped_spans растёт (это OK)
  - Vault down → fail-closed PII-маскинг (как в CRITICAL)
  - ClickHouse down → tail-sampler переключается на stdout_exporter (через CRITICAL stdout_exporter)
  - Postgres down → compliance-catalog в NullCatalogWriter (как в CRITICAL)
7. Тесты: kill каждый dependency → SDK продолжает работать (с graceful degradation).

DoD
- [ ] Active health-probes работают для всех 6 зависимостей
- [ ] CircuitBreaker корректно переключает состояния
- [ ] Метрики health и breaker-state экспортируются
- [ ] Алерт DependencyUnhealthy работает
- [ ] Graceful degradation проверена для каждого dependency
- [ ] pytest + integration test зелёные

Коммит: feat(ha): add active health probes and circuit breakers for all dependencies
```

---

## ВОЛНА 9: NEW MODE A — OryaObservability как прокси между агентом и LLM

> **Главная инновация Уровня 3 (по требованию пользователя).** SDK поднимает in-process (или
> side-by-side) HTTP-прокси, через который агент обращается к LLM-провайдеру. Прокси: (1) перехватывает
> request/response bodies, (2) выставляет cost-атрибуты автоматически (без ручного кода в агенте),
> (3) генерирует span `llm.call` сам — агент пишет «обычный» `openai.chat.completions.create(...)`,
> но URL указывает на `localhost:8765/llm/v1`. Триггер Уровня 3: «observability должна работать на
> agent'ах, которые мы не контролируем». Включается флагом `AGENT_OBS_PROXY_MODE=on`.

### PP39 — [T3.9.1] Конфигурация режима прокси + env-toggle + fail-fast

> PP0. Файл `agent_obs/proxy/__init__.py` + `agent_obs/observability.py` (config loading).

```text
ЗАДАЧА (тикет T3.9.1)
Конфигурация режима прокси: env-toggle `AGENT_OBS_PROXY_MODE=on/off/auto`. При `auto` — прокси
поднимается только если хотя бы один из компонентов SDK недоступен (failover-режим). Fail-fast при
некорректных комбинациях (см. таблицу в §0.4).

ЧТО СДЕЛАТЬ
1. agent_obs/proxy/__init__.py (NEW):
- @dataclass class ProxyConfig:
    enabled: bool                  # AGENT_OBS_PROXY_MODE=on
    listen_host: str = "127.0.0.1"
    listen_port: int = 8765         # AGENT_OBS_PROXY_PORT
    upstream_provider: str         # "openai" / "anthropic" / "azure_openai" / "custom"
    upstream_base_url: str         # https://api.openai.com/v1
    upstream_api_key_env: str      # имя env-переменной с ключом
    request_timeout_seconds: float = 120.0
    streaming_enabled: bool = True
    log_bodies: bool = True        # AGENT_OBS_PROXY_LOG_BODIES (влияет на content-sampling)
    log_bodies_max_bytes: int = 100_000  # ограничение размера тела в span
2. В ObservabilitySDK.__init__():
- if config.proxy.enabled: self._proxy = LLMProxyServer(config.proxy, sdk=self)
  else: self._proxy = None
- async def start(self): if self._proxy: await self._proxy.start()
- async def shutdown(self): if self._proxy: await self._proxy.stop()
3. Fail-fast валидация (см. таблицу §0.4):
- if AGENT_OBS_PROXY_MODE=off and SHARED_PROXY_ENABLED=on: raise ConfigError("SHARED_PROXY requires AGENT_OBS_PROXY_MODE=on")
- if upstream_provider not in supported: raise ConfigError
- if upstream_api_key_env not in os.environ: raise ConfigError("missing API key")
4. .env.example — добавить блок:
- AGENT_OBS_PROXY_MODE=off  # off / on / auto
- AGENT_OBS_PROXY_HOST=127.0.0.1
- AGENT_OBS_PROXY_PORT=8765
- AGENT_OBS_PROXY_UPSTREAM_PROVIDER=openai
- AGENT_OBS_PROXY_UPSTREAM_BASE_URL=https://api.openai.com/v1
- AGENT_OBS_PROXY_UPSTREAM_API_KEY_ENV=OPENAI_API_KEY
- AGENT_OBS_PROXY_LOG_BODIES=true
- AGENT_OBS_PROXY_LOG_BODIES_MAX_BYTES=100000
5. README в agent_obs/proxy/:
- как переключить агента на прокси: изменить base_url на http://localhost:8765/llm/v1
- пример: openai.Client(base_url="http://localhost:8765/llm/v1", api_key="dummy")
  (настоящий ключ — в env, прокси подставляет свой)
6. Тесты:
- config load корректно для on / off / auto
- fail-fast при невалидных комбинациях
- при enabled=False прокси не поднимается, SDK работает в обычном режиме

DoD
- [ ] ProxyConfig загружается из env корректно
- [ ] Fail-fast при невалидных комбинациях MODE_A/MODE_B
- [ ] SDK.init() поднимает прокси только при enabled=True
- [ ] .env.example обновлён, README создан
- [ ] pytest зелёный (≥ 10 кейсов)

Коммит: feat(proxy): add proxy config with env-toggle and fail-fast validation
```

---

### PP40 — [T3.9.2] LLMProxyServer: FastAPI, перехват /llm/v1/chat/completions

> PP0 + PP39. Файл `agent_obs/proxy/server.py` (NEW).

```text
ЗАДАЧА (тикет T3.9.2)
Реализация FastAPI-прокси сервера. Endpoint POST /llm/v1/chat/completions принимает OpenAI-совместимый
запрос, форвардит в upstream (OpenAI/Anthropic/Azure), перехватывает request/response bodies,
выставляет span llm.proxy.call.

ЧТО СДЕЛАТЬ
1. agent_obs/proxy/server.py (NEW):
- from fastapi import FastAPI, Request, Response
- app = FastAPI(title="OryaObservability LLM Proxy")
- @app.post("/llm/v1/chat/completions")
  async def chat_completions(request: Request) -> Response:
    * body = await request.body()
    * request_id = generate_id()
    * upstream_response = await forward_to_upstream(body, request.headers, request_id)
    * span = build_proxy_span(request, body, upstream_response, request_id)
    * await sdk._enqueue(span)  # не блокирующий
    * return Response(content=upstream_response.content, media_type="application/json")
2. Streaming support:
- @app.post("/llm/v1/chat/completions") — если "stream": true в request body
- ответ — Server-Sent Events (text/event-stream), прокси передаёт chunks по мере поступления
- span закрывается после последнего chunk, накапливает stream events
3. Authentication passthrough:
- прокси НЕ требует API key от клиента (клиент передаёт "dummy")
- прокси подставляет upstream API key из env
- (опционально) mTLS для internal-only доступа (если включён AGENT_OBS_PROXY_TLS)
4. Headers propagation:
- X-OryaObs-Trace-Id (если клиент передал — используем; если нет — генерируем)
- X-OryaObs-User-Id, X-OryaObs-Tenant-Id, X-OryaObs-Agent-Id (клиент может передать)
- эти заголовки попадают в SpanContext
5. Error handling:
- upstream timeout → 504, span помечается error, span.error.type="upstream_timeout"
- upstream 4xx/5xx → forward response, span помечается error с кодом
- proxy internal error → 500, span помечается error
6. Тесты:
- non-streaming запрос → 200, span создан с request/response bodies
- streaming запрос → 200 + chunks, span создан со stream events
- upstream timeout → 504, span error
- missing headers → span создан с дефолтными значениями

DoD
- [ ] FastAPI-прокси поднимается на :8765
- [ ] Non-streaming и streaming оба работают
- [ ] Span llm.proxy.call создаётся на каждый запрос
- [ ] Headers propagation работает
- [ ] Error handling покрывает timeout/4xx/5xx
- [ ] pytest + integration test зелёные

Коммит: feat(proxy): add fastapi llm proxy server with streaming and header propagation
```

---

### PP41 — [T3.9.3] Span enrichment на прокси: bodies, tokens, cost, latency автоматически

> PP0 + PP40. Файл `agent_obs/proxy/obs_pipeline.py` (NEW).

```text
ЗАДАЧА (тикет T3.9.3)
Span enrichment pipeline на прокси: автоматически извлекает из request/response ключевые атрибуты
(model, tokens, finish_reason, latency, ttft — time-to-first-token для streaming). Cost считается
автоматически через _compute_cost (из MVP).

ЧТО СДЕЛАТЬ
1. agent_obs/proxy/obs_pipeline.py (NEW):
- class ObsPipeline:
  - async def enrich_span(self, span: Span, request_body: bytes, response_body: bytes,
                          stream_chunks: list[bytes] | None, headers: dict, timing: Timing) -> Span:
    * parse request_body (JSON): model, messages, tools, temperature, max_tokens, stream
    * parse response_body (JSON): usage.prompt_tokens, usage.completion_tokens, usage.cached_tokens,
      choices[0].finish_reason, choices[0].message.content (если log_bodies)
    * для streaming: aggregate chunks, extract usage из последнего chunk (OpenAI streaming
      включает usage в финальном chunk если stream_options.include_usage=true)
    * span.attributes.update({
        "llm.provider": upstream_provider,
        "llm.model": model,
        "llm.temperature": temperature,
        "llm.input_messages_count": len(messages),
        "llm.output_messages_count": 1,
        "llm.input_chars": len(json.dumps(messages)),
        "llm.output_chars": len(content),
        "tokens.input": prompt_tokens,
        "tokens.output": completion_tokens,
        "tokens.cached": cached_tokens,
        "llm.finish_reason": finish_reason,
        "llm.stream": stream,
        "llm.ttft_ms": ttft_ms (только для streaming, см. ниже),
        "cost.usd": compute_cost(...).cost_usd,
        "cost.price_book_version": compute_cost(...).price_book_version,
      })
    * if log_bodies: span.attributes["llm.input_text"] = truncated(messages, max_bytes)
      span.attributes["llm.output_text"] = truncated(content, max_bytes)
      (это сохраняется в Hot store в соответствии с content-sampling политикой)
2. Timing capture:
- t_request_received
- t_upstream_started
- t_first_token (только для streaming — первый chunk)
- t_upstream_completed
- ttft_ms = (t_first_token - t_upstream_started) * 1000
- latency_ms = (t_upstream_completed - t_request_received) * 1000
3. Content-sampling интеграция:
- если trace_id в content-sampling 10% (детерминированный хеш) → bodies сохраняются полностью
- иначе → только sha256 + char count (как в MVP P22)
4. Тесты:
- non-streaming запрос → span содержит все атрибуты, включая ttft_ms=0 (N/A)
- streaming запрос → ttft_ms > 0, latency_ms > ttft_ms
- при content-sampling < 10% → bodies не сохраняются, только хэш

DoD
- [ ] ObsPipeline корректно извлекает все атрибуты из request/response
- [ ] Streaming timing (ttft_ms) работает
- [ ] Cost-атрибуты автоматически через _compute_cost
- [ ] Content-sampling интегрирован
- [ ] pytest зелёный

Коммит: feat(proxy): add span enrichment pipeline with bodies tokens cost and ttft
```

---

### PP42 — [T3.9.4] Cost attribution в proxy-mode: tenant-aware биллинг

> PP0 + PP41. Файл `agent_obs/proxy/obs_pipeline.py` + `agent_obs/cost/`.

```text
ЗАДАЧА (тикет T3.9.4)
Cost attribution в proxy-mode: стоимость LLM-запроса автоматически атрибутируется на tenant + agent +
user (по headers). Это позволяет строить отчёты «сколько tenant X потратил на agent Y за неделю Z».

ЧТО СДЕЛАТЬ
1. В ObsPipeline.enrich_span (PP41):
-tenant_id = headers.get("X-OryaObs-Tenant-Id", "default")
- agent_id = headers.get("X-OryaObs-Agent-Id", "unknown")
- user_id = headers.get("X-OryaObs-User-Id", "anonymous")
- span.context.tenant_id = tenant_id  (если SpanContext поддерживает, иначе в attributes)
- span.attributes["cost.attribution.tenant_id"] = tenant_id
- span.attributes["cost.attribution.agent_id"] = agent_id
- span.attributes["cost.attribution.user_id"] = user_id
2. Background cost-aggregation job (расширяет CRITICAL PC33 — compliance-catalog):
- каждые 60 секунд: SELECT sum(cost.usd) FROM spans_hot WHERE tenant_id=... AND agent_id=...
  → upsert в traces_warm.cost_usd_total (tenant + agent + day)
- метрика cost_per_tenant_usd{tenant_id, agent_id} — gauge (обновляется раз в минуту)
3. Budget enforcement (как в CRITICAL, но на tenant уровне):
- tenant_budget_per_day = env TENANT_BUDGET_USD_DEFAULT=10.0 (overridable в Postgres tenant_config)
- при достижении 80% → rate-limit на tenant (прокси возвращает 429 с hint "budget approaching")
- при 100% → 403 Forbidden с hint "budget exhausted"
4. Audit: каждый cost-attribution update логируется в audit_trail (action="cost_attribution").
5. Тесты:
- запрос с заголовком tenant_id → cost атрибутируется правильно
- запрос без заголовка → tenant=default
- budget exhaustion → 429/403

DoD
- [ ] Cost attribution работает через headers
- [ ] Background aggregation job обновляет traces_warm
- [ ] Tenant-level budget enforcement работает
- [ ] Audit логируется
- [ ] pytest зелёный

Коммит: feat(proxy): add tenant-aware cost attribution with budget enforcement
```

---

### PP43 — [T3.9.5] Failover / degradation в proxy-mode: upstream-down → cache или 503

> PP0 + PP40..PP42. Файл `agent_obs/proxy/server.py`.

```text
ЗАДАЧА (тикет T3.9.5)
Failover-логика в прокси: если upstream (LLM-провайдер) недоступен — кеш (последние N ответов на
идентичные запросы, см. PP54 semantic cache preview) или 503 с понятным сообщением. Никогда не блокирует
агента надолго: timeout 5 сек на upstream, после чего fallback.

ЧТО СДЕЛАТЬ
1. В LLMProxyServer.chat_completions (PP40):
- timeout на upstream: 5 сек для non-streaming, 10 сек для streaming (env: PROXY_UPSTREAM_TIMEOUT=5)
- при timeout: проверить semantic cache (если включён) → вернуть cached response с заголовком
  X-OryaObs-Cache-Hit=true; иначе → 503 с JSON-телом {"error": "upstream_timeout", "retry_after": 30}
2. Circuit breaker на upstream (используя класс из PP38):
- 5 последовательных failures → mark upstream as unhealthy
- при unhealthy: 503 всем клиентам до восстановления (recovery after 60 сек half-open)
3. Retry policy:
- 429 (rate limit) от upstream → retry с exponential backoff (1s, 2s, 4s), max 3 retries
- 5xx → retry 1 раз
- 4xx (кроме 429) → не retry, forward response
4. Метрика proxy_upstream_failures_total{reason="timeout|429|5xx|circuit_open"}.
5. Audit: каждый fallback to cache или 503 логируется.
6. Тесты:
- simulate upstream timeout → 503 или cached response
- simulate 429 → retry, eventual success
- simulate circuit open → 503 всем клиентам

DoD
- [ ] Timeout 5 сек, fallback to cache или 503
- [ ] Retry policy для 429 и 5xx
- [ ] Circuit breaker интегрирован
- [ ] Метрики failures_total экспортируются
- [ ] pytest зелёный

Коммит: feat(proxy): add failover with timeout retry circuit breaker and cache fallback
```

---

## ВОЛНА 10: NEW MODE B — Shared proxy OryaObservability + OryaSecurity

> **Главная инновация Уровня 3 (по требованию пользователя).** Один HTTP-прокси обслуживает и
> observability-tap (cost, spans, latency), и security-scan (PII, injection, secrets — из соседнего
> репо `OryaSecurity`). Single audit-trail, single decision-point, single latency-budget. Триггер:
> «observability и security — две стороны одного потока agent→LLM; двойной прокси = двойной latency».
> Включается флагом `SHARED_PROXY_ENABLED=on` + `SHARED_PROXY_SECURITY_URL` (URL OryaSecurity-scanner).

### PP44 — [T3.10.1] Конфигурация shared-proxy + интеграция с OryaSecurity

> PP0 + PP39. Файл `agent_obs/proxy/shared_pipeline.py` + `agent_obs/proxy/security_client.py`.

```text
ЗАДАЧА (тикет T3.10.1)
Конфигурация shared-proxy: env-toggle `SHARED_PROXY_ENABLED=on`, URL OryaSecurity-scanner
(`SHARED_PROXY_SECURITY_URL=http://orya-security:8000/scan`), timeout на security-scan (50 мс p99).
Прокси-пайплайн: запрос → security-scan (async) → obs-enrich (async) → upstream (async).

ЧТО СДЕЛАТЬ
1. agent_obs/proxy/security_client.py (NEW):
- class SecurityClient:
  - def __init__(self, base_url: str, timeout: float = 0.05)
  - async def scan(self, request_body: bytes, headers: dict) -> SecurityScanResult:
    * POST {base_url}/scan с телом {request_body, headers, mode="full"}
    * timeout 50 мс (env: SHARED_PROXY_SECURITY_TIMEOUT=0.05)
    * при timeout → fail-open (возвращается verdict="clean", audit warning)
- @dataclass class SecurityScanResult:
    verdict: str           # "clean" | "flag" | "block"
    score: float           # 0..1
    redacted_fields: list[str]
    masked_request_body: bytes   # PII-замаскированный request
    audit_event_id: str
    security_metadata: dict      # детали по каждому detector
2. agent_obs/proxy/shared_pipeline.py (NEW):
- class SharedPipeline:
  - async def process(self, request: Request) -> tuple[bytes, SecurityScanResult, Span]:
    * security_result = await security_client.scan(request.body, request.headers)
    * если verdict="block" → возвращаем 403, спан создаётся с audit-event (record-then-block)
    * иначе → forward masked_request_body в upstream (маскинг произошёл в security)
    * obs-pipeline (PP41) обогащает span security-результатом
3. Конфигурация .env:
- SHARED_PROXY_ENABLED=off  # on / off
- SHARED_PROXY_SECURITY_URL=http://orya-security:8000/scan
- SHARED_PROXY_SECURITY_TIMEOUT=0.05  # 50 мс p99 budget
- SHARED_PROXY_SECURITY_FAIL_OPEN=true  # true = если security down, позволяем запрос
4. Запуск shared-proxy:
- if SHARED_PROXY_ENABLED=on: SharedPipeline is used в LLMProxyServer вместо ObsPipeline
- if SHARED_PROXY_ENABLED=off: ObsPipeline (PP41) как обычно
5. Тесты:
- security verdict=clean → запрос проходит, span включает security metadata
- security verdict=block → 403, span создаётся с error
- security timeout → fail-open, запрос проходит, audit warning
- SHARED_PROXY_ENABLED=off → shared_pipeline не вызывается

DoD
- [ ] SecurityClient интегрирован с OryaSecurity
- [ ] SharedPipeline работает в трёх сценариях (clean/flag/block)
- [ ] Fail-open при security timeout
- [ ] .env.example обновлён
- [ ] pytest + integration test с mock OryaSecurity зелёные

Коммит: feat(proxy): add shared proxy config with oryasecurity integration
```

---

### PP45 — [T3.10.2] Единый decision-router: security + observability в одной транзакции

> PP0 + PP44. Файл `agent_obs/proxy/decision_router.py` (NEW).

```text
ЗАДАЧА (тикет T3.10.2)
Decision-router: единая точка принятия решения на основе security + observability + (опционально)
policy. Решение security (allow/flag/block) приоритетнее observability — если security block,
observability-tap всё равно пишет span + audit-event (record-then-block).

ЧТО СДЕЛАТЬ
1. agent_obs/proxy/decision_router.py (NEW):
- class DecisionRouter:
  - async def decide(self, security_result, obs_span, policy_engine=None) -> Decision:
    * priority: security > policy > obs
    * если security.verdict == "block": return Decision(action="block_response",
        status_code=403, body={"error": "blocked_by_security", "reason": security_result.reason})
    * если security.verdict == "flag": return Decision(action="forward_with_flag",
        forward_masked_body=True, span_attribute="security.flag=true")
    * если security.verdict == "clean": return Decision(action="forward",
        forward_masked_body=True (PII masked), span_attribute="security.verdict=clean")
    * (опционально) policy_engine: например, "tenants on free plan can't use gpt-4o"
      → if not allowed: Decision(action="block_response", status_code=403,
        body={"error": "policy_violation", "rule": "free_tier_cant_use_gpt4o"})
2. Record-then-block: даже при block decision:
   - span создаётся и пишется в ring buffer с attributes["security.decision"]="block"
   - audit-event создаётся в audit_trail с action="blocked_request"
3. Single transaction: все 3 записи (span, audit-event, decision) пишутся атомарно через
   batched-async, идемпотентно по request_id.
4. Метрика decisions_total{action="forward|flag|block", reason="clean|pii|injection|policy"}.
5. Тесты:
- clean → forward
- flag → forward с span attribute
- block → 403 + span + audit-event
- policy violation → 403 + span

DoD
- [ ] DecisionRouter корректно приоритизирует security > policy > obs
- [ ] Record-then-block работает (span и audit-event создаются даже при block)
- [ ] Single-transaction batched write работает
- [ ] Метрики decisions_total экспортируются
- [ ] pytest зелёный

Коммит: feat(proxy): add unified decision router with record-then-block semantics
```

---

### PP46 — [T3.10.3] Cross-system correlation: trace_id связывает obs-span и security-audit

> PP0 + PP45. Файл `agent_obs/proxy/audit_trail.py` (NEW).

```text
ЗАДАЧА (тикет T3.10.3)
Cross-system correlation: единый trace_id проходит через observability-span и security-audit-event.
Это позволяет видеть в одном трейсе: что просканировал security, что записал observability, какое
решение принял decision-router.

ЧТО СДЕЛАТЬ
1. agent_obs/proxy/audit_trail.py (NEW):
- class SharedAuditTrail:
  - async def write(self, request_id: str, trace_id: str, security_audit_id: str,
                    obs_span_id: str, decision: str, timestamp: float):
    * INSERT в audit_events_hot (ClickHouse):
      (audit_id, trace_id, request_id, security_audit_id, obs_span_id, decision, timestamp, ...)
    * audit_id = sha256(request_id + trace_id)
  - async def get_by_trace(self, trace_id: str) -> list[dict]:
    * SELECT * FROM audit_events_hot WHERE trace_id = ?
2. trace_id propagation:
- прокси генерирует trace_id при приёме запроса (если не передан в X-OryaObs-Trace-Id)
- trace_id передаётся в security-scan (header X-OryaObs-Trace-Id)
- security-scan возвращает audit_event_id, который включает trace_id
3. UI: в trace-viewer показывается отдельная панель «Security Audit» с результатом сканирования
   (verdict, score, redacted_fields, security_metadata) + кнопка «View in OryaSecurity UI» (deep link).
4. Drill-down из observability trace → audit-event в OryaSecurity (если есть OryaSecurity UI).
5. Тесты:
- один запрос → span + audit-event с одинаковым trace_id
- query audit_trail по trace_id → находит связку
- UI trace-viewer показывает security-audit панель

DoD
- [ ] trace_id propagation через все компоненты работает
- [ ] SharedAuditTrail.write + get_by_trace работают
- [ ] UI показывает security-audit панель в trace-viewer
- [ ] Drill-down в OryaSecurity UI (deep link)
- [ ] pytest + e2e зелёные

Коммит: feat(proxy): add cross-system trace_id correlation between obs and security audit
```

---

### PP47 — [T3.10.4] Performance isolation: security-scan и obs-tap не мешают друг другу

> PP0 + PP44 + PP45. Файл `agent_obs/proxy/shared_pipeline.py`.

```text
ЗАДАЧА (тикет T3.10.4)
Performance isolation: security-scan и observability-enrichment выполняются параллельно (asyncio.gather)
если возможно, с независимыми timeouts и circuit breakers. Если security-scan не успел за 50 мс — fail-open,
observability-tap всё равно пишет span.

ЧТО СДЕЛАТЬ
1. В SharedPipeline.process (PP44):
- async def process(self, request: Request):
  * security_task = asyncio.create_task(self.security.scan(request.body, request.headers))
  * obs_task = asyncio.create_task(self.obs.enrich_span(...))  # если нужно обогатить request
  * пробуем security_task с timeout 50 мс:
    try: security_result = await asyncio.wait_for(security_task, timeout=0.05)
    except TimeoutError:
      security_result = SecurityScanResult.fail_open()  # verdict=clean, audit warning
      metric: shared_proxy_security_timeouts_total++
  * пробуем obs_task с timeout 100 мс:
    try: obs_span = await asyncio.wait_for(obs_task, timeout=0.1)
    except TimeoutError: obs_span = build_minimal_span()  # только trace_id + request_id
  * decision = await decision_router.decide(security_result, obs_span)
2. Независимые circuit breakers:
- circuit_breaker_security (PP38) — failures_threshold=3, recovery=30s
- circuit_breaker_obs (PP38) — failures_threshold=10, recovery=60s
3. Если security-circuit open → fail-open, метрика shared_proxy_security_circuit_open_total++
4. Если obs-circuit open → fail-open (span не создаётся, логируется warning), агент продолжает работать
5. Метрики per-pipeline:
- shared_proxy_security_latency_seconds — histogram p50, p99
- shared_proxy_obs_latency_seconds — histogram p50, p99
- shared_proxy_total_latency_seconds — histogram (sum)
6. SLA: shared_proxy_total_latency_seconds p99 < 100 мс (50 мс security + 50 мс obs parallel)
7. Тесты:
- normal case → оба успели, decision forward
- security slow → fail-open, obs ok, decision forward (clean fallback)
- obs slow → fail-open, security ok, decision based on security only
- both slow → fail-open both, span minimal, decision forward

DoD
- [ ] Security и obs в параллели с независимыми timeouts
- [ ] Независимые circuit breakers
- [ ] Fail-open для обоих pipelines
- [ ] Метрики latency per pipeline
- [ ] SLA p99 < 100 мс подтверждён load test
- [ ] pytest зелёный

Коммит: feat(proxy): add performance isolation between security and obs pipelines with fail-open
```

---

### PP48 — [T3.10.5] Единый audit-trail UI: наблюдение за obs+security decisions в одном месте

> PP0 + PP46 + PP47. UI + docs.

```text
ЗАДАЧА (тикет T3.10.5)
Единый audit-trail UI: страница, где SRE/DPO/auditor видят все decisions (forward/flag/block) с
фильтрами по tenant, user, agent, security-verdict, time. Drill-down в trace-viewer и в OryaSecurity UI.

ЧТО СДЕЛАТЬ
1. UI-страница /admin/audit-trail (RBAC: admin / sre / auditor):
- таблица: timestamp, tenant_id, user_id, agent_id, trace_id, security_verdict, decision, reason,
  security_score, redacted_fields_count
- фильтры: tenant_id, user_id, agent_id, security_verdict (clean/flag/block), decision (forward/flag/block),
  time range
- pagination: 50/page, sort by timestamp desc
- export to CSV (для compliance-отчётов)
2. Drill-down: клик на строку → trace-viewer с этим trace_id (панель Security Audit подсвечена).
3. Кнопка «View in OryaSecurity UI» — deep link на trace в OryaSecurity (если OryaSecurity UI доступен).
4. Saved queries: «all blocks за последние 24 часа», «flag от tenant_X за неделю»
5. Alerting (расширяет PP34):
- SharedProxyBlockRateHigh — если block-rate > 5% за 5 минут (возможно, атака)
- SharedProxySecurityTimeoutsHigh — если security-timeout-rate > 10% за 5 минут (security не справляется)
6. README в docs/audit-trail-runbook.md: как использовать страницу, какие действия при разных alert'ах.
7. Тесты (e2e): открыть audit-trail → отфильтровать → drill-down в trace → drill-down в OryaSecurity.

DoD
- [ ] UI страница audit-trail работает с фильтрами
- [ ] Drill-down в trace-viewer и OryaSecurity UI
- [ ] Saved queries работают
- [ ] Alerts настроены
- [ ] README создан, e2e-тесты зелёные

Коммит: feat(proxy): add unified audit-trail ui for obs+security decisions with drill-down
```

---

## ВОЛНА 11: Верификация PRODUCTION-READY (все exit-criteria)

> Финальный промпт. Запускать только после завершения всех тикетов PP01–PP48 + 80/20 улучшений PP51–PP60.

### PP49 — [VERIFY] Верификация всех exit-criteria Уровня 3 (§6.5 ROADMAP)

> PP0 + все PP01..PP48. Финальный приёмочный прогон.

```text
ЗАДАЧА (верификация PRODUCTION-READY)
Прогнать все exit-criteria Уровня 3 из §6.5 ROADMAP.md, заполнить чек-лист, создать отчёт приёмки.

EXIT-CRITERIA (из §6.5 ROADMAP):
1. Overhead instrumentation на LLM-call: < 2 мс p99, < 2% CPU
   - проверка: нагрузочный тест 1000 RPS в течение 10 минут, p99 измеряется в benchmark
2. Доля потерянных трейсов при пиках: < 0.1%
   - проверка: тест с искусственным бэкенд-failure, замер dropped_spans / spans_total
3. Задержка поступления события в UI: < 5 с p95
   - проверка: метрика langfuse_ingest_latency_seconds (из MVP P19) + drill_down_latency
4. Retention: hot/warm/cold — 14д / 90д / 1г, drill-down < 1 мин для cold
   - проверка: тест PP21 + PP24 (drill-down на cold-trace)
5. Доступность observability-стека: 99.9%
   - проверка: тест PP01 (HA-chaos-test) + uptime за 7 дней ≥ 99.9%
6. Идеальность по §11.4 ARCHITECT.md: ≥ 2.5
   - расчёт: Σ полезных функций / (Σ вредных + Σ затрат), фиксируется в docs/production-ideality.md
7. Adaptive sampling v2: доля сохранённых «интересных» трейсов = 100%
   - проверка: PP03–PP07 + тест на error/security/eval-fail трейсах (все сохранены)
8. Span-aggregation: сетевой трафик сокращён на ≥ 60%
   - проверка: PP08–PP11 + сравнение bytes_sent до/после агрегации
9. Tree + compression: трейс с 50+ шагами сохраняет структуру дерева
   - проверка: PP16–PP20 + визуальная проверка в trace-viewer
10. On-demand drill-down: запрос в cold-storage < 1 мин p95
    - проверка: PP21–PP24
11. SLO/SLA: 4 SLO (availability/latency/quality/cost), multi-window alerting
    - проверка: PP25–PP29 + симуляция burn-rate
12. RBAC + multi-tenant: cross-tenant access невозможен
    - проверка: PP30–PP34 + cross-tenant тесты
13. HA 99.9%: каждый компонент переживает потерю 1 реплики без деградации
    - проверка: PP35–PP38 + chaos-test
14. NEW MODE A (LLM-proxy): агент без изменения кода получает observability
    - проверка: PP39–PP43 + e2e с pilot_agent + base_url на прокси
15. NEW MODE B (shared proxy): security + observability в одной транзакции, latency < 100 мс p99
    - проверка: PP44–PP48 + load test на 100 RPS через shared-proxy

ЧТО СДЕЛАТЬ
1. scripts/verify_production_ready.py — прогон всех 15 exit-criteria с измерениями:
- каждый критерий — отдельная функция, возвращает dict {criterion_id, passed: bool, value, target, details}
- итоговый отчёт в docs/production-acceptance.md с таблицей:
  | # | Критерий | Целевое | Фактическое | PASS/FAIL | Заметки |
- артефакты: benchmarks logs, screenshots из Grafana, e2e видео
2. Тесты:
- tests/production/test_all_exit_criteria.py — integration tests, запускаются в CI
- mark: @pytest.mark.production_acceptance — отдельный stage в CI
3. Tags в git: git tag v3.0.0-production-ready после всех PASS
4. README в docs/production-acceptance.md с executive summary + ссылками на артефакты
5. Если FAIL по любому критерию — запустить PP50 (шаблон фиксов) для этого критерия

DoD
- [ ] Все 15 exit-criteria проверены
- [ ] Отчёт docs/production-acceptance.md создан
- [ ] Все критерии PASS (если FAIL — заведены тикеты в BACKLOG для PP50 cycle)
- [ ] Git tag v3.0.0-production-ready создан
- [ ] README с executive summary

Коммит: chore(verify): production-ready acceptance report with all exit criteria passed
```

---

## ВОЛНА 12: Шаблон фиксов по проваленным критериям

> Использовать только если PP49 выявил FAIL по любому критерию. Запускать итеративно — один PP50 на каждый
> проваленный критерий.

### PP50 — [FIX-TEMPLATE] Цикл фиксов по проваленному критерию

> PP0 + PP49 (отчёт со списком FAIL). Заполнять template под конкретный FAIL.

```text
ЗАДАЧА (шаблон фикса)
Для каждого проваленного критерия из PP49 — заполнить этот template, сделать fix-коммит,
перепроверить критерий, повторить до PASS.

TEMPLATE (заполни под конкретный FAIL):

### FIX-{номер} — критерий {#} {название критерия}
Фактическое значение: {value}
Целевое значение: {target}
Gap: {target - value}
Root cause: {анализ, почему не достигнуто}
Fix plan:
1. {шаг 1 — конкретное изменение в коде/конфиге}
2. {шаг 2}
3. {шаг 3}
Ожидаемый эффект: {value → target}
Тесты: {какие тесты добавляются/модифицируются}
Коммит: fix({scope}): {description}

ЧТО СДЕЛАТЬ (общий алгоритм)
1. Открыть docs/production-acceptance.md, найти FAIL-критерий
2. Скопировать template, заполнить под конкретный случай
3. Реализовать fix plan (1-3 конкретных изменения)
4. Прогнать соответствующий integration test
5. Перепроверить критерий через scripts/verify_production_ready.py --only {criterion_id}
6. Если PASS → обновить docs/production-acceptance.md (FAIL → PASS)
7. Если всё ещё FAIL → повторить template с новым root cause analysis
8. После всех PASS → перезапустить полный PP49

DoD
- [ ] Template заполнен для каждого FAIL-критерия
- [ ] Все fix-коммиты сделаны
- [ ] Все критерии перепроверены, PASS
- [ ] docs/production-acceptance.md обновлён (FAIL → PASS)
- [ ] Git tag обновлён (v3.0.0-production-ready)

Коммит: fix({scope}): {description} — fill template per FAIL criterion
```

---

## ВОЛНА 13: 80/20 улучшения (20% усилий → 80% пользы)

> После генерации PP01–PP50 пройдёмся по всему файлу и добавим 10 улучшающих промптов, которые
> покрывают типичные production-дыры: observability-of-observability, chaos-тесты, versioning,
> semantic cache, cost-routing-preview, hot-reload, drift-explainability, e2e-encryption,
> golden-signals-sli, obs-cost-budget. Каждый промпт — самостоятельный, можно выполнять
> параллельно после PP49. Промпты PP51–PP60 добавлены именно в рамках 80/20 прохода.

### PP51 — [IMPROVE-1] Observability-of-observability: meta-метрики здоровья самого observability-стека

> PP0 + все. Файл `agent_obs/metrics.py` + `infra/grafana/dashboards/meta-observability.json`.

```text
ЗАДАЧА (improvement)
«Кто наблюдает за наблюдателем?» — observability-of-observability: метрики здоровья самого стека
(ring buffer fill ratio, export latency, dropped spans, eval-queue backlog, vault latencies, drift
detector cadence). Без этого observability может тихо сломаться и никто не заметит.

ЧТО СДЕЛАТЬ
1. agent_obs/metrics.py — добавить meta-метрики:
- obs_ring_buffer_fill_ratio — gauge 0..1 (current_size / maxsize). Alert если > 0.8 (близко к drop'ам)
- obs_export_latency_seconds — histogram (время от enqueue до попадания в Langfuse/ClickHouse)
- obs_exporter_failures_total{exporter="langfuse|ch|warm"} — counter
- obs_eval_queue_backlog — gauge (текущая длина Redis eval-queue)
- obs_vault_recovery_latency_seconds — histogram
- obs_drift_detector_last_run_seconds — gauge (seconds since last drift cron tick)
2. infra/grafana/dashboards/meta-observability.json — дашборд «Obs Stack Health»:
- panel: ring buffer fill ratio (graph + threshold line at 0.8)
- panel: export latency p50/p95/p99
- panel: eval queue backlog (graph)
- panel: drift detector last run (gauge, alert if > 1800 сек = 30 мин)
- panel: vault latencies
3. Alerting rules:
- ObsRingBufferNearFull — fill ratio > 0.8 в течение 5 минут
- ObsExportLatencyHigh — p99 > 10 сек в течение 5 минут
- ObsEvalQueueBacklogHigh — backlog > 1000 в течение 10 минут
- ObsDriftDetectorStalled — last_run > 30 минут
4. Self-check cron раз в минуту: READ all meta-метрики, если любая вне нормы → page on-call.
5. README в docs/meta-observability-runbook.md: как реагировать на каждый alert.

DoD
- [ ] 6+ meta-метрик экспортируются
- [ ] Grafana-дашборд «Obs Stack Health» импортирован
- [ ] 4 alerting rules добавлены
- [ ] Self-check cron работает
- [ ] README создан
- [ ] pytest зелёный

Коммит: feat(meta-obs): add observability-of-observability meta-metrics and dashboard
```

---

### PP52 — [IMPROVE-2] Chaos engineering: weekly game-day для HA-стека

> PP0 + PP35–PP38. Файлы в `scripts/chaos/` + `docs/chaos-runbook.md`.

```text
ЗАДАЧА (improvement)
Weekly chaos test: каждый понедельник в 04:00 UTC — автоматический chaos-прогон, который убивает по
одному компоненту observability-стека (random choice) и проверяет, что SLO не нарушены.

ЧТО СДЕЛАТЬ
1. scripts/chaos/weekly_chaos.py — cron-скрипт:
- выбирает random target из {langfuse-1, ch-1, pg-primary, vault-1, redis-1, minio-1, otel-collector-1}
- docker stop <target> через docker API
- ждёт 5 минут (recover period)
- проверяет SLO за эти 5 минут (через Prometheus query)
- docker start <target>
- пишет отчёт в docs/chaos-reports/{date}-{target}.md
2. SLO check during chaos:
- availability > 99.5% (не 99.9%, т.к. допускаем лёгкую деградацию при chaos)
- latency p95 < 5 сек (не 3 сек, т.к. допускаем)
- error budget consumption < 20% за 5 минут
3. Game-day сценарий (manual): docs/chaos-runbook.md с инструкциями для SRE — как проводить
полный chaos-тест вручную (для off-schedule проверок).
4. Alert: ChaosTestFailed — если chaos test обнаружил SLO violation, автоматический тикет в BACKLOG.
5. Тесты (e2e): test_chaos.py с mock docker stop.

DoD
- [ ] Weekly chaos test запускается по cron
- [ ] SLO проверяются во время chaos
- [ ] Отчёт docs/chaos-reports/ формируется автоматически
- [ ] Game-day runbook создан
- [ ] Alert ChaosTestFailed работает
- [ ] pytest зелёный

Коммит: feat(chaos): add weekly chaos engineering game-day with slo validation
```

---

### PP53 — [IMPROVE-3] Prompt-template versioning: версионирование system prompts

> PP0 + PP41 (proxy). Файл `agent_obs/proxy/obs_pipeline.py`.

```text
ЗАДАЧА (improvement)
Versioning system prompts: прокси извлекает system-prompt из request, считает его sha256, кладёт
в атрибут llm.system_prompt_sha256 (это есть в MVP), но также записывает mapping sha256 → text в
отдельную таблицу prompt_templates. Это позволяет видеть, какая версия промпта использовалась
на конкретном трейсе, и делать A/B-сравнения.

ЧТО СДЕЛАТЬ
1. В ObsPipeline.enrich_span (PP41):
- prompt_text = extract_first_system_prompt(request_body)  # из messages[0].content если role=system
- prompt_sha = sha256(prompt_text)
- span.attributes["llm.system_prompt_sha256"] = prompt_sha
- if trace_id in content-sampling 10%: background INSERT в prompt_templates (sha, text, first_seen,
  last_seen, version_label (если в начале промпта есть comment "VERSION: 1.2.3" — извлечь))
2. ClickHouse-таблица prompt_templates (NEW):
- sha256 String, version_label String, first_seen DateTime, last_seen DateTime, sample_text String
- TTL 90 дней
3. UI: в trace-viewer показывается «System Prompt: version 1.2.3 (sha: 5f3a...» с кнопкой
«View all traces with this prompt» → query по sha256.
4. A/B comparison: при изменении prompt → автоматический alert «Prompt template changed, consider
running eval comparison».
5. Тесты: запрос с system prompt → sha вычисляется, mapping записывается; UI показывает.

DoD
- [ ] Prompt template sha256 вычисляется в прокси
- [ ] ClickHouse-таблица prompt_templates создана
- [ ] UI показывает version label и link на traces с этим prompt
- [ ] A/B alerting работает
- [ ] pytest зелёный

Коммит: feat(proxy): add prompt-template versioning with sha256 mapping and ab alerting
```

---

### PP54 — [IMPROVE-4] Semantic cache preview: кеширование идентичных LLM-запросов

> PP0 + PP43 (failover). Файл `agent_obs/proxy/semantic_cache.py` (NEW).

```text
ЗАДАЧА (improvement)
Preview-реализация semantic cache: идентичные запросы (по sha256(request_body)) в течение 5 минут
обслуживаются из кеша, без обращения к upstream. Сэкономленные $ идут в бюджет eval-pipeline.

ЧТО СДЕЛАТЬ
1. agent_obs/proxy/semantic_cache.py (NEW):
- class SemanticCache:
  - redis_client: Redis
  - ttl_seconds: 300  # 5 минут (env: SEMANTIC_CACHE_TTL=300)
  - async def get(self, request_hash: str) -> Optional[CachedResponse]:
    * redis.get(f"semcache:{request_hash}") → parse JSON → return CachedResponse
  - async def set(self, request_hash: str, response: CachedResponse, ttl: int = None):
    * redis.set(f"semcache:{request_hash}", json.dumps(response), ex=ttl or self.ttl_seconds)
2. В LLMProxyServer.chat_completions:
- перед форвардом в upstream — проверить semantic cache
- если hit → вернуть cached response, span создаётся с attribute "cache.hit"=true, cost=0
- если miss → обычный forward + после получения ответа — cache.set
3. Безопасность:
- PII-маскинг: маскированный request хешируется, не оригинал (чтобы tenant_a не получил PII tenant_b
  через cache hit — в общем случае это невозможно, т.к. hash разный)
- header propagation: cache.hit response получает те же headers, что и оригинал
4. Метрика:
- semantic_cache_hits_total{tenant_id, agent_id}
- semantic_cache_misses_total
- semantic_cache_hit_ratio — gauge (должна быть 0.05-0.15 в норме, выше = эффективный кеш)
- semantic_cache_cost_saved_usd — counter
5. Disabling: env SEMANTIC_CACHE_ENABLED=true (default on, можно отключить для debugging)
6. Тесты:
- 2 идентичных запроса подряд → 2-й cached, span показывает cache.hit=true
- разные запросы → cache miss
- TTL истёк → cache miss

DoD
- [ ] Semantic cache работает с TTL 5 минут
- [ ] PII-безопасность (маскированный хеш)
- [ ] Метрики hits/misses/cost_saved экспортируются
- [ ] Cache можно отключить env
- [ ] pytest зелёный

Коммит: feat(proxy): add semantic cache preview for identical llm requests with 5min ttl
```

---

### PP55 — [IMPROVE-5] Cost-aware model routing preview: дешёвая модель для простых запросов

> PP0 + PP42 (cost attribution). Файл `agent_obs/proxy/router.py` (NEW).

```text
ЗАДАЧА (improvement)
Preview cost-aware routing: прокси может переопределить модель в request на более дешёвую, если
запрос «простой» (короткий prompt, нет tools, нет system prompt с reasoning). Это preview —
не включается по умолчанию, только через env COST_AWARE_ROUTING_PREVIEW=on.

ЧТО СДЕЛАТЬ
1. agent_obs/proxy/router.py (NEW):
- class CostAwareRouter:
  - def should_downgrade(self, request_body: bytes) -> tuple[bool, str]:
    * parse request_body: model, messages, tools
    * if model == "gpt-4o" and len(messages) <= 3 and not tools:
      → (True, "gpt-4o-mini")  # дешёвая альтернатива
    * else → (False, original_model)
2. В LLMProxyServer.chat_completions:
- if COST_AWARE_ROUTING_PREVIEW=on:
  - should_downgrade, new_model = router.should_downgrade(request.body)
  - if should_downgrade: request_body["model"] = new_model
    span.attributes["llm.original_model"] = original_model
    span.attributes["llm.routed_model"] = new_model
    span.attributes["llm.routing_reason"] = "cost_aware_simple_request"
3. Audit: каждый downgrade логируется в audit_trail (action="cost_aware_downgrade").
4. Метрика:
- cost_aware_downgrades_total{original_model, routed_model}
- cost_aware_cost_saved_usd — counter
5. A/B evaluation: nightly job сравнивает faithfulness-score трейсов с downgrade vs без —
если drop > 5% → disable routing automatically.
6. Тесты:
- простой запрос + gpt-4o → downgraded до gpt-4o-mini
- сложный запрос (с tools) → не downgraded
- COST_AWARE_ROUTING_PREVIEW=off → routing не срабатывает

DoD
- [ ] Cost-aware router работает в preview-режиме
- [ ] Audit логирует downgrades
- [ ] Метрики экспортируются
- [ ] A/B evaluation nightly job работает
- [ ] pytest зелёный

Коммит: feat(proxy): add cost-aware model routing preview for simple requests
```

---

### PP56 — [IMPROVE-6] Hot-reload для security policies (через OPA bundle)

> PP0 + PP31 (OPA) + PP44 (shared pipeline). Файлы в `infra/rbac/`.

```text
ЗАДАЧА (improvement)
Hot-reload OPA policies без рестарта прокси: при изменении Rego-файла — OPA автоматически
перезагружает bundle (через OPA bundle API или file-watcher). Это позволяет менять security-политики
без downtime.

ЧТО СДЕЛАТЬ
1. infra/rbac/policies/.gitkeep — Rego-файлы в этой папке
2. OPA bundle server (NEW в docker-compose):
- opa-bundle-server (образ openpolicyagent/opa:latest) — запускается с флагом --bundle
- читает policies из volume, отдаёт через HTTP /bundles/observability.tar.gz
- file-watcher: при изменении Rego-файла — пересобирает bundle (atomic swap)
3. OPA policy config (infra/rbac/opa.yaml):
- services: observability-bundle (url: http://opa-bundle-server:8080)
- bundles: observability (service: observability-bundle, resource: bundles/observability.tar.gz)
- poll interval: 30 секунд
4. SDK-side: PolicyEngine читает policies через HTTP API OPA (что уже в PP31) — но теперь
OPA всегда имеет свежую версию policies без рестарта.
5. Audit: каждый bundle reload логируется (с diff old vs new), audit_event_id/action="policy_reload".
6. Тесты:
- изменить Rego-файл → через 30 сек policy применяется → новый запрос оценивается по новой policy
- rollback policy → другая политика снова вступает в силу
- audit-trail показывает reload

DoD
- [ ] OPA bundle server работает
- [ ] File-watcher пересобирает bundle при изменении Rego
- [ ] SDK читает policies через HTTP API
- [ ] Hot-reload применяется < 30 сек
- [ ] Audit логирует reloads
- [ ] pytest зелёный

Коммит: feat(rbac): add hot-reload for opa policies via bundle server
```

---

### PP57 — [IMPROVE-7] Drift explainability: UMAP + top-k contributors

> PP0 + CRITICAL PC28 (Phoenix UMAP). Файл `agent_obs/drift/explainability.py` (NEW).

```text
ЗАДАЧА (improvement)
Drift explainability: при срабатывании drift alert — автоматически вычислять top-k «наиболее
отклоняющихся» embeddings, показывать их в Phoenix UMAP, и предлагать LLM-сгенерированное
объяснение «что изменилось в ответах».

ЧТО СДЕЛАТЬ
1. agent_obs/drift/explainability.py (NEW):
- class DriftExplainer:
  - async def explain(self, drift_alert: DriftAlert) -> DriftExplanation:
    * query ClickHouse: top-k embeddings из last-window с максимальным distance от centroid baseline
    * fetch corresponding trace_id, llm.output_text для этих embeddings
    * query LLM-judge: «Compare these N responses vs these N baseline responses. What changed?»
    * return DriftExplanation(top_k_traces, summary, hypothesis)
2. DriftAlert hook:
- при срабатывании alert → enqueue DriftExplainer.explain в eval-queue (low priority)
- после завершения → write explanation в drift_explanations (ClickHouse table)
3. UI: alert в Alertmanager с annotation «Click here for drift explanation» → deep link в
observability UI на страницу с UMAP-визуализатором + summary + top-k traces.
4. Метрика drift_explanations_generated_total — counter.
5. Тесты: simulate drift → explanation генерируется в течение 5 минут; содержит top-k + LLM summary.

DoD
- [ ] DriftExplainer работает (top-k + LLM summary)
- [ ] UI интеграция с Phoenix UMAP
- [ ] Метрика explanations_generated_total
- [ ] pytest + integration test зелёные

Коммит: feat(drift): add drift explainability with umap top-k and llm summary
```

---

### PP58 — [IMPROVE-8] End-to-end encryption для PII: vault transit encrypt на лету

> PP0 + CRITICAL PC02 (Vault) + PP41 (proxy). Файл `agent_obs/proxy/obs_pipeline.py`.

```text
ЗАДАЧА (improvement)
E2E encryption для PII: найденный PII в request body сразу шифруется через Vault Transit (не просто
маскируется). В storage попадает encrypted_blob. Recovery требует MFA + Vault decrypt. Это добавляет
слой защиты даже при компрометации storage.

ЧТО СДЕЛАТЬ
1. Расширить ObsPipeline (PP41):
- после PII-детекции (через security_client в shared mode или встроенный в obs mode):
  * original_value → vault.transit_encrypt(plaintext, key="pii_field_key_{tenant_id}")
  * в span.attributes["pii.encrypted_fields"] = [
      {"field": "user_message.email", "encrypted_value": "vault:v1:...", "key_version": 1}
    ]
  * оригинал НЕ сохраняется в span (только encrypted_blob)
2. Vault transit keys:
- per-tenant key: tenant_a_pii_field_key (rotation каждые 90 дней)
- key_version сохраняется в span (для будущего decrypt с правильной версией)
3. Recovery: vault.transit_decrypt(encrypted_value, key_version=N) — требует MFA-token (как в CRITICAL).
4. Storage: encrypted_blob хранится как String в ClickHouse, не влияет на схему.
5. Метрика:
- pii_encrypted_total{entity_type} — counter
- pii_vault_transit_failures_total — counter
6. Opt-out: env PII_E2E_ENCRYPTION=true (default on; off только для debugging).
7. Тесты: запрос с PII → span содержит encrypted_fields, оригинал отсутствует; decrypt с MFA работает.

DoD
- [ ] E2E encryption через Vault Transit работает
- [ ] Per-tenant keys с rotation
- [ ] MFA-required decrypt работает
- [ ] Метрики экспортируются
- [ ] pytest зелёный

Коммит: feat(proxy): add e2e vault transit encryption for pii in spans
```

---

### PP59 — [IMPROVE-9] Golden signals SLI: латентность, трафик, ошибки, saturation

> PP0 + PP25 (SLO). Файл `agent_obs/slo/definitions.py`.

```text
ЗАДАЧА (improvement)
Google SRE «Four Golden Signals»: LATENCY, TRAFFIC, ERRORS, SATURATION. Добавить как
дополнительный SLO-набор (поверх availability/latency/quality/cost из PP25).

ЧТО СДЕЛАТЬ
1. agent_obs/slo/definitions.py — расширить SLO_CATALOG:
- latency_p99: target=2 сек (p99 latency ≤ 2 сек), burn-rate thresholds (5m, 1h)
- traffic: target = +20% к 7-day moving average (если traffic падает > 20% — alert)
- errors: target = error_rate < 0.5%, burn-rate (5m, 1h)
- saturation: target = cpu < 80%, ring_buffer_fill < 70%, eval_queue_backlog < 100
2. Метрики golden signals:
- latency_p99_seconds — gauge (rolling p99)
- traffic_rps — gauge (rolling requests/sec)
- error_rate — gauge (rolling 5m)
- cpu_saturation_ratio — gauge
- ring_buffer_saturation_ratio — gauge
3. Дашборд «Golden Signals» в Grafana (infra/grafana/dashboards/golden-signals.json):
- 4 panel-графика по 4 сигналам, плюс общий «health» gauge
4. Alerts: переиспользуют multi-window burn-rate из PP26 для каждого сигнала.
5. README: docs/golden-signals-runbook.md — что делать при срабатывании каждого alert.
6. Тесты: каждый сигнал считается корректно; alerts срабатывают при threshold violation.

DoD
- [ ] 4 golden signals добавлены в SLO_CATALOG
- [ ] Метрики экспортируются
- [ ] Дашборд «Golden Signals» импортирован
- [ ] Alerts работают
- [ ] README создан
- [ ] pytest зелёный

Коммит: feat(slo): add four golden signals latency traffic errors saturation
```

---

### PP60 — [IMPROVE-10] Observability cost budget: tracking $$ на сам observability-стек

> PP0 + PP42 (cost attribution). Файл `agent_obs/cost/obs_self_cost.py` (NEW) + Grafana.

```text
ЗАДАЧА (improvement)
Tracking расходов на сам observability-стек: LLM-judge calls, Vault storage, ClickHouse storage,
Postgres storage, S3 storage, Langfuse license (if paid). Это позволяет ответить на вопрос
«сколько стоит observability и не превысила ли она value».

ЧТО СДЕЛАТЬ
1. agent_obs/cost/obs_self_cost.py (NEW):
- class ObservabilityCostTracker:
  - async def aggregate_daily(self) -> dict:
    * llm_judge_cost: SELECT sum(cost.usd) FROM spans_hot WHERE span_type="eval.judge" AND ts today
    * vault_storage_cost: bytes * $0.05/GB/month / 30 (приблизительно)
    * ch_storage_cost: SELECT sum(bytes) FROM system.parts WHERE database='observability' today
    * pg_storage_cost: SELECT pg_database_size('warm_store') * $0.10/GB/month / 30
    * s3_storage_cost: bytes in cold-traces bucket * $0.023/GB/month / 30
    * langfuse_license: $0 (self-hosted) или env LANGFUSE_LICENSE_MONTHLY/30
    * return {component: cost_usd, total: sum}
2. Cron nightly 02:00 UTC: aggregate → upsert в obs_self_cost_daily (date, component, cost_usd).
3. Метрика obs_self_cost_usd{component="llm_judge|vault|ch|pg|s3|langfuse"} — gauge (today's cost).
4. Дашборд «Obs Self Cost» в Grafana:
- graph: total cost по дням (30-day window)
- pie: breakdown by component
- gauge: cost/value ratio (obs_self_cost / agent_value_created) — если > 0.2 (observability стоит
  больше 20% от value) → alert ObsCostTooHigh
5. README: docs/obs-cost-runbook.md — как оптимизировать (в основном — снизить LLM-judge rate).
6. Тесты: aggregate_daily считает корректно по mock-данным.

DoD
- [ ] ObservabilityCostTracker работает
- [ ] Cron nightly aggregation запущен
- [ ] Метрики obs_self_cost_usd экспортируются
- [ ] Дашборд создан
- [ ] Alert ObsCostTooHigh работает
- [ ] README создан
- [ ] pytest зелёный

Коммит: feat(cost): add observability self-cost tracking with grafana dashboard and alert
```

---

## Приложение A. Сводный индекс промптов

> Сквозная нумерация PP0 → PP60 без пропусков. 61 промпт всего.

| # | Промпт | Тикет | Эпик | Файл/слой |
|---|---|---|---|---|
| 0 | PP0 — мастер-контекст PRODUCTION-READY | — | — | (master) |
| 1 | PP01 — HA-инфраструктура | T3.0.1 | E3.0 | infra/ha/ |
| 2 | PP02 — mTLS / cert management | T3.0.2 | E3.0 | infra/rbac/, infra/ha/certs/ |
| 3 | PP03 — Sampling policy v2 (eval-aware) | T3.1.1 | E3.1 | agent_obs/exporters/tail_sampler.py |
| 4 | PP04 — Feedback loop eval→policy | T3.1.2 | E3.1 | tail_sampler.py |
| 5 | PP05 — Audit-trail для sampling | T3.1.3 | E3.1 | tail_sampler.py, audit_trail.py |
| 6 | PP06 — Sampler Policy History dashboard | T3.1.4 | E3.1 | infra/grafana/ |
| 7 | PP07 — Метрика kept_ratio_by_reason | T3.1.5 | E3.1 | metrics.py |
| 8 | PP08 — SpanAggregator | T3.2.1 | E3.2 | span_aggregator.py |
| 9 | PP09 — Embedded events сериализация | T3.2.2 | E3.2 | observability.py |
| 10 | PP10 — Expandable events UI | T3.2.3 | E3.2 | UI |
| 11 | PP11 — Метрики aggregation | T3.2.4 | E3.2 | metrics.py |
| 12 | PP12 — AdaptiveEvalSampler day/night/incident | T3.3.1 | E3.3 | eval/adaptive_sampler.py |
| 13 | PP13 — Nightly golden eval | T3.3.2 | E3.3 | scripts/ |
| 14 | PP14 — Incident-mode 100% eval | T3.3.3 | E3.3 | adaptive_sampler.py |
| 15 | PP15 — Eval cost dashboard + budget | T3.3.4 | E3.3 | infra/grafana/, adaptive_sampler.py |
| 16 | PP16 — TraceTreeBuilder | T3.4.1 | E3.4 | tree_compressor.py |
| 17 | PP17 — Compression policy | T3.4.2 | E3.4 | tree_compressor.py |
| 18 | PP18 — Async LLM-summary для сжатых узлов | T3.4.3 | E3.4 | tree_compressor.py, eval/ |
| 19 | PP19 — Drill-down из compressed-node | T3.4.4 | E3.4 | UI + storage |
| 20 | PP20 — Embedded subtree (матрёшка) | T3.4.5 | E3.4 | tree_compressor.py |
| 21 | PP21 — DrillDown API | T3.5.1 | E3.5 | storage/drill_down.py |
| 22 | PP22 — Athena integration | T3.5.2 | E3.5 | storage/athena_client.py |
| 23 | PP23 — Drill-down cache Redis | T3.5.3 | E3.5 | drill_down.py |
| 24 | PP24 — Drill-down UI кнопка | T3.5.4 | E3.5 | UI |
| 25 | PP25 — SLO-каталог | T3.6.1 | E3.6 | slo/definitions.py |
| 26 | PP26 — Multi-window burn-rate alerting | T3.6.2 | E3.6 | slo/burn_rate.py, prometheus-rules.yml |
| 27 | PP27 — Error budget tracking | T3.6.3 | E3.6 | slo/error_budget.py |
| 28 | PP28 — SLO-дашборд | T3.6.4 | E3.6 | infra/grafana/ |
| 29 | PP29 — SLO-based canary deployments | T3.6.5 | E3.6 | .github/workflows/, scripts/ |
| 30 | PP30 — Role-модель | T3.7.1 | E3.7 | rbac/roles.py |
| 31 | PP31 — OPA/Rego policy engine | T3.7.2 | E3.7 | rbac/policy_engine.py, infra/rbac/ |
| 32 | PP32 — Tenant-изоляция в storage | T3.7.3 | E3.7 | storage/, infra/storage/ |
| 33 | PP33 — Multi-tenant Vault | T3.7.4 | E3.7 | infra/vault/ |
| 34 | PP34 — RBAC audit-trail | T3.7.5 | E3.7 | audit_trail.py, UI |
| 35 | PP35 — ClickHouse cluster replication | T3.8.1 | E3.8 | infra/ha/, storage/ |
| 36 | PP36 — Postgres streaming replication | T3.8.2 | E3.8 | infra/ha/ |
| 37 | PP37 — Vault raft HA + auto-unseal | T3.8.3 | E3.8 | infra/vault/ |
| 38 | PP38 — Health-probes + circuit breaker | T3.8.4 | E3.8 | ha/health_checks.py |
| 39 | PP39 — Proxy config + env-toggle | T3.9.1 | E3.9 (NEW) | proxy/__init__.py |
| 40 | PP40 — LLMProxyServer FastAPI | T3.9.2 | E3.9 (NEW) | proxy/server.py |
| 41 | PP41 — Span enrichment на прокси | T3.9.3 | E3.9 (NEW) | proxy/obs_pipeline.py |
| 42 | PP42 — Cost attribution в proxy | T3.9.4 | E3.9 (NEW) | obs_pipeline.py, cost/ |
| 43 | PP43 — Failover / degradation | T3.9.5 | E3.9 (NEW) | proxy/server.py |
| 44 | PP44 — Shared proxy config + OryaSecurity | T3.10.1 | E3.10 (NEW) | proxy/shared_pipeline.py, security_client.py |
| 45 | PP45 — Decision-router | T3.10.2 | E3.10 (NEW) | proxy/decision_router.py |
| 46 | PP46 — Cross-system trace_id correlation | T3.10.3 | E3.10 (NEW) | proxy/audit_trail.py |
| 47 | PP47 — Performance isolation security/obs | T3.10.4 | E3.10 (NEW) | proxy/shared_pipeline.py |
| 48 | PP48 — Единый audit-trail UI | T3.10.5 | E3.10 (NEW) | UI, docs |
| 49 | PP49 — Верификация PRODUCTION-READY | VERIFY | — | scripts/verify_production_ready.py |
| 50 | PP50 — Шаблон фиксов | FIX | — | (template) |
| 51 | PP51 — Observability-of-observability | IMPROVE-1 | — | metrics.py, grafana/ |
| 52 | PP52 — Weekly chaos game-day | IMPROVE-2 | — | scripts/chaos/ |
| 53 | PP53 — Prompt-template versioning | IMPROVE-3 | — | proxy/obs_pipeline.py |
| 54 | PP54 — Semantic cache preview | IMPROVE-4 | — | proxy/semantic_cache.py |
| 55 | PP55 — Cost-aware routing preview | IMPROVE-5 | — | proxy/router.py |
| 56 | PP56 — Hot-reload security policies | IMPROVE-6 | — | infra/rbac/ |
| 57 | PP57 — Drift explainability | IMPROVE-7 | — | drift/explainability.py |
| 58 | PP58 — E2E encryption для PII | IMPROVE-8 | — | proxy/obs_pipeline.py |
| 59 | PP59 — Golden signals SLI | IMPROVE-9 | — | slo/definitions.py |
| 60 | PP60 — Observability cost budget | IMPROVE-10 | — | cost/obs_self_cost.py |

---

## Приложение B. Чек-листы для ревьюера

### B.1. Перед запуском PP0 (мастер-промпт)
- [ ] MVP Уровень 1 полностью реализован (256/256 тестов PASS, exit-criteria §4.5 ROADMAP)
- [ ] CRITICAL Уровень 2 полностью реализован (PC01–PC38 + PP0..PP38, exit-criteria §5.5 ROADMAP)
- [ ] Доступ к docker daemon для HA-инфры (PP01)
- [ ] Vault transit backend настроен (для PP58)
- [ ] OryaSecurity репозиторий склонирован (для PP44–PP48)
- [ ] Backlog обновлён: все тикеты T3.x.x переведены в IN PROGRESS

### B.2. Перед PP49 (верификация)
- [ ] Все PP01–PP48 выполнены, коммиты в git log
- [ ] Все PP51–PP60 выполнены (80/20 улучшения)
- [ ] Live-стенд docker-compose.ha.yml поднят
- [ ] Нагрузочные тесты из PP11, PP38, PP47 прогнаны
- [ ] SLO-дашборды (PP28) видны в Grafana
- [ ] Audit-trail (PP34, PP48) проверен вручную на 10+ решениях
- [ ] Cross-tenant тесты (PP32) прогнаны

### B.3. После PP49 (post-acceptance)
- [ ] Git tag v3.0.0-production-ready создан
- [ ] BACKLOG.md обновлён: все T3.x.x → DONE
- [ ] ROADMAP.md обновлён: §6.5 exit-criteria → все PASS
- [ ] ARCHITECT.md: проверена совместимость (non-contradiction §1.2)
- [ ] Промпты для Уровня 4 (Улучшения) пока не пишем — это 12 месяцев horizon
- [ ] Runbooks (PP06, PP15, PP26, PP28, PP34, PP48, PP51, PP52, PP59) собраны в docs/runbooks/

