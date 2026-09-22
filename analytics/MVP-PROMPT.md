# MVP-PROMPT.md — Промпты для ИИ-агента: быстрый выход на Уровень 1 (MVP)

**Назначение:** готовая серия промптов для ИИ-кодинг-агента (Claude Code, Cursor, Codex, Aider или любой LLM-агент), которая доводит проект до **Уровня 1 MVP** observability-стека для LLM-агента за минимальное время.

**Источники:** `ARCHITECT.md` v1.0 (§2.3, §3.1, §3.5, §6, §7, §8, §12.1), `ROADMAP.md` v1.0 (§4 — Уровень 1 MVP, эпики E1.1–E1.6, задачи T1.x.x, exit criteria §4.5).

**Формат:** универсальные промпты без привязки к среде. Каждый промпт самодостаточен при условии, что в сессию сначала отправлен мастер-промпт **P0**.

---

## 0. Как пользоваться

### 0.1. Правила скармливания

1. **Каждая новая сессия** агента начинается с промпта **P0** (мастер-контекст), затем отправляется **один** тикет-промпт (P01…P25).
2. **Одна сессия = один тикет = один коммит.** Не склеивайте несколько тикетов в одну сессию — падает качество и теряется контроль.
3. После выполнения агента **обязательно** дождайтесь от него: (а) список сделанного, (б) результат запуска тестов, (в) заполненный чек-лист DoD из промпта. Нет отчёта — тикет не принят.
4. Если агент предлагает сделать больше, чем указано в промпте, — **отказывайте**: расширение scope на MVP главный тормоз.
5. Промпты с пометкой **[ПАРАЛЛЕЛЬ]** можно выполнять одновременно в отдельных сессиях/субагентах — они не пересекаются по файлам.

### 0.2. Карта волн выполнения (критический путь для скорости)

| Волна | Промпты | Тикеты | Можно параллельно с |
|---|---|---|---|
| **0** | P01 | T1.2.1 + T1.3.1 (инфраструктура) | P02–P06 |
| **1** | P02→P03→P04→P05→P06 | T1.1.1–T1.1.5 (SDK-каркас) | P01 |
| **2a** | P07→P08→P09→P10→P11 | T1.6.1–T1.6.5 (ring buffer) | 2b (после P06) |
| **2b** | P12→P13→P14→P15→P16 | T1.4.1–T1.4.5 (cost-трекинг) | 2a (после P04) |
| **3** | P17→P18→P19 | T1.2.2–T1.2.4 (Langfuse export) | после P08 и P01 |
| **4a** | P20→P21→P22 | T1.3.2–T1.3.4 (tail-sampler) | 4b (после P19) |
| **4b** | P23→P24→P25 | T1.5.1–T1.5.3 (Langfuse UI) | 4a (после P19) |
| **5** | P26 | Верификация MVP (все exit-criteria) | после всех |
| **6** | P27 (шаблон) | Цикл фиксов по проваленным критериям | итеративно |

**Быстрая последовательность для одного агента (без параллели):**
`P0 → P01 → P02 → P03 → P04 → P05 → P06 → P07 → P08 → P09 → P10 → P11 → P12 → P13 → P14 → P15 → P16 → P17 → P18 → P19 → P20 → P21 → P22 → P23 → P24 → P25 → P26 → (P27 при провалах)`

---

## P0 — МАСТЕР-ПРОМПТ: контекст проекта

> Отправлять **первым** в каждой новой сессии агента. Все дальнейшие промпты опираются на этот контекст.

```text
РОЛЬ
Ты — senior Python-инженер, внедряешь observability-стек для LLM-агента.
Работаешь строго в рамках одного тикета из ROADMAP Уровня 1 (MVP). Не расширяешь scope.

ПРОЕКТ
Пакет SDK: agent_obs (Python 3.11+, asyncio-only). Пилотный агент — простой Python-агент,
который вызывает LLM (OpenAI-совместимый API) и инструменты (tools). Цель MVP: трейсы
пилотного агента + их стоимость видны в Langfuse UI, observability не блокирует агента.

СТРУКТУРА РЕПОЗИТОРИЯ (поддерживай её)
agent-obs/
├── agent_obs/                  # Python SDK
│   ├── __init__.py             # from agent_obs import ObservabilitySDK
│   ├── observability.py        # SpanContext, Span, ObservabilitySDK
│   ├── exporters/base.py       # BaseExporter (контракт)
│   ├── exporters/langfuse_exporter.py
│   ├── exporters/stdout_exporter.py   # для тестов/debug
│   ├── cost/price_book.py      # загрузка price_book.yaml
│   ├── cost/compute_cost.py    # _compute_cost()
│   └── metrics.py              # Prometheus-метрики
├── pilot_agent/agent.py        # пилотный агент с @sdk.agent_observed
├── infra/
│   ├── docker-compose.yml      # langfuse + postgres + redis
│   └── otel-collector/config.yaml
├── tests/                      # pytest + pytest-asyncio
├── price_book.yaml
├── pyproject.toml
└── README.md

ТИПЫ SPAN'ОВ (на MVP только 3 из 9; остальные — Уровень 2, НЕ РЕАЛИЗУЕМ)
- agent.loop  — root-span одного цикла агента (создаёт декоратор @agent_observed)
- llm.call    — один вызов LLM API (context manager sdk.llm_call)
- tool.call   — вызов инструмента агентом (context manager sdk.tool_call)

КОНТРАКТЫ (канонические, менять нельзя)

@dataclass
class SpanContext:
    trace_id: str
    span_id: str
    parent_span_id: Optional[str] = None
    agent_id: str = ""
    agent_version: str = ""
    user_id: str = ""
    session_id: str = ""

@dataclass
class Span:
    name: str
    span_type: str  # agent.loop | llm.call | tool.call
    context: SpanContext
    attributes: dict = field(default_factory=dict)
    events: list = field(default_factory=list)
    start_time: float = 0.0
    end_time: Optional[float] = None

class BaseExporter:
    async def export(self, batch: list[Span]) -> None:
        """Батч span'ов. Идемпотентен по span_id."""
        raise NotImplementedError
    async def flush(self) -> None:
        """Досылка остатков при shutdown."""
        raise NotImplementedError

АТРИБУТЫ SPAN llm.call (схема §7.1 ARCHITECT.md — обязательный минимум)
llm.provider, llm.model, llm.temperature, tokens.input, tokens.output,
tokens.cached, cost.usd, cost.price_book_version

АТРИБУТЫ SPAN tool.call (§7.2)
tool.name, tool.input_hash (sha256), tool.input_summary, tool.rows_returned, tool.error

ФОРМУЛА СТОИМОСТИ (§3.5)
cost = tokens.input * price.input_per_1k / 1000
     + tokens.output * price.output_per_1k / 1000
     + cached-токены по отдельной цене (если есть)
Цены — из price_book.yaml (версионированный, valid_from/valid_to).

ЖЕЛЕЗНЫЕ ПРАВИЛА (нарушение = тикет не принят)
1. SDK всегда fire-and-forget: НИКОГДА не блокирует бизнес-логику агента.
   Все операции SDK — async, кроме быстрых sync-точек (< 1 мкс).
2. Все блокирующие I/O — только в фоновых задачах (asyncio.create_task / worker).
3. При ошибке экспорта/бэкинда агент продолжает работать. Ошибки — в лог + метрики.
4. Observability выключена конфигом (enabled=False) → декораторы no-op, overhead < 1 мкс.
5. Cost — атрибут span'а (cost.usd), НЕ отдельная система (антипаттерн §8.4).
6. Никакого PII-маскинга, eval, security, drift, multi-tenant — это Уровень 2+.
   На MVP — только синтетические данные, dev/staging-окружение.
7. Не вводить зависимости сверх необходимого без явного указания в тикете.
8. Код и комментарии — на английском; ответы мне — на русском.
9. После каждого тикета: прогнать pytest, сделать один коммит
   (conventional commits: feat(agent_obs): ...), отчитаться по чек-листу DoD.

СТЕК
Python 3.11+, asyncio, dataclasses, pytest + pytest-asyncio, prometheus-client,
pyyaml, httpx. Langfuse self-hosted (docker-compose). OTel Collector (для tail-sampling).
```

---

## ВОЛНА 0–1: Инфраструктура и SDK-каркас

### P01 — [T1.2.1 + T1.3.1] Инфраструктура: Langfuse + OTel Collector [ПАРАЛЛЕЛЬ с P02–P06]

> Контекст: P0. Запускать можно параллельно с Волной 1 — файлы не пересекаются (`infra/`).

```text
ЗАДАЧА (тикет T1.2.1 + T1.3.1)
Развернуть локальную инфраструктуру observability в docker-compose.

ЧТО СДЕЛАТЬ
1. infra/docker-compose.yml с сервисами:
   - langfuse (self-hosted, образ langfuse/langfuse:3, порт 3000,
     + langfuse-worker:3, clickhouse, minio для v3)
   - postgres:17 (база метаданных Langfuse, volume, healthcheck)
   - redis:7 (очереди Langfuse)
   - otel-collector (образ otel/opentelemetry-collector-contrib:0.xx)
   Все переменные окружения (LANGFUSE_SALT, keys, DATABASE_URL) — через .env.example.
2. infra/otel-collector/config.yaml:
   - receivers: otlp (grpc :4317, http :4318)
   - processors: batch (timeout 5s, send_batch_size 512)
   - exporters: otlp → langfuse (или otlphttp на endpoint Langfuse /api/public/otel)
     плюс debug-экспортёр для отладки
   - pipelines: traces → otlp receiver → batch → exporter
3. README-раздел в infra/: как поднять (docker compose up -d), как создать
   проект в Langfuse UI и получить public/secret key, куда их положить (.env).
4. Smoke-check: после `docker compose up -d` Langfuse отвечает на :3000,
   Collector пишет в лог «Everything is ready».

DoD
- [ ] docker compose up -d поднимает все 4 сервиса без ошибок
- [ ] Langfuse UI доступен на :3000, можно создать проект и получить ключи
- [ ] OTel Collector готов принимать OTLP на :4317/:4318
- [ ] .env.example содержит все необходимые переменные
Коммит: feat(infra): add langfuse, postgres, redis and otel collector compose stack
```

---

### P02 — [T1.1.1] SpanContext dataclass

> Контекст: P0. Первая задача SDK-каркаса.

```text
ЗАДАЧА
Создать пакет agent_obs и зафиксировать контекстный объект трейса.

ЧТО СДЕЛАТЬ
1. Каркас пакета: agent_obs/__init__.py, agent_obs/observability.py, pyproject.toml.
2. В observability.py — dataclass SpanContext ТОЧНО по контракту из P0:
   trace_id, span_id, parent_span_id, agent_id, agent_version, user_id, session_id.
3. trace_id/span_id — генерируются ULID-подобно (26/12 символов, сортируемые),
   либо uuid4-hex, если тянуть зависимость не хочется (решение зафиксируй в docstring).
4. Фабрика SpanContext.new(agent_id, agent_version, parent=None, user_id="", session_id="").
5. Тесты tests/test_span_context.py: уникальность id, заполнение полей,
   parent_span_id пробрасывается, сериализация в dict.

DoD
- [ ] `from agent_obs import ObservabilitySDK` пока может падать (пакет пуст), но модуль observability импортируется
- [ ] SpanContext соответствует контракту P0 поле-в-поле
- [ ] pytest зелёный
Коммит: feat(agent_obs): add SpanContext dataclass with id generation
```

---

### P03 — [T1.1.2] Span dataclass

> Контекст: P0 + результат P02.

```text
ЗАДАЧА
Реализовать основной объект телеметрии — Span.

ЧТО СДЕЛАТЬ
1. dataclass Span по контракту P0: name, span_type, context, attributes, events,
   start_time, end_time.
2. Валидация span_type по Enum SpanType: AGENT_LOOP="agent.loop", LLM_CALL="llm.call",
   TOOL_CALL="tool.call" (перечень §2.3; остальные 6 типов — заглушкой в Enum
   с комментом "level 2", использовать нельзя).
3. Методы: span.duration_ms() (end-start), span.set_attribute(k, v),
   span.add_event(name, attrs=None) с timestamp внутри события.
4. Сериализация: to_dict() → формат §7.1 ARCHITECT.md (trace_id, span_id,
   parent_span_id, name, span_type, start/end ISO8601, status, attributes, events).
5. Тесты: события с таймстампами, duration, сериализация соответствует схеме.

DoD
- [ ] Span создаётся только с валидным span_type из Enum
- [ ] to_dict() отдаёт структуру как в §7.1 (поля совпадают по именам)
- [ ] pytest зелёный
Коммит: feat(agent_obs): add Span dataclass with events, duration and to_dict serialization
```

---

### P04 — [T1.1.3] Декоратор @agent_observed (root span agent.loop)

> Контекст: P0 + P02 + P03.

```text
ЗАДАЧА
Реализовать точку входа instrumentation — декоратор, оборачивающий async run() агента.

ЧТО СДЕЛАТЬ
1. Скелет класса ObservabilitySDK (набросок ниже — развивай в этом тикете только
   декоратор и _build_context):

   class ObservabilitySDK:
       def __init__(self, exporters: list):
           self._exporters = exporters

       def agent_observed(self, agent_id: str, version: str = ""):
           def decorator(fn):
               @functools.wraps(fn)
               async def wrapper(*args, **kwargs):
                   ctx = self._build_context(agent_id=agent_id, version=version)
                   span = Span(name=f"agent.loop:{agent_id}", span_type="agent.loop", context=ctx)
                   span.start_time = _now()
                   try:
                       result = await fn(*args, _obs_ctx=ctx, **kwargs)
                       span.attributes["status"] = "ok"
                       return result
                   except Exception as e:
                       span.attributes["status"] = "error"
                       span.attributes["error.type"] = type(e).__name__
                       raise
                   finally:
                       span.end_time = _now()
                       await self._enqueue(span)
               return wrapper
           return decorator

2. _build_context() создаёт SpanContext (user_id/session_id можно передавать
   через contextvar ObsContext — заведите contextvars, они пригодятся в P05).
3. _enqueue() — пока временная заглушка (складывает span в список last_spans
   для тестов); ring buffer появится в P07–P08, контракт не менять.
4. Ветка enabled=False пока не нужна (это P06), но сигнатуру конструктора
  ObservabilitySDK(exporters: list, enabled: bool = True) уже зафиксируй.
5. Тесты: декоратор на async-функции; при успехе status=ok; при исключении
   status=error + error.type, исключение пробрасывается дальше; span попал в last_spans.

DoD
- [ ] Декоратор применяется к async-методу класса агента без изменения бизнес-логики
- [ ] Исключение в run() не глотается, span помечается error
- [ ] pytest зелёный
Коммит: feat(agent_obs): add agent_observed decorator creating agent.loop root span
```

---

### P05 — [T1.1.4] Контекст-менеджеры llm_call() и tool_call()

> Контекст: P0 + P04 (нужны SpanType и SDK-скелет).

```text
ЗАДАЧА
Дочерние span'ы для LLM-вызовов и инструментов.

ЧТО СДЕЛАТЬ
1. В ObservabilitySDK добавь два asynccontextmanager:

   @asynccontextmanager
   async def llm_call(self, ctx: SpanContext, *, model: str, provider: str):
       span = Span(name=f"llm.call:{model}", span_type="llm.call", context=ctx,
                   attributes={"llm.model": model, "llm.provider": provider})
       span.start_time = _now()
       try:
           yield span
       finally:
           span.end_time = _now()
           await self._enqueue(span)

   @asynccontextmanager
   async def tool_call(self, ctx: SpanContext, *, tool_name: str):
       # span_type="tool.call", name=f"tool.call:{tool_name}",
       # атрибуты заполняет вызывающий (tool.name и т.д. по схеме §7.2)

2. parent_span_id: дочерний span наследует trace_id из ctx, получает свой span_id,
   parent_span_id = span_id родителя. Реализуй через contextvar-стек активных span'ов
   (push/pop вокруг yield) — вложенность agent.loop → llm.call / tool.call должна работать.
3. Тесты: вложенный llm.call внутри agent.observed-функции имеет правильный
   parent_span_id и тот же trace_id; при исключении внутри блока span всё равно
   закрывается (end_time выставлен) и помечен error.

DoD
- [ ] async with sdk.llm_call(ctx, model="gpt-4o", provider="openai") as span: работает
- [ ] Дерево трейса: agent.loop → дочерние span'ы с корректным parent_span_id
- [ ] Исключение внутри context manager не оставляет «висячий» span
- [ ] pytest зелёный
Коммит: feat(agent_obs): add llm_call and tool_call context managers with span nesting
```

---

### P06 — [T1.1.5] Флаг enabled: zero-overhead no-op режим

> Контекст: P0 + P04/P05 (готовые декоратор и менеджеры).

```text
ЗАДАЧА
Режим полного отключения observability без изменения кода агента (требование §6.1,
rollback-стратегия §4.7 ROADMAP: sdk.enabled=False → декораторы no-op).

ЧТО СДЕЛАТЬ
1. В ObservabilitySDK.__init__(exporters, enabled=True, config: dict | None = None).
   Конфиг читается из переменных окружения AGENT_OBS_ENABLED (default "true").
2. При enabled=False:
   - agent_observed возвращает fn БЕЗ обёртки (нулевые накладные на вызов,
     никакой asyncio-работы);
   - llm_call/tool_call становятся «прозрачными» asynccontextmanager, которые
     yield-ят объект-заглушку (NullSpan) с no-op set_attribute/add_event;
   - enqueue/export-worker не запускаются.
3. Overhead-тест (T1.1.5 DoD): benchmark-тест (pytest-benchmark или time.perf_counter
   на 100k вызовов): при enabled=False накладные на вызов < 1 мкс.
4. Тесты: при enabled=False ни один span не создаётся, агент возвращает результат
   как обычно; флаг читается из env.

DoD
- [ ] AGENT_OBS_ENABLED=false → декораторы no-op, overhead < 1 мкс на вызов
- [ ] Код агента не меняется при включении/выключении
- [ ] pytest зелёный
Коммит: feat(agent_obs): add enabled flag with zero-overhead no-op mode
```

---

## ВОЛНА 2a: Ring buffer + async export (fire-and-forget)

> Эпик E1.6. Это ядро отказоустойчивости: observability никогда не блокирует агента
> (антипаттерн §8.5). Начинать только после P06. Промпты P07–P11 выполняются строго последовательно.

### P07 — [T1.6.1] Ring buffer на asyncio.Queue

> Контекст: P0 + готовый SDK (P02–P06), где `_enqueue()` — временная заглушка.

```text
ЗАДАЧА
Заменить заглушку _enqueue() на настоящий in-process ring buffer.

ЧТО СДЕЛАТЬ
1. В ObservabilitySDK.__init__: self._ring_buffer = asyncio.Queue(maxsize=100_000).
2. _enqueue(span) должен быть максимально дешёвым: положить span в очередь через
   put_nowait(); никаких await на сетевой I/O внутри enqueue.
3. Тесты: enqueue кладёт span; очередь ограничена 100_000; enqueue не делает
   сетевых вызовов (мок-экспортёры не дёргаются до запуска worker).

DoD
- [ ] asyncio.Queue(maxsize=100_000) создана в конструкторе SDK
- [ ] _enqueue() — non-blocking (put_nowait), < 10 мкс на вызов
- [ ] pytest зелёный
Коммит: feat(agent_obs): add in-process ring buffer (asyncio.Queue maxsize=100k)
```

---

### P08 — [T1.6.2] Export worker: drain-окно 50 мс, batch ≤ 512, fan-out

> Контекст: P0 + P07.

```text
ЗАДАЧА
Фоновая таска-экспортёр, формирующая батчи и рассылающая их всем export'ерам.

ЧТО СДЕЛАТЬ
1. В конструкторе SDK: self._worker_task = asyncio.create_task(self._export_worker()).
2. _export_worker() по каноническому циклу из §6.2 ARCHITECT.md:

   async def _export_worker(self):
       while True:
           batch = []
           try:
               first = await asyncio.wait_for(self._ring_buffer.get(), timeout=0.5)
               batch.append(first)
           except asyncio.TimeoutError:
               continue
           deadline = _now() + 0.05                      # drain-окно 50 мс
           while _now() < deadline and len(batch) < 512: # batch size ≤ 512
               try:
                   batch.append(self._ring_buffer.get_nowait())
               except asyncio.QueueEmpty:
                   break
           await asyncio.gather(
               *[exp.export(batch) for exp in self._exporters],
               return_exceptions=True                    # упавший экспортёр не роняет других
           )

3. Ошибки отдельных export'еров: ловятся gather'ом (return_exceptions=True),
   логируются warning'ом, worker НЕ умирает (outer try/except на случай бага).
4. Замена заглушки P04: last_spans больше не нужен — тесты переключаются на
   mock-экспортёры, которые собирают батчи.
5. Тесты: батч ≤ 512; при пустой очереди worker спит (0.5s poll); два экспортёра
   получают одинаковый батч; исключение у одного не мешает второму.

DoD
- [ ] Worker запускается автоматически из конструктора, переживает ошибки экспортёров
- [ ] Drain-окно 50 мс, batch ≤ 512, fan-out на все экспортёры
- [ ] pytest зелёный
Коммит: feat(agent_obs): add batched async export worker with 50ms drain window
```

---

### P09 — [T1.6.3] Переполнение: drop + метрика dropped_spans_total

> Контекст: P0 + P07. Антипаттерн §8.5: при переполнении — drop, а не блокировка.

```text
ЗАДАЧА
Корректная деградация при переполнении буфера + наблюдаемость потерь.

ЧТО СДЕЛАТЬ
1. agent_obs/metrics.py (prometheus-client):
   - Counter dropped_spans_total  «Spans dropped due to full ring buffer»
   - Counter spans_total          «Spans enqueued» (пригодится для exit-criteria)
2. В _enqueue(): при asyncio.QueueFull — инкремент dropped_spans_total,
   лог warning (не чаще 1 раза в 5 секунд, чтобы не спамить), span отбрасывается.
   НИКАКОГО ожидания места в очереди (никаких await put).
3. Тесты: очередь забивается до maxsize (в тесте уменьшенный maxsize через
   параметр конструктора) → новые span'ы дропаются, счётчик растёт,
   enqueue возвращает управление мгновенно; бизнес-логика не блокируется.

DoD
- [ ] QueueFull → drop + dropped_spans_total++ + throttled warning
- [ ] _enqueue() никогда не ждёт
- [ ] pytest зелёный
Коммит: feat(agent_obs): drop spans on ring buffer overflow with dropped_spans_total metric
```

---

### P10 — [T1.6.4] flush() при shutdown (timeout 5 с)

> Контекст: P0 + P08/P09.

```text
ЗАДАЧА
Гарантированная досылка телеметрии при остановке агента.

ЧТО СДЕЛАТЬ
1. ObservabilitySDK.shutdown(): останавливает приём новых span'ов, дожидается
   опустошения ring buffer (worker продолжает разгребать), затем вызывает
   flush() у всех экспортёров, затем отменяет worker_task.
2. Общий timeout всей процедуры — 5 секунд (asyncio.wait_for / deadline);
   по истечении — отмена, лог warning со счётчиком недосланных span'ов.
3. Удобство: async context manager `async with sdk:` вызывающий shutdown на выходе;
   а также хук atexit для sync-скриптов.
4. BaseExporter.flush() — контракт из P0, у mock-экспортёров фиксирует вызов.
5. Тесты: 1000 span'ов в очереди → shutdown → все долетели до мока; shutdown при
   «зависшем» экспортёре завершается по таймауту 5 с без исключений наружу.

DoD
- [ ] shutdown() досылает остатки и вызывает flush() у всех экспортёров
- [ ] Timeout 5 с, зависший экспортёр не подвешивает приложение
- [ ] pytest зелёный
Коммит: feat(agent_obs): add graceful shutdown with 5s flush deadline
```

---

### P11 — [T1.6.5] Нагрузочный тест instrumentation (10k RPS)

> Контекст: P0 + P07–P10. Это риск R1.3 из ROADMAP (блокировка event loop — Critical).

```text
ЗАДАЧА
Доказать, что instrumentation не блокирует event loop и укладывается в overhead.

ЧТО СДЕЛАТЬ
1. tests/load/test_overhead.py (или scripts/bench_instrumentation.py):
   - генератор 10 000 span'ов/сек через enqueue (длительность 10–30 с);
   - измерение p99 времени самого _enqueue() и полной обёртки agent_observed;
   - измерение задержек event loop (asyncio loop lag monitor) во время нагрузки.
2. Критерии (фиксируются в тесте как assert):
   - enqueue p99 < 0.1 мс; агентский вызов не блокируется > 1 мс на instrumentation;
   - loop lag под нагрузкой < 50 мс.
3. Сценарий отказа бэкинда (DoD эпика E1.6): экспортёр-мок с asyncio.sleep(3600)
   (имитация недоступного Langfuse) → агент работает без ошибок; через ~60 с буфер
   (100k) переполняется, dropped_spans_total растёт, бизнес-логика продолжает отвечать.
4. Результаты запуска оформи таблицей в docs/benchmarks.md.

DoD
- [ ] При 10k span/сек instrumentation-задержка < 1 мс, loop не блокируется
- [ ] Симуляция недоступного бэкенда: агент живёт, дропы считаются, ошибок нет
- [ ] docs/benchmarks.md с цифрами создан
Коммит: test(agent_obs): add load test proving non-blocking instrumentation at 10k spans/sec
```

---

## ВОЛНА 2b: Cost-трекинг как атрибут span'а

> Эпик E1.4. [ПАРАЛЛЕЛЬ] с Волной 2a (P07–P11) — файлы не пересекаются
> (cost/* и price_book.yaml vs observability.py). Нужен только P04 (атрибуты span).

### P12 — [T1.4.1] price_book.yaml для топ-5 моделей

```text
ЗАДАЧА
Создать версионированный справочник цен на LLM (§3.5 ARCHITECT.md).

ЧТО СДЕЛАТЬ
1. price_book.yaml в корне репо. Формат:

   version: "2026-09-01"
   currency: USD
   models:
     gpt-4o:
       input_per_1k: 0.0025
       output_per_1k: 0.010
       cached_input_per_1k: 0.00125
     gpt-4o-mini:        { input_per_1k: 0.00015, output_per_1k: 0.0006, cached_input_per_1k: 0.000075 }
     claude-3.5-sonnet:  { input_per_1k: 0.003,  output_per_1k: 0.015 }
     claude-3-haiku:     { input_per_1k: 0.00025, output_per_1k: 0.00125 }
     gemini-1.5-pro:     { input_per_1k: 0.00125, output_per_1k: 0.005 }

   (цены-заглушки актуализируются конфигом; важно — поле cached_input_per_1k опционально)
2. Каждая запись опционально несёт valid_from / valid_to (ISO-даты).
3. Валидация схемы через JSON Schema / pydantic-модель PriceBook (см. P13).
4. README-раздел: как обновлять цены (ручной процесс на MVP).

DoD
- [ ] price_book.yaml покрывает 5 моделей, читается yaml.safe_load
- [ ] Формат с version/valid_from/valid_to соответствует P0
Коммит: feat(agent_obs): add versioned price_book.yaml for top-5 LLM models
```

---

### P13 — [T1.4.2] Загрузка price_book с валидацией valid_from/valid_to

```text
ЗАДАЧА
Надёжный загрузчик справочника цен.

ЧТО СДЕЛАТЬ
1. agent_obs/cost/price_book.py:
   - класс PriceBook.load(path) → pydantic-модель (или dataclass с валидацией);
   - валидация: version непустой, цены > 0, даты valid_from/valid_to корректны,
     valid_from < valid_to, отсутствуют перекрывающиеся периоды одной модели;
   - метод price_for(model, at: datetime | None = None) → набор цен, активных
     на указанный момент (учитывая valid_from/valid_to); нет цены → PriceNotFoundError.
2. Поведение при ошибке: эксплицитное исключение (никаких silent defaults) —
   ошибка конфигурации цен должна падать на старте, а не в проде.
3. Тесты: успешная загрузка; ошибка перекрытия периодов; expired-период не
   выбирается; отсутствие модели → PriceNotFoundError.

DoD
- [ ] load() валидирует valid_from/valid_to и перекрытия
- [ ] price_for() выбирает актуальный период
- [ ] pytest зелёный
Коммит: feat(agent_obs): add validated price_book loader with valid_from/valid_to periods
```

---

### P14 — [T1.4.3] _compute_cost() с поддержкой cached tokens

```text
ЗАДАЧА
Функция расчёта стоимости вызова LLM.

ЧТО СДЕЛАТЬ
1. agent_obs/cost/compute_cost.py:

   def compute_cost(
       usage: Usage,          # Usage(input: int, output: int, cached: int = 0)
       model: str,
       book: PriceBook,
       at: datetime | None = None,
   ) -> CostResult:

   CostResult = dataclass(cost_usd: float, price_book_version: str)
   Формула (P0): cost = input*price_in/1000 + output*price_out/1000
                  + cached*price_cached/1000 (если цена cached задана;
                  если не задана — cached считаются по input-цене).
2. Округление: НЕ округлять внутри (float), округление до 8 знаков только на
   границе записи в атрибут.
3. Тесты: арифметика по формуле; cached-токены; unknown model → исключение;
   точность vs эталонные значения (5–6 кейсов с ручным расчётом).

DoD
- [ ] compute_cost() соответствует формуле §3.5, включая cached-токены
- [ ] Возвращает cost.usd + версию price_book (для атрибута cost.price_book_version)
- [ ] pytest зелёный
Коммит: feat(agent_obs): add compute_cost with cached-token pricing
```

---

### P15 — [T1.4.4] Cost-атрибуты на span llm.call

```text
ЗАДАЧА
Прицепить стоимость к трейсу: cost — это атрибут span'а, НЕ отдельная сущность
(антипаттерн §8.4).

ЧТО СДЕЛАТЬ
1. В пилотном агенте (pilot_agent/agent.py) после получения ответа LLM:

   usage = Usage(input=response.usage.prompt_tokens,
                 output=response.usage.completion_tokens,
                 cached=getattr(response.usage, "cached_tokens", 0) or 0)
   result = compute_cost(usage, model="gpt-4o", book=price_book)
   span.attributes.update({
       "tokens.input":  usage.input,
       "tokens.output": usage.output,
       "tokens.cached": usage.cached,
       "cost.usd":      result.cost_usd,
       "cost.price_book_version": result.price_book_version,
   })

2. Сахар в SDK: sdk.llm_call(..., price_book=..., usage=...) — опционально
   автозаполняет cost-атрибуты при выходе из блока (удобно, но не обязательно).
3. Пилотный агент обязан выставлять ВСЕ 5 атрибутов из P0 на каждом llm.call.
4. Тесты: интеграционный тест с фейковым LLM-клиентом — атрибуты появились,
   cost.usd совпадает с ручным расчётом.

DoD
- [ ] Каждый llm.call пилотного агента несёт tokens.input/output/cached + cost.usd + версию прайса
- [ ] pytest зелёный
Коммит: feat(agent_obs): attach token and cost attributes to llm.call spans
```

---

### P16 — [T1.4.5] Метрика cost_per_request_usd

```text
ЗАДАЧА
Экспонировать стоимость в Prometheus для дашборда.

ЧТО СДЕЛАТЬ
1. agent_obs/metrics.py: Gauge cost_per_request_usd с лейблами agent_id, model.
   Обновление: после закрытия span llm.call (в _enqueue или отдельном хуке) —
   значение cost.usd последнего запроса; плюс Counter cost_total_usd
   (лейблы agent_id, model) — накопленный расход (для реконсиляции).
2. HTTP endpoint /metrics (prometheus_client.start_http_server(9090) или
   aiohttp-роут в пилотном агенте — на выбор; фиксируй в README).
3. Тесты: метрики появляются в exposition-формате; лейблы корректны;
   cost_total_usd монотонно растёт.

DoD
- [ ] /metrics отдаёт cost_per_request_usd{agent_id, model} и cost_total_usd
- [ ] pytest зелёный
Коммит: feat(agent_obs): export cost_per_request_usd and cost_total_usd metrics
```

---

## ВОЛНА 3: Доставка в Langfuse (self-hosted)

> Эпик E1.2 (задачи T1.2.2–T1.2.4; инфраструктура T1.2.1 уже готова в P01).
> Старт: после P08 (worker) и P01 (Langfuse поднят, ключи получены).

### P17 — [T1.2.2] LangfuseExporter.export(): батчинг ≤ 512 + ретрай

> Контракт BaseExporter — из P0. OTLP-транспорт: Langfuse принимает OTLP
> (endpoint /api/public/otel/v1/traces, Basic Auth public:secret).

```text
ЗАДАЧА
Экспортёр span'ов в Langfuse по OTLP/HTTP.

ЧТО СДЕЛАТЬ
1. agent_obs/exporters/langfuse_exporter.py:

   class LangfuseExporter(BaseExporter):
       def __init__(self, endpoint: str, public_key: str, secret_key: str,
                    timeout_s: float = 5.0, max_retries: int = 5): ...
       async def export(self, batch: list[Span]) -> None: ...
       async def flush(self) -> None: ...   # детали в P18

2. Конвертация Span → OTLP JSON (ResourceSpans/ScopeSpans/Span):
   trace_id/span_id hex, timestamps в наносекундах, attributes — KV-массив,
   события → span.events. Идемпотентность по span_id (повторная отправка не
   должна дублировать — Langfuse дедуплицирует по (trace_id, span_id)).
3. Транспорт: httpx.AsyncClient, POST на endpoint, Basic Auth public:secret.
4. Ретрай с экспоненциальной задержкой (base 0.2s, factor 2, jitter, max_retries 5)
   на 429/5xx/сетевые ошибки; 4xx (кроме 429) — не ретраить, лог + счётчик ошибок.
5. Экспортёр не бросает исключения наружу наружного цикла агента: все ошибки —
   в метрику exporter_errors_total и лог.
6. Тесты (respx/httpx mock): успешная отправка 512 span'ов; ретраи на 500/429;
   формат тела OTLP-валиден (проверка структуры KV-атрибутов).

DoD
- [ ] Батч ≤ 512 span'ов уходит в Langfuse за < 200 мс p95 (локальный замер)
- [ ] Ретрай с exponential backoff на 429/5xx, без ретрая на прочие 4xx
- [ ] pytest зелёный
Коммит: feat(agent_obs): add LangfuseExporter with OTLP JSON transport and exponential retry
```

---

### P18 — [T1.2.3] LangfuseExporter.flush()

```text
ЗАДАЧА
Корректная досылка остатков при shutdown (в связке с sdk.shutdown() из P10).

ЧТО СДЕЛАТЬ
1. flush(): отправляет всё, что осталось во внутреннем буфере экспортёра
   (если есть неотправленное из-за ретраев), синхронно дожидается ответа;
   повторно вызывается безопасно (идемпотентна).
2. Интеграция: sdk.shutdown() (P10) вызывает flush() всех экспортёров в рамках
   общего дедлайна 5 с; flush не должен его нарушать.
3. Тесты: N зависших из-за ретраев span'ов → flush доставляет; двойной flush — ок;
   flush при недоступном бэкинде завершается по дедлайну, не бросая исключений.

DoD
- [ ] flush() досылает остатки и идемпотентен
- [ ] Вписан в 5-секундный дедлайн shutdown
- [ ] pytest зелёный
Коммит: feat(agent_obs): implement idempotent flush for LangfuseExporter
```

---

### P19 — [T1.2.4] Аутентификация и TLS

```text
ЗАДАЧА
Безопасный транспорт до Langfuse.

ЧТО СДЕЛАТЬ
1. Креденшелы: env LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY / LANGFUSE_HOST
   (через .env; в .env.example — шаблон). Ключи НИКОГДА не в коде и не в git.
2. Basic Auth public:secret на каждый запрос (уже реализовано в P17 — проверь
   покрытие тестом на заголовок Authorization).
3. TLS: если LANGFUSE_HOST начинается с https:// — verify включён (default);
   опция самоподписанного сертификата только через явный env AGENT_OBS_TLS_CA
   (путь к CA-bundle), никаких verify=False по умолчанию.
4. Проверка конфигурации на старте SDK: ключи/хост заданы → иначе понятная ошибка
   (или деградация в StdoutExporter с warning, если AGENT_OBS_FAIL_ON_CONFIG=0).
5. Тесты: заголовок авторизации; отказ отправки с дефолтным verify=False
   (т.е. такого кода нет); чтение конфига из env.

DoD
- [ ] Креденшелы только из env, Basic Auth на каждом запросе
- [ ] TLS verify по умолчанию включён
- [ ] pytest зелёный
Коммит: feat(agent_obs): secure Langfuse transport with env-based auth and TLS verify
```

---

## ВОЛНА 4a: Tail-sampler с фиксированной политикой

> Эпик E1.3 (задачи T1.3.2–T1.3.4; развёртывание Collector — T1.3.1 уже готово в P01).
> Старт: после P19 (трейсы реально летят в Langfuse через Collector).

### P20 — [T1.3.2] Политика сэмплирования (errors/cost 100%, normal 10%)

```text
ЗАДАЧА
Tail-sampling-политика между SDK и Langfuse: «интересные» трейсы — 100%,
обычные — 10% (ТРИЗ Принцип 16; адаптивность — Уровень 3, НЕ ДЕЛАТЬ).

ЧТО СДЕЛАТЬ
1. Реализация на базе OTel Collector tail_sampling processor в
   infra/otel-collector/config.yaml (предпочтительно) либо мини-прокси на Python,
   если кастомный processor невозможен (решение обосновать в README).
2. Политика (tail_sampling/and-комбинация):
   - status_code == ERROR → keep 100%
   - атрибут cost.usd > порога (env TAIL_SAMPLER_COST_THRESHOLD, default 0.05) → keep 100%
   - атрибут security.incident == true → keep 100% (поле-заглушка на MVP:
     в SDK атрибут всегда false, но сэмплер уже умеет его проверять)
   - прочие → probabilistic 10% (env TAIL_SAMPLER_NORMAL_RATE)
3. Подготовка данных на стороне SDK: атрибуты span'а, по которым решает сэмплер
   (cost.usd на llm.call; статус ошибки на agent.loop), должны быть на КАЖДОМ span
   корневого трейса (или в resource) — если сэмплер смотрит только на root,
   скопируй агрегаты (error, sum cost) в атрибуты root-агента в _enqueue.
4. Тесты: ошибочный трейс сохраняется всегда; дорогой трейс — всегда;
   из 1000 нормальных в Langfuse попадает ~10% (допуск 8–12%).

DoD
- [ ] Политика: errors 100%, cost>threshold 100%, security-заглушка 100%, normal 10%
- [ ] SDK отдаёт сэмплеру все нужные атрибуты на root
- [ ] pytest / config-тесты зелёные
Коммит: feat(infra): add tail sampling policy errors/cost/security 100%, normal 10%
```

---

### P21 — [T1.3.3] Метрика tail_sampler_kept_ratio

```text
ЗАДАЧА
Наблюдаемость самого сэмплера.

ЧТО СДЕЛАТЬ
1. Если реализация на Collector: включить telemetry Collector'а
   (otelcol_processor_tail_sampling_* метрики) + правило prometheus-скрейпа в
   infra/docker-compose.yml (сервис prometheus опционально — минимальный конфиг).
2. Если мини-прокси на Python: свои счётчики traces_received_total /
   traces_kept_total (лейблы reason=error|cost|security|random) и экспозиция
   /metrics; производная метрика tail_sampler_kept_ratio = kept/received.
3. Дашборд не нужен (Уровень 3) — достаточно метрик в exposition.
4. Тесты: ratio считается корректно на фиксированном наборе трейсов.

DoD
- [ ] tail_sampler_kept_ratio доступен в Prometheus-экспозиции
- [ ] Причины keep различимы (error/cost/security/random)
Коммит: feat(infra): expose tail_sampler_kept_ratio with keep reasons
```

---

### P22 — [T1.3.4] Content-sampling: полный текст промпта только у 10%

```text
ЗАДАЧА
Экономия storage: полный текст промпта/ответа хранится только у части трейсов,
у остальных — SHA-256-хэш + длина (§3.1 ARCHITECT.md).

ЧТО СДЕЛАТЬ
1. В SDK (пилотный агент / llm_call): атрибуты llm.input_text / llm.output_text
   записываются полностью только если span.attrs["trace.content_sampled"] == true,
   иначе — llm.input_sha256, llm.input_chars, llm.output_sha256, llm.output_chars.
2. Решение о content-sampling: детерминированный хэш от trace_id
   (hash(trace_id) % 100 < CONTENT_RATE, env AGENT_OBS_CONTENT_RATE, default 10)
   — чтобы все span'ы одного трейса решались одинаково.
3. ВАЖНО (риск R1.5): полный текст никогда не попадает даже в «недосэмплированный»
   span — никаких утечек текста через другие атрибуты (проверь tool.input_summary:
   только первые 200 символов + хэш).
4. Тесты: у ~10% трейсов текст есть, у остальных — только хэш/длина; решение
   консистентно внутри трейса; в tool.input_summary нет полного текста.

DoD
- [ ] Content-sampling 10% детерминирован по trace_id
- [ ] Несэмплированные span'ы не содержат полного текста (только sha256 + chars)
- [ ] pytest зелёный
Коммит: feat(agent_obs): deterministic content sampling keeps full prompts for 10% traces
```

---

## ВОЛНА 4b: Trace-viewer через Langfuse UI

> Эпик E1.5. Старт: после P19 (трейсы в Langfuse). [ПАРАЛЛЕЛЬ] с Волной 4a.

### P23 — [T1.5.1] Маппинг span-атрибутов в лейблы Langfuse UI

```text
ЗАДАЧА
Сделать трейсы читаемыми в стандартном UI Langfuse (без кастомного UI — ТРИЗ Принцип 5).

ЧТО СДЕЛАТЬ
1. Проверь/доведи маппинг при конвертации в OTLP (P17), чтобы в Langfuse UI
   корректно отображались:
   - имя трейса = agent.loop:<agent_id>; наблюдения-observation'ы = llm.call / tool.call;
   - metadata/labels: agent_id, agent.version, model, status;
   - токены и стоимость: Langfuse нативно читает usage-поля из OTLP —
     заполни стандартные поля (input/output tokens), а также cost.usd в metadata;
   - events (llm.call.started / completed) — как события observation.
2. Скриншот-чек в UI: дерево span'ов разворачивается, атрибуты видны, стоимость видна.
3. Оформи настройки маппинга в agent_obs/exporters/otlp_mapping.py с тестами.

DoD
- [ ] В UI Langfuse: дерево трейса, токены, cost, статус — всё видно без ковыряния в JSON
- [ ] Маппинг покрыт тестами
Коммит: feat(agent_obs): map span attributes to Langfuse UI labels and usage fields
```

---

### P24 — [T1.5.2] Три saved query для поддержки

```text
ЗАДАЧА
Готовые фильтры для дежурного инженера.

ЧТО СДЕЛАТЬ
1. Создай в Langfuse (через UI или API) и задокументируй 3 сохранённых
   фильтра/вью:
   - «errors-24h» — трейсы со статусом error за последние 24 часа;
   - «cost-over-5c» — трейсы с суммой cost.usd > $0.05;
   - «long-loops» — трейсы с числом span'ов agent.loop > 10 шагов
     (агрегат steps.count на root-атрибуте — добавь его в SDK, если нужен).
2. Идиомы поиска задокументируй в README (см. P25): поиск по trace_id,
   по agent_id, по user_id/session_id.
3. Если root-атрибут steps.count отсутствует — добавь его заполнение в SDK
   (число дочерних llm.call/tool.call на root-span) с тестом.

DoD
- [ ] 3 saved query доступны и работают на реальных данных
- [ ] steps.count заполняется в SDK
Коммит: feat(agent_obs): add steps.count attribute and document saved queries
```

---

### P25 — [T1.5.3] README для команды поддержки (time-to-trace < 30 с)

```text
ЗАДАЧА
Инструкция для дежурных: от инцидента до трейса за 30 секунд.

ЧТО СДЕЛАТЬ
1. docs/support-runbook.md:
   - как зайти в Langfuse UI (URL, доступы — ссылка на секрет-менеджер, НЕ сами ключи);
   - поиск трейса по trace_id (из логов приложения) — точная последовательность кликов;
   - три saved query из P24 — когда какую открывать;
   - как прочитать трейс: дерево span'ов, где токены, где cost.usd, где ошибка;
   - что делать при недоступности Langfuse (агент работает, трейсы дропаются —
     метрика dropped_spans_total; куда смотреть);
   - процедура rollback: AGENT_OBS_ENABLED=false (полностью), отключение
     одного экспортёра, снижение TAIL_SAMPLER_NORMAL_RATE (§4.7 ROADMAP).
2. Шаги с скриншотами (по возможности) и ожидаемым временем каждого шага.
3. Проверка DoD: коллега (или ролевая самопроверка) находит тестовый трейс по
   trace_id за < 30 секунд строго по инструкции.

DoD
- [ ] docs/support-runbook.md создан, шаги воспроизводимы
- [ ] Поиск трейса по trace_id укладывается в 30 секунд
Коммит: docs(support): add runbook for trace lookup and observability rollback
```

---

## ФИНАЛ: Верификация и цикл фиксов

### P26 — Верификационный промпт: приёмка MVP по exit-criteria

> Отправлять после выполнения P01–P25. Это приёмка уровня по ROADMAP §4.5.

```text
РОЛЬ
Ты — QA/SRE-инженер, принимающий Уровень 1 (MVP) observability-стека.
Проводишь приёмочные испытания СТРОГО по критериям ниже. Ничего не дорабатываешь —
только измеряешь и фиксируешь. Результат — отчёт docs/mvp-acceptance.md.

ПОДГОТОВКА
1. Подними полный стенд: docker compose up -d (Langfuse, Postgres, Redis,
   OTel Collector), запусти пилотного агента с включённым SDK.
2. Зафиксируй версии всех компонентов и коммит репозитория.

ИСПЫТАНИЯ (по каждому — методика, измеренное значение, pass/fail)

C1. Покрытие трейсами (целевое: ≥ 99%)
    Нагрузка: 30 минут трейсов через пилотного агента (или генератор синтетических
    агентов). Сравни count(distinct trace_id) в Langfuse vs count(trace_id) в
    agent-логах/метрике spans_total. Ratio ≥ 0.99 → pass.

C2. Overhead instrumentation (целевое: < 5% CPU при 100 RPS)
    Нагрузочный тест 100 RPS на агента, 10 минут, замер CPU profile процесса
    (py-spy / cProfile / /proc). Доля времени в коде SDK < 5% → pass.
    Дополнительно: p99 задержки enqueue < 0.1 мс.

C3. Доставка трейса в UI (целевое: < 5 с p95)
    Метрика langfuse_ingest_latency_seconds (или собственный замер: timestamp
    завершения agent.loop → timestamp видимости в Langfuse API). p95 < 5 с → pass.

C4. Drop rate при нормальной нагрузке (целевое: 0%)
    dropped_spans_total / spans_total за окно C1 == 0 → pass.

C5. Точность cost-учёта (целевое: ±5% от биллинга)
    Сумма cost.usd по всем трейсам окна C1 vs отчёт биллинга OpenAI за то же окно.
    Расхождение ≤ 5% → pass. (Если реальный биллинг недоступен — эталонный
    расчёт по токенам из фиксированного набора запросов.)

C6. Отказоустойчивость (DoD E1.6)
    Останови контейнер Langfuse на 2 минуты под нагрузкой 10k span/сек:
    агент работает без ошибок; dropped_spans_total растёт после переполнения
    буфера; после возврата Langfuse экспорт восстанавливается. → pass/fail.

C7. Батч-производительность (DoD E1.2)
    Батч 512 span'ов долетает за < 200 мс p95 (замер экспортёра). → pass/fail.

ФОРМАТ ОТЧЁТА docs/mvp-acceptance.md
| Критерий | Целевое | Измерено | Методика | Вердикт |
+ сводка: «MVP ПРИНЯТ» или список проваленных критериев.
Проваленные критерии не чини сам — верни отчёт человеку для запуска P27.

DoD
- [ ] docs/mvp-acceptance.md содержит все 7 критериев с измеренными значениями
- [ ] Все pass → MVP достигнут
Коммит: test(acceptance): add MVP acceptance report with exit criteria results
```

---

### P27 — ШАБЛОН промпта-фиксов (итеративный цикл)

> Использовать для КАЖДОГО проваленного критерия из отчёта P26.
> Скопируй шаблон и подставь значения в {скобках}.

```text
КОНТЕКСТ
Приёмка MVP (docs/mvp-acceptance.md) выявила провал критерия.

ПРОВАЛЕННЫЙ КРИТЕРИЙ: {C1..C7 — название}
ЦЕЛЕВОЕ ЗНАЧЕНИЕ: {из ROADMAP §4.5}
ФАКТИЧЕСКОЕ ЗНАЧЕНИЕ: {из отчёта}
МЕТОДИКА ИЗМЕРЕНИЯ: {из отчёта}

ЗАДАЧА
1. Найди корневую причину провала. Сначала измерь/запрофилируй, потом меняй код.
   Зафиксируй причину одной фразой («because ...»).
2. Исправь МИНИМАЛЬНО: только то, что относится к причине. Не рефактори соседнее.
   Не добавляй функциональность Уровня 2+ (eval, security, drift, adaptive sampling).
3. Соблюдай железные правила P0 (fire-and-forget, async, cost в атрибутах span).
4. Перепроведи методику измерения критерия и покажи новое значение.
5. Прогони полный pytest. Один коммит: fix(agent_obs): ... (или fix(infra): ...).
6. Обнови docs/mvp-acceptance.md (значение + пометка «fixed: <причина>»).

ОГРАНИЧЕНИЯ
- Если причина в архитектуре (например, нужен async-пул другого вида) — СТОП:
  верни мне описание противоречия и 2 варианта решения, не реализуй сам.
- Если фикс требует новой зависимости — СТОП: перечисли варианты и плюсы/минусы.

DoD
- [ ] Критерий проходит повторное измерение
- [ ] pytest зелёный, scope не расширен
```

---

## Сводный чек-лист выхода на MVP (ROADMAP §4.5)

| # | Критерий | Целевое значение | Метод проверки | Промпт |
|---|---|---|---|---|
| C1 | 100% трейсов пилотного агента видны в Langfuse UI | ratio ≥ 0.99 | distinct trace_id в Langfuse / spans_total | P26 |
| C2 | Overhead на instrumentation | < 5% CPU | Нагрузочный тест 100 RPS, 10 мин, CPU profile | P11, P26 |
| C3 | Доставка трейса в UI | < 5 с p95 | langfuse_ingest_latency_seconds | P26 |
| C4 | Drop rate при нормальной нагрузке | 0% | dropped_spans_total / spans_total | P26 |
| C5 | Точность cost-учёта | ±5% от биллинга | Реконсиляция с OpenAI billing | P26 |
| C6 | Отказ бэкинда не блокирует агента | агент жив, дропы считаются | Сценарий остановки Langfuse | P11, P26 |
| C7 | Батч 512 span'ов | < 200 мс p95 | Замер LangfuseExporter | P26 |

**Памятка по рискам (ROADMAP §4.6), контролируемая промптами:**

| Риск | Митигируется в |
|---|---|
| R1.1 Langfuse падает под нагрузкой | P07–P10 (fire-and-forget), C6 |
| R1.2 Cost расходится с биллингом > 10% | P12–P16 (версионный прайс + реконсиляция C5) |
| R1.3 SDK блокирует event loop | P11 (нагрузочный тест) |
| R1.4 PII в Langfuse на dev | Только синтетические данные; P22 (content-sampling) |
| R1.5 Storage > 10 ГБ/день | P20–P22 (tail + content sampling) |
