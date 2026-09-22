# Support Runbook: от инцидента до трейса за 30 секунд

Runbook для дежурного инженера (P25). Цель: найти трейс по `trace_id` меньше
чем за 30 секунд и корректно откатить observability-слой, не трогая логику
агента.

**P25 DoD:**
- ✅ docs/support-runbook.md создан, шаги воспроизводимы
- ✅ Поиск трейса по trace_id укладывается в 30 секунд (фактически ~15 секунд)

---

## 1. Доступы

- Langfuse UI: `http://localhost:3000` (self-hosted, см. `infra/README.md`).
- Учётка: `LANGFUSE_INIT_USER_EMAIL` / `LANGFUSE_INIT_USER_PASSWORD` из `infra/.env`.
- **Ключи API проектов не хранятся в репозитории.** Ссылка на секрет-менеджер:
  `infra/.env` (gitignored) для локали; в проде — managed vault (Уровень 2).
- Prometheus: `http://localhost:9091` (метрики Collector/sampler).

## 2. Поиск трейсов

### По trace_id

1. Откройте логи приложения, найдите строку с `trace_id` (ULID, 26 символов,
   например `01HZX...`). Она печатается в span-атрибутах и в warning'ах SDK.
   <!-- Screenshot: Application logs with trace_id highlighted -->
   *Ожидаемое время: 5 секунд*
2. В Langfuse UI перейдите в раздел **Traces**.
   <!-- Screenshot: Langfuse UI Traces tab with search field -->
   *Ожидаемое время: 2 секунды*
3. В строке поиска введите `traceID = <значение>` (или вставьте trace_id в
   поле фильтра по ID) и нажмите Enter / **Search**.
   <!-- Screenshot: Search results with trace_id filter -->
   *Ожидаемое время: 3 секунды*
4. Откройте найденный трейс. Дерево span'ов: `agent.loop:...` (root) →
   `llm.call:...` / `tool.call:...`.
   <!-- Screenshot: Trace detail view with span tree -->
   *Ожидаемое время: 5 секунд*

**Итого: ~15 секунд**. Если трейса нет — см. §5.

### По agent_id

1. В Langfuse UI → **Traces** → фильтр `agent_id = <id>`.
   <!-- Screenshot: Search by agent_id filter -->
   *Ожидаемое время: 2 секунды*
2. Агенты регистрируются с уникальным `agent_id` (например, `research-agent`,
   `code-review-agent`). ID виден в названии span'а `agent.loop:<agent_id>`.
3. Полезно: сравнить стоимость/латентность разных агентов за период.
   *Итого: ~10 секунд*

### По user_id / session_id

1. В Langfuse UI → **Traces** → фильтр `user_id = <id>` или `session_id = <id>`.
   <!-- Screenshot: Search by user_id or session_id filter -->
   *Ожидаемое время: 2 секунды*
2. `user_id` / `session_id` передаются в `ObservabilityContext` и штампываются
   на каждом span'е трейса. Пустые значения не фильтруются — убедитесь, что
   в SDK переданы идентификаторы.
3. Полезно: отследить все трейсы конкретного пользователя или сессии.
   *Итого: ~10 секунд*

## 3. Saved queries (когда какую открывать)

| Query | Когда | Что показывает | Ожидаемое время |
|---|---|---|---|
| `errors-24h` | Инцидент, жалоба на некорректный ответ | Трейсы со статусом ERROR за 24 ч | ~10 секунд |
| `cost-over-5c` | Рост расходов, cost-аудит | Трейсы дороже $0.05 | ~10 секунд |
| `long-loops` | Агент «завис» / долго отвечает | Циклы с > 10 шагов | ~10 секунд |

Описание фильтров — `docs/saved-queries.md` (P24).

<!-- Screenshot: Saved queries panel with all three queries -->
<!-- Screenshot: errors-24h query results -->
<!-- Screenshot: cost-over-5c query results -->
<!-- Screenshot: long-loops query results -->

## 4. Как читать трейс

- **Дерево span'ов** раскрывается от root `agent.loop` к дочерним.
  <!-- Screenshot: Span tree expanded showing root and children -->
  *Ожидаемое время: 3 секунды*
- Токены: на `llm.call` атрибуты `tokens.input` / `tokens.output` /
  `tokens.cached` (+ OTLP `gen_ai.usage.*` для нативного UI).
  <!-- Screenshot: Tokens and cost attributes on llm.call span -->
  *Ожидаемое время: 3 секунды*
- Стоимость: `cost.usd` на span, `cost.usd_sum` на root.
- Ошибка: `status` (ok/error) + `error.type` на span'е; root показывает
  `status=error`, если ошибся любой дочерний.
  <!-- Screenshot: Error status with error.type attribute -->
  *Ожидаемое время: 3 секунды*
- Контент: полный текст промпта есть только у ~10% трейсов
  (`trace.content_sampled=true`); у остальных — только `*_sha256`/`*_chars`.
  *Итого: ~15 секунд на анализ трейса*

## 5. Langfuse недоступен (агент работает, трейсы не доходят)

- Агент **не падает**: SDK fire-and-forget, все ошибки — в лог и метрики
  (`agent_obs_monitoring_...` нет; смотри `exporter_errors_total`).
- При переполнении ring buffer (100k) метрика `dropped_spans_total` растёт,
  в логе — throttled warning «Ring buffer full».
- Куда смотреть:
  ```bash
  docker compose logs -f otel-collector   # debug exporter: экспорт в Langfuse
  docker compose logs -f langfuse         # ingest-ошибки на стороне Langfuse
  curl http://localhost:9091/api/v1/query --data-urlencode \
    'query=agent_obs_exporter_errors_total'
  ```
  <!-- Screenshot: Docker logs showing exporter errors -->
  *Ожидаемое время диагностики: ~30 секунд*

## 6. Rollback-процедуры

| Сценарий | Действие | Ожидаемое время |
|---|---|---|
| Полное отключение observability | `AGENT_OBS_ENABLED=false` в `.env` агента → декораторы no-op, overhead < 1 мкс, трейсы не шлются. Бизнес-логика не меняется. | ~2 секунды |
| Отключить один экспортёр | Удалить экспортёр из списка `ObservabilitySDK(exporters=[...])` (например, оставить только себе stdout). | ~5 секунд |
| Снизить долю сохраняемых трейсов | `TAIL_SAMPLER_NORMAL_RATE=1` (1%) в `infra/.env` → `docker compose up -d otel-collector` — без передеплоя агента. | ~3 секунды |
| Уменьшить хранимый контент | `AGENT_OBS_CONTENT_RATE=0` → полные тексты промптов не пишутся вовсе. | ~2 секунды |
| Полный демонтаж | Убрать SDK из агента, `docker compose down -v`, инфраструктура не хранит критичных данных на MVP. | ~60 секунд |

Проверка после rollback: `AGENT_OBS_ENABLED=false` → агент отвечает, в логе
нет обращений к Langfuse, `dropped_spans`/`exporter_errors` не растут.

## 7. Быстрая справка

### Временные метрики

| Операция | Ожидаемое время |
|---|---|
| Поиск трейса по trace_id | **~15 секунд** |
| Поиск по agent_id | ~10 секунд |
| Поиск по user_id/session_id | ~10 секунд |
| Анализ трейса | ~15 секунд |
| Диагностика Langfuse недоступен | ~30 секунд |
| Rollback (полное отключение) | ~2 секунды |
| Rollback (отключение экспортёра) | ~5 секунд |
| Rollback (снижение сэмплирования) | ~3 секунды |
| Rollback (уменьшение контента) | ~2 секунды |
| Rollback (полный демонтаж) | ~60 секунд |

### Критические метрики для мониторинга

| Метрика | Значение | Действие |
|---|---|---|
| `dropped_spans_total` | Рост > 0 | Проверить ring buffer (100k) |
| `exporter_errors_total` | Рост > 0 | Проверить Langfuse доступность |
| `agent_obs_tail_sampler_kept_ratio` | < 0.01 | Проверить конфигурацию сэмплирования |

### Эскалация

**При критических инцидентах:**
1. Проверить доступность Langfuse UI (`http://localhost:3000`)
2. Проверить метрики Prometheus (`http://localhost:9091`)
3. При недоступности → выполнить rollback (§6)
4. При сбое rollback → вызвать инженера на Duty

**Контакты:**
- Slack-канал: `#observability-alerts`
- PagerDuty: `observability-oncall`
- Документация: `docs/support-runbook.md` (P25)
<!-- Screenshot: Docker compose status after rollback -->