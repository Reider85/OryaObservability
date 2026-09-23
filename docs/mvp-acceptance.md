# MVP Acceptance Report (P26)

Приёмочные испытания Уровня 1 (MVP) по exit-criteria `ROADMAP.md §4.5`.
Критерии, требующие живого стенда (docker compose + реальный Langfuse),
помечены **P** (pending) — их нужно прогнать после поднятия стенда по методике
ниже. Критерии, покрытые автоматизированными тестами, помечены **PASS**.

| # | Критерий | Целевое | Измерено | Методика | Вердикт |
|---|---|---|---|---|---|
| C1 | Покрытие трейсами | ratio ≥ 0.99 | — (нет live-стенда) | 30 мин нагрузки пилотного агента; `distinct trace_id` в Langfuse `/ spans_total` | **P** |
| C2 | Overhead instrumentation | < 5% CPU @100 RPS | enqueue p99 **0.0215 мс** (<0.1), agent_observed p99 **0.0944 мс** (<1), loop lag **0.000 мс** (<50), throughput **157 772 span/s** | `tests/load/test_overhead.py` — 10k+ span/сек | **PASS** (latency); CPU-profile — **P** |
| C3 | Доставка трейса в UI | < 5 с p95 | — (нет live-стенда) | замер `timestamp(agent.loop end) → timestamp(Langfuse API)` | **P** |
| C4 | Drop rate при норме | 0% | 0 при нормальной нагрузке; 50 000 при переполнении (буфер 100k) | unit + `tests/load` (back-end failure) | **PASS** |
| C5 | Точность cost-учёта | ±5% от биллинга | 19/19 unit-тестов PASS; `compute_cost` совпадает с расчётом для 5 моделей; все 5 атрибутов cost/tokens на llm.call | `test_compute_cost.py` + `test_cost_attributes.py` + `test_price_book.py`; сверка с биллингом OpenAI | **PASS** (unit); реконсиляция — **P** |
| C6 | Отказ бэкинда не блокирует агента | агент жив, дропы считаются | 200/200 + 1000/1000 вызовов успешны; 50 000 дропов при overflow; 10/10 shutdown-тестов PASS | `tests/load/test_overhead.py::TestBackendFailureScenario` + `test_shutdown.py` | **PASS** |
| C7 | Батч 512 span'ов | < 200 мс p95 | batch_max_512 PASS; LangfuseExporter export/retry/flush 23/23 PASS | `test_export_worker.py::test_batch_max_512` + `test_langfuse_exporter.py` | **PASS** (logic); latency — **P** |

## Что уже измерено автоматически

- **C2 — overhead:** p99 `_enqueue` = 0.0215 мс (таргет <0.1), p99
  `@agent_observed` = 0.0944 мс (таргет <1), event loop lag = 0.000 мс (таргет <50),
  throughput = 157 772 span/s. Детали — `docs/benchmarks.md` и `tests/load/test_overhead.py`.
- **C4 — drop rate:** 0 дропов при нормальной нагрузке; при переполнении ring buffer
  (150k enqueue при maxsize 100k) ровно 50k дропов — корректная работа буфера.
- **C6 — отказоустойчивость:** при недоступном бэкенде агент продолжает отвечать
  (200/200 + 1000/1000 вызовов); `sdk.shutdown()` досылает остатки за 5-секундный
  дедлайн (`tests/test_shutdown.py` — 10/10 PASS).
- **C5 — cost:** `compute_cost()` совпадает с ручным расчётом по формуле §3.5
  для 5 моделей, включая cached-токены (`tests/test_compute_cost.py` — 12/12 PASS);
  пилотный агент выставляет все 5 атрибутов cost/tokens
  (`tests/test_cost_attributes.py` — 7/7 PASS); price book валидация и загрузка
  (`tests/test_price_book.py` — 22/22 PASS). Итого 19/19+22=41/41 cost-related тестов PASS.
- **C7 — batch/export:** batch_max_512 проверен (`test_export_worker.py`);
  LangfuseExporter: export, retry, flush, TLS, config — 23/23 PASS
  (`test_langfuse_exporter.py`).

## Методика для pending-критериев (после поднятия стенда)

```bash
cd infra && docker compose up -d
# получить public/secret key в Langfuse UI, наполнить .env, перезапустить
docker compose up -d
# запустить пилотного агента / генератор синтетических трейсов ~30 минут
```

- **C1:** `count(distinct trace_id)` в Langfuse / `agent_obs_spans_total` ≥ 0.99.
- **C2 (CPU-profile):** py-spy / cProfile на процесс агента при 100 RPS, 10 мин.
  Доля времени в коде SDK < 5%.
- **C3:** метрика доставки (см. `agent_obs_export_errors_total` == 0) + замер
  времени ingest на реальном стенде.
- **C5:** сумма `cost.usd` за окно vs отчёт OpenAI billing (эталон — расчёт по
  зафиксированному набору запросов).
- **C7 (latency):** замер `LangfuseExporter.export` на реальном Langfuse endpoint,
  батч 512 span'ов — p95 < 200 мс.

## Сводка

- **PASS by tests:** C2 (latency), C4, C5 (unit), C6, C7 (logic) — **256/256 тестов PASS**.
- **PENDING (live stand):** C1, C2 (CPU-profile), C3, C5 (billing), C7 (latency).

**Промежуточный вердикт:** MVP-код полностью покрыт тестами (256/256 PASS).
Все критерии, проверяемые автоматически, пройдены.
Ожидается live-стенд (docker compose + Langfuse) для финальной приёмки.
При провалах pending-критериев — цикл P27.