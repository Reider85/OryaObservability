# BACKLOG.md — Полный бэклог внедрения observability-стека OryaObservability

**Версия документа:** 1.0
**Дата:** 2026-09-26
**Базовые документы:** `ROADMAP.md` v1.0 (§4–§7 — все 4 уровня), `ARCHITECT.md` v1.0 (§3–§7 — контракты слоёв), `MVP-PROMPT.md` v1.0 (P0..P27 — готовые промпты MVP), `CRITICAL-PROMPTS.md` v1.0 (PC0..PC38 — готовые промпты CRITICAL)
**Применён метод:** ТРИЗ (теория решения изобретательских задач), §10–§11 ARCHITECT.md
**Аудитория:** tech-лиды, product-owners, SRE, backend-инженеры

---

## 0. Как пользоваться этим документом

### 0.1. Назначение

`BACKLOG.md` — единый реестр всех задач проекта OryaObservability от MVP до финального ТРИЗ-состояния (ИКР). Документ консолидирует:

1. Все эпики из `ROADMAP.md` (§4–§7) — 30 эпиков в 4 уровнях.
2. Все тикеты, декомпозированные до уровня 3–5 задач на эпик — **131 задача**.
3. Статус выполнения: `DONE` (завершено), `IN PROGRESS` (в работе), `TODO` (запланировано), `PLANNED` (отложено до следующего уровня), `PoC` (исследование).
4. Зависимости между задачами (внутри и между уровнями).
5. Ссылки на готовые промпты для ИИ-кодинг-агента (для MVP: `MVP-PROMPT.md` P0..P27; для CRITICAL: `CRITICAL-PROMPTS.md` PC0..PC38). Для production-ready и Улучшения промпты будут созданы отдельными файлами по мере приближения к этим уровням.
6. DoD (Definition of Done) и KPI по каждому эпику (из `ROADMAP.md`).
7. Реестр рисков (§4.6, §5.6, §6.6, §7.6 ROADMAP).
8. Rollback-стратегии для каждого уровня.

### 0.2. Соглашения об именах

| Префикс | Назначение | Пример |
|---|---|---|
| `T1.x.y` | Тикет уровня MVP (Уровень 1) | T1.1.1 |
| `T2.x.y` | Тикет уровня CRITICAL (Уровень 2) | T2.1.5 |
| `T3.x.y` | Тикет уровня production-ready (Уровень 3) | T3.4.3 |
| `T4.x.y` | Тикет уровня Улучшения (Уровень 4) | T4.5.2 |
| `E1.x` | Эпик уровня MVP | E1.4 Cost-трекинг |
| `E2.x` | Эпик уровня CRITICAL | E2.4 Async eval-pipeline |
| `E3.x` | Эпик уровня production-ready | E3.4 Tree + compression |
| `E4.x` | Эпик уровня Улучшения | E4.5 LLM-intrinsic trace |
| `R1.x` | Риск уровня MVP | R1.5 Storage > 10 ГБ/день |
| `R2.x` | Риск уровня CRITICAL | R2.3 Drift alert всегда горит |
| `R3.x` | Риск уровня production-ready | R3.5 RBAC-мисконфиг |
| `R4.x` | Риск уровня Улучшения | R4.7 Federation нарушает tenant изоляцию |
| `C1..C7` | Exit-criterion уровня MVP (§4.5) | C3 Доставка трейса в UI < 5 с p95 |
| `C1..C14` | Exit-criterion уровня CRITICAL (§5.5) | C3 PII-утечка в логи = 0 |
| `P0..P27` | Промпт MVP (`MVP-PROMPT.md`) | P20 Tail-sampler политика |
| `PC0..PC38` | Промпт CRITICAL (`CRITICAL-PROMPTS.md`) | PC04 PIIDetector |

### 0.3. Сводная карта уровней

| # | Уровень | Горизонт | Цель | Эпиков | Задач | Идеальность | Статус |
|---|---|---|---|---|---|---|---|
| 1 | **MVP** | 4–6 недель | Базовое Logs/Traces + Cost для пилота | 6 | 26 | 0.83 | **DONE** (256/256 тестов PASS, частично pending live-стенд) |
| 2 | **CRITICAL** | 2–3 месяца | Guardrail + Eval + Drift + Tiered storage | 8 | 38 | 1.5 (+0.67) | **TODO** (промпты готовы в `CRITICAL-PROMPTS.md`) |
| 3 | **production-ready** | 6 месяцев | Adaptive sampling, tree-compression, SLO/SLA | 8 | 33 | 2.5 (+1.0) | **PLANNED** |
| 4 | **Улучшения production-ready** | 12 месяцев | Sidecar SDK, federation, cost-aware routing, ИКР | 7 | 34 | 3.6 (+1.1) | **PLANNED** (3 эпика — PoC) |
| | **ИТОГО** | **12 месяцев** | | **29** | **131** | **3.6 (×4.3 от baseline)** | |

### 0.4. Принцип non-contradiction (§1.2 ROADMAP)

**Ключевое правило:** ни одна задача уровня N не отменяет решений уровня N-1. Каждое ТРИЗ-улучшение (§11.1 ARCHITECT.md — всего 15 улучшений) применяется на строго определённом уровне, после того как базис предыдущего уровня стабилизирован. См. §3 ниже «ТРИЗ-карта».

---

## 1. Уровень 1 — MVP (4–6 недель)

### 1.1. Цель уровня

Базовое покрытие слоёв **Logs/Traces** и **Cost** для одного пилотного агента на Python. Доказать, что observability-инструментация не блокирует продакшен-логику и даёт немедленную диагностическую ценность: trace с привязкой стоимости виден в UI. Слой Metrics — минимальный (только технические метрики для overhead-контроля). Слои Eval, Security, Federation — out-of-scope (переносятся на Уровень 2+).

### 1.2. Эпики и задачи

### 1.2. Эпики и задачи

Уровень MVP включает 6 эпиков (E1.1–E1.6), всего 26 задач. Все задачи выполнены, 256/256 тестов PASS (см. `docs/mvp-acceptance.md`). Часть exit-criteria (C1, C3, C5-биллинг, C7-latency) требует live-стенда (docker compose + Langfuse) и помечена как PENDING в отчёте приёмки.

#### E1.1 — Python SDK каркас (декораторы + context manager)

**ТРИЗ-обоснование.** Принцип 28 (замена механической схемы): sync-логирование → async-first. Принцип 25 (самообслуживание): SDK управляет контекстом span'а через `_obs_ctx`.


**DoD.** SDK импортируется как `from agent_obs import ObservabilitySDK`, декоратор применяется к async-классу агента без изменения бизнес-логики. При `enabled=False` overhead < 1 мкс.


**KPI.** API-покрытие: 3 из 9 типов span'ов (`agent.loop`, `llm.call`, `tool.call`).


**Задачи:**


| Тикет | Описание | Статус | Промпт |
|---|---|---|---|
| T1.1.1 | SpanContext dataclass (trace_id, span_id, parent_span_id, agent_id, agent_version, user_id, session_id) | **DONE** | `P02` |
| T1.1.2 | Span dataclass с атрибутами, events, таймстампами, to_dict() по схеме §7.1 | **DONE** | `P03` |
| T1.1.3 | Декоратор @agent_observed(agent_id, version) — обёртывает async run(), создаёт root span agent.loop | **DONE** | `P04` |
| T1.1.4 | Контекстные менеджеры llm_call() и tool_call() с типами span'ов llm.call / tool.call | **DONE** | `P05` |
| T1.1.5 | Флаг enabled в конфиг SDK — при False декораторы no-op (zero-overhead) | **DONE** | `P06` |
#### E1.2 — OTLP-экспорт в Langfuse (self-hosted)

**ТРИЗ-обоснование.** Принцип 5 (объединение): единый OTLP-протокол как транспорт. Принцип 12 (эквипотенциальность): внутренний формат уже OTLP-совместимый, замена экспортёра не требует изменения SDK.


**DoD.** В UI Langfuse виден трейс пилотного агента с деревом span'ов, атрибутами и событиями. Батч 512 span'ов долетает за < 200 мс p95.


**KPI.** Доставка: 100% трейсов пилотного агента видны в UI в течение 5 секунд p95 после завершения agent.loop.


**Задачи:**


| Тикет | Описание | Статус | Промпт |
|---|---|---|---|
| T1.2.1 | Развёртывание Langfuse self-hosted в docker-compose (langfuse+postgres+redis+otel-collector) | **DONE** | `P01` |
| T1.2.2 | LangfuseExporter.export(batch) — батчинг ≤ 512, ретрай с экспоненциальной задержкой | **DONE** | `P17` |
| T1.2.3 | LangfuseExporter.flush() — досылка остатков при shutdown | **DONE** | `P18` |
| T1.2.4 | Аутентификация по public/secret key, TLS для продакшена, env-cred'ы | **DONE** | `P19` |
#### E1.3 — Tail-sampler с фиксированной политикой

**ТРИЗ-обоснование.** Принцип 16 (частичное или избыточное действие): 10% обычных трейсов вместо 100% дают 10× экономию storage без потери худших. Стандарт 2.2.4 закладывается как цель — уровень 3.


**DoD.** При нагрузке 100 RPS в Langfuse попадает 10–15% трейсов (с учётом 100% для ошибок). Storage растёт < 5 ГБ/день.


**KPI.** Доля сохранённых трейсов: 10–15%. Доля потерянных «интересных» трейсов: 0% (по политике).


**Задачи:**


| Тикет | Описание | Статус | Промпт |
|---|---|---|---|
| T1.3.1 | Развёртывание OTel Collector с custom processor llm_tail_sampler | **DONE** | `P01` |
| T1.3.2 | Политика: status_code=ERROR 100%, cost.usd>threshold 100%, security.incident=true 100%, прочие 10% | **DONE** | `P20` |
| T1.3.3 | Метрика tail_sampler_kept_ratio для контроля доли сохранённых трейсов | **DONE** | `P21` |
| T1.3.4 | Content-sampling: полный текст промпта только у 10% (по trace_id), у остальных sha256+chars | **DONE** | `P22` |
#### E1.4 — Cost-трекинг как атрибут span'а

**ТРИЗ-обоснование.** Принцип 26 (копирование): cost-атрибут в трейсе вместо отдельной биллинг-системы. Принцип 12: price_book версионирован, cost.price_book_version в span'е — для реконсиляции.


**DoD.** В UI Langfuse на span'е llm.call видны все cost-атрибуты. Сумма cost-атрибутов по трейсу совпадает с биллингом OpenAI ±5%.


**KPI.** Точность cost-учёта: ±5% от реальной стоимости биллинга провайдера.


**Задачи:**


| Тикет | Описание | Статус | Промпт |
|---|---|---|---|
| T1.4.1 | price_book.yaml для топ-5 моделей (gpt-4o, gpt-4o-mini, claude-3.5-sonnet, claude-3-haiku, gemini-1.5-pro) | **DONE** | `P12` |
| T1.4.2 | Загрузка price_book с валидацией valid_from/valid_to, проверка перекрытий | **DONE** | `P13` |
| T1.4.3 | _compute_cost() с поддержкой cached tokens (отдельная цена) | **DONE** | `P14` |
| T1.4.4 | Атрибуты span'а llm.call: tokens.input/output/cached, cost.usd, cost.price_book_version | **DONE** | `P15` |
| T1.4.5 | Метрика cost_per_request_usd (gauge) с лейблами agent_id, model + Counter cost_total_usd | **DONE** | `P16` |
#### E1.5 — Базовый trace-viewer через Langfuse UI

**ТРИЗ-обоснование.** Принцип 5 (объединение): не делаем свой UI, переиспользуем Langfuse. Кастомный trace-tree viewer — на уровень 3.


**DoD.** Разработчик может за < 30 секунд найти трейс по trace_id или по фильтру «ошибки 24ч».


**KPI.** Time-to-trace: < 30 секунд на поиск конкретного трейса.


**Задачи:**


| Тикет | Описание | Статус | Промпт |
|---|---|---|---|
| T1.5.1 | Маппинг span-атрибутов в лейблы Langfuse UI (agent_id, agent.version, model, status) | **DONE** | `P23` |
| T1.5.2 | 3 saved query: «ошибки 24ч», «cost > $0.05», «loop > 10 шагов» | **DONE** | `P24` |
| T1.5.3 | README для команды поддержки: как искать трейс по trace_id | **DONE** | `P25` |
#### E1.6 — In-process ring buffer + async export (fire-and-forget)

**ТРИЗ-обоснование.** Принцип 28: sync-логирование → lock-free ring buffer + async export, overhead падает до наносекунд. Принцип 22 (вред в пользу): переполнение → метрика dropped_spans — сигнал для SRE.


**DoD.** При отключении Langfuse агент продолжает работать без ошибок. Через 60 секунд буфер переполняется, dropped_spans растёт, бизнес-логика не ломается.


**KPI.** Overhead на instrumentation: < 5% CPU при 100 RPS. Доля потерянных span'ов при нормальной нагрузке: 0%.


**Задачи:**


| Тикет | Описание | Статус | Промпт |
|---|---|---|---|
| T1.6.1 | Ring buffer asyncio.Queue(maxsize=100_000) в конструкторе SDK | **DONE** | `P07` |
| T1.6.2 | _export_worker(): drain-окно 50 мс, batch ≤ 512, fan-out на все экспортёры | **DONE** | `P08` |
| T1.6.3 | При QueueFull: drop + инкремент dropped_spans_total + throttled warning | **DONE** | `P09` |
| T1.6.4 | shutdown(): досылка остатков через flush() с timeout 5 секунд | **DONE** | `P10` |
| T1.6.5 | Нагрузочный тест: 10 000 RPS агент не блокируется > 1 мс на instrumentation | **DONE** | `P11` |
---

## 2. Уровень 2 — CRITICAL (2–3 месяца)

### 2.1. Цель уровня

Довести observability до продакшен-критического минимума: добавить слои **Security** (§3.4 `ARCHITECT.md`), **Eval/Quality** (§3.3) и **Drift Detection**, ввести tiered storage (Hot/Warm/Cold). Без этого уровня нельзя пускать агента на реальных пользователей — нет защиты от PII-утечки, нет оценки качества ответов, нет раннего обнаружения дрейфа. Этот уровень воспроизводит и расширяет §12.2 `ARCHITECT.md` (v1).

Слой Metrics расширяется ML-качеством (`faithfulness_score`, `hallucination_rate`), дрифтом (`embedding_drift_score`) и стоимостью (`budget_utilization_ratio`). Адаптивное сэмплирование закладывается в архитектуру, но полностью динамическая rate переносится на уровень 3.

### 2.2. Эпики и задачи

Уровень CRITICAL включает 8 эпиков (E2.1–E2.8), всего 38 задач. Все тикеты покрыты готовыми промптами в `CRITICAL-PROMPTS.md` (PC0 мастер-контекст + PC01–PC36 тикет-промпты + PC37 верификация + PC38 шаблон фиксов). Статус всех задач — TODO (старт после финальной приёмки MVP по live-стенду).

#### E2.1 — Guardrail Engine (PII detection + injection detection)

**ТРИЗ-обоснование.** Стандарт 2.2.1 (переход к внутреннему комплексу веполя): guardrail внутри SDK, не внешний WAF. Устраняет окно между проверкой и записью, где PII может утечь (антипаттерн §8.3).


**DoD.** Любой PII в промпте маскируется до записи в ring buffer. Injection с score > 0.85 блокирует вызов LLM и пишет audit-event.


**KPI.** Latency guardrail: < 5 мс p99. PII recall: > 95% на тестовом датасете. Injection F1: > 0.90.


**Задачи:**


| Тикет | Описание | Статус | Промпт |
|---|---|---|---|
| T2.1.1 | PIIDetector с регулярками для email/phone/INN/passport/payment | **TODO** | `PC04` |
| T2.1.2 | Интеграция CRF-модели (Presidio) для медицинского PII и ФИО | **TODO** | `PC05` |
| T2.1.3 | Интеграция deberta-v3-base-prompt-injection для injection-детекции | **TODO** | `PC06` |
| T2.1.4 | GuardrailEngine.check_input(text) -> GuardrailVerdict с score, verdict (clean/flag/block) | **TODO** | `PC07` |
| T2.1.5 | Встроить guardrail в _enqueue() SDK: PII-маскинг синхронно, до любой передачи | **TODO** | `PC08` |
#### E2.2 — Field-aware PII masking (по типу поля)

**ТРИЗ-обоснование.** Принцип 3 (местное качество): разные части span'а выполняют разные функции — system_prompt это контракт, user_message это приватный ввод. Blanket-маскинг снижает debuggability без увеличения privacy.


**DoD.** На span'е llm.call атрибут pii.redacted_fields содержит список замаскированных полей. system_prompt виден в UI полностью.


**KPI.** Debuggability score: ≥ 90% случаев можно дебажить без vault recovery.


**Задачи:**


| Тикет | Описание | Статус | Промпт |
|---|---|---|---|
| T2.2.1 | FieldMasker с конфигом по типам полей (YAML: system_prompt=no_mask, user_message=full, tool_output=partial) | **TODO** | `PC14` |
| T2.2.2 | Для tool_output: парсить известные tool-схемы (SQL колонки, REST JSON-path), маскировать только PII-поля | **TODO** | `PC15` |
| T2.2.3 | Сохранять pii.redacted_fields = ['user_message.email.5f3a', ...] в атрибутах span'а | **TODO** | `PC16` |
| T2.2.4 | Тест: debug-сессия восстанавливает контекст без PII на 90% (выборка 100 трейсов) | **TODO** | `PC17` |
#### E2.3 — Vault для PII-восстановления через MFA

**ТРИЗ-обоснование.** Принцип 26 (копирование): оригинал PII заменяется маской, оригинал хранится отдельно. Стандарт 5.1.1: masked в продакшен-логах, оригинал в vault'е.


**DoD.** Инженер с MFA может восстановить PII по маске [EMAIL:5f3a] за < 30 секунд. Без MFA — отказ.


**KPI.** Recovery success rate: 100% для авторизованных. Время жизни PII в vault: ≤ 24 часа.


**Задачи:**


| Тикет | Описание | Статус | Промпт |
|---|---|---|---|
| T2.3.1 | Развёртывание HashiCorp Vault (или аналог) с auto-unseal | **TODO** | `PC02` |
| T2.3.2 | VaultClient.store(mask, original, ttl=24h) и VaultClient.recover(mask, mfa_token) | **TODO** | `PC18` |
| T2.3.3 | Интеграция TOTP MFA (pyotp) для recovery | **TODO** | `PC19` |
| T2.3.4 | Логирование каждого recovery в audit trail (кто, когда, какой mask, по какой причине) | **TODO** | `PC20` |
| T2.3.5 | Cron-джоб для очистки истёкших TTL | **TODO** | `PC21` |
#### E2.4 — Async eval-pipeline с late annotation

**ТРИЗ-обоснование.** Принцип 9 (предварительное противопоставление): precompute embeddings golden-ответов офлайн. Принцип 24 (посредник): асинхронный eval-worker через очередь — пользователь получает ответ сразу.


**DoD.** Пользователь получает ответ за то же время, что и без eval. В UI через 5–30 секунд появляется eval-результат с reasoning.


**KPI.** Latency добавленная для пользователя: 0 мс. Latency eval: < 30 секунд p95.


**Задачи:**


| Тикет | Описание | Статус | Промпт |
|---|---|---|---|
| T2.4.1 | Развёртывание Redis + RQ workers (отдельный процесс, не в агенте) | **TODO** | `PC03` |
| T2.4.2 | RuleBasedEvaluator (regex, JSON-schema, blacklist) — синхронно | **TODO** | `PC09` |
| T2.4.3 | LLMJudgeEvaluator с промптами для faithfulness, answer_relevancy, completeness | **TODO** | `PC10` |
| T2.4.4 | EmbeddingEvaluator — cosine similarity до golden-ответа | **TODO** | `PC11` |
| T2.4.5 | Late annotation: после завершения эвал-джобы, результат пишется в Hot store (ClickHouse) с trace_id ключом | **TODO** | `PC12` |
| T2.4.6 | UI: показать eval-результат в trace-viewer в отдельной панели (если ещё не пришёл — показать «pending») | **TODO** | `PC13` |
#### E2.5 — Drift detector на embeddings

**ТРИЗ-обоснование.** Стандарт 1.1.5 (замена вещества полем): ручная проверка → автоматический embedding-detection аномалий. Принцип 15 (динамичность): baseline-окно скользит, не фиксируется раз и навсегда.


**DoD.** При дрейфе ответов alert приходит < 15 минут после начала дрейфа.


**KPI.** Drift detection latency: < 15 минут. False positive rate: < 5% (по историческим данным).


**Задачи:**


| Тикет | Описание | Статус | Промпт |
|---|---|---|---|
| T2.5.1 | Хранить embeddings ответов в Hot store (ClickHouse column response_embedding Array(Float32)) | **TODO** | `PC24` |
| T2.5.2 | Cron-джоб: каждые 15 минут вычислять KL-дивергенцию последнего часа vs baseline (7д минус последний час) | **TODO** | `PC25` |
| T2.5.3 | Подобрать порог: p99 KL за последние 30 дней = alert threshold | **TODO** | `PC26` |
| T2.5.4 | Alert в Alertmanager с лейблами agent_id, kl_divergence, baseline_window | **TODO** | `PC27` |
| T2.5.5 | Интеграция с Phoenix UMAP-визуализатором для root-cause | **TODO** | `PC28` |
#### E2.6 — Hot/Warm/Cold tiering storage

**ТРИЗ-обоснование.** Принцип 18 (механические колебания): tiered retention с колеблющейся детализацией — 14 дней full, 90 дней structure+metrics, 1 год агрегаты. Принцип 17 (переход в другое измерение): трейс как многомерная структура.


**DoD.** Запрос за последние 14 дней — < 1 секунды. Запрос за 90 дней — < 5 секунд. Запрос за 1 год — < 1 минуты (через Athena).


**KPI.** Hot query latency: < 1 с p95. Storage cost: < $0.5/GB/месяц (Hot + Warm + Cold с учётом сжатия).


**Задачи:**


| Тикет | Описание | Статус | Промпт |
|---|---|---|---|
| T2.6.1 | Развёртывание ClickHouse с шардированием по agent_id и tenant_id | **TODO** | `PC01` |
| T2.6.2 | Развёртывание Postgres для Warm (join с user/tenant сущностями) | **TODO** | `PC01` |
| T2.6.3 | Развёртывание S3 + Parquet для Cold (Athena для запросов) | **TODO** | `PC01` |
| T2.6.4 | Migration-джоб: ежедневно переносит трейсы старше 14 дней из Hot в Warm (со сжатием — удаляем llm.input_text, оставляем llm.input_chars и хэш) | **TODO** | `PC22` |
| T2.6.5 | Migration-джоб: еженедельно переносит трейсы старше 90 дней из Warm в Cold (только агрегаты) | **TODO** | `PC23` |
#### E2.7 — Adaptive sampling по load/error-rate

**ТРИЗ-обоснование.** Принцип 15 (динамичность): характеристики сэмплера меняются так, чтобы быть оптимальными в каждом режиме. Стандарт 2.2.4 (динамизированный веполь): поле observability адаптируется к обстановке.


**DoD.** При CPU > 80% rate автоматически падает до 5%, алерт не срабатывает. При росте error_rate > 5% rate растёт до 30%.


**KPI.** Overhead reduction при пиках: 50% (по сравнению с фиксированной 10% rate).


**Задачи:**


| Тикет | Описание | Статус | Промпт |
|---|---|---|---|
| T2.7.1 | Экспортировать метрики system_cpu_ratio, agent_error_rate_5m в sampler | **TODO** | `PC29` |
| T2.7.2 | Реализовать policy: if cpu>0.8: rate=0.05; elif error_rate>0.05: rate=0.3; else: rate=0.1 | **TODO** | `PC30` |
| T2.7.3 | Логировать изменения rate в audit trail | **TODO** | `PC31` |
| T2.7.4 | Метрика tail_sampler_current_rate для мониторинга | **TODO** | `PC32` |
#### E2.8 — Compliance-catalog как побочный продукт PII-маскинга

**ТРИЗ-обоснование.** Принцип 22 (превращение вреда в пользу): PII-маскирование даёт готовый GDPR Data Map, за который иначе пришлось бы платить консультантам.


**DoD.** При запросе от DPO команда может за 5 минут выгрузить GDPR Data Map.


**KPI.** Time-to-GDPR-report: < 5 минут (вместо недель при ручном подходе).


**Задачи:**


| Тикет | Описание | Статус | Промпт |
|---|---|---|---|
| T2.8.1 | При каждом маскировании писать запись в compliance_catalog (агент, tool, field, PII type, frequency) | **TODO** | `PC33` |
| T2.8.2 | Cron-джоб: ежедневная агрегация каталога | **TODO** | `PC34` |
| T2.8.3 | Экспорт в формате GDPR Data Map (Excel/CSV) | **TODO** | `PC35` |
| T2.8.4 | UI: страница «Data Map» в observability UI | **TODO** | `PC36` |
---

## 3. Уровень 3 — production-ready (6 месяцев)

### 3.1. Цель уровня

Довести observability до полноценного production-grade: внедрить все оставшиеся ТРИЗ-улучшения из §11.1 `ARCHITECT.md`, не требующие переписывания SDK-архитектуры (sidecar/federation — это уровень 4). После этого уровня система соответствует всем нефункциональным требованиям из §1.4 `ARCHITECT.md`: overhead < 2 мс p99, потеря трейсов < 0.1%, доступность 99.9%, retention 14д/90д/1г.

Уровень затрагивает ТРИЗ-улучшения #1, #2, #4, #8, #9, #10 из §11.1 — то есть всё, что касается оптимизации производительности и storage.

### 3.2. Эпики и задачи

Уровень production-ready включает 8 эпиков (E3.1–E3.8), всего 33 задачи. Готовые промпты (`PRODUCTION-PROMPTS.md`) будут созданы по мере приближения к этому уровню, после приёмки CRITICAL. Статус всех задач — PLANNED.

#### E3.1 — Tiered adaptive sampling с динамической rate

**ТРИЗ-обоснование.** Принцип 16 (частичное или избыточное действие): 100% для «интересных», 5% для обычных. Принцип 25: span-агрегация. Стандарт 2.2.4 (динамизированный веполь).


**DoD.** При падении faithfulness ниже 0.7 доля сохранённых полных промптов автоматически растёт до 30%, без ручного вмешательства.


**KPI.** Overhead reduction: 70% от базового (по §11.4). Storage cost reduction: 70% для обычных трейсов.


**Задачи:**


| Тикет | Описание | Статус | Промпт |
|---|---|---|---|
| T3.1.1 | Расширить policy engine: условие eval.faithfulness < 0.7 → поднять content-sampling до 30% | **PLANNED** | — |
| T3.1.2 | Feedback loop: метрика eval_score_avg_5m → в policy engine | **PLANNED** | — |
| T3.1.3 | Логировать все изменения политики в audit trail | **PLANNED** | — |
| T3.1.4 | Дашборд «Sampler Policy History» — график rate по времени с аннотациями причин | **PLANNED** | — |
| T3.1.5 | Метрика tail_sampler_kept_ratio_by_reason{reason="error|cost|eval_fail|random"} | **PLANNED** | — |
#### E3.2 — Span-aggregation в SDK (micro-spans внутри одного span)

**ТРИЗ-обоснование.** Принцип 25 (самообслуживание): SDK сам агрегирует микроспаны. Принцип 28: вместо множества мелких sync-вызовов — один batched-агрегат.


**DoD.** Трейс с 50 reasoning-шагами отправляется как 1 span + 50 events вместо 50 span'ов. Сетевой трафик сокращается на 60%.


**KPI.** Network traffic reduction: ≥ 60%. Span/sec to collector: ≤ 20% от базового.


**Задачи:**


| Тикет | Описание | Статус | Промпт |
|---|---|---|---|
| T3.2.1 | SpanAggregator в SDK: собирает микроспаны до 50 штук или до 1 секунды, сливает в один | **PLANNED** | — |
| T3.2.2 | Embedded events как массив events: [{name, ts, attrs}, ...] в агрегированном span'е | **PLANNED** | — |
| T3.2.3 | UI: поддержка expandable events в trace-viewer | **PLANNED** | — |
| T3.2.4 | Метрика spans_aggregated_total и bytes_saved_total | **PLANNED** | — |
#### E3.3 — Adaptive eval-sampling (день/ночь/инцидент)

**ТРИЗ-обоснование.** Принцип 15 (динамичность): характеристики eval-pipeline меняются по режиму. Ночью ресурс дешевле — больше eval, днём пик — меньше.


**DoD.** Eval cost снижается на 40% (по §11.4) без потери качества мониторинга.


**KPI.** Eval cost reduction: ≥ 40%. Coverage в инциденты: 50% (вместо 10%).


**Задачи:**


| Тикет | Описание | Статус | Промпт |
|---|---|---|---|
| T3.3.1 | Расширить policy engine: time_of_day, current_rps, error_rate_5m → eval_sampling_rate | **PLANNED** | — |
| T3.3.2 | Scheduler: ночь 00:00–06:00 — 30%, день 10:00–18:00 — 5%, остальное — 10% | **PLANNED** | — |
| T3.3.3 | При error_rate > 0.05 или faithfulness < 0.7 — поднять до 50% | **PLANNED** | — |
| T3.3.4 | Метрика eval_sampling_rate_current и eval_jobs_executed_total | **PLANNED** | — |
#### E3.4 — Tree + compression для длинных трейсов

**ТРИЗ-обоснование.** Принцип 17 (переход в другое измерение): от одномерного массива span'ов к двумерной структуре (tree + content с приоритетами). Принцип 7 (матрёшка): span содержит вложенный микро-трейс.


**DoD.** Трейс с 50 шагами занимает в 4 раза меньше storage, чем в уровне 2. Сквозная навигация по tree сохранена.


**KPI.** Storage reduction для long traces: ≥ 75% (4×). UI trace load time: < 2 секунд для 50-шагового трейса.


**Задачи:**


| Тикет | Описание | Статус | Промпт |
|---|---|---|---|
| T3.4.1 | TraceTree структура в Hot store (ClickHouse): parent_id, depth, priority | **PLANNED** | — |
| T3.4.2 | Для каждого span'а вычислять priority (0–1) на основе: error? high cost? low eval? rare tool? | **PLANNED** | — |
| T3.4.3 | Migration-джоб: для трейсов старше 1 часа удалять llm.input_text и llm.output_text у span'ов с priority < 0.5, оставлять llm.input_chars + хэш | **PLANNED** | — |
| T3.4.4 | UI: показывать «compressed» badge на span'ах без полного текста | **PLANNED** | — |
#### E3.5 — Tiered retention с on-demand drill-down

**ТРИЗ-обоснование.** Принцип 18 (механические колебания): tiered-retention с колеблющейся детализацией — пользователь видит «глубину погружения», подгружается по требованию.


**DoD.** Пользователь может посмотреть полный трейс 30-дневной давности за < 30 секунд, не покидая UI.


**KPI.** Drill-down latency: < 30 секунд p95. Cost: хранение 1 года при cost 14 дней full + S3 archives.


**Задачи:**


| Тикет | Описание | Статус | Промпт |
|---|---|---|---|
| T3.5.1 | API endpoint GET /traces/{trace_id}?detail=auto|full|aggregate | **PLANNED** | — |
| T3.5.2 | При detail=full для трейса старше 14 дней: запрос к Athena, кэш результата в Redis на 1 час | **PLANNED** | — |
| T3.5.3 | UI: кнопка «Drill down» в trace-viewer, показывать progress bar | **PLANNED** | — |
| T3.5.4 | Метрика drill_down_request_count, drill_down_latency_seconds | **PLANNED** | — |
#### E3.6 — Embedded subtree в span'е (матрёшка)

**ТРИЗ-обоснование.** Принцип 7 (матрёшка): micro-трейс размещается внутри span'а.


**DoD.** Span с 10 дочерними хранится как 1 строка в ClickHouse вместо 10. При клике в UI — разворачивается.


**KPI.** ClickHouse rows reduction: ≥ 80% для трейсов с > 5 дочерними.


**Задачи:**


| Тикет | Описание | Статус | Промпт |
|---|---|---|---|
| T3.6.1 | Расширить схему span'а: compressed_subtree: Optional[bytes] (protobuf-serialized) | **PLANNED** | — |
| T3.6.2 | SDK: если span имеет > 5 дочерних, агрегировать их в compressed_subtree | **PLANNED** | — |
| T3.6.3 | UI: expandable subtree в trace-viewer | **PLANNED** | — |
| T3.6.4 | Метрика compressed_subtree_count, bytes_saved_by_subtree | **PLANNED** | — |
#### E3.7 — SLO/SLA observability с dashboards и escalation

**ТРИЗ-обоснование.** Принцип 25 (самообслуживание): SLO-система сама отслеживает и эскалирует без ручного вмешательства.


**DoD.** Все 4 SLO имеют alerting. При срабатывании alert доходит до on-call инженера < 1 минуты.


**KPI.** Alert delivery latency: < 1 минута. SLO burn rate visibility: 100% SLO отображаются в UI.


**Задачи:**


| Тикет | Описание | Статус | Промпт |
|---|---|---|---|
| T3.7.1 | Создать 4 SLO-дэшборда в Grafana (или Langfuse UI): availability, latency, quality, cost | **PLANNED** | — |
| T3.7.2 | Alerting rules: каждый SLO имеет 3 уровня (warning 80%, critical 95%, burn 100%) | **PLANNED** | — |
| T3.7.3 | Escalation policy: warning → Slack, critical → PagerDuty (P2), burn → PagerDuty (P1) + CTO | **PLANNED** | — |
| T3.7.4 | SLO review meeting: ежемесячная калибровка порогов | **PLANNED** | — |
#### E3.8 — Multi-tenant изоляция + RBAC для UI

**ТРИЗ-обоснование.** Принцип 3 (местное качество): разные части observability UI (trace-viewer, vault-recovery, policy editor) выполняют разные функции и должны быть в разных условиях доступа.


**DoD.** Пользователь tenant A не может получить доступ к данным tenant B (автоматизированный тест).


**KPI.** Tenant isolation test: 100% pass. RBAC enforcement: 100% actions логируются.


**Задачи:**


| Тикет | Описание | Статус | Промпт |
|---|---|---|---|
| T3.8.1 | Добавить tenant_id во все span'ы и во все таблицы ClickHouse/Postgres | **PLANNED** | — |
| T3.8.2 | RBAC: viewer (только просмотр), engineer (просмотр + PII recovery via MFA), admin (+ policy edit) | **PLANNED** | — |
| T3.8.3 | UI: SSO-интеграция (OIDC), принудительный tenant filter | **PLANNED** | — |
| T3.8.4 | Audit trail: логирование всех действий пользователя в UI | **PLANNED** | — |
---

## 4. Уровень 4 — Улучшения production-ready (12 месяцев)

### 4.1. Цель уровня

Реализовать целевую ТРИЗ-архитектуру из §11 `ARCHITECT.md` в полном объёме. Это уровень стратегических улучшений: sidecar-SDK с тонкими клиентами на нескольких языках, protobuf-first подход с codegen, federation для организации агентов, cost-aware routing, и PoC-уровневые шаги к Идеальной Конечной Цели (ИКР) из §10.6: LLM-intrinsic trace, implicit eval, compact-embedding reconstruction.

После этого уровня идеальность системы достигает 3.6 (рост в 4.3× от базовой 0.83), что соответствует расчётам из §11.4 `ARCHITECT.md`. Уровень затрагивает ТРИЗ-улучшения #11, #12, #13, #14, #15.

### 4.2. Эпики и задачи

Уровень Улучшения включает 7 эпиков (E4.1–E4.7), всего 34 задачи (из них 11 — PoC-уровневые исследования). Готовые промпты (`IMPROVEMENTS-PROMPTS.md`) будут созданы после приёмки production-ready. Статус всех задач — PLANNED или PoC.

#### E4.1 — Sidecar-based SDK + thin language clients

**ТРИЗ-обоснование.** Принцип 2 (вынесение): объект (SDK-логика) выносится из системы (агент-процесса) в sidecar-процесс. В каждом языке остаётся только тонкий клиент.


**DoD.** Python/JS/Go агенты используют один и тот же sidecar. Thin client на каждом языке — < 500 строк кода.


**KPI.** Поддержка 3+ языков без переписывания SDK-логики. Thin client LOC: < 500 на язык. SDK-логика централизована в sidecar.


**Задачи:**


| Тикет | Описание | Статус | Промпт |
|---|---|---|---|
| T4.1.1 | Sidecar-процесс на Rust или Go (gRPC server, batcher, exporter, guardrail-plugin) | **PLANNED** | — |
| T4.1.2 | Thin client на Python — ~200 строк, замещает текущий SDK (контракт декораторов сохраняется) | **PLANNED** | — |
| T4.1.3 | Thin client на JavaScript/TypeScript для Node.js | **PLANNED** | — |
| T4.1.4 | Thin client на Go | **PLANNED** | — |
| T4.1.5 | Deploy sidecar как DaemonSet в Kubernetes (один sidecar на узел, разделяется между агентами) | **PLANNED** | — |
| T4.1.6 | Migration path: thin client использует тот же декоратор API, что и старый SDK — агент не меняет код | **PLANNED** | — |
#### E4.2 — Protobuf-first с codegen для всех слоёв

**ТРИЗ-обоснование.** Принцип 5 (объединение): единый OTLP-протокол как contract. Принцип 12 (эквипотенциальность): все слои производные, не противоречат друг другу.


**DoD.** Изменение protobuf-схемы автоматически обновляет все слои без ручного редактирования кода.


**KPI.** Time-to-add-field: < 1 час (proto edit + codegen + тест). Schema inconsistency incidents: 0.


**Задачи:**


| Тикет | Описание | Статус | Промпт |
|---|---|---|---|
| T4.2.1 | Определить финальную protobuf-схему span.proto со всеми LLM-специфичными полями | **PLANNED** | — |
| T4.2.2 | Codegen для Python, JS, Go thin clients (из proto) | **PLANNED** | — |
| T4.2.3 | Codegen для ClickHouse DDL (миграции) | **PLANNED** | — |
| T4.2.4 | Codegen для валидаторов в OTel Collector | **PLANNED** | — |
| T4.2.5 | CI: любое изменение proto запускает codegen + тесты на всех языках | **PLANNED** | — |
| T4.2.6 | Документация: «Как изменить схему span'а» — пошаговый гайд | **PLANNED** | — |
#### E4.3 — Cost-aware model routing

**ТРИЗ-обоснование.** ИКР (§10.6): «стоимость учитывается в момент выбора модели агентом (cost-aware routing)». Cost перестаёт быть отдельным наблюдаемым параметром — встроен в decision logic.


**DoD.** Агент не может превысить tenant budget на 20%+. Cost-aware decision виден в трейсе.


**KPI.** Budget violation incidents: 0 (hard-limit срабатывает). Cost savings от routing: 20–30% (по сравнению с always-premium-model).


**Задачи:**


| Тикет | Описание | Статус | Промпт |
|---|---|---|---|
| T4.3.1 | CostAwareRouter в sidecar: select_model(task, budget_context) -> model | **PLANNED** | — |
| T4.3.2 | Budget enforcement: trace ($0.10), user ($5/day), tenant ($1000/month) | **PLANNED** | — |
| T4.3.3 | Soft-limit: при 100% trace budget → переключение на дешёвую модель | **PLANNED** | — |
| T4.3.4 | Hard-limit: при 120% tenant budget → отказ в обслуживании | **PLANNED** | — |
| T4.3.5 | UI: показать «cost-aware decision» в trace-viewer (почему выбрана эта модель) | **PLANNED** | — |
#### E4.4 — Federation: tenant-level trace map

**ТРИЗ-обоснование.** Стандарт 3.1.1 (переход к макроуровню): переход от системы (один агент) к надсистеме (организация агентов).


**DoD.** В UI видна карта всех взаимодействий пользователя со всеми агентами за период.


**KPI.** Cross-agent trace correlation coverage: 100% для A2A-вызовов.


**Задачи:**


| Тикет | Описание | Статус | Промпт |
|---|---|---|---|
| T4.4.1 | Ввести federation_trace_id — связывает трейсы разных агентов одного пользователя за сессию | **PLANNED** | — |
| T4.4.2 | FederationLayer в API: GET /federation/user/{user_id}/traces возвращает связанные трейсы | **PLANNED** | — |
| T4.4.3 | UI: «User Journey» view — карта всех трейсов пользователя за период | **PLANNED** | — |
| T4.4.4 | Cross-agent correlation: если agent A вызвал agent B через A2A-протокол, их трейсы связываются | **PLANNED** | — |
#### E4.5 — LLM-intrinsic trace (PoC)

**ТРИЗ-обоснование.** ИКР (§10.6): «идеальная observability-система не имеет отдельных компонентов». LLM-intrinsic trace — устранение SDK как отдельного компонента: трейс генерируется самой моделью. Стандарт 1.1.1 (синтез веполя).


**DoD.** PoC завершён, документ с результатами и рекомендацией.


**KPI.** Intrinsic trace overhead: 0% (по сравнению с SDK). Качество трейса: ≥ 80% от SDK-trace.


**Задачи:**


| Тикет | Описание | Статус | Промпт |
|---|---|---|---|
| T4.5.1 | Разработать system prompt, заставляющий LLM выводить JSON-трейс вместе с ответом | **PoC** | — |
| T4.5.2 | Реализовать парсер intrinsic trace → standard span format | **PoC** | — |
| T4.5.3 | Сравнить overhead: intrinsic trace vs SDK instrumentation | **PoC** | — |
| T4.5.4 | Сравнить качество: faithfulness у intrinsic vs SDK | **PoC** | — |
| T4.5.5 | Решение: продакшен-внедрение или нет (по результатам PoC) | **PoC** | — |
#### E4.6 — Implicit eval (качество через факт следующего действия пользователя)

**ТРИЗ-обоснование.** ИКР (§10.6): «качество оценивается самим фактом успешности следующего действия пользователя (implicit eval)». Устраняет отдельный eval-слой.


**DoD.** Каждый трейс имеет eval.implicit_score без дополнительной LLM-cost.


**KPI.** Implicit eval coverage: 100% трейсов с продолжением диалога. Согласованность с explicit eval: ≥ 70%.


**Задачи:**


| Тикет | Описание | Статус | Промпт |
|---|---|---|---|
| T4.6.1 | ImplicitEvalDetector: классифицирует следующее сообщение пользователя (positive/neutral/negative) | **PLANNED** | — |
| T4.6.2 | Атрибут span'а eval.implicit_score — -1 (negative), 0 (neutral), +1 (positive) | **PLANNED** | — |
| T4.6.3 | Корреляция с explicit eval (LLM-as-judge): метрика согласованности | **PLANNED** | — |
| T4.6.4 | Если implicit score стабильно расходится с explicit — alert (drift в eval-judge) | **PLANNED** | — |
#### E4.7 — Compact-embedding reconstruction

**ТРИЗ-обоснование.** ИКР (§10.6): «storage не нужен, т.к. трейсы „сжимаются" в compact-embedding'и, по которым восстанавливается дерево». Устраняет storage как отдельный слой.


**DoD.** PoC завершён, отчёт с результатами.


**KPI.** Storage reduction: 100× (если PoC удачен). Reconstruction accuracy: ≥ 80%.


**Задачи:**


| Тикет | Описание | Статус | Промпт |
|---|---|---|---|
| T4.7.1 | Обучить модель encoding: trace tree → fixed-size embedding (например, 1024 dims) | **PoC** | — |
| T4.7.2 | Обучить модель decoding: embedding → approximate trace tree | **PoC** | — |
| T4.7.3 | PoC: хранить только embedding + ключевые атрибуты, восстанавливать дерево по требованию | **PoC** | — |
| T4.7.4 | Сравнить storage: full trace vs compact embedding | **PoC** | — |
---

## 5. ТРИЗ-карта: соответствие 15 улучшений уровням

Все 15 улучшений из §11.1 `ARCHITECT.md` распределены по уровням roadmap без пропусков и без противоречий с исходной архитектурой. Каждое улучшение привязано к уровню, на котором его реализация становится рентабельной и не блокирует предыдущие уровни.

| Улучшение (§11.1) | ТРИЗ-приём | Противоречие (§10.3) | Уровень | Обоснование уровня |
|---|---|---|---|---|
| #1 Tiered adaptive sampling с динамической rate | 16, 25, Стандарт 2.2.4 | ТП-1 | production-ready | Требует метрик load/error-rate из CRITICAL |
| #2 Span-aggregation в SDK | 25, 28 | ТП-1 | production-ready | Требует стабильного базового формата span из MVP |
| #3 Async eval с late annotation | 9, 24 | ТП-2 | CRITICAL | Без eval нет смысла в late annotation; базис — трейсы MVP |
| #4 Adaptive eval-sampling | 15 | ТП-2 | production-ready | Требует baseline эвал-метрик из CRITICAL |
| #5 Vault-based PII с MFA recovery | 26, 3, Стандарт 5.1.1 | ТП-3 | CRITICAL | Без PII-маскинга нельзя пускать в production |
| #6 Field-aware PII masking | 3 | ТП-3 | CRITICAL | Базис для Compliance-catalog |
| #7 Compliance-catalog как побочный продукт | 22 | ТП-3 | CRITICAL | Дёшево, если #5 и #6 уже сделаны |
| #8 Tree + compression для длинных трейсов | 17, 7 | ТП-4 | production-ready | MVP и CRITICAL работают с линейными трейсами |
| #9 Tiered-retention с on-demand drill-down | 18 | ТП-4 | production-ready | Требует Hot/Warm/Cold из CRITICAL |
| #10 Embedded subtree (матрёшка) | 7 | ТП-4 | production-ready | Зависит от #8 |
| #11 Sidecar-SDK + thin language clients | 2 | ТП-5 | Улучшения | Дорогая миграция; оправдана только когда SDK стабилен |
| #12 Protobuf-first с codegen | 5, 12 | ТП-5 | Улучшения | Должно идти параллельно с #11 |
| #13 LLM-intrinsic trace | ИКР, Стандарт 1.1.1 | — | Улучшения (PoC) | Требует зрелой eval-инфраструктуры |
| #14 Cost-aware model routing | ИКР | — | Улучшения | Требует cost-слоя из MVP и eval-слоя из CRITICAL |
| #15 Federation: tenant-level trace map | Стандарт 3.1.1 | — | Улучшения | Имеет смысл только при наличии 3+ агентов в tenant |

**Контрольное утверждение:** ни одна задача уровня N не противоречит решениям уровня N-1. Уровень 4 (Улучшения) заменяет реализации уровня 1–3 на более идеальные, но сохраняет контракты (декораторы SDK, OTLP-транспорт, схемы данных из §7 `ARCHITECT.md`).

---

## 6. Сквозная KPI-карта

Сводная таблица всех KPI по всем уровням — позволяет стейкхолдеру увидеть прогресс от MVP к финальному состоянию. Каждая строка — отдельный KPI, столбцы — значения на каждом уровне.

| KPI | MVP | CRITICAL | production-ready | Улучшения |
|---|---|---|---|---|
| **Overhead p99** | < 5% CPU | < 3% CPU | < 2 мс, < 5% CPU | < 0.6 мс, < 1% CPU |
| **Drop rate (норма)** | 0% | 0% | < 0.1% | < 0.01% |
| **Drop rate (пики)** | < 5% | < 1% | < 0.1% | < 0.05% |
| **Latency в UI** | < 5 с p95 | < 3 с p95 | < 2 с p95 | < 1 с p95 |
| **Retention hot** | 14 дней | 14 дней | 14 дней | 14 дней |
| **Retention warm** | — | 90 дней | 90 дней (+ drill-down) | 90 дней |
| **Retention cold** | — | 1 год (агрегаты) | 1 год (+ on-demand) | 1 год (+ compact-embedding PoC) |
| **Доступность** | 95% | 99% | 99.9% | 99.95% |
| **PII recall** | — | > 95% | > 97% | > 99% |
| **PII leak incidents** | 0 (только dev) | 0 | 0 | 0 |
| **Eval coverage (LLM-judge)** | 0% | 10% | 5–30% (adaptive) | + 100% implicit |
| **Eval latency (late annotation)** | — | < 30 с p95 | < 20 с p95 | < 10 с p95 |
| **Drift detection latency** | — | < 15 минут | < 10 минут | < 5 минут |
| **Storage cost (на трейс)** | baseline | -30% (tiering) | -75% (tree+compression) | -90% (+ compact-embedding PoC) |
| **Network traffic (на трейс)** | baseline | -30% | -60% (aggregation) | -70% (sidecar) |
| **Cost accuracy (vs биллинг)** | ±5% | ±3% | ±2% | ±1% |
| **Budget violations** | — | — | — | 0 (hard-limit) |
| **SLO coverage** | 0/4 | 4/4 (alerting) | 4/4 (full observability) | 4/4 (+ cost-aware) |
| **MTTR** | — | < 30 минут | < 15 минут | < 10 минут |
| **Time-to-GDPR-report** | — | < 5 минут | < 5 минут | < 5 минут |
| **Multi-tenant isolation** | — | — | 100% | 100% (+ federation) |
| **Поддерживаемые языки** | Python | Python | Python | Python, JS, Go |
| **Tenant federation** | — | — | — | Cross-agent trace map |
| **Идеальность (по §10.2)** | 0.83 | 1.5 | 2.5 | 3.6 |

**Динамика идеальности:** 0.83 → 1.5 → 2.5 → 3.6 = рост в **4.3×** (по §11.4 `ARCHITECT.md`).

---

## 7. Реестр рисков (по всем уровням)

### 7.1. MVP (§4.6 ROADMAP)

| ID | Риск | Likelihood | Impact | Митигация | Статус |
|---|---|---|---|---|---|
| R1.1 | Langfuse self-hosted падает под нагрузкой | Medium | High | E1.6: fire-and-forget с ring buffer; Langfuse не блокирует агента | **покрыто** (P07–P10, C6) |
| R1.2 | Cost-расчёт расходится с биллингом > 10% | Medium | Medium | E1.4: версионированный price_book + weekly reconciliation job | **покрыто** (P12–P16) |
| R1.3 | SDK ломает async-поведение агента (event loop блокировка) | Low | Critical | T1.6.5: нагрузочный тест; все операции SDK — async | **покрыто** (P11, C2) |
| R1.4 | PII попадает в Langfuse на dev-окружении | Medium | High | На MVP — только синтетические данные; полный запрет на production-deploy до уровня 2 | **покрыто** (политика) |
| R1.5 | Storage растёт быстрее 10 ГБ/день | Medium | Medium | E1.3: tail-sampler с 10% normal + content-sampling; alert на daily_storage_growth_gb | **покрыто** (P20–P22) |

### 7.2. CRITICAL (§5.6 ROADMAP)

| ID | Риск | Likelihood | Impact | Митигация | Промпт |
|---|---|---|---|---|---|
| R2.1 | PII-маскинг не покрывает новый тип PII | Medium | Critical | E2.1: регулярное обновление детекторов; аудит раз в квартал | PC04, PC05 |
| R2.2 | LLM-as-judge сам галлюцинирует | Medium | Medium | E2.4: gpt-4o-mini + периодическая калибровка на golden set | PC10 |
| R2.3 | Drift alert всегда горит (false positive) | Medium | High | E2.5: порог по p99 исторических данных, калибровка раз в месяц | PC26 |
| R2.4 | ClickHouse падает под нагрузкой | Low | Critical | E2.6: шардирование, репликация, fallback на Postgres при сбое | PC01 |
| R2.5 | Vault становится SPOF | Low | Critical | E2.3: HA-режим Vault, регулярные backup'ы | PC02 |
| R2.6 | Cost eval-слоя превышает бюджет | Medium | Medium | E2.7: adaptive sampling снижает eval-нагрузку при пиках | PC30 |

### 7.3. production-ready (§6.6 ROADMAP)

| ID | Риск | Likelihood | Impact | Митигация |
|---|---|---|---|---|
| R3.1 | Span-aggregation теряет важные events | Medium | High | E3.2: метрика events_lost_during_aggregation = 0; тесты на golden traces |
| R3.2 | Drill-down queries перегружают Athena | Medium | Medium | E3.5: кэш в Redis, rate-limit на пользователя (10 drill-down/час) |
| R3.3 | Compressed subtree ломает UI trace-viewer | Medium | Medium | E3.6: fallback на linear view, gradual rollout |
| R3.4 | Adaptive eval-sampling пропускает критичный трейс | Low | High | E3.3: 100% eval при error_rate > 5% |
| R3.5 | RBAC-мисконфиг даёт доступ не тому tenant | Low | Critical | E3.8: автоматизированный тест на изоляцию при каждом релизе |
| R3.6 | SLO-пороги слишком агрессивны (false alerts) | Medium | Medium | E3.7: ежемесячная калибровка, weeks-of-history baseline |

### 7.4. Улучшения production-ready (§7.6 ROADMAP)

| ID | Риск | Likelihood | Impact | Митигация |
|---|---|---|---|---|
| R4.1 | Sidecar становится SPOF | Medium | Critical | HA-режим sidecar, fallback на direct SDK |
| R4.2 | Protobuf migration ломает существующие трейсы | Medium | High | T4.2.5: backward compatibility, gradual rollout |
| R4.3 | Cost-aware routing выбирает слишком дешёвую модель, качество падает | Medium | High | T4.3.5: visible decision в трейсе, SLO на качество |
| R4.4 | LLM-intrinsic trace даёт некачественный трейс | High | Medium | E4.5: PoC, не продакшен-обязательство |
| R4.5 | Implicit eval ошибается на сарказме/neutral | Medium | Medium | E4.6: классификатор с confidence threshold |
| R4.6 | Compact-embedding reconstruction не работает | High | Medium | E4.7: PoC, fallback на tiered storage |
| R4.7 | Federation нарушает tenant изоляцию | Low | Critical | E4.4: автоматизированный тест на изоляцию |
| R4.8 | Vendor lock-in на sidecar-реализации | Medium | Medium | T4.1.1: sidecar в open-source, альтернативные реализации |

---

## 8. Rollback-стратегии по уровням

### 8.1. MVP

- **Технический rollback:** `sdk.enabled = False` в конфиге агента → декораторы становятся no-op, агент продолжает работу без observability. Трейсы перестают идти в Langfuse, но бизнес-логика не страдает.
- **Частичный rollback:** отключить один из экспортёров (например, Phoenix, если он добавлен) без отключения Langfuse.
- **Storage rollback:** снизить `tail_sampler_kept_ratio` с 10% до 1% через конфиг OTel Collector без передеплоя агента.
- **Полный демонтаж:** удалить SDK из агента (вернуть коммит), остановить Langfuse и Collector. Никаких миграций данных — на MVP ничего критичного не хранится.

### 8.2. CRITICAL

- **Guardrail rollback:** выключить `guardrail.enabled` в конфиге → SDK пропускает PII-маскинг (только для dev-окружения, не для production!).
- **Eval rollback:** выключить `eval.enabled` → eval-jobs не создаются, late annotation не приходит, бизнес-логика не страдает.
- **Drift rollback:** выключить `drift.enabled` → alert'ы перестают приходить.
- **Storage rollback:** отключить Warm/Cold миграции — данные остаются в Hot 14 дней, потом удаляются (без архива).
- **Compliance-catalog rollback:** отключить генерацию каталога, но маскирование продолжает работать.
- **Частичный rollback уровня:** если какой-то эпик уровня 2 ломает продакшен, откатываем только его, остальные остаются.

### 8.3. production-ready

- **Span-aggregation rollback:** флаг `sdk.aggregation.enabled = False` → микроспаны отправляются как раньше.
- **Adaptive eval rollback:** `eval.adaptive.enabled = False` → фиксированная 10% rate.
- **Tree-compression rollback:** `tree_compression.enabled = False` → все span'ы хранятся linear.
- **Drill-down rollback:** `drill_down.enabled = False` → пользователь видит только Hot (14 дней).
- **RBAC rollback:** временно отключить SSO (только для dev-окружения).
- **SLO rollback:** понизить alerting до warning-only (без critical escalation).

### 8.4. Улучшения production-ready

- **Sidecar rollback:** переключить thin client на direct SDK mode (без sidecar), но с тем же API.
- **Protobuf rollback:** версионирование схемы, old consumers поддерживаются 2 версии назад.
- **Cost-aware routing rollback:** флаг `cost_aware.enabled = False` → всегда используется default-модель.
- **Federation rollback:** флаг `federation.enabled = False` → трейсы не связываются, изоляция сохранена.
- **LLM-intrinsic trace rollback:** это PoC, не в продакшене — rollback не требуется.
- **Implicit eval rollback:** флаг `implicit_eval.enabled = False` → используется только explicit eval.
- **Compact-embedding rollback:** это PoC, не в продакшене — rollback не требуется.
- **Полный уровень 4 rollback:** каждый эпик независимо отключаем, уровень 3 продолжает работать.

---

## 9. Сводка покрытия бэклога

### 9.1. Подсчёт задач по уровням

| Уровень | Эпиков | Задач | Готовых промптов | Статус |
|---|---|---|---|---|
| 1. MVP | 6 (E1.1–E1.6) | 26 (T1.1.1–T1.6.5) | 27 (`MVP-PROMPT.md`: P0, P01–P25, P26, P27) | **DONE** (256/256 тестов PASS) |
| 2. CRITICAL | 8 (E2.1–E2.8) | 38 (T2.1.1–T2.8.4) | 39 (`CRITICAL-PROMPTS.md`: PC0, PC01–PC36, PC37, PC38) | **TODO** (промпты готовы) |
| 3. production-ready | 8 (E3.1–E3.8) | 33 (T3.1.1–T3.8.4) | 0 (`PRODUCTION-PROMPTS.md` будет создан после приёмки CRITICAL) | **PLANNED** |
| 4. Улучшения production-ready | 7 (E4.1–E4.7) | 34 (T4.1.1–T4.7.4, из них 11 PoC) | 0 (`IMPROVEMENTS-PROMPTS.md` будет создан после приёмки production-ready) | **PLANNED / PoC** |
| **ИТОГО** | **29 эпиков** | **131 задача** | **66 готовых промптов** | — |

### 9.2. Подсчёт задач по слоям архитектуры (§3 ARCHITECT.md)

| Слой | Уровень появления | Полная реализация | Задач |
|---|---|---|---|
| Logs & Traces | MVP (E1.1, E1.2, E1.6) | production-ready (E3.2 aggregation, E3.4 tree, E3.6 embedded subtree) | ~30 |
| Metrics | MVP (E1.4 cost) → CRITICAL (E2.5 drift, E2.7 sampling) | production-ready (E3.7 SLO) | ~12 |
| Eval & Quality | CRITICAL (E2.4) | Улучшения (E4.5 intrinsic, E4.6 implicit) | ~16 |
| Security | CRITICAL (E2.1, E2.2, E2.3) | — | ~14 |
| Cost & Budget | MVP (E1.4) → CRITICAL (E2.7 sampling-cost) | Улучшения (E4.3 cost-aware routing) | ~10 |
| Storage tiering | CRITICAL (E2.6) | production-ready (E3.4 tree, E3.5 drill-down) → Улучшения (E4.7 compact-embedding PoC) | ~14 |
| Compliance | CRITICAL (E2.8) | — | 4 |
| Federation | — | Улучшения (E4.4) | 4 |
| Multi-tenancy & RBAC | — | production-ready (E3.8) | 4 |
| Cross-language SDK | — | Улучшения (E4.1 sidecar, E4.2 protobuf) | 12 |
| SLO/SLA | — | production-ready (E3.7) | 4 |
| PoC-исследования (ИКР) | — | Улучшения (E4.5, E4.6, E4.7) | 11 |

### 9.3. Покрытие ТРИЗ-улучшений (§11.1 ARCHITECT.md, 15 шт.)

Все 15 улучшений распределены по уровням (см. §5 ТРИЗ-карта выше). **Ни одно улучшение не пропущено, ни одно не противоречит решениям предыдущего уровня.**

### 9.4. Покрытие 40 приёмов Альтшуллера и 76 стандартов

В проекте применены (через эпики ROADMAP):
- **Принципы:** 2 (вынесение), 3 (местное качество), 5 (объединение), 7 (матрёшка), 9 (предварительное противопоставление), 12 (эквипотенциальность), 15 (динамичность), 16 (частичное действие), 17 (переход в другое измерение), 18 (механические колебания), 22 (вред в пользу), 25 (самообслуживание), 26 (копирование), 28 (замена механической схемы) — **14 из 40 приёмов**.
- **Стандарты:** 1.1.1 (синтез веполя), 1.1.5 (замена вещества полем), 2.2.1 (внутренний комплекс веполя), 2.2.4 (динамизированный веполь), 3.1.1 (переход к макроуровню), 5.1.1 (разрешение физического противоречия в пространстве) — **6 стандартов**.
- **ИКР (идеальная конечная цель):** 4 направления — LLM-intrinsic trace, implicit eval, cost-aware routing (cost как input), compact-embedding reconstruction.

### 9.5. Покрытие KPI (§8 ROADMAP)

Сквозная KPI-карта (см. §6 выше) содержит **24 KPI** по всем уровням. Каждый KPI имеет целевое значение на каждом уровне — позволяет стейкхолдерам отслеживать прогресс без технических деталей.

### 9.6. Что НЕ входит в бэклог (out-of-scope по уровням)

Чтобы избежать расширения scope, явно фиксируем что НЕ делается на каждом уровне:

**MVP out-of-scope (§4.4 ROADMAP):**
- Eval-слой (§3.3): LLM-as-judge, faithfulness, RAGAS — уровень 2.
- Security-слой (§3.4): PII-маскинг, injection-детекция — уровень 2.
- Multi-tenant изоляция — уровень 3.
- Sidecar-SDK — уровень 4.
- Federation — уровень 4.
- Cost-aware routing — уровень 4.
- Drift detector — уровень 2.
- Drill-down UI — уровень 3.

**CRITICAL out-of-scope (§5.4 ROADMAP):**
- Adaptive eval-sampling (день/ночь/инцидент) — уровень 3.
- Tree + compression для длинных трейсов — уровень 3.
- On-demand drill-down в UI — уровень 3.
- Sidecar-SDK — уровень 4.
- Federation — уровень 4.
- Cost-aware routing — уровень 4.
- Multi-tenant RBAC — уровень 3.
- LLM-intrinsic trace — уровень 4.

**production-ready out-of-scope (§6.4 ROADMAP):**
- Sidecar-SDK — уровень 4.
- Protobuf-first с codegen — уровень 4.
- Cost-aware routing — уровень 4.
- Federation — уровень 4.
- LLM-intrinsic trace — уровень 4.
- Implicit eval — уровень 4.
- Compact-embedding reconstruction — уровень 4.

**Улучшения out-of-scope (§7.4 ROADMAP):**
- Полная замена SDK на LLM-intrinsic trace — только PoC на одной модели.
- Полный отказ от storage в пользу compact-embedding — только PoC.
- Полная замена eval на implicit — implicit дополняет explicit, не заменяет.
- Поддержка Java/Rust/C# clients — добавляются по запросу, архитектурно готовы.
- Cross-tenant federation — изоляция tenant'ов сохраняется.

---

## 10. Дальнейшие шаги команды

1. **Финализировать приёмку MVP** по live-стенду (C1, C3, C5-биллинг, C7-latency из `docs/mvp-acceptance.md`). Если провалы — запустить итеративный цикл `P27` (шаблон в `MVP-PROMPT.md`).
2. **Старт CRITICAL-уровня.** Открыть `CRITICAL-PROMPTS.md`, начать с `PC0` (мастер-контекст) в новой сессии ИИ-агента, далее — тикет-промпты по волнам (§0.2 в `CRITICAL-PROMPTS.md`). Рекомендуемая стратегия для команды 3–5 человек — параллельные стримы A/B/C (§0.3).
3. **По завершении CRITICAL** — запустить `PC37` (верификация по 14 exit-criteria), при провалах — `PC38` (шаблон фиксов).
4. **После приёмки CRITICAL** — перенести эпики E3.1–E3.8 в трекер задач (Jira/Linear/YouTrack) с оценкой в story points и назначением ответственных. Создать `PRODUCTION-PROMPTS.md` (план промптов будет зеркалить структуру `CRITICAL-PROMPTS.md`).
5. **По завершении production-ready** — аналогично для E4.1–E4.7 и `IMPROVEMENTS-PROMPTS.md`. PoC-эпики (E4.5, E4.7) выполнять отдельной research-командой, не блокируя основной стрим.
6. **Ежеквартальный аудит** соответствия ТРИЗ-карте (§5 выше) и сквозной KPI-карте (§6) — что ничего не «выпадает» из архитектуры при итеративной реализации.

**Памятка:** `BACKLOG.md` — живой документ, обновляется при каждом завершённом эпике (статус TODO → DONE, добавляется ссылка на отчёт приёмки). Не допускайте расхождения между статусом в этом документе и реальным состоянием кода — это индикатор технического долга.
---

## 11. Зависимости (внешние и внутренние)

### 11.1. Внешние зависимости по уровням (§9.3 ROADMAP)

| Зависимость | Когда нужна | Ответственный | Альтернатива |
|---|---|---|---|
| Langfuse self-hosted | MVP | DevOps | Langfuse Cloud (SaaS) — на уровне MVP, но не продакшен |
| HashiCorp Vault | CRITICAL | DevOps + Security | AWS KMS + DynamoDB |
| ClickHouse | CRITICAL | DevOps | Postgres + TimescaleDB (медленнее) |
| OTel Collector | MVP | DevOps | Self-built collector (не рекомендуется) |
| Phoenix (Arize) | CRITICAL | DevOps | Langfuse UI (без UMAP) |
| S3 / MinIO | CRITICAL | DevOps | Локальный storage (только для dev) |
| Kubernetes | Улучшения | DevOps | Docker Compose (до уровня 4) |
| OIDC SSO | production-ready | Security | Local auth (не для production) |

### 11.2. Внутренние зависимости между задачами

| Зависимость | Тип | Что блокирует |
|---|---|---|
| Python 3.11+ в пилотном агенте | Внешняя | Старт MVP |
| Docker-инфраструктура для Langfuse self-hosted | Инфра | E1.2 (MVP) |
| Доступ к API OpenAI/Anthropic (для cost-трекинга) | Внешняя | E1.4 (MVP) |
| OTel Collector с поддержкой custom processors | Инфра | E1.3 (MVP) |
| Завершённый MVP (уровень 1) | Внутренняя | Старт CRITICAL |
| HashiCorp Vault инфраструктура | Инфра | E2.3 (CRITICAL) |
| Redis + RQ workers | Инфра | E2.4 (CRITICAL) |
| ClickHouse, Postgres, S3 | Инфра | E2.6 (CRITICAL) |
| LLM для eval (gpt-4o-mini) | Внешняя | E2.4 (CRITICAL) |
| Embedding model (text-embedding-3-small) | Внешняя | E2.5 (CRITICAL) |
| Phoenix (Arize) self-hosted | Инфра | E2.5 (CRITICAL) |
| Prompt injection классификатор | Внешняя | E2.1 (CRITICAL) |
| Завершённый уровень 2 (CRITICAL) | Внутренняя | Старт production-ready |
| Athena / Presto для Cold queries | Инфра | E3.5 (production-ready) |
| Grafana или расширенный Langfuse UI | Инфра | E3.7 (production-ready) |
| SSO-провайдер (OIDC) | Внешняя | E3.8 (production-ready) |
| ClickHouse с поддержкой `compressed_subtree` столбца | Инфра | E3.6 (production-ready) |
| Завершённый уровень 3 (production-ready) | Внутренняя | Старт Улучшения |
| Kubernetes-инфраструктура для sidecar DaemonSet | Инфра | E4.1 (Улучшения) |
| Buf / protoc toolchain для codegen | Инфра | E4.2 (Улучшения) |
| A2A-протокол между агентами | Внешняя | E4.4 (Улучшения) |
| ML-инфраструктура для обучения encoding/decoding моделей | Инфра | E4.7 (Улучшения) |
| Бюджет на LLM-вызовы для cost-aware routing testing | Внешняя | E4.3 (Улучшения) |

### 11.3. Стратегия реализации (§9.1 ROADMAP)

| Стратегия | Подходит для | Преимущества | Недостатки |
|---|---|---|---|
| A: Последовательная | Команды 3–5 человек | Низкий риск, предсказуемые сроки | 12 месяцев до финального состояния |
| B: Параллельные стримы | Команды 8+ человек | Сокращение времени на 30–40% | Выше координационные накладные |
| C: Quick-wins first | Стартапы | Быстрый ROI | Некоторые архитектурные улучшения откладываются |

**Рекомендация:** для CRITICAL — Стратегия B (3 параллельных стрима по §0.3 `CRITICAL-PROMPTS.md`); для production-ready и Улучшения — Стратегия A (последовательная, низкий риск на зрелых уровнях).

---

## 12. Рекомендации по команде (§9.4 ROADMAP)

| Роль | MVP | CRITICAL | production-ready | Улучшения |
|---|---|---|---|---|
| Tech-лид observability | 1 | 1 | 1 | 1 |
| Backend-разработчики | 2–3 | 3–4 | 4–5 | 5–6 |
| SRE/DevOps | 1 | 2 | 2 | 2+ |
| ML-инженер | — | 1 (drift) | 1 | 1–2 (PoC-задачи) |
| Security-инженер | — | 1 (PII, Vault) | периодический консалтинг | периодический консалтинг |

### 12.1. Quick-wins (§9.5 ROADMAP) — порядок минимально-заметных результатов

Если нужно быстро показать ценность observability, рекомендуется следующий порядок:

1. **Неделя 1–2:** E1.1 SDK + E1.4 Cost-трекинг — первый cost-дашборд.
2. **Неделя 3–4:** E1.2 Langfuse UI — первый trace-viewer.
3. **Неделя 5–6:** E1.6 Ring buffer + E1.3 Tail-sampler — overhead в норме.
4. **Месяц 2:** E2.1 Guardrail + E2.4 Async eval — первое выявление галлюцинаций.
5. **Месяц 3:** E2.5 Drift detector — первый пойманный drift.
6. **Месяц 6:** E3.7 SLO/SLA — дашборд для стейкхолдеров.

Эти вехи дают видимый результат каждые 4–6 недель, что важно для поддержания momentum.

---

*Документ сгенерирован на основе `ROADMAP.md` v1.0, `ARCHITECT.md` v1.0, `MVP-PROMPT.md` v1.0, `CRITICAL-PROMPTS.md` v1.0. Все 131 задача из 29 эпиков учтены. Полнота покрытия проверена в §9 «Сводка покрытия бэклога».*
