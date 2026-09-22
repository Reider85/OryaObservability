# Support Runbook: от инцидента до трейса за 30 секунд

Runbook для дежурного инженера (P25). Цель: найти трейс по `trace_id` меньше
чем за 30 секунд и корректно откатить observability-слой, не трогая логику
агента.

---

## 1. Доступы

- Langfuse UI: `http://localhost:3000` (self-hosted, см. `infra/README.md`).
- Учётка: `LANGFUSE_INIT_USER_EMAIL` / `LANGFUSE_INIT_USER_PASSWORD` из `infra/.env`.
- **Ключи API проектов не хранятся в репозитории.** Ссылка на секрет-менеджер:
  `infra/.env` (gitignored) для локали; в проде — managed vault (Уровень 2).
- Prometheus: `http://localhost:9091` (метрики Collector/sampler).

## 2. Поиск трейса по trace_id

Последовательность кликов в Langfuse UI:

1. Откройте логи приложения, найдите строку с `trace_id` (ULID, 26 символов,
   например `01HZX...`). Она печатается в span-атрибутах и в warning'ах SDK.
2. В Langfuse UI перейдите в раздел **Traces**.
3. В строке поиска введите `traceID = <значение>` (или вставьте trace_id в
   поле фильтра по ID) и нажмите Enter / **Search**.
4. Откройте найденный трейс. Дерево span'ов: `agent.loop:...` (root) →
   `llm.call:...` / `tool.call:...`.

Ожидаемое время: **< 30 секунд**. Если трейса нет — см. §5.

## 3. Saved queries (когда какую открывать)

| Query | Когда | Что показывает |
|---|---|---|
| `errors-24h` | Инцидент, жалоба на некорректный ответ | Трейсы со статусом ERROR за 24 ч |
| `cost-over-5c` | Рост расходов, cost-аудит | Трейсы дороже $0.05 |
| `long-loops` | Агент «завис» / долго отвечает | Циклы с > 10 шагов |

Описание фильтров — `docs/saved-queries.md` (P24).

## 4. Как читать трейс

- **Дерево span'ов** раскрывается от root `agent.loop` к дочерним.
- Токены: на `llm.call` атрибуты `tokens.input` / `tokens.output` /
  `tokens.cached` (+ OTLP `gen_ai.usage.*` для нативного UI).
- Стоимость: `cost.usd` на span, `cost.usd_sum` на root.
- Ошибка: `status` (ok/error) + `error.type` на span'е; root показывает
  `status=error`, если ошибся любой дочерний.
- Контент: полный текст промпта есть только у ~10% трейсов
  (`trace.content_sampled=true`); у остальных — только `*_sha256`/`*_chars`.

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

## 6. Rollback-процедуры

| Сценарий | Действие |
|---|---|
| Полное отключение observability | `AGENT_OBS_ENABLED=false` в `.env` агента → декораторы no-op, overhead < 1 мкс, трейсы не шлются. Бизнес-логика не меняется. |
| Отключить один экспортёр | Удалить экспортёр из списка `ObservabilitySDK(exporters=[...])` (например, оставить только себе stdout). |
| Снизить долю сохраняемых трейсов | `TAIL_SAMPLER_NORMAL_RATE=1` (1%) в `infra/.env` → `docker compose up -d otel-collector` — без передеплоя агента. |
| Уменьшить хранимый контент | `AGENT_OBS_CONTENT_RATE=0` → полные тексты промптов не пишутся вовсе. |
| Полный демонтаж | Убрать SDK из агента, `docker compose down -v`, инфраструктура не хранит критичных данных на MVP. |

Проверка после rollback: `AGENT_OBS_ENABLED=false` → агент отвечает, в логе
нет обращений к Langfuse, `dropped_spans`/`exporter_errors` не растут.