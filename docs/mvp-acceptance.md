# MVP Acceptance Report (P26)

Приёмочные испытания Уровня 1 (MVP) по exit-criteria `ROADMAP.md §4.5`.
Критерии, требующие живого стенда (docker compose + реальный Langfuse),
помечены **P** (pending) — их нужно прогнать после поднятия стенда по методике
ниже. Критерии, покрытые автоматизированными тестами, помечены **PASS**.

| # | Критерий | Целевое | Измерено | Методика | Вердикт |
|---|---|---|---|---|---|
| C1 | Покрытие трейсами | ratio ≥ 0.99 | — (нет live-стенда) | 30 мин нагрузки пилотного агента; `distinct trace_id` в Langfuse `/ spans_total` | **P** |
| C2 | Overhead instrumentation | < 5% CPU @100 RPS | enqueue p99 **0.0468 мс** (<0.1), agent_observed p99 **0.4117 мс** (<1), loop lag **0 мс** (<50) | `tests/load/test_overhead.py` — 10k+ span/сек | **PASS** (latency); CPU-profile — **P** |
| C3 | Доставка трейса в UI | < 5 с p95 | — (нет live-стенда) | замер `timestamp(agent.loop end) → timestamp(Langfuse API)` | **P** |
| C4 | Drop rate при норме | 0% | 0 при нормальной нагрузке; 50 000 при переполнении (буфер 100k) | unit + `tests/load` (back-end failure) | **PASS** |
| C5 | Точность cost-учёта | ±5% от биллинга | эталонный расчёт совпадает (unit-тесты `test_compute_cost`, `test_cost_attributes`) | сверка с биллингом OpenAI | **PASS** (unit); реконсиляция — **P** |
| C6 | Отказ бэкинда не блокирует агента | агент жив, дропы считаются | 100/100 вызовов успешны, дропы считаются | `tests/load/test_overhead.py::TestBackendFailureScenario` | **PASS** |
| C7 | Батч 512 span'ов | < 200 мс p95 | — (нет live-стенда) | замер `LangfuseExporter.export` на локальном Langfuse | **P** |

## Что уже измерено автоматически

- **C2 — overhead:** p99 `_enqueue` = 0.0468 мс (таргет <0.1), p99
  `@agent_observed` = 0.4117 мс (таргет <1), event loop lag = 0 мс (таргет <50).
  Детали — `docs/benchmarks.md` и `tests/load/test_overhead.py`.
- **C4/C6 — отказоустойчивость:** при недоступном бэкенде агент продолжает
  отвечать; переполнение ring buffer (150k enqueue при maxsize 100k) даёт ровно
  50k дропов с ростом `dropped_spans_total`; `sdk.shutdown()` досылает остатки
  за 5-секундный дедлайн (`tests/test_shutdown.py`).
- **C5 — cost:** `compute_cost()` совпадает с ручным расчётом по формуле §3.5
  для 5 моделей, включая cached-токены (`tests/test_compute_cost.py`); пилотный
  агент выставляет все 5 атрибутов cost/tokens (`tests/test_cost_attributes.py`).

## Методика для pending-критериев (после поднятия стенда)

```bash
cd infra && docker compose up -d
# получить public/secret key в Langfuse UI, наполнить .env, перезапустить
docker compose up -d
# запустить пилотного агента / генератор синтетических трейсов ~30 минут
```

- **C1:** `count(distinct trace_id)` в Langfuse / `agent_obs_spans_total` ≥ 0.99.
- **C3:** метрика доставки (см. `agent_obs_export_errors_total` == 0) + замер
  времени ingest на реальном стенде.
- **C5:** сумма `cost.usd` за окно vs отчёт OpenAI billing (эталон — расчёт по
  зафиксированному набору запросов).
- **C7:** локальный бенч `LangfuseExporter.export` на батче 512 span'ов.

## Сводка

- **PASS by tests:** C2 (latency), C4, C5 (unit), C6.
- **PENDING (live stand):** C1, C2 (CPU-profile), C3, C5 (billing), C7.

MVP-код, покрытый тестами, готов; финальная приёмка по полному стенду —
после прогона pending-критериев. При провалах — цикл P27.