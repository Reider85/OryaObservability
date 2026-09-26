# CRITICAL-PROMPTS.md — Промпты для ИИ-агента: выход на Уровень 2 (CRITICAL)

**Назначение:** готовая серия промптов для ИИ-кодинг-агента (Claude Code, Cursor, Codex, Aider или любой LLM-агент), которая доводит проект до **Уровня 2 CRITICAL** observability-стека для LLM-агента. Без этого уровня нельзя пускать агента на реальных пользователей.

**Источники:** `ARCHITECT.md` v1.0 (§2.3, §3.3, §3.4, §4.2, §7.3, §7.5, §8.3, §8.7, §10.4, §12.2), `ROADMAP.md` v1.0 (§5 — Уровень 2 CRITICAL, эпики E2.1–E2.8, задачи T2.x.x, exit criteria §5.5), `MVP-PROMPT.md` v1.0 (контракты SDK P0+P02..P06, формат промптов).

**Формат:** универсальные промпты без привязки к среде. Каждый промпт самодостаточен при условии, что в сессию сначала отправлен мастер-промпт **PC0**. Промпты опираются на готовый MVP-код (SDK, экспортёр, cost-трекинг, ring buffer, tail-sampler — 256/256 тестов PASS).

**Принцип non-contradiction (§1.2 ROADMAP):** ни одна задача CRITICAL не отменяет решения MVP. Контракты SDK (декораторы, context managers, BaseExporter, Span/SpanContext) сохраняются. CRITICAL добавляет новые слои (Security, Eval, Drift, Tiered Storage) и расширяет существующие (Metrics — ML-качеством; tail-sampler — адаптивностью).

---

## 0. Как пользоваться

### 0.1. Правила скармливания

1. **Каждая новая сессия** агента начинается с промпта **PC0** (мастер-контекст CRITICAL), затем отправляется **один** тикет-промпт (PC01…PC36).
2. **Одна сессия = один тикет = один коммит.** Не склеивайте несколько тикетов в одну сессию — падает качество и теряется контроль.
3. После выполнения агента **обязательно** дождитесь от него: (а) список сделанного, (б) результат запуска тестов, (в) заполненный чек-лист DoD из промпта. Нет отчёта — тикет не принят.
4. Если агент предлагает сделать больше, чем указано в промпте, — **отказывайте**: расширение scope — production-ready (Уровень 3), не CRITICAL.
5. Промпты с пометкой **[ПАРАЛЛЕЛЬ]** можно выполнять одновременно в отдельных сессиях/субагентах — они не пересекаются по файлам.
6. Промпты со скоупом MVP-файлов (agent_obs/observability.py, agent_obs/metrics.py) — строгая последовательность внутри волны: каждый следующий опирается на предыдущий.

### 0.2. Карта волн выполнения (критический путь для скорости)

| Волна | Промпты | Тикеты | Можно параллельно с | Зависимости |
|---|---|---|---|---|
| **0** | PC01 → PC02 → PC03 | T2.6.1+T2.6.2+T2.6.3, T2.3.1, T2.4.1 (инфраструктура) | Волны 1–8 (файлы не пересекаются) | MVP |
| **1a** | PC04→PC05→PC06→PC07→PC08 | T2.1.1–T2.1.5 (Guardrail Engine) | Волна 1b (eval-pipeline) | PC0 |
| **1b** | PC09→PC10→PC11→PC12→PC13 | T2.4.2–T2.4.6 (Async eval) | Волна 1a (guardrail) | PC03 |
| **2** | PC14→PC15→PC16→PC17 | T2.2.1–T2.2.4 (Field-aware PII) | после Волны 1a | PC08 (T2.1.5) |
| **3** | PC18→PC19→PC20→PC21 | T2.3.2–T2.3.5 (Vault integration) | после Волны 1a | PC02, PC08 |
| **4** | PC22→PC23 | T2.6.4, T2.6.5 (Hot/Warm/Cold migrations) | после Волны 0 | PC01 |
| **5** | PC24→PC25→PC26→PC27→PC28 | T2.5.1–T2.5.5 (Drift detector) | после Волны 4 | PC01, Phoenix self-hosted |
| **6** | PC29→PC30→PC31→PC32 | T2.7.1–T2.7.4 (Adaptive sampling) | после Волн 1a, 4 | PC01, MVP-сэмплер (P20) |
| **7** | PC33→PC34→PC35→PC36 | T2.8.1–T2.8.4 (Compliance-catalog) | после Волн 2, 3 | PC08, PC18 |
| **8** | PC37 | Верификация CRITICAL (все exit-criteria §5.5) | после всех | все тикеты |
| **9** | PC38 (шаблон) | Цикл фиксов по проваленным критериям | итеративно | — |

**Быстрая последовательность для одного агента (без параллели):**
`PC0 → PC01 → PC02 → PC03 → PC04 → PC05 → PC06 → PC07 → PC08 → PC09 → PC10 → PC11 → PC12 → PC13 → PC14 → PC15 → PC16 → PC17 → PC18 → PC19 → PC20 → PC21 → PC22 → PC23 → PC24 → PC25 → PC26 → PC27 → PC28 → PC29 → PC30 → PC31 → PC32 → PC33 → PC34 → PC35 → PC36 → PC37 → (PC38 при провалах)`

### 0.3. Стримы для параллельной команды (рекомендация §9.2 ROADMAP)

**Стрим A (Security-стек, 1 подкоманда):** PC01(infra share) → PC02 → PC04→PC08 → PC14→PC17 → PC18→PC21 → PC33→PC36
**Стрим B (Eval-стек, 1 подкоманда):** PC03 → PC09→PC13
**Стрим C (Инфра-стек, 1 подкоманда):** PC01 → PC22→PC23 → PC24→PC28 → PC29→PC32
**Завершение:** интеграция всех трёх стримов в общую систему, затем PC37.

---

## PC0 — МАСТЕР-ПРОМПТ: контекст CRITICAL-уровня

> Отправлять **первым** в каждой новой сессии агента. Все дальнейшие промпты опираются на этот контекст. Промпт расширяет P0 из `MVP-PROMPT.md` новыми слоями (Security, Eval, Drift, Tiered Storage) и новыми контрактами.

```text
РОЛЬ
Ты — senior Python-инженер, внедряешь CRITICAL-уровень observability-стека для LLM-агента.
Работаешь строго в рамках одного тикета из ROADMAP Уровня 2 (CRITICAL). Не расширяешь scope
на Уровень 3 (production-ready): adaptive eval-sampling, tree+compression, drill-down UI,
sidecar-SDK, federation, cost-aware routing — НЕ РЕАЛИЗУЕМ.

ПРОЕКТ
Пакет SDK: agent_obs (Python 3.11+, asyncio-only). MVP уже реализован: SpanContext, Span,
ObservabilitySDK с декоратором @agent_observed и context managers llm_call()/tool_call(),
LangfuseExporter с OTLP-транспортом и retry, ring buffer asyncio.Queue(maxsize=100_000)
с _export_worker (drain 50ms, batch ≤512, fan-out), _compute_cost() с price_book.yaml,
tail-sampler с фиксированной политикой (errors 100%, cost>threshold 100%, normal 10%),
content-sampling по trace_id. 256/256 тестов PASS.

Цель CRITICAL: довести observability до продакшен-критического минимума — добавить слои
Security (PII + injection), Eval/Quality (rule + LLM-judge + embedding), Drift Detection
(KL на embeddings), Tiered Storage (Hot ClickHouse 14д / Warm Postgres 90д / Cold S3+Parquet
1г). Без этого уровня нельзя пускать агента на реальных пользователей.

СТРУКТУРА РЕПОЗИТОРИЯ (расширение MVP — поддерживай её)
agent-obs/
├── agent_obs/
│   ├── __init__.py
│   ├── observability.py          # ObservabilitySDK, Span, SpanContext, _enqueue
│   ├── content_sampling.py       # детерминированный content-sampling по trace_id
│   ├── guardrail/                # NEW на CRITICAL
│   │   ├── __init__.py
│   │   ├── engine.py             # GuardrailEngine.check_input() -> GuardrailVerdict
│   │   ├── pii_detector.py       # regex + CRF (Presidio) для email/phone/INN/passport/payment
│   │   ├── injection_classifier.py  # deberta-v3-base-prompt-injection
│   │   ├── field_masker.py       # field-aware PII-маскинг (YAML-конфиг)
│   │   └── vault_client.py       # HashiCorp Vault client: store/recover с TTL+MFA
│   ├── eval/                     # NEW на CRITICAL
│   │   ├── __init__.py
│   │   ├── base.py               # BaseEvaluator (контракт)
│   │   ├── rule_based.py         # regex/JSON-schema/blacklist — синхронно
│   │   ├── llm_judge.py          # faithfulness/relevancy/completeness — async через очередь
│   │   ├── embedding.py           # cosine similarity до golden-ответа
│   │   └── late_annotation.py    # привязка eval-результата к closed trace по trace_id
│   ├── drift/                    # NEW на CRITICAL
│   │   ├── __init__.py
│   │   ├── kl_divergence.py      # KL между last-window и baseline-window
│   │   └── detector.py           # DriftDetector cron, alerts
│   ├── storage/                  # NEW на CRITICAL
│   │   ├── __init__.py
│   │   ├── hot.py                # ClickHouse client (Full span, 14д)
│   │   ├── warm.py                # Postgres (structure+metrics, 90д)
│   │   ├── cold.py                # S3+Parquet (агрегаты, 1г, через Athena)
│   │   └── migrations.py         # Hot→Warm daily, Warm→Cold weekly
│   ├── compliance/               # NEW на CRITICAL
│   │   ├── __init__.py
│   │   ├── catalog.py            # ComplianceCatalog — побочный продукт PII-маскинга
│   │   └── gdpr_export.py        # экспорт GDPR Data Map (CSV/Excel)
│   ├── exporters/                # MVP (расширяется tail_sampler policy)
│   │   ├── base.py
│   │   ├── langfuse_exporter.py
│   │   ├── otlp_mapping.py
│   │   └── stdout_exporter.py
│   ├── cost/                     # MVP
│   │   ├── compute_cost.py
│   │   └── price_book.py
│   └── metrics.py                # MVP + NEW метрики CRITICAL (см. ниже)
├── pilot_agent/agent.py
├── infra/
│   ├── docker-compose.yml        # + vault, redis, rq, clickhouse, postgres, minio, phoenix
│   ├── otel-collector/config.yaml
│   ├── prometheus.yml
│   └── prometheus-rules.yml      # NEW: SLO/drift/budget alert rules
├── infra/vault/                  # NEW: Vault policies, audit config
├── infra/eval/                   # NEW: RQ worker supervisor config
├── infra/storage/               # NEW: ClickHouse DDL, Postgres schema, S3 lifecycle
├── tests/
├── docs/
└── pyproject.toml

ТИПЫ SPAN'ОВ (на CRITICAL — те же 3 + НОВЫЕ 4 из §2.3 ARCHITECT.md)
- agent.loop, llm.call, tool.call — MVP, без изменений
- agent.reasoning — NEW (подкатегория reasoning-цикла внутри agent.loop, для late annotation эвала)
- eval.judge — NEW (LLM-as-judge span, прицепляется к trace_id через late annotation)
- guardrail.check — NEW (sync-check перед записью лога / tool-call)
- audit.event — NEW (security audit, не span, а отдельная сущность в Hot store + audit log)

КОНТРАКТЫ CRITICAL (канонические, менять нельзя)

@dataclass
class GuardrailVerdict:
    verdict: str          # "clean" | "flag" | "block"
    score: float          # 0..1 для injection; для PII — доля замаскированного текста
    masked_text: str     # уже замаскированный текст (если был PII)
    redacted_fields: list[str]  # ["user_message.email", "tool_output.rows[0].phone", ...]
    audit_event_id: str  # id записи в audit trail (всегда пишется, даже при clean)

class BaseEvaluator:
    async def evaluate(self, span: Span, full_trace: list[Span]) -> EvalResult:
        # Оценивает span (обычно llm.call) в контексте всего трейса.
        # Идемпотентен по (trace_id, eval_id).
        raise NotImplementedError

@dataclass
class EvalResult:
    trace_id: str
    eval_id: str
    eval_name: str         # "faithfulness_llm_judge", "rule_no_pii", "embedding_similarity"
    eval_version: str
    eval_timestamp: float
    eval_latency_seconds: float
    scores: dict          # {"faithfulness": 0.92, "answer_relevancy": 0.88, ...}
    judge_model: str = "" # для LLM-judge; "" для rule-based
    judge_prompt_sha256: str = ""
    reasoning: str = ""
    flags: list = field(default_factory=list)

class BaseStorage:
    async def write_spans(self, spans: list[Span]) -> None: ...
    async def get_trace(self, trace_id: str) -> list[Span]: ...
    async def write_eval_result(self, result: EvalResult) -> None: ...
    async def get_eval_results(self, trace_id: str) -> list[EvalResult]: ...

ЖЕЛЕЗНЫЕ ПРАВИЛА CRITICAL (нарушение = тикет не принят)
1. Все контракты MVP сохраняются: SDK fire-and-forget, async-only, cost в атрибутах span.
2. PII-маскинг выполняется ТОЛЬКО в процессе агента, синхронно в _enqueue(), ДО любой
   передачи (антипаттерн §8.3). Никакого «маскинга на collector'е».
3. LLM-as-judge — ТОЛЬКО асинхронно через очередь. Пользователь никогда не ждёт eval.
4. Late annotation: eval-результат прицепляется к closed trace по trace_id, не нарушая
   существующих span'ов (антипаттерн §8.2).
5. Vault recovery требует MFA. Никакого доступа к оригиналу PII без TOTP-токена.
6. Drift baseline — скользящее окно (7д минус последний час), не разовое измерение
   (антипаттерн §8.7).
7. Storage rollback возможен: если Hot/Warm/Cold миграции ломают продакшен, откатываем
   миграции, данные остаются в Hot 14 дней.
8. Compliance-catalog — побочный продукт маскинга, отдельной инвентаризации нет.
9. Не вводить Уровень 3: tree-compression, drill-down UI, sidecar, federation, adaptive
   eval-sampling (день/ночь), multi-tenant RBAC. Их обработка — отдельный уровень.
10. Код и комментарии — на английском; ответы мне — на русском.
11. После каждого тикета: прогнать pytest, сделать один коммит (conventional commits:
    feat(guardrail): ..., feat(eval): ..., feat(storage): ...), отчитаться по чек-листу DoD.

СТЕК
Python 3.11+, asyncio, dataclasses, pytest + pytest-asyncio, prometheus-client, pyyaml,
httpx. NEW: presidio-analyzer (CRF PII), transformers (deberta-v3-base-prompt-injection),
pyotp (TOTP MFA), hvac (HashiCorp Vault client), redis + rq (eval queue), clickhouse-driver,
psycopg, boto3 (S3), pyarrow (Parquet), arize-phoenix-client (drift UMAP). Langfuse
self-hosted (расширение MVP-стека в docker-compose).
```

---

## ВОЛНА 0: Инфраструктура CRITICAL

> Три инфра-промпта, разворачивают новые сервисы в docker-compose. [ПАРАЛЛЕЛЬ] с Волнами 1–8
> (файлы не пересекаются с agent_obs/*). Старт: после MVP-инфраструктуры (P01 из MVP-PROMPT.md).

### PC01 — [T2.6.1+T2.6.2+T2.6.3] Инфраструктура tiered storage: ClickHouse + Postgres + S3 [ПАРАЛЛЕЛЬ]

> PC0. Запускать можно параллельно с Волнами 1–8 — файлы в infra/storage/.

```text
ЗАДАЧА (тикеты T2.6.1, T2.6.2, T2.6.3)
Развернуть локальный tiered storage в docker-compose: Hot (ClickHouse), Warm (Postgres), Cold (S3 + Parquet через MinIO).

ЧТО СДЕЛАТЬ
1. infra/docker-compose.yml — добавить сервисы (не ломая MVP langfuse/postgres/redis):
   - clickhouse/clickhouse-server:24 (порт 8123 HTTP, 9000 TCP; volume, healthcheck)
     с шардированием по agent_id, tenant_id (preset в /etc/clickhouse-server/config.d/)
   - postgres:17 — отдельный инстанс от MVP (новая БД warm_store; volume, healthcheck)
     или расширение существующего langfuse-postgres новой БД (решение обосновать в README)
   - minio/minio:latest (S3-совместимый, порты 9000 API / 9001 console; buckets:
     cold-traces, audit-events; lifecycle policy 1 год)
2. infra/storage/clickhouse_ddl.sql:
   - таблица spans_hot: trace_id String, span_id String, parent_span_id String,
     agent_id String, tenant_id String, name String, span_type String,
     start_time DateTime64(3), end_time DateTime64(3), status String,
     attributes JSON, events JSON, cost_usd Float64,
     response_embedding Array(Float32) NULL — для drift (см. PC24)
     ORDER BY (tenant_id, agent_id, start_time), PARTITION BY toYYYYMMDD(start_time),
     TTL start_time + INTERVAL 14 DAY
   - таблица eval_results_hot: trace_id, eval_id, eval_name, eval_timestamp, scores JSON
   - таблица audit_events_hot: audit_id, timestamp, trace_id, actor JSON, action, decision
3. infra/storage/postgres_warm.sql:
   - таблица traces_warm (trace_id, tenant_id, agent_id, start_time, end_time, status,
     cost_usd_total, span_count, error_count, eval_avg JSON)
   - таблица compliance_catalog (id, agent_id, tool, field, pii_type, frequency, last_seen)
4. infra/storage/s3_lifecycle.json: правило 1 год для cold-traces, 90 дней для audit-events.
5. README-раздел в infra/storage/: как инициализировать (DDL загружается при первом `up`),
   как проверить connectivity, как загрузить тестовые данные в каждый тир.

DoD
- [ ] docker compose up -d поднимает 3 новых сервиса без ошибок
- [ ] ClickHouse отвечает на :8123, таблицы созданы (SELECT * FROM system.tables WHERE database='observability')
- [ ] Postgres warm_store доступен, таблицы созданы
- [ ] MinIO отвечает на :9001 console, buckets созданы, lifecycle применён
- [ ] .env.example содержит все новые переменные (CLICKHOUSE_HOST, WARM_PG_DSN, S3_ENDPOINT, ...)
Коммит: feat(infra): add tiered storage stack — clickhouse hot, postgres warm, minio cold
```

---

### PC02 — [T2.3.1] HashiCorp Vault с auto-unseal [ПАРАЛЛЕЛЬ]

> PC0. Запускать можно параллельно с Wave 0/1. Файлы в infra/vault/.

```text
ЗАДАЧА (тикет T2.3.1)
Развернуть HashiCorp Vault для хранения mapping'а mask → оригинал PII с TTL 24 часа.

ЧТО СДЕЛАТЬ
1. infra/docker-compose.yml — добавить сервис:
   - hashicorp/vault:1.18 (dev-режим на старте, переход на file storage для prod-like)
   - порт 8200 (API), volume для /vault/data (file storage), cap_add IPC_LOCK
2. infra/vault/vault.json — конфиг:
   - storage "file" { path = "/vault/data" }
   - listener "tcp" { tls_disable = 1 } (на dev/staging; production — за mTLS)
   - default_lease_ttl = "24h", max_lease_ttl = "24h"
3. infra/vault/policies/pii-recovery.hcl:
   - path "secret/data/pii/*" { capabilities = ["read"] } — только чтение
   - path "secret/metadata/pii/*" { capabilities = ["delete"] } — для cron-cleanup
4. infra/vault/policies/audit.hcl:
   - path "sys/audit-hash/*" { capabilities = ["update"] } — для audit-логов
5. Включить audit backend: vault audit enable file file_path=/vault/audit/audit.log
6. README: как инициализировать vault (vault operator init), как распломбировать
   (vault operator unseal), как создать токен для SDK (vault token create -policy=pii-recovery).
7. .env.example: VAULT_ADDR, VAULT_TOKEN, VAULT_NAMESPACE (для cloud), VAULT_TLS_CA.

DoD
- [ ] Vault поднят, отвечает на :8200 v1/sys/health
- [ ] Auto-unseal настроен (на dev — manual 5-key-share, документировано как prod-migrate)
- [ ] Policy pii-recovery применена, токен для SDK создан и в .env.example (НЕ в git!)
- [ ] Audit backend включён, лог пишется в /vault/audit/audit.log
Коммит: feat(infra): add hashicorp vault with pii-recovery policy and audit backend
```

---

### PC03 — [T2.4.1] Redis + RQ workers для async eval [ПАРАЛЛЕЛЬ]

> PC0. Запускать можно параллельно. Файлы в infra/eval/.

```text
ЗАДАЧА (тикет T2.4.1)
Развёртывание очереди для async eval-jobs (LLM-as-judge) — отдельно от процесса агента.

ЧТО СДЕЛАТЬ
1. infra/docker-compose.yml — добавить:
   - redis:7 (новый инстанс, не MVP-шный, или расширить MVP-redis с новой DB — обосновать)
     порт 6379, volume, healthcheck (redis-cli ping)
   - eval-worker (образ python:3.11-slim + rq + agent_obs + зависимости); scale=2;
     command: rq worker eval-queue --url redis://eval-redis:6379
2. infra/eval/worker.Dockerfile: pip install rq httpx pyyaml prometheus-client
   presidio-analyzer transformers pyotp hvac clickhouse-driver psycopg boto3 pyarrow
   + копия agent_obs/ в образ (или общая volume с агентом — на dev, для prod — wheel)
3. infra/eval/supervisord.conf: restart_policy on-failure, max 3, log в stdout для docker logs
4. .env.example: EVAL_REDIS_URL, EVAL_QUEUE_NAME=eval-queue, EVAL_WORKER_CONCURRENCY=4,
   LLM_JUDGE_MODEL=gpt-4o-mini, LLM_JUDGE_API_KEY (от OpenAI), EMBEDDING_MODEL=text-embedding-3-small
5. README в infra/eval/: как запустить workers вручную (rq worker ...), как посмотреть очередь
   (rq info), как отладить зависший job (rq cancel <job_id>).

DoD
- [ ] docker compose up eval-worker создаёт 2 реплики, обе видят redis и отвечают ping
- [ ] Очередь eval-queue доступна, можно enqueue тестовый job и увидеть выполнение
- [ ] .env.example содержит все переменные окружения для worker
Коммит: feat(infra): add redis queue and rq workers for async llm-judge eval
```

---

## ВОЛНА 1a: Guardrail Engine (PII + injection)

> Эпик E2.1. Это критическая точка безопасности: guardrail работает синхронно в процессе
> агента (антипаттерн §8.3 — маскинг обязан быть до любой передачи). Промпты PC04–PC08
> выполняются строго последовательно. [ПАРАЛЛЕЛЬ] с Волнами 1b (eval-pipeline), 0 (инфра).

### PC04 — [T2.1.1] PIIDetector: regex для email/phone/INN/passport/payment

> PC0 + PC02 (Vault поднят, для записи masks). Первая задача guardrail-стека.

```text
ЗАДАЧА (тикет T2.1.1)
Локальный PII-детектор для российских и международных PII-типов.

ЧТО СДЕЛАТЬ
1. agent_obs/guardrail/pii_detector.py:
   - класс PIIDetector с методами detect(text) -> list[PIIMatch]
   - PIIMatch = dataclass(entity_type, value, span_start, span_end, mask)
   - mask формат: "[EMAIL:5f3a]" — тип + первые 4 hex от sha256(value)
2. Реализовать regex-детекторы для:
   - EMAIL: стандартный RFC-подобный regex
   - PHONE: +7 (XXX) XXX-XX-XX, 8XXXXXXXXXX, +1 XXX-XXX-XXXX (мульти-формат)
   - INN: 10/12-значный с контрольной суммой (для юридических/физических лиц)
   - PASSPORT: серия+номер (4 цифры пробел 6 цифр), либо новый ID-карта формат
   - PAYMENT: PAN кредитки (13–19 цифр), с Luhn-check; CVV не детектить (3-значный слишком
     ложноположительный на числа)
3. Конфиг-флаги включения/выключения каждого детектора через env
   (AGENT_OBS_PII_DETECTORS="email,phone,inn,passport,payment").
4. Тесты tests/test_pii_detector.py: каждый тип — recall > 95% на тестовом датасете
   (≥ 100 сущностей каждого типа), false positive rate < 1% на neutral text (литература).

DoD
- [ ] 5 PII-типов детектируются, masks соответствуют формату [TYPE:hex4]
- [ ] INN и PAN валидируются контрольной суммой (Luhn для PAN, ИНН-контроль)
- [ ] pytest зелёный, recall > 95%, FPR < 1%
Коммит: feat(guardrail): add pii detector with regex for email/phone/inn/passport/payment
```

---

### PC05 — [T2.1.2] CRF-модель (Presidio) для медицинского PII

> PC0 + PC04. Расширяет PIIDetector ML-составляющей.

```text
ЗАДАЧА (тикет T2.1.2)
ML-детектор для PII, не ловящегося regex (ФИО, медицинские диагнозы, адреса).

ЧТО СДЕЛАТЬ
1. Добавить presidio-analyzer в pyproject.toml (предпочтительно) или nlp_russian (если
   Presidio плохо работает на русском — обосновать выбор).
2. В PIIDetector добавить ML-стадию:
   - import presidio_analyzer, nlp_engine = SpacyNlpEngine(model="ru_core_news_sm")
   - recognizers: RuPersonRecognizer, RuAddressRecognizer, RuMedicalConditionRecognizer
     (либо кастомные через patterns + deny_list)
   - processor: SpacyRecognizer
3. Агрегация результатов: regex + ML → единый list[PIIMatch] с приоритетом regex
   (regex точнее для email/phone, ML — для ФИО и адресов).
4. Кеширование модели: загрузить SpacyNlpEngine один раз при старте SDK, не на каждый вызов
   (lazy-init в singleton).
5. Тесты: ML-детектор находит ФИО (Anna Ivanova, Ivan Ivanov) с recall > 90%, не
   срабатывает на "Москва", "Россия" (geo-имена), не падает на нейтральном тексте.

DoD
- [ ] Presidio (или аналог) интегрирован, модель ru_core_news_sm загружается лениво
- [ ] ФИО/адреса/диагнозы детектируются, masks — по тому же формату [TYPE:hex4]
- [ ] pytest зелёный, recall > 90% на ФИО, FPR < 5% на нейтральном
Коммит: feat(guardrail): integrate presidio crf model for medical and person-name pii
```

---

### PC06 — [T2.1.3] Deberta-v3 prompt-injection классификатор

> PC0 + PC04/PC05. Файл injection_classifier.py — отдельный от PII.

```text
ЗАДАЧА (тикет T2.1.3)
Локальный классификатор промпт-инъекций на базе DeBERTa-v3.

ЧТО СДЕЛАТЬ
1. Добавить transformers в pyproject.toml (+ torch или onnxruntime — обосновать выбор
   в README; onnxruntime быстрее в инференсе, но требует конвертации модели).
2. agent_obs/guardrail/injection_classifier.py:
   - класс InjectionClassifier с методом classify(text) -> InjectionScore
   - InjectionScore = dataclass(score: float 0..1, label: str "benign|suspicious|injection",
     confidence: float)
   - модель: deepset/deberta-v3-base-prompt-injection (HuggingFace Hub), lazy-init singleton
3. Пороги (конфиг через env AGENT_OBS_INJECTION_*):
   - score < 0.5 → benign (пропуск)
   - 0.5 <= score < 0.85 → suspicious (flag + продолжение выполнения)
   - score >= 0.85 → injection (block + audit-event)
4. Кеширование модели: загрузка при первом вызове, не на старте SDK (холодный старт
   может быть медленным — задокументировать в README, что первый запрос с инъекцией
   будет медленнее).
5. Fallback: если модель недоступна (нет интернета, нет весов) — лог warning, score=0.0
   (fail-open, не fail-closed, чтобы не блокировать продакшен при сбое ML-инфры).
6. Тесты: на датасете prompt-injection-attacks (≥ 200 примеров) — F1 > 0.90, recall на
   injection > 0.95, FPR на benign < 2%.

DoD
- [ ] InjectionClassifier работает, score в диапазоне [0,1]
- [ ] Пороги применяются согласно конфигу, при score >= 0.85 возвращается label=injection
- [ ] Fallback на fail-open, модель lazy-init
- [ ] pytest зелёный, F1 > 0.90 на тестовом датасете
Коммит: feat(guardrail): add deberta-v3 prompt injection classifier with fail-open fallback
```

---

### PC07 — [T2.1.4] GuardrailEngine.check_input() -> GuardrailVerdict

> PC0 + PC04..PC06. Склейка PII + injection в единый engine.

```text
ЗАДАЧА (тикет T2.1.4)
Единый движок политик, объединяющий PII-маскинг и injection-классификацию в один вызов.

ЧТО СДЕЛАТЬ
1. agent_obs/guardrail/engine.py:
   - класс GuardrailEngine(pii_detector: PIIDetector, injection_classifier: InjectionClassifier,
     vault_client: VaultClient, config: GuardrailConfig)
   - async def check_input(self, text: str, *, context: SpanContext, field: str = "user_message")
     -> GuardrailVerdict
2. Логика check_input (полностью sync-часть — ML-инференс тоже sync, но быстрый < 5 мс на CPU):
   a. PII-детекция → masked_text с заменой каждой сущности на mask
   b. Vault: для каждого mask — vault_client.store(mask, original=value, ttl=24h),
      возвращается vault_key (если vault недоступен — fail-closed: mask всё равно ставится,
      recovery невозможен, лог warning + metric vault_unavailable_total++)
   c. Injection-классификация на исходном text (НЕ на masked — инъекция может быть в самом
      запросе, не в PII-части)
   d. Формирование verdict:
      - если injection.score >= 0.85 → verdict="block", masked_text возвращается, audit-event
        с decision="block", reason="injection_score_N"
      - если 0.5 <= injection.score < 0.85 → verdict="flag", masked_text возвращается,
        выполнение продолжается, audit-event decision="flag"
      - если найден PII (любой) и injection.score < 0.5 → verdict="clean" (с masking),
        audit-event decision="allow" с reason="pii_masked_N_entities"
      - если ничего не найдено → verdict="clean", audit-event decision="allow" reason="no_pii_injection"
3. Audit-event всегда пишется (даже при clean) в audit trail — это §3.4 требование (1 год retention).
4. Метрики: guardrail_check_total{decision}, guardrail_check_duration_seconds (histogram),
   pii_entities_detected_total{entity_type}, injection_score_distribution (histogram).
5. Контракт GuardrailVerdict — из PC0; redacted_fields заполняется в PC14 (field-aware).
6. Тесты: каждый сценарий (clean/flag/block/injection+pii); audit-event всегда есть;
   метрики корректны; vault недоступен → fail-closed с маскингом, но без recovery.

DoD
- [ ] GuardrailEngine.check_input() возвращает GuardrailVerdict по контракту PC0
- [ ] Все 4 сценария (clean/flag/block/injection+pii) покрыты тестами
- [ ] Audit-event пишется в каждом вызове (включая clean)
- [ ] Latency p99 < 5 мс (PC11-нагрузочный тест для MVP использовать как референс)
- [ ] pytest зелёный
Коммит: feat(guardrail): add GuardrailEngine combining pii masking and injection detection
```

---

### PC08 — [T2.1.5] Встроить guardrail в _enqueue() SDK синхронно

> PC0 + PC07. КРИТИЧЕСКОЕ место: anti-pattern §8.3 — маскинг обязан быть в процессе агента.

```text
ЗАДАЧА (тикет T2.1.5)
Встроить guardrail в критический путь SDK: каждый span перед попаданием в ring buffer
проходит PII-маскинг. Это устраняет окно между проверкой и записью, где PII может утечь
в логи контейнера (антипаттерн §8.3).

ЧТО СДЕЛАТЬ
1. В ObservabilitySDK.__init__ добавить guardrail: Optional[GuardrailEngine] = None
   (если None — guardrail отключён, например на dev-окружении с синтетическими данными;
   флаг AGENT_OBS_GUARDRAIL_ENABLED=true/false).
2. В _enqueue(span) — ПЕРЕД put_nowait в ring buffer:
   a. Если guardrail включён и span имеет текстовые атрибуты
      (llm.input_text, llm.output_text, tool.input_summary, tool.output_text):
      - вызов guardrail.check_input(text, context=span.context, field=<имя_поля>)
        для каждого текстового атрибута
      - заменяем span.attributes[field] на masked_text
      - добавляем в span.attributes["pii.redacted_fields"] список masks
      - audit_event_id из verdict кладём в span.events (как событие guardrail.check)
   b. Если guardrail вернул verdict="block" — span всё равно записывается (для дебага),
      но в span.attributes["guardrail.block"] = true, а вызывающий агент получает
      GuardrailBlockException (на уровне декоратора/менеджера — НЕ в _enqueue,
      а в самом llm_call контексте, до вызова LLM)
3. Hook в llm_call context manager: ПЕРЕД yield (т.е. до вызова LLM) — вызвать
   guardrail.check_input на user_message; если verdict="block" — выбросить
   GuardrailBlockException, span закрыть со статусом "blocked", не вызывать LLM.
4. Hook в tool_call context manager: ПЕРЕД yield — guardrail.check_input на tool input
   (если текстовый); verdict="block" → tool не вызывается, span статус "blocked".
5. Метрики: guardrail_block_total{stage=input|tool|output}, guardrail_check_in_enqueue_total.
6. ВАЖНО: маскинг в _enqueue — последний барьир, не полагаться на него одного. Главный
   барьир — hooks в llm_call/tool_call, до сетевого вызова. _enqueue маскирует на случай,
   если атрибуты были выставлены напрямую (например, из tool-output, который ещё не прошёл
   guardrail — это PC14).
7. Тесты:
   - span с llm.input_text="my email is ivan@example.com" → в ring buffer попадает с
     [EMAIL:hex4], оригинал не виден ни в span, ни в логах (capture log assertions)
   - llm_call с verdict="block" → LLM не вызывается, span статус "blocked"
   - guardrail disabled (env=false) → span проходит как есть, метрика guardrail_check_in_enqueue_total не растёт

DoD
- [ ] Каждый span в ring buffer имеет PII-маскинг на текстовых атрибутах (если guardrail включён)
- [ ] GuardrailBlockException выбрасывается до сетевого вызова в llm_call/tool_call
- [ ] На dev-окружении (AGENT_OBS_GUARDRAIL_ENABLED=false) span'ы проходят без маскинга
- [ ] pytest зелёный, в т.ч. assert no plaintext PII in captured logs
Коммит: feat(agent_obs): embed guardrail check in enqueue and llm_call/tool_call hooks
```

---

## ВОЛНА 1b: Async eval-pipeline с late annotation

> Эпик E2.4 (задачи T2.4.2–T2.4.6; T2.4.1 инфра-задача — в PC03). [ПАРАЛЛЕЛЬ] с Волнами 1a
> (guardrail), 0 (инфра). Старт: после PC03 (Redis+RQ поднят).
> Принцип 9 (предварительное противопоставление) + Принцип 24 (посредник) — precompute
> embeddings офлайн, асинхронный eval-worker через очередь. Пользователь получает ответ сразу,
> эвал прицепляется через 5–30 секунд (§7.3 ARCHITECT.md).

### PC09 — [T2.4.2] RuleBasedEvaluator (regex/JSON-schema/blacklist) [ПАРАЛЛЕЛЬ с 1a]

> PC0 + PC03 (Redis+RQ поднят). Первая задача eval-стека.

```text
ЗАДАЧА (тикет T2.4.2)
Синхронный rule-based эвалюатор — дёшево, мгновенно, низкий recall (по §3.3 ARCHITECT.md).

ЧТО СДЕЛАТЬ
1. agent_obs/eval/base.py:
   - ABC BaseEvaluator с методом evaluate(span: Span, full_trace: list[Span]) -> EvalResult
   - контракт EvalResult из PC0
2. agent_obs/eval/rule_based.py:
   - класс RuleBasedEvaluator(rules: list[Rule]) — синхронный (не async)
   - Rule = dataclass(name, field, rule_type, params) — rule_type: "regex" | "json_schema" | "blacklist"
3. Стандартные правила (конфиг в configs/eval_rules.yaml):
   - "no_pii_in_output" — regex на PII-паттерны в llm.output_text; score=1.0 если PII найден
     (это означает, что guardrail что-то пропустил — critical alert)
   - "json_output_valid" — JSON-schema валидация tool.output_text, если tool возвращает JSON
   - "no_blacklisted_words" — blacklist запрещённых слов (например, для контент-модерации)
   - "max_response_chars" — длина ответа не больше N (защита от зацикливания)
4. EvalResult: scores={"rules_passed": N, "rules_failed": M, "rule_results": {...}},
   flags=["pii_leak" если no_pii_in_output провален]
5. Интеграция: rule-based вызывается синхронно в составе span'а (до _enqueue или внутри
   llm_call после получения ответа). Результат — в span.attributes["eval.rule_based"].
6. Тесты: каждое правило — pass/fail case; no_pii_in_output ловит пропущенный email;
   json_output_valid ловит невалидный JSON; blacklist — match.

DoD
- [ ] RuleBasedEvaluator работает, 4 стандартных правила реализованы
- [ ] EvalResult соответствует контракту, scores и flags заполняются
- [ ] pytest зелёный
Коммит: feat(eval): add rule-based evaluator with regex, json-schema, and blacklist rules
```

---

### PC10 — [T2.4.3] LLMJudgeEvaluator (faithfulness/relevancy/completeness)

> PC0 + PC03 + PC09. Главный компонент async-eval — LLM-as-judge.

```text
ЗАДАЧА (тикет T2.4.3)
Асинхронный LLM-as-judge эвалюатор — дорогой, медленный, высокий recall (§3.3 ARCHITECT.md).

ЧТО СДЕЛАТЬ
1. agent_obs/eval/llm_judge.py:
   - класс LLMJudgeEvaluator(client: httpx.AsyncClient, model: str, prompts: dict[str, str])
   - async def evaluate(span, full_trace) -> EvalResult
2. Промпты для трёх канонических метрик (RAGAS-style, см. §3.3):
   - faithfulness — ответ опирается на предоставленный контекст (не галлюцинирует)
   - answer_relevancy — ответ релевантен вопросу пользователя
   - completeness — ответ покрывает все аспекты вопроса
3. Каждый промпт возвращает JSON: {"score": 0..1, "reasoning": "...", "flags": []}
   (использовать gpt-4o-mini с response_format={"type": "json_object"} для надёжности)
4. Сэмплирование (10% по умолчанию, env AGENT_OBS_LLM_JUDGE_RATE=0.1): если random.random()
   > rate → возвращать EvalResult с eval_name="llm_judge_skipped", scores={}
5. ВАЖНО: evaluate НЕ делает сетевой вызов внутри — она кладёт job в Redis-очередь
   (через rq.enqueue) и возвращает заглушку EvalResult с eval_name="llm_judge_pending",
   eval_timestamp=now, scores={}, flags=["pending"]. Реальный результат придёт через
   late annotation (см. PC12).
6. Хеширование промпта: judge_prompt_sha256 = sha256(rendered_prompt).encode() —
   для аудита (какой именно промпт был использован).
7. Тесты: evaluate кладёт job в очередь (мок rq), возвращает pending-результат; нет
   сетевого вызова LLM внутри evaluate (assert not httpx.AsyncClient.post called).

DoD
- [ ] LLMJudgeEvaluator кладёт job в rq-очередь, возвращает pending-результат
- [ ] Промпты для 3 метрик реализованы, sha256 промпта считается
- [ ] Сэмплирование 10% работает (по env)
- [ ] pytest зелёный, без сетевых вызовов
Коммит: feat(eval): add llm-judge evaluator with faithfulness, relevancy, completeness
```

---

### PC11 — [T2.4.4] EmbeddingEvaluator (cosine similarity до golden-ответа)

> PC0 + PC10. Быстрый путь eval — embedding similarity (синхронно или быстрый async).

```text
ЗАДАЧА (тикет T2.4.4)
Быстрый embedding-based эвалюатор — cosine similarity до golden-ответа или centroid'а
нормальных ответов (§3.3 ARCHITECT.md, тип 3).

ЧТО СДЕЛАТЬ
1. agent_obs/eval/embedding.py:
   - класс EmbeddingEvaluator(client: httpx.AsyncClient, model: str,
     golden_store: GoldenStore)
   - async def evaluate(span, full_trace) -> EvalResult
2. Golden store: в configs/golden_answers.yaml — список (agent_id, intent, golden_answer).
   Precompute embeddings на старте (singleton при первом вызове evaluate).
3. Логика:
   a. Найти golden-ответ по intent (intent извлекается из span.attributes["agent.intent"]
      или выводится из user_message — на CRITICAL оставляем заглушкой "default")
   b. Получить embedding ответа агента (text-embedding-3-small, async через OpenAI API)
   c. Cosine similarity между ответом и golden-ответом
   d. EvalResult: scores={"embedding_similarity": 0..1}, flags=["low_similarity" if < 0.7]
4. Можно делать синхронно (быстрый LLM-API call, ~100 мс) или через очередь — на CRITICAL
   выбрать sync (быстрый путь, не блокирует пользователя в фоне). Решение обосновать.
5. Сэмплирование: 100% на CRITICAL (быстро и дёшево); на Уровне 3 — adaptive.
6. Тесты: golden-ответ из 3 примеров; cosine similarity высокой (> 0.85) для семантически
   близкого ответа, низкой (< 0.5) для нерелевантного; mок httpx для embeddings API.

DoD
- [ ] EmbeddingEvaluator возвращает EvalResult с embedding_similarity score
- [ ] Golden-ответы precomput'ятся лениво, не на каждый вызов
- [ ] pytest зелёный, similarity корректна на эталонных кейсах
Коммит: feat(eval): add embedding evaluator with cosine similarity to golden answers
```

---

### PC12 — [T2.4.5] Late annotation: привязка eval-результата к closed trace по trace_id

> PC0 + PC01 (ClickHouse Hot store) + PC10 (LLM-judge). КРИТИЧЕСКИЙ механизм — §7.3 ARCHITECT.md.

```text
ЗАДАЧА (тикет T2.4.5)
Late annotation: после завершения eval-джобы (LLM-as-judge, 5–30 сек после трейса),
результат пишется в Hot store (ClickHouse eval_results_hot) с ключом trace_id. Это
разрешает ТП-2 (§10.4): пользователь не ждёт eval, эвал прицепляется потом.

ЧТО СДЕЛАТЬ
1. agent_obs/eval/late_annotation.py:
   - async def annotate(eval_result: EvalResult, hot_store: HotStore) -> None
   - пишет в ClickHouse eval_results_hot (см. PC01 DDL)
   - идемпотентен по (trace_id, eval_id) — повторная запись не дублирует (ReplacingMergeTree
     или UNIQUE KEY в DDL)
2. RQ worker (см. PC03) обрабатывает job:
   a. Достаёт trace_id, span_id, prompt, model из job payload
   b. Вызывает LLM-judge (gpt-4o-mini) с этим промптом
   c. Формирует EvalResult (scores, reasoning, judge_model, judge_prompt_sha256)
   d. Вызывает annotate() — пишется в Hot store
   e. Метрика eval_jobs_completed_total, eval_job_latency_seconds (histogram)
3. На стороне SDK: span.attributes["eval.pending"] = true при отправке в очередь;
   late annotation НЕ модифицирует span (он уже закрыт и в ring buffer/exporter) —
   eval-результат живёт отдельно в ClickHouse, привязан к trace_id.
4. Query API (см. PC13): GET /traces/{trace_id}/evals возвращает все eval-результаты
   по trace_id из Hot store.
5. Fallback: если ClickHouse недоступен — eval-результат кладётся в Redis с TTL 1 час
   для повторной записи; метрика eval_annotation_redis_fallback_total.
6. Тесты: LLM-judge job → annotate → запись в ClickHouse (mock или testcontainers);
   повторная запись с тем же eval_id не дублирует; Redis fallback при ClickHouse down.

DoD
- [ ] RQ worker обрабатывает LLM-judge job и пишет в ClickHouse по trace_id
- [ ] Идемпотентность по (trace_id, eval_id) — повторная запись не дублирует
- [ ] Redis fallback при недоступном ClickHouse
- [ ] pytest зелёный, метрики корректны
Коммит: feat(eval): add late annotation worker writing eval results to hot store by trace_id
```

---

### PC13 — [T2.4.6] UI: eval-панель в trace-viewer с pending-состоянием

> PC0 + PC12. Использует Langfuse UI (или Phoenix) — без кастомного UI (Principle 5, MVP-стиль).

```text
ЗАДАЧА (тикет T2.4.6)
Показать eval-результаты в trace-viewer'е Langfuse (или Phoenix). Если эвал ещё не пришёл —
показать «pending» с таймстампом ожидания.

ЧТО СДЕЛАТЬ
1. Решение по UI (обосновать в README):
   a. ВАРИАНТ A — расширить Langfuse UI (через метаданные трейса и saved queries)
   b. ВАРИАНТ B — использовать Phoenix (Arize) self-hosted, его trace-viewer лучше для
      ML-метрик (включая eval scores)
   На CRITICAL рекомендуется ВАРИАНТ B (Phoenix — это зависимость из ROADMAP §5.3 для E2.5
   drift detector; используем тот же Phoenix для eval UI). Решение зафиксировать.
2. agent_obs/exporters/otlp_mapping.py — расширить маппинг:
   - span.attributes["eval.pending"] → metadata в Phoenix
   - span.attributes["eval.rule_based"] → annotations в Phoenix trace-viewer
3. Query API (если Phoenix не покрывает): GET /traces/{trace_id}/evals — простой FastAPI
   endpoint, читает из ClickHouse eval_results_hot (см. PC01 DDL). На CRITICAL — минимальный,
   без аутентификации (Уровень 3 — RBAC).
4. Phoenix UMAP-визуализатор интегрируется в PC28 (drift). На этом шаге — только trace-viewer
   с eval scores в панели.
5. UI check (manual): открыть Phoenix trace-viewer на тестовом трейсе — видеть:
   - дерево span'ов
   - панель "Eval Results": faithfulness=0.92, answer_relevancy=0.88, completeness=0.85
   - если эвал ещё pending — бейдж "PENDING" с таймстампом очереди
6. Тесты: интеграционный — отправить span → дождаться eval job → запросить /evals →
   увидеть результат. Если Phoenix self-hosted недоступен на CI — мок + skip-if-not-present.

DoD
- [ ] В Phoenix (или Langfuse) trace-viewer видны eval scores для трейса
- [ ] Pending-состояние отображается, если эвал ещё не завершён
- [ ] pytest зелёный (с skip-if-no-phoenix)
- [ ] Скриншот в docs/ — пример trace-viewer с eval панелью
Коммит: feat(eval): expose eval results in trace-viewer with pending state
```

---

## ВОЛНА 2: Field-aware PII masking

> Эпик E2.2. Старт: после PC08 (Guardrail встроен в _enqueue). Принцип 3 (местное качество):
> разные части span'а (system_prompt vs user_message vs tool_output) — разные политики
> маскинга. Без этого debuggability падает ниже 90% при той же privacy.

### PC14 — [T2.2.1] FieldMasker с конфигом по типам полей (YAML)

> PC0 + PC08 (guardrail встроен в _enqueue).

```text
ЗАДАЧА (тикет T2.2.1)
Field-aware PII masking: разные части span'а — разные политики маскинга (Принцип 3, §10.4
разрешение ТП-3: privacy сохранён, debuggability 90%).

ЧТО СДЕЛАТЬ
1. configs/field_masking.yaml — конфиг политик по типам полей:
   - system_prompt: policy=no_mask (там нет пользовательского PII по архитектуре)
   - user_message: policy=full_mask (все PII маскируются)
   - tool_output: policy=partial_mask (маскируются только PII-поля из ответа БД/REST)
   - llm_output_text: policy=full_mask (LLM может "вытащить" PII из контекста)
   - tool_input_summary: policy=hash_only (только sha256 + chars, см. P22 MVP)
2. agent_obs/guardrail/field_masker.py:
   - класс FieldMasker(config: dict)
   - метод apply(field_name: str, text: str, pii_matches: list[PIIMatch]) -> tuple[str, list[str]]
     возвращает (masked_text, redacted_fields_list)
3. Поле "redacted_fields" — список в формате "field_name.entity_type.hex4"
   (например, "user_message.email.5f3a", "tool_output.rows[0].phone.abc1")
4. Интеграция с GuardrailEngine: check_input теперь принимает field_name и применяет
   FieldMasker поверх PIIDetector'а — для system_fieldPIIDetector не вызывается вовсе
   (политика no_mask).
5. Тесты: 4 политики покрыты; redacted_fields заполняется корректно; system_prompt
   НЕ маскируется (даже если там найден email — это архитектурное решение).

DoD
- [ ] FieldMasker работает с 4 политиками по YAML-конфигу
- [ ] redacted_fields содержит список в формате field.entity.hex
- [ ] system_prompt не маскируется (policy=no_mask)
- [ ] pytest зелёный
Коммит: feat(guardrail): add field-aware masker with yaml-configured policies per field type
```

---

### PC15 — [T2.2.2] Парсинг tool_output: SQL-колонки и REST JSON-path

> PC0 + PC14. Реализация partial_mask для tool_output.

```text
ЗАДАЧА (тикет T2.2.2)
Для tool_output — маскировать только PII-колонки/поля, не весь ответ.

ЧТО СДЕЛАТЬ
1. Расширить FieldMasker для tool_output:
   a. Если tool_output — SQL-ответ (детекция по tool.name или формату JSON): распарсить
      schema-aware — например, SELECT * FROM users возвращает {rows: [{id:1, email:"...",
      phone:"..."}]}. Маскируются только поля с именами email/phone/inn/passport (по словарю
      PII-колонок в configs/pii_columns.yaml).
   b. Если REST-ответ (JSON): пройтись по JSON-path'ам, маскировать только пути,
      содержащие PII-имена (например, $.user.email, $.customers[*].phone).
   c. Если неструктурированный текст — fallback на full_mask (как user_message).
2. Сохранение masks в redacted_fields: "tool_output.rows[0].email.5f3a",
   "tool_output.rows[1].phone.abc1" — с индексами и JSON-path.
3. конфиг configs/pii_columns.yaml — словарь синонимов PII-колонок:
   email: [email, e_mail, mail, contact_email]
   phone: [phone, tel, mobile, contact_phone]
   inn: [inn, taxpayer_id, tin]
   passport: [passport, passport_series, passport_number, id_document]
4. Тесты: SQL-ответ с 3 PII-колонками — маскируются 3 поля, остальное видно; REST-ответ
   с nested JSON — маскируются только PII-path; неструктурированный текст — fallback full_mask.

DoD
- [ ] SQL tool_output: маскируются только PII-колонки по словарю синонимов
- [ ] REST JSON tool_output: маскируются только PII-paths, остальное видно
- [ ] Неструктурированный tool_output: fallback на full_mask
- [ ] pytest зелёный
Коммит: feat(guardrail): parse sql and rest tool_output for partial pii field masking
```

---

### PC16 — [T2.2.3] Атрибут pii.redacted_fields на span llm.call

> PC0 + PC14/PC15. Финальная склейка в span-атрибут.

```text
ЗАДАЧА (тикет T2.2.3)
Сохранять список замаскированных полей в атрибуте pii.redacted_fields span'а llm.call
и tool.call — для последующего дебага и compliance-каталога (см. PC33).

ЧТО СДЕЛАТЬ
1. В _enqueue() SDK (см. PC08) после работы guardrail:
   - если GuardrailVerdict.redacted_fields непустой:
     span.attributes["pii.redacted_fields"] = [f.full_path for f in verdict.redacted_fields]
     span.attributes["pii.total_entities"] = len(verdict.redacted_fields)
     span.attributes["pii.entity_types"] = list({f.entity_type for f in verdict.redacted_fields})
   - span.events.append({"name": "guardrail.check", "audit_event_id": verdict.audit_event_id,
     "verdict": verdict.verdict, "score": verdict.score, "ts": _now()})
2. Формат redacted_fields — JSON-массив строк:
   ["user_message.email.5f3a", "tool_output.rows[0].phone.abc1", "llm_output_text.inn.xyz9"]
3. ВАЖНО: оригинал PII НИКОГДА не попадает в span (даже в events). Только mask и
   redacted_fields. Проверка в unit-тестах: assert "ivan@example.com" not in json.dumps(span.to_dict())
4. UI: в Phoenix/Langfuse trace-viewer красным бейджем показать "PII masked: N entities"
   и список redacted_fields в metadata.
5. Тесты: span с PII в user_message → атрибут заполнен корректно; оригинал не виден
   в to_dict(); span без PII → атрибут отсутствует (не пустой массив).

DoD
- [ ] pii.redacted_fields заполняется на span llm.call и tool.call
- [ ] Оригинал PII отсутствует в to_dict() (assert plaintext not in dump)
- [ ] В UI виден список замаскированных полей
- [ ] pytest зелёный
Коммит: feat(agent_obs): record pii.redacted_fields attribute on spans with original pii never exposed
```

---

### PC17 — [T2.2.4] Тест debuggability: 90% кейсов дебажатся без vault recovery

> PC0 + PC16. Приёмочный тест эпика E2.2.

```text
ЗАДАЧА (тикет T2.2.4)
Доказать, что 90% production-трейсов можно дебажить без vault recovery (DoD эпика E2.2).

ЧТО СДЕЛАТЬ
1. tests/test_debuggability.py — выборочный тест на 100 синтетических трейсов:
   - каждый трейс содержит от 1 до 5 PII-сущностей в разных полях
   - применить GuardrailEngine → получить masked span'ы
   - эмулировать дебаг: инженер видит system_prompt + masked user_message + masked tool_output
   - может ли он понять контекст диалога? (руками прогнать 10 кейсов)
2. Метрика debuggability score:
   - 100% — если контекст полностью понятен без оригинала
   - 50% — если нужен vault recovery для 1-2 полей
   - 0% — если нужен vault recovery для > 2 полей
3. Цель: ≥ 90% трейсов имеют debuggability score 100% (по DoD E2.2).
4. Если < 90% — вернуться к PC14/PC15: пересмотреть политики маскинга (например,
   добавить exempted_fields для конкретного tool, где маскинг ломает дебаг).
5. Записать результат в docs/field_aware_masking_report.md: 100 трейсов, процент
   debuggable, типичные кейсы где нужен vault.

DoD
- [ ] tests/test_debuggability.py — 100 трейсов, средний score >= 90%
- [ ] docs/field_aware_masking_report.md с результатами
- [ ] pytest зелёный (assert debuggability_score >= 0.9 on sample)
Коммит: test(guardrail): prove 90% debuggability without vault recovery on 100 sample traces
```

---

## ВОЛНА 3: Vault для PII-восстановления через MFA

> Эпик E2.3 (задачи T2.3.2–T2.3.5; T2.3.1 инфра-задача — в PC02). Старт: после PC02 (Vault
> поднят) и PC08 (guardrail генерирует masks). Стандарт 5.1.1 — противоречие masked↔accessible
> разрешено в пространстве: логи masked, vault accessible.

### PC18 — [T2.3.2] VaultClient.store/recover с TTL=24h

> PC0 + PC02 (Vault поднят) + PC08 (guardrail генерирует masks).

```text
ЗАДАЧА (тикет T2.3.2)
Python-клиент к Vault для хранения mapping'а mask → оригинал с TTL 24 часа.

ЧТО СДЕЛАТЬ
1. agent_obs/guardrail/vault_client.py:
   - класс VaultClient(addr: str, token: str, namespace: str = "")
   - async def store(self, mask: str, original: str, ttl_seconds: int = 86400) -> str
     возвращает vault_key (например, "pii/5f3a/2026-09-21/abc123")
   - async def recover(self, vault_key: str, mfa_token: str) -> str — возвращает оригинал
2. Логика store:
   a. POST на Vault API v1/secret/data/pii/<random_uuid> с payload {data: {mask, original}}
   b. TTL через lease_duration (Vault сам удалит через 24 часа)
   c. Возврат: vault_key = путь в Vault (для последующего recovery)
3. Логика recover:
   a. Запрос TOTP-токена (см. PC19) — без него 403
   b. GET v1/secret/data/<path> — возвращает data.data.original
   c. Запись в audit trail (см. PC20)
   d. TTL проверки: если lease истёк — 404 с понятной ошибкой
4. Батчинг: store поддерживает list[mask, original] для batch (PIIDetector возвращает
   несколько matches за раз) — PUT массивом, один HTTP-вызов.
5. Fallback: если Vault недоступен — store возвращает vault_key=None; guardrail
   помечает span.attributes["pii.recovery_unavailable"]=true (маскинг всё равно работает,
   recovery нет). Метрика vault_unavailable_total++.
6. Тесты: store/recover цикл работает; TTL истекает → 404; vault недоступен →
   fail-closed с masking но без recovery; батчинг работает.

DoD
- [ ] VaultClient.store/recover реализованы, TTL через Vault lease_duration
- [ ] Батчинг store поддерживается (несколько masks за один вызов)
- [ ] Fallback при недоступном Vault: маскинг работает, recovery нет, метрика растёт
- [ ] pytest зелёный (мок Vault через hvac test или testcontainers)
Коммит: feat(guardrail): add vault client with store/recover and ttl-based expiration
```

---

### PC19 — [T2.3.3] TOTP MFA для vault recovery (pyotp)

> PC0 + PC18. Без MFA — recovery невозможен (Стандарт 5.1.1).

```text
ЗАДАЧА (тикет T2.3.3)
MFA через TOTP (RFC 6238) для любого vault recovery — без токена доступ к оригиналу PII
невозмож, даже если у инженера есть vault-токен.

ЧТО СДЕЛАТЬ
1. agent_obs/guardrail/vault_client.py — расширить recover():
   - before GET-запросом в Vault: проверить mfa_token через pyotp.verify(totp_secret, token)
   - если mfa_token невалиден или отсутствует — raise MFARequiredError (без раскрытия,
     есть ли токен у пользователя)
   - TOTP-секрет хранится per-engineer: в configs/mfa_secrets.yaml (НЕ в git, через .env)
2. Реестр инженеров с правом recovery (configs/mfa_authorized.yaml, в git только список
   логинов; секреты через .env):
   - "alice@company.com"
   - "bob@company.com"
3. Команда CLI для генерации TOTP-секрета для нового инженера:
   python -m agent_obs.guardrail.vault_client issue-mfa --login alice@company.com
   → выводит QR-код в stdout + secret для ручной инициализации в Authenticator app.
4. Поддержка window=1 (допускает ±30 секунд) — для человеческой ошибки ввода.
5. Rate-limit: 3 неудачные попытки → 5-минутный cooldown для (vault_key, login).
   Реализовать через Redis (см. PC03) с ключом rate_limit:vault:{login}.
6. Тесты: валидный TOTP → recover работает; невалидный → MFARequiredError; rate-limit
   срабатывает после 3 неудач; TOTP-секрет не в git.

DoD
- [ ] recover() требует mfa_token, без него — MFARequiredError
- [ ] CLI issue-mfa генерирует TOTP-секрет и QR-код
- [ ] Rate-limit: 3 неудачи → 5-мин cooldown
- [ ] pytest зелёный
Коммит: feat(guardrail): add totp mfa for vault recovery with rate limiting
```

---

### PC20 — [T2.3.4] Audit trail для каждого recovery

> PC0 + PC19. Все recovery действия логируются (§3.4 ARCHITECT.md, retention 1 год).

```text
ЗАДАЧА (тикет T2.3.4)
Каждый vault recovery пишется в append-only audit trail: кто, когда, какой mask, по какой
причине. Это §3.4 требование — retention 1 год минимум.

ЧТО СДЕЛАТЬ
1. agent_obs/guardrail/vault_client.py — расширить recover():
   - after successful recovery: write audit-event в ClickHouse audit_events_hot (см. PC01 DDL)
2. AuditEvent = dataclass(
     audit_id, timestamp, trace_id (если recovery связано с трейсом),
     actor={type:"engineer", id: login}, action="vault.recover",
     resource={type:"pii_vault_key", id: vault_key}, reason: str,
     ip_address: str, user_agent: str)
3. Причина recovery (reason): обязательное поле, передаётся инженером через CLI:
   python -m agent_obs.guardrail.vault_client recover --key pii/5f3a/.../abc \
     --mfa 123456 --reason "debugging ticket INC-1234 hallucination in trace 01HZ..."
   Без reason — отказ (это форсирует инженера объяснить, зачем нужен оригинал).
4. Запись в Vault's native audit (PC02) И в наш ClickHouse audit_events_hot — две независимые
   записи, для cross-validation (если кто-то подделает одну, видно по другой).
5. Cron-джоб (см. PC21) через 1 год архивирует audit_events в S3+Parquet (cold).
6. Метрики: vault_recovery_total{reason_category}, vault_recovery_duration_seconds.
7. Тесты: recovery без reason → отказ; с reason → audit-event в ClickHouse; vault native
   audit лог содержит ту же запись; reason обязательный.

DoD
- [ ] recover() пишет audit-event в ClickHouse audit_events_hot и в Vault native audit
- [ ] reason обязательное поле CLI, без него — отказ
- [ ] pytest зелёный, метрики корректны
Коммит: feat(guardrail): write audit events for every vault recovery with reason field
```

---

### PC21 — [T2.3.5] Cron-очистка истёкших TTL в vault

> PC0 + PC02 + PC18. Vault TTL сам удаляет lease, но метаданные нужно чистить.

```text
ЗАДАЧА (тикет T2.3.5)
Cron-джоб для очистки истёкших TTL: Vault сам удаляет lease по истечении 24 часов, но
накопившиеся метаданные (например, в нашем compliance_catalog — см. PC33) нужно чистить.

ЧТО СДЕЛАТЬ
1. scripts/cron/cleanup_vault.py — Python-скрипт, запускается через cron (или k8s CronJob):
   -每小时: list expired leases (vault lease list, фильтр по TTL истёк)
   - пометить их в audit_events_hot как "expired_at" (для истории)
   - фактическое удаление делает Vault сам (lease revoke по TTL)
2. scripts/cron/cleanup_audit_events.py — ежедневно:
   - audit_events старше 365 дней → архив в S3+Parquet (cold), удалить из ClickHouse Hot
3. scripts/cron/cleanup_eval_results.py — ежедневно:
   - eval_results старше 14 дней → перенести в Warm Postgres (structure-only,
     без reasoning, только scores+timestamp)
4. Setup: в infra/docker-compose.yml добавить cron-сервис (или ofelia/cronic) с
   расписанием. ВАЖНО: cron-jobs — это НЕ SDK-код, они работают в отдельном процессе.
5. Метрики: cron_runs_total{job_name}, cron_duration_seconds{job_name},
   cron_errors_total{job_name}, cleanup_rows_deleted_total{job}.
6. Alert: если cron не отработал > 2 циклов — alert (Prometheus rule + Alertmanager).
7. Тесты: скрипт cleanup_vault.py запускается с mock-vault, корректно находит expired
   leases; cleanup_audit_events.py с тестовыми данными старше 365 дней архивирует в
   S3 mock (localstack или testcontainers).

DoD
- [ ] 3 cron-скрипта созданы (vault cleanup, audit archive, eval warm migration)
- [ ] Расписание в docker-compose (ofelia/cronic) настроено
- [ ] Метрики и alert на пропущенный запуск
- [ ] pytest зелёный с mock-бэкендами
Коммит: feat(infra): add cron jobs for vault ttl cleanup and audit/eval archival
```

---

## ВОЛНА 4: Hot/Warm/Cold migrations

> Эпик E2.6 (задачи T2.6.4, T2.6.5; T2.6.1+T2.6.2+T2.6.3 — в PC01). Старт: после PC01 (storage
> поднят). Принцип 18 (механические колебания): tiered retention с колеблющейся детализацией.

### PC22 — [T2.6.4] Migration-джоб Hot→Warm daily (со сжатием)

> PC0 + PC01 (ClickHouse+Postgres поднят).

```text
ЗАДАЧА (тикет T2.6.4)
Ежедневный migration-джоб: трейсы старше 14 дней переносятся из ClickHouse Hot в Postgres
Warm со сжатием — удаляем llm.input_text и llm.output_text, оставляем llm.input_chars +
sha256 + метрики.

ЧТО СДЕЛАТЬ
1. scripts/cron/migrate_hot_to_warm.py:
   - daily в 03:00 UTC
   - SELECT из ClickHouse spans_hot WHERE start_time < now() - INTERVAL 14 DAY
   - для каждого трейса: агрегировать (trace_id, tenant_id, agent_id, start_time, end_time,
     status, cost_usd_total, span_count, error_count, eval_avg JSON)
   - INSERT в Postgres traces_warm
   - DELETE из ClickHouse spans_hot WHERE trace_id IN (...)
2. Сжатие: llm.input_text / llm.output_text НЕ переносятся в Warm (по §11.1 ROADMAP).
   В Warm остаётся только llm.input_chars, llm.input_sha256, llm.output_chars,
   llm.output_sha256, cost-атрибуты, eval scores (если есть).
3. Идемпотентность: повторный запуск для того же дня не дублирует записи (UNIQUE KEY
   trace_id в Postgres traces_warm; DELETE-then-INSERT в транзакции).
4. Метрики: migration_hot_to_warm_rows_total, migration_hot_to_warm_duration_seconds,
   migration_hot_to_warm_errors_total.
5. Если Postgres недоступен — migration откладывается, ретрай каждые 30 минут, до 3 раз.
   После 3 неудач — alert.
6. Тесты: 100 трейсов в ClickHouse со start_time=15 дней назад → migration → в Warm 100
   записей (без input_text), из Hot удалены; повторный запуск — 0 новых записей.

DoD
- [ ] Daily cron переносит трейсы старше 14 дней из Hot в Warm
- [ ] Сжатие: llm.input_text / llm.output_text не в Warm (только sha256 + chars)
- [ ] Идемпотентность по trace_id
- [ ] pytest зелёный
Коммит: feat(storage): add daily hot-to-warm migration with content compression
```

---

### PC23 — [T2.6.5] Migration-джоб Warm→Cold weekly (только агрегаты)

> PC0 + PC22. Финальная стадия tiering.

```text
ЗАДАЧА (тикет T2.6.5)
Еженедельный migration-джоб: трейсы старше 90 дней переносятся из Postgres Warm в S3+Parquet
Cold — только агрегаты (count, sum cost, avg eval score).

ЧТО СДЕЛАТЬ
1. scripts/cron/migrate_warm_to_cold.py:
   - weekly в Sunday 04:00 UTC
   - SELECT из Postgres traces_warm WHERE start_time < now() - INTERVAL 90 DAY
   - агрегировать по (tenant_id, agent_id, day) — counts, sum cost_usd, avg eval,
     distinct users, error_rate
   - записать в S3 bucket cold-traces, partition: tenant_id/year/month/day.parquet
     (pyarrow.ParquetWriter, snappy compression)
   - DELETE из Postgres traces_warm WHERE trace_id IN (...)
2. Parquet-схема:
   struct tenant_id: string, agent_id: string, day: date32,
          traces_count: int32, cost_usd_sum: double, eval_avg: struct<faithfulness, relevancy, completeness>,
          users_count: int32, error_rate: double
3. Athena/Presto queryable: partition projection в S3 lifecycle (см. PC01).
4. Идемпотентность: паркет за (tenant, day) уже существует → overwrite.
5. Метрики: migration_warm_to_cold_files_total, migration_warm_to_cold_rows_total,
   migration_warm_to_cold_bytes_total, migration_warm_to_cold_duration_seconds.
6. Тесты: 1000 трейсов в Warm с start_time=91 дней назад → migration → в S3 parquet
   создан, из Warm удалены; повторный запуск — 0 новых файлов (overwrite).

DoD
- [ ] Weekly cron переносит трейсы старше 90 дней из Warm в Cold S3+Parquet
- [ ] Parquet-схема соответствует спеке, partitioning по tenant/day
- [ ] Идемпотентность (overwrite для того же partition)
- [ ] pytest зелёный (mock S3 через moto/localstack)
Коммит: feat(storage): add weekly warm-to-cold migration with parquet aggregates to s3
```

---

## ВОЛНА 5: Drift detector на embeddings

> Эпик E2.5. Старт: после Волн 0 (ClickHouse) и 4 (migrations — embeddings хранятся в Hot).
> Стандарт 1.1.5 (замена вещества полем): ручная проверка трейсов → автоматический embedding-
> detection аномалий. Принцип 15 (динамичность): baseline-окно скользит.

### PC24 — [T2.5.1] Хранение embeddings ответов в Hot store (ClickHouse)

> PC0 + PC01 (ClickHouse Hot) + PC11 (EmbeddingEvaluator). База для drift detection.

```text
ЗАДАЧА (тикет T2.5.1)
Каждый ответ агента получает embedding (text-embedding-3-small) и сохраняется в ClickHouse
Hot store в колонке response_embedding — для последующего drift detection.

ЧТО СДЕЛАТЬ
1. В EmbeddingEvaluator (см. PC11) — расширить: после вычисления similarity,
   async-записать embedding в ClickHouse:
   INSERT INTO spans_hot (trace_id, span_id, response_embedding) VALUES (...)
   Колонка response_embedding Array(Float32) уже в DDL (см. PC01).
2. Батчинг: embeddings не пишутся по одной — через ring buffer в SDK:
   - evaluator возвращает EvalResult с embedding inside (если eval.enabled)
   - SDK складывает embeddings в отдельную asyncio.Queue(maxsize=50_000)
   - фоновый worker раз в 5 секунд формирует батч и INSERT в ClickHouse
3. Политика: даже если EmbeddingEvaluator считает similarity на 100%, embedding всё
   равно сохраняется (нужен для drift baseline).
4. Размер: 1536-мерный embedding × 1М трейсов/мес = 6 GB/мес в Hot — укладывается в
   retention 14 дней (< 3 GB).
5. Метрики: embeddings_stored_total, embeddings_storage_failed_total, embeddings_batch_size.
6. Тесты: evaluator сохраняет embedding (mock ClickHouse); батчинг работает (10k embeddings
   → 1 INSERT); при недоступном ClickHouse — fallback в Redis с TTL 1 час.

DoD
- [ ] Embedding ответа сохраняется в ClickHouse response_embedding колонке
- [ ] Батчинг через отдельный worker, не блокирует основной export
- [ ] pytest зелёный
Коммит: feat(eval): store response embeddings in clickhouse hot store for drift detection
```

---

### PC25 — [T2.5.2] Cron KL-дивергенция: последнее окно vs baseline (7д минус последний час)

> PC0 + PC24 (embeddings сохранены). Ядро drift detector.

```text
ЗАДАЧА (тикет T2.5.2)
Крон каждые 15 минут сравнивает распределение embeddings последнего часа с baseline-окном
(7 дней за вычетом последнего часа). Метрика — KL-дивергенция.

ЧТО СДЕЛАТЬ
1. agent_obs/drift/kl_divergence.py:
   - функция compute_kl(last_window: np.ndarray, baseline: np.ndarray) -> float
   - использует гистограммный метод: bins=50, density normalization, KL = sum(p_i * log(p_i/q_i))
   - защита от деления на ноль: добавить epsilon (1e-10) к каждому bin
2. agent_obs/drift/detector.py:
   - класс DriftDetector(clickhouse_client, alert_threshold: float)
   - async def run_once(self) -> DriftReport
     a. SELECT response_embedding FROM spans_hot WHERE start_time > now() - INTERVAL 1 HOUR
     b. SELECT response_embedding FROM spans_hot WHERE start_time BETWEEN
        now() - INTERVAL 7 DAY AND now() - INTERVAL 1 HOUR (baseline, скользящее окно)
     c. compute_kl(last, baseline) → KL score
     d. Если KL > alert_threshold → generate alert
3. Cron-расписание: каждые 15 минут (k8s CronJob или ofelia). Запуск из отдельного
   процесса (как cron-cleanup PC21).
4. DriftReport = dataclass(kl_score, baseline_window_start, baseline_window_end,
   last_window_start, last_window_end, sample_size_last, sample_size_baseline, agent_id)
5. Если sample_size_last < 100 — skip (мало данных для статистики), не алертить.
6. Метрики: drift_kl_score{agent_id} (gauge), drift_runs_total, drift_alerts_total{severity}.
7. Тесты: KL на одинаковых распределениях ~ 0; KL на drifted данных >> 0 (искусственно
   сдвинуть распределение); мало данных → skip; cron отрабатывает без exceptions.

DoD
- [ ] DriftDetector.run_once() считает KL между last и baseline окном
- [ ] Cron каждые 15 минут, запуск из отдельного процесса
- [ ] При KL > threshold → DriftReport с метрикой и alert
- [ ] pytest зелёный
Коммит: feat(drift): add kl-divergence detector with 15min cron and 7d sliding baseline
```

---

### PC26 — [T2.5.3] Подбор порога: p99 KL за последние 30 дней = alert threshold

> PC0 + PC25. Антипаттерн §8.7 — без baseline-окно, сейчас калибровка порога.

```text
ЗАДАЧА (тикет T2.5.3)
Калибровка порога alert_threshold: p99 KL за последние 30 дней = текущий порог.

ЧТО СДЕЛАТЬ
1. scripts/cron/calibrate_drift_threshold.py — monthly job:
   - SELECT KL scores за последние 30 дней из метрики drift_kl_score (или из отдельной
     таблицы drift_history, если метрика не хранит историю — создать в ClickHouse)
   - вычислить p99 (per agent_id), сохранить как новый alert_threshold
   - обновить конфиг (configs/drift_thresholds.yaml) или env AGENT_OBS_DRIFT_THRESHOLD_<AGENT_ID>
2. Чтобы не перезаписывать SDK, threshold читается из configs/drift_thresholds.yaml при
   каждом запуске DriftDetector (см. PC25) — динамическое обновление без рестарта.
3. Безопасность: если history < 30 дней — не калибровать (использовать default 0.1);
   alert в лог "drift threshold not calibrated yet, using default".
4. Защита от постепенного drift'а всей системы (когда baseline уже дрейфовал):
   - раз в квартал — sanity-check: случайно выбрать 100 известных "good" трейсов,
     проверить, что их embeddings всё ещё в норме. Если нет — baseline устарел,
     пересобрать из известных good трейсов.
5. Метрики: drift_threshold_value{agent_id} (gauge), drift_threshold_calibrated_at (info).
6. Тесты: калибровка с 30-дневной историей → p99; с < 30 дней → default + alert; sanity-check
   на 100 трейсов → ok или fail.

DoD
- [ ] Monthly cron калибрует alert_threshold как p99 KL за 30 дней per agent_id
- [ ] Threshold читается динамически из configs/drift_thresholds.yaml (без рестарта)
- [ ] Fallback на default 0.1 при < 30 дней истории
- [ ] pytest зелёный
Коммит: feat(drift): add monthly threshold calibration as p99 kl from 30d history
```

---

### PC27 — [T2.5.4] Alert в Alertmanager с лейблами agent_id, kl_divergence, baseline_window

> PC0 + PC25/PC26. Связь drift detector с дежурным.

```text
ЗАДАЧА (тикет T2.5.4)
При срабатывании drift alert — уходит в Alertmanager (уже развёрнут в MVP-стеке для
Prometheus). Лейблы: agent_id, kl_divergence, baseline_window_start, baseline_window_end.

ЧТО СДЕЛАТЬ
1. infra/prometheus-rules.yml — расширить (или создать, если в MVP не было):
   groups:
   - name: orya-observability-critical
     rules:
     - alert: DriftDetected
       expr: drift_kl_score > on(agent_id) drift_threshold_value
       for: 5m
       labels:
         severity: warning
         component: drift-detector
       annotations:
         summary: "Drift detected for agent {{ $labels.agent_id }}"
         description: "KL={{ $value }}, baseline window={{ $labels.baseline_window }}"
2. В DriftDetector.run_once() — после compute_kl: записать drift_kl_score в Prometheus
   через prometheus_client, плюс метрику drift_alerts_total{severity="warning"|"critical"}.
3. Подключение Alertmanager: config в infra/alertmanager.yml (если ещё нет — добавить
   сервис в docker-compose; receivers: slack #oncall,PagerDuty — на CRITICAL уровне только
   Slack,PagerDuty на Уровне 3).
4. Уведомление должно содержать: agent_id, KL-score, baseline_window, ссылку на Phoenix
   UMAP (см. PC28) для root-cause.
5. Suppress: если alert уже сработал в течение 30 минут — не дублировать.
6. Тесты: при KL > threshold — метрика растёт, Alertmanager получает alert (mock webhook);
   suppression работает (в течение 30 минут — 1 alert, не 2).

DoD
- [ ] DriftDetected alert rule в prometheus-rules.yml
- [ ] Alertmanager подключён, Slack webhook работает (на dev — #test-drift)
- [ ] Уведомление содержит все 4 поля (agent_id, KL, baseline, ссылку на Phoenix)
- [ ] Suppression 30 минут
- [ ] pytest / promtool test rules зелёный
Коммит: feat(drift): add alertmanager integration with suppression and phoenix umap link
```

---

### PC28 — [T2.5.5] Phoenix UMAP-визуализатор для root-cause drift

> PC0 + PC13 (Phoenix self-hosted) + PC24 (embeddings).

```text
ЗАДАЧА (тикет T2.5.5)
Интеграция с Arize Phoenix UMAP-визуализатором: при drift alert дежурный открывает
Phoenix, видит scatter plot embeddings за last-window и baseline — глазами находит кластер
"ушедших" ответов.

ЧТО СДЕЛАТЬ
1. Развернуть Phoenix self-hosted (если ещё нет — добавить в docker-compose, образ
   arizephoenix/phoenix:latest, порт 6006, volume для sqlite/duckdb хранения projects).
2. Конфигурация: Phoenix primary dataset = spans_hot (через OTLP-gRPC из OTel Collector,
   см. PC01 infra).
3. В SDK или отдельном cron-скрипте:
   - cron scripts/cron/export_embeddings_to_phoenix.py — каждые 5 минут:
     SELECT trace_id, agent_id, response_embedding FROM spans_hot WHERE start_time > now() - 1 HOUR
     → POST в Phoenix API как "embeddings" dataset
4. UMAP-проекция: Phoenix автоматически строит 2D-проекцию embeddings. Дежурный открывает
   "Scatter Map", фильтр по agent_id, видит кластеры.
5. Интеграция с drift alert (см. PC27): annotation в Slack-уведомлении —
   "Phoenix UMAP: http://phoenix:6006/datasets/<dataset_id>/embeddings?start=<last_window>"
6. drill-down из Phoenix на trace_id: кликаешь на embedding-точку → открывается trace-viewer
   (использует Query API из PC13).
7. Метрики: phoenix_export_runs_total, phoenix_export_duration_seconds,
   phoenix_export_rows_total.
8. Тесты: cron export работает с mock Phoenix API; 1000 embeddings уходят батчем < 5 секунд.

DoD
- [ ] Phoenix self-hosted поднят, UI доступен на :6006
- [ ] Cron экспортирует embeddings в Phoenix каждые 5 минут
- [ ] UMAP-проекция видна, фильтр по agent_id работает
- [ ] Slack-уведомление содержит прямую ссылку на Phoenix UMAP для drift alert
- [ ] pytest зелёный
Коммит: feat(drift): integrate phoenix umap visualizer for drift root cause analysis
```

---

## ВОЛНА 6: Adaptive sampling по load/error-rate

> Эпик E2.7. Старт: после Волн 0 (storage) и 1a (guardrail — для корректной классификации
> security.incident атрибута в tail-sampler). Расширяет MVP tail-sampler (P20) — динамическая
> rate вместо фиксированной 10%. Полностью динамическая rate (с eval-фидбэком) — Уровень 3,
> НЕ ДЕЛАТЬ.

### PC29 — [T2.7.1] Экспорт system_cpu_ratio, agent_error_rate_5m в tail-sampler

> PC0 + MVP P20 (tail-sampler).

```text
ЗАДАЧА (тикет T2.7.1)
Сэмплер получает системные метрики (CPU, error rate) для адаптивной rate-политики.

ЧТО СДЕЛАТЬ
1. agent_obs/metrics.py — расширить MVP-метрики:
   - Gauge system_cpu_ratio (c updating every 10 sec; использует psutil.cpu_percent(interval=None)
     или загрузку из /proc/loadavg; метрики собираются отдельной таской в SDK)
   - Gauge agent_error_rate_5m (вычисляется из кольцевого буфера последних 5 минут: count(error spans) / count(spans))
2. OTel Collector policy engine (или custom mini-proxy из MVP P20) читает эти метрики
   через Prometheus scrape (Configure prometheus.yml endpoint /metrics):
   - источником является пилотный агент на :9090/metrics (см. MVP P16)
3. Конфиг tail-sampler в infra/otel-collector/config.yaml — добавить load_dropped:
   tail_sampling:
     policies:
       - name: adaptive-normal
         type: probabilistic
         probabilistic:
           sampling_percentage: ${env:TAIL_SAMPLER_NORMAL_RATE}  # теперь вычисляется, см. PC30
4. ВАЖНО: метрики публикуются в /metrics pilot-агентом; Collector'у нужен access.
   При multi-agent деплое — общий scrape-config в Prometheus, сэмплер читает per-agent метрики
   (но принимает решение per-trace, по labeлам).
5. Метрики мониторятся: system_cpu_ratio проверяется на каждом evaluate-spans (5 сек
   polling); agent_error_rate_5m обновляется при закрытии каждого span.
6. Тесты: system_cpu_ratio обновляется (mock psutil); agent_error_rate_5m правильно
   считается на 5-min окне; сэмплер видит метрику в /metrics.

DoD
- [ ] system_cpu_ratio и agent_error_rate_5m публикуются в /metrics
- [ ] OTel Collector policy engine скрейпит /metrics каждые 10 сек
- [ ] pytest зелёный
Коммит: feat(metrics): export system_cpu_ratio and agent_error_rate_5m for adaptive sampler
```

---

### PC30 — [T2.7.2] Policy: cpu>0.8 → 5%, error_rate>0.05 → 30%, else 10%

> PC0 + PC29. Главное правило адаптивности (базовый уровень; полная на Уровне 3).

```text
ЗАДАЧА (тикет T2.7.2)
Реализовать policy engine: на основе system_cpu_ratio и agent_error_rate_5m выбрать
sampling rate для normal-трейсов.

ЧТО СДЕЛАТЬ
1. scripts/sampler/policy_engine.py — отдельный процесс (или cron, раз в 30 сек):
   - scrape /metrics pilot-агента (или общий Prometheus, если агентов несколько)
   - apply rules:
     if system_cpu_ratio > 0.8: rate = 0.05
     elif agent_error_rate_5m > 0.05: rate = 0.30
     else: rate = 0.10
   - записать rate в env-файл для Collector'а (или прямое обновление через Collector admin API)
2. Для OTel Collector: tail_sampling probabilistic.sampling_percentage — это static-конфиг.
   Для динамического изменения — кастомный processor на Python (mini-proxy из MVP P20
   вариант B) или headless HTTP-конфиг через otelcol-contrib admin API (если поддерживается).
   Решение обосновать в README.
3. Альтернатива: mini-proxy сэмплер (вариант B из MVP P20) — тогда policy engine просто
   меняет переменную в Python-сэмплере. Рекомендуется для гибкости на CRITICAL.
4. Метрики: tail_sampler_current_rate{policy_reason="cpu_high|error_high|default"} (gauge),
   tail_sampler_rate_changes_total{from, to} (counter).
5. Аудит: каждое изменение rate логируется в audit trail (см. PC20) — "policy changed from
   0.10 to 0.30, reason=error_rate_high (0.07 > 0.05)".
6. Тесты: cpu=0.9 → rate=0.05; error_rate=0.07 → rate=0.30; defaults → 0.10; переходы
   логируются; rate применяется к новому батчу трейсов.

DoD
- [ ] Policy engine выбирает rate по правилу (cpu>0.8 / error>0.05 / default 0.10)
- [ ] Rate динамически обновляется в сэмплере без рестарта
- [ ] Все изменения rate логируются в audit trail с reason
- [ ] pytest зелёный
Коммит: feat(sampler): add adaptive policy engine for normal-trace sampling rate
```

---

### PC31 — [T2.7.3] Логировать изменения rate в audit trail

> PC0 + PC30. Требование прозрачности политики сэмплинга.

```text
ЗАДАЧА (тикет T2.7.3)
Каждое изменение sampling rate — в audit trail, для последующего расследования «почему
этот трейс не попал в Langfuse».

ЧТО СДЕЛАТЬ
1. В policy_engine.py (см. PC30) — при каждом изменении rate:
   - создать AuditEvent (см. PC20, контракт) с action="sampler.rate_change",
     resource={type:"sampler_policy", id:"normal-trace"}, decision="applied",
     reason="cpu_high (0.92 > 0.80) → rate 0.10→0.05"
   - записать в ClickHouse audit_events_hot (через тот же audit-trail API, что и vault recovery)
2. Метаданные в AuditEvent: prev_rate, new_rate, prev_reason, new_reason, system_cpu_ratio
   at moment, agent_error_rate_5m at moment, agent_id.
3. Query API (расширение PC13): GET /audit/sampler-rate-history?from=2026-09-20&to=2026-09-21
   возвращает историю изменений rate для расследования инцидентов.
4. UI: на Phoenix dashboard — аннотации на временной шкале, когда rate менялся (для
   корреляции с метрикой dropped_spans_total).
5. Тесты: 3 изменения rate подряд → 3 audit-event'а в ClickHouse; каждый содержит
   prev/new rate и reason; query API возвращает корректную историю.

DoD
- [ ] Каждое изменение rate → audit-event в ClickHouse с prev_rate, new_rate, reason
- [ ] Query API /audit/sampler-rate-history отдаёт историю
- [ ] pytest зелёный
Коммит: feat(sampler): log all sampling rate changes to audit trail with reason metadata
```

---

### PC32 — [T2.7.4] Метрика tail_sampler_current_rate + дашборд "Sampler Policy History"

> PC0 + PC31. Наблюдаемость самого сэмплера (на Уровне 3 — дашборд в Grafana).

```text
ЗАДАЧА (тикет T2.7.4)
Мониторинг текущей rate сэмплера: метрика tail_sampler_current_rate и (минимальный) дашборд
для истории изменений.

ЧТО СДЕЛАТЬ
1. agent_obs/metrics.py — добавить:
   - Gauge tail_sampler_current_rate{agent_id, policy_reason} (обновляется при каждом
     изменении rate в policy_engine)
   - Counter tail_sampler_rate_changes_total{from_reason, to_reason}
2. policy_engine.py — при изменении rate: set gauge с новой rate, increment counter.
3. Минимальный дашборд в Phoenix (или в Grafana, если есть в MVP-стеке): один график
   "Sampler rate over time" с аннотациями policy_reason. На Уровне 3 — расширенный дашборд
   с корреляцией dropped_spans, system_cpu, error_rate.
4. HTTP endpoint /sampler/status — простой JSON: {current_rate, policy_reason,
   last_changed_at, last_change_reason}. Для ручной проверки дежурным.
5. Alert (Prometheus rule): если tail_sampler_current_rate держится на 0.05 (cpu_high)
   более 30 минут — warning (нужно масштабировать или разбираться с CPU).
6. Тесты: метрика обновляется; endpoint /sampler/status отдаёт корректный JSON; alert
   rule валидируется promtool.

DoD
- [ ] tail_sampler_current_rate gauge обновляется, метрики видны в /metrics
- [ ] /sampler/status endpoint отдаёт текущую политику
- [ ] Минимальный дашборд в Phoenix или Grafana
- [ ] Alert rule promtool test зелёный
- [ ] pytest зелёный
Коммит: feat(sampler): expose current_rate metric, status endpoint, and policy history dashboard
```

---

## ВОЛНА 7: Compliance-catalog как побочный продукт

> Эпик E2.8. Старт: после Волн 1a (guardrail генерирует masks) и 3 (vault — на случай recovery
> для DPO). Принцип 22 (превращение вреда в пользу): маскинг даёт готовый GDPR Data Map.

### PC33 — [T2.8.1] compliance_catalog: запись при каждом маскировании PII

> PC0 + PC08 (guardrail маскирует) + PC16 (pii.redacted_fields). Побочный продукт.

```text
ЗАДАЧА (тикет T2.8.1)
Каждый masked PII генерирует запись в compliance_catalog: (agent_id, tool, field, PII type,
frequency). Это бесплатно — побочный продукт маскинга (Принцип 22).

ЧТО СДЕЛАТЬ
1. agent_obs/compliance/catalog.py:
   - класс ComplianceCatalog(writer: CatalogWriter)
   - метод record(agent_id: str, tool: str, field: str, pii_type: str) -> None
     увеличивает счётчик frequency для (agent_id, tool, field, pii_type) в Postgres
     compliance_catalog (см. PC01 DDL), или батчем через очередь.
2. Интеграция: в GuardrailEngine.check_input (PC07) — после формирования redacted_fields:
   for rf in verdict.redacted_fields:
       compliance_catalog.record(agent_id=ctx.agent_id, tool=verdict.tool_name or "",
                                 field=rf.field_name, pii_type=rf.entity_type)
3. Запись неблокирующая: через asyncio.create_task, не блокирует _enqueue (это
   дополнение к маскингу, observability не должна ломаться).
4. Батчинг: compliance_catalog накапливает записи в in-memory dict, раз в 1 минуту
   flush'ит в Postgres (UPSERT — increment counter для существующих записей).
5. Метрики: compliance_catalog_writes_total, compliance_catalog_buffer_size,
   compliance_catalog_flush_errors_total.
6. Тесты: 5 маскирований (3 email, 2 phone) для одного agent+tool → в catalog одна запись
   с frequency=3 (email) и одна с frequency=2 (phone); батч-flush работает; при недоступном
   Postgres — буфер растёт, метрика errors_total++.

DoD
- [ ] ComplianceCatalog.record() инкрементирует frequency в Postgres
- [ ] Запись неблокирующая, батчинг раз в минуту
- [ ] pytest зелёный
Коммит: feat(compliance): record every pii masking event into compliance catalog
```

---

### PC34 — [T2.8.2] Cron-джоб ежедневной агрегации каталога

> PC0 + PC33. Поддержание актуальности frequency + cleanup дубликатов.

```text
ЗАДАЧА (тикет T2.8.2)
Ежедневный cron агрегирует compliance_catalog: обновляет last_seen, чистит дубликаты.

ЧТО СДЕЛАТЬ
1. scripts/cron/aggregate_compliance_catalog.py — daily в 02:00 UTC:
   - SELECT из compliance_catalog всех записей
   - UPDATE last_seen = max(last_seen, latest_masking_timestamp)
   - удаление записей старше 90 дней без новой activity (мёртвые поля — больше не маскируются)
   - агрегация по разрезам: построить view compliance_catalog_by_agent (agent_id, total_pii_count,
     distinct_pii_types) и compliance_catalog_by_tool (tool, total_pii_count)
2. На Warm тире (см. PC22): weekly перенос compliance_catalog в Warm (Postgres), в Hot
   остаются последние 14 дней для быстрых запросов.
3. Метрики: compliance_catalog_rows_total, compliance_catalog_aggregation_duration_seconds,
   compliance_catalog_rows_cleaned_total.
4. Alert: если за сутки в каталоге не появилось НИ ОДНОЙ записи — alert "PII masking not
   working?" (гипотеза: guardrail сломался, или трафик нулевой — нужно проверить).
5. Тесты: 3 записи (старше 90 дней без activity) → удаляются; 5 записей (с activity в
   последние 14 дней) → last_seen обновляется, остаются в Hot.

DoD
- [ ] Daily cron агрегирует compliance_catalog, обновляет last_seen, чистит stale записи
- [ ] Метрики и alert работают
- [ ] pytest зелёный
Коммит: feat(compliance): add daily aggregation cron for compliance catalog
```

---

### PC35 — [T2.8.3] Экспорт GDPR Data Map в CSV/Excel

> PC0 + PC34. Главный deliverable для DPO (Data Protection Officer).

```text
ЗАДАЧА (тикет T2.8.3)
Экспорт compliance_catalog в формате GDPR Data Map — готовый отчёт для DPO.

ЧТО СДЕЛАТЬ
1. agent_obs/compliance/gdpr_export.py:
   - async def export_data_map(as_of: date, fmt: str = "csv" | "xlsx") -> bytes
   - SELECT из compliance_catalog (Hot + Warm, см. PC22 migration):
     WHERE last_seen >= as_of - 90 days
   - колонки в отчёте:
     * agent_id — где PII обрабатывается
     * tool — в каком инструменте
     * field — какое поле
     * pii_type — тип PII (email/phone/inn/passport/payment/medical/name/address)
     * frequency — сколько раз маскировалось за последние 90 дней
     * first_seen / last_seen — когда впервые и последний раз
     * data_subject_category — категория субъекта (user/employee/customer — выводится из agent_id+tool)
     * lawful_basis — основание обработки (по умолчанию "legitimate_interest",
       настраивается в configs/lawful_basis.yaml per agent_id)
     * retention_period — "24h in vault, 90d in compliance_catalog"
     * recipient — "observability team"
2. Форматы:
   - CSV (utf-8 with BOM для Excel) — простой, быстро
   - XLSX (openpyxl) — с заголовком и sheet "Data Map" + "Summary" (агрегаты по типам)
3. CLI: python -m agent_obs.compliance.gdpr_export --as-of 2026-09-21 --fmt xlsx
   --output /tmp/gdpr_data_map_2026-09-21.xlsx
4. Метрики: gdpr_export_runs_total, gdpr_export_duration_seconds,
   gdpr_export_rows_total, gdpr_export_bytes_total.
5. Тесты: экспорт CSV → 5 строк (по числу distinct (agent, tool, field, pii_type));
   XLSX открывается openpyxl.load_workbook, содержит два sheet'а; lawful_basis берётся
   из конфига; поля first_seen/last_seen корректны.

DoD
- [ ] export_data_map() работает в CSV и XLSX
- [ ] Все 11 GDPR-колонок присутствуют
- [ ] CLI запускается, создаёт файл в /tmp/ или по --output
- [ ] pytest зелёный
Коммит: feat(compliance): add gdpr data map export in csv and xlsx formats
```

---

### PC36 — [T2.8.4] UI: страница "Data Map" в observability UI

> PC0 + PC35. Финальный deliverable: 5-минутный time-to-GDPR-report.

```text
ЗАДАЧА (тикет T2.8.4)
Простая web-страница в Phoenix (или отдельном FastAPI-приложении) для DPO: показать
compliance_catalog, кнопка экспорта.

ЧТО СДЕЛАТЬ
1. Если Phoenix поддерживает custom dashboards — построить страницу "Data Map" через
   Phoenix API (его annotations / saved views).
2. Альтернатива (рекомендуется на CRITICAL для простоты): отдельный FastAPI-сервис
   oraya-compliance-ui на :8088:
   - GET / — главная страница с таблицей compliance_catalog (с фильтрами по agent_id,
     tool, pii_type, дате last_seen)
   - GET /export?fmt=csv|xlsx&as_of=2026-09-21 — отдаёт файл из PC35
   - простая HTML-страница (Jinja2 templates), без JS-фреймворков
3. Read-only: пользователь может смотреть, фильтровать, экспортировать — НЕ может
   редактировать (это audit trail). RBAC на Уровне 3 (T3.8.2).
4. Скриншот-чек: DPO заходит на /, видит таблицу "Где есть PII" с фильтрами, кнопка
   "Export GDPR Data Map" → скачивает XLSX.
5. README в docs/compliance_ui.md: URL, как подключить SSO (опционально, на CRITICAL —
   basic auth через .htpasswd).
6. Тесты: GET / возвращает 200 с таблицей; GET /export?fmt=csv → 200 с text/csv;
   filters работают (query params).

DoD
- [ ] Compliance UI доступен на :8088, таблица с фильтрами
- [ ] Кнопка экспорта отдаёт CSV/XLSX из PC35
- [ ] Read-only (нет edit-методов), basic auth на dev
- [ ] Time-to-GDPR-report < 5 минут (manual check: DPO заходит, фильтрует, экспортирует)
- [ ] pytest зелёный
Коммит: feat(compliance): add data map web ui with export and filter capabilities
```

---

## ФИНАЛ: Верификация и цикл фиксов

### PC37 — [VERIFICATION] Верификационный промпт: приёмка CRITICAL по exit-criteria §5.5

> Отправлять после выполнения PC01–PC36. Это приёмка уровня по ROADMAP §5.5.

```text
ЗАДАЧА
РОЛЬ
Ты — QA/SRE-инженер, принимающий Уровень 2 (CRITICAL) observability-стека.
Проводишь приёмочные испытания СТРОГО по критериям ниже. Ничего не дорабатываешь —
только измеряешь и фиксируешь. Результат — отчёт docs/critical-acceptance.md.

ПОДГОТОВКА
1. Подними полный стенд: docker compose up -d (Langfuse + Postgres + Redis + OTel Collector
   + ClickHouse + Warm Postgres + MinIO + Vault + Eval Workers + Phoenix + Compliance UI).
2. Запусти пилотного агента с включённым SDK и guardrail/eval/drift.
3. Зафиксируй версии всех компонентов и коммит репозитория.

ИСПЫТАНИЯ (по каждому — методика, измеренное значение, pass/fail)

C1. SLO покрыты alerting'ом (целевое: 100%)
    Проверь в infra/prometheus-rules.yml: 4 SLO (availability, latency, quality, cost)
    имеют alert rules. Каждый SLO имеет 3 уровня (warning 80%, critical 95%, burn 100%).
    Audit alerting rules: promtool check rules + grep по именам. 100% → pass.

C2. MTTR инцидентов (целевое: < 30 минут)
    Симулируй инцидент: останови Vault на 5 минут, восстановь. Измерь время от падения
    vault_unavailable_total++ до полного восстановления (masking работает, recovery снова
    доступен). < 30 минут → pass.

C3. PII-утечка в логи (целевое: 0 случаев)
    Запусти агент с синтетическими PII-данными (100 трейсов с известными PII). Запусти
    PII-сканер (например,Microsoft Presidio offline) по логам контейнера пилотного агента
    (docker logs + grep). 0 matches → pass.

C4. Drift detection latency (целевое: < 15 минут)
    Симулируй drift: пошли 100 запросов с существенно другим доменом (например, медицинские
    вопросы вместо финансовых). Измерь время от первого запроса до drift alert в Slack.
    < 15 минут → pass.

C5. GDPR Data Map готов (целевое: < 5 минут time-to-report)
    DPO заходит на /observability/data-map, фильтрует по дате, нажимает Export → XLSX.
    Замерь время. < 5 минут → pass.

C6. Latency guardrail (целевое: < 5 мс p99)
    Нагрузочный тест с guardrail-проверками (10k RPS, 30 секунд). p99 времени
    guardrail.check_input() — из метрики guardrail_check_duration_seconds. < 5 мс → pass.

C7. PII recall на guardrail (целевое: > 95%)
    На тестовом датасете (1000 PII-сущностей всех 5 типов) — recall = detected / total.
    > 95% → pass.

C8. Injection F1 (целевое: > 0.90)
    На датасете prompt-injection-attacks (200 примеров) — F1 = 2*P*R/(P+R).
    > 0.90 → pass.

C9. Latency добавленная для пользователя от eval (целевое: 0 мс)
    Замерь latency ответа агента с eval.enabled=true vs eval.enabled=false. Разница = 0 → pass.

C10. Latency eval late annotation (целевое: < 30 секунд p95)
    Из метрики eval_job_latency_seconds — p95. < 30 → pass.

C11. Hot query latency (целевое: < 1 секунда p95)
    SELECT * FROM spans_hot WHERE trace_id = ? — 100 случайных trace_id, p95. < 1 с → pass.

C12. Storage cost (целевое: < $0.5/GB/мес Hot + Warm + Cold)
    Расчёт по итогам месяца: Hot (ClickHouse compressed) + Warm (Postgres) + Cold (S3).
    < $0.5/GB → pass.

C13. Overhead reduction при пиках (целевое: 50% от baseline)
    Сравни dropped_spans_total при 10k RPS с адаптивным сэмплером (rate=0.05 при cpu>0.8) vs
    фиксированным rate=0.10. Снижение storage/network на 50%+ → pass.

C14. Идеальность (целевое: >= 1.5)
    По формуле §10.2: 7 полезных функций / 4.7 затрат (Security, Eval, Drift, Tiered
    Storage, Compliance, Adaptive sampler, Audit). >= 1.5 → pass.

ФОРМАТ ОТЧЁТА docs/critical-acceptance.md
| Критерий | Целевое | Измерено | Методика | Вердикт |
+ сводка: «CRITICAL ПРИНЯТ» или список проваленных критериев.
Проваленные критерии не чини сам — верни отчёт человеку для запуска PC38.

DoD
- [ ] docs/critical-acceptance.md содержит все 14 критериев с измеренными значениями
- [ ] Все pass → CRITICAL достигнут
Коммит: test(acceptance): add critical acceptance report with exit criteria results
```

---

### PC38 — [TEMPLATE] ШАБЛОН промпта-фиксов (итеративный цикл)

> Использовать для КАЖДОГО проваленного критерия из отчёта PC37. Скопируй и подставь значения в {скобках}.

```text
КОНТЕКСТ
Приёмка CRITICAL (docs/critical-acceptance.md) выявила провал критерия.

ПРОВАЛЕННЫЙ КРИТЕРИЙ: {C1..C14 — название}
ЦЕЛЕВОЕ ЗНАЧЕНИЕ: {из ROADMAP §5.5}
ФАКТИЧЕСКОЕ ЗНАЧЕНИЕ: {из отчёта}
МЕТОДИКА ИЗМЕРЕНИЯ: {из отчёта}

ЗАДАЧА
1. Найди корневую причину провала. Сначала измерь/запрофилируй, потом меняй код.
   Зафиксируй причину одной фразой ("because ...").
2. Исправь МИНИМАЛЬНО: только то, что относится к причине. Не рефактори соседнее.
   Не добавляй функциональность Уровня 3 (tree-compression, drill-down UI, sidecar,
   federation, cost-aware routing, adaptive eval-sampling день/ночь, multi-tenant RBAC).
3. Соблюдай железные правила PC0 (PII только в процессе агента, eval async, vault MFA,
   drift скользящий baseline, rollback возможен).
4. Перепроведи методику измерения критерия и покажи новое значение.
5. Прогони полный pytest. Один коммит: fix(guardrail): ... / fix(eval): ... / fix(storage): ...
6. Обнови docs/critical-acceptance.md (значение + пометка "fixed: <причина>").

ОГРАНИЧЕНИЯ
- Если причина в архитектуре (например, нужно переписать guardrail на sidecar — это
  Уровень 4) — СТОП: верни мне описание противоречия и 2 варианта решения, не реализуй сам.
- Если фикс требует новой зависимости — СТОП: перечисли варианты и плюсы/минусы.
- Если фикс затрагивает контракты MVP (SpanContext, BaseExporter, декораторы) — СТОП:
  это нарушение non-contradiction (§1.2 ROADMAP), верни описание и альтернативы.

DoD
- [ ] Критерий проходит повторное измерение
- [ ] pytest зелёный, scope не расширен
```

---

## Сводный чек-лист выхода на CRITICAL (ROADMAP §5.5)

| # | Эпик | Тикет | Промпт | Волна | Описание |
|---|---|---|---|---|---|
| 1 | E2.6 (infra) | T2.6.1+T2.6.2+T2.6.3 | PC01 | 0 | ClickHouse + Postgres + MinIO (tiered storage stack) |
| 2 | E2.3 (infra) | T2.3.1 | PC02 | 0 | HashiCorp Vault с auto-unseal + pii-recovery policy |
| 3 | E2.4 (infra) | T2.4.1 | PC03 | 0 | Redis + RQ workers для async eval |
| 4 | E2.1 | T2.1.1 | PC04 | 1a | PIIDetector: regex (email/phone/INN/passport/payment) |
| 5 | E2.1 | T2.1.2 | PC05 | 1a | CRF-модель (Presidio) для медицинского и ФИО PII |
| 6 | E2.1 | T2.1.3 | PC06 | 1a | DeBERTa-v3 prompt-injection классификатор |
| 7 | E2.1 | T2.1.4 | PC07 | 1a | GuardrailEngine.check_input() → GuardrailVerdict |
| 8 | E2.1 | T2.1.5 | PC08 | 1a | Встроить guardrail в _enqueue() SDK синхронно |
| 9 | E2.4 | T2.4.2 | PC09 | 1b | RuleBasedEvaluator (regex/JSON-schema/blacklist) |
| 10 | E2.4 | T2.4.3 | PC10 | 1b | LLMJudgeEvaluator (faithfulness/relevancy/completeness) |
| 11 | E2.4 | T2.4.4 | PC11 | 1b | EmbeddingEvaluator (cosine similarity) |
| 12 | E2.4 | T2.4.5 | PC12 | 1b | Late annotation в Hot store по trace_id |
| 13 | E2.4 | T2.4.6 | PC13 | 1b | UI: eval-панель в trace-viewer с pending |
| 14 | E2.2 | T2.2.1 | PC14 | 2 | FieldMasker с YAML-конфигом политик |
| 15 | E2.2 | T2.2.2 | PC15 | 2 | Парсинг tool_output: SQL-колонки и REST JSON-path |
| 16 | E2.2 | T2.2.3 | PC16 | 2 | pii.redacted_fields атрибут span llm.call |
| 17 | E2.2 | T2.2.4 | PC17 | 2 | Тест debuggability 90% без vault recovery |
| 18 | E2.3 | T2.3.2 | PC18 | 3 | VaultClient.store/recover с TTL=24h |
| 19 | E2.3 | T2.3.3 | PC19 | 3 | TOTP MFA (pyotp) для vault recovery |
| 20 | E2.3 | T2.3.4 | PC20 | 3 | Audit trail для каждого recovery |
| 21 | E2.3 | T2.3.5 | PC21 | 3 | Cron-очистка истёкших TTL в vault |
| 22 | E2.6 | T2.6.4 | PC22 | 4 | Migration Hot→Warm daily со сжатием |
| 23 | E2.6 | T2.6.5 | PC23 | 4 | Migration Warm→Cold weekly (только агрегаты) |
| 24 | E2.5 | T2.5.1 | PC24 | 5 | Хранение embeddings в Hot store (ClickHouse) |
| 25 | E2.5 | T2.5.2 | PC25 | 5 | Cron KL-дивергенция: last vs baseline 7д |
| 26 | E2.5 | T2.5.3 | PC26 | 5 | Калибровка порога: p99 KL за 30 дней |
| 27 | E2.5 | T2.5.4 | PC27 | 5 | Alert в Alertmanager с лейблами |
| 28 | E2.5 | T2.5.5 | PC28 | 5 | Phoenix UMAP-визуализатор для root-cause |
| 29 | E2.7 | T2.7.1 | PC29 | 6 | Экспорт system_cpu_ratio, agent_error_rate_5m |
| 30 | E2.7 | T2.7.2 | PC30 | 6 | Policy: cpu>0.8→5%, error>0.05→30%, else 10% |
| 31 | E2.7 | T2.7.3 | PC31 | 6 | Логирование rate changes в audit trail |
| 32 | E2.7 | T2.7.4 | PC32 | 6 | tail_sampler_current_rate метрика + дашборд |
| 33 | E2.8 | T2.8.1 | PC33 | 7 | compliance_catalog запись при маскировании |
| 34 | E2.8 | T2.8.2 | PC34 | 7 | Cron ежедневной агрегации каталога |
| 35 | E2.8 | T2.8.3 | PC35 | 7 | Экспорт GDPR Data Map в CSV/Excel |
| 36 | E2.8 | T2.8.4 | PC36 | 7 | UI: страница Data Map в observability UI |
| 37 | §5.5 | VERIFICATION | PC37 | 8 | Приёмка CRITICAL по 14 exit-criteria |
| 38 | — | TEMPLATE | PC38 | 9 | Шаблон промпта-фиксов для проваленных критериев |

**Памятка по рискам (ROADMAP §5.6), контролируемая промптами:**

| Риск | Likelihood | Impact | Митигируется в |
|---|---|---|---|
| R2.1 PII-маскинг не покрывает новый тип PII | Medium | Critical | PC04+PC05 (регулярное обновление детекторов); калибровка раз в квартал |
| R2.2 LLM-as-judge сам галлюцинирует | Medium | Medium | PC10 (gpt-4o-mini) + периодическая калибровка на golden set |
| R2.3 Drift alert всегда горит (false positive) | Medium | High | PC26 (порог по p99 исторических данных, калибровка раз в месяц) |
| R2.4 ClickHouse падает под нагрузкой | Low | Critical | PC01 (шардирование, репликация); fallback на Postgres при сбое |
| R2.5 Vault становится SPOF | Low | Critical | PC02 (HA-режим Vault); регулярные backup'ы |
| R2.6 Cost eval-слоя превышает бюджет | Medium | Medium | PC30 (adaptive sampling снижает eval-нагрузку при пиках) |

