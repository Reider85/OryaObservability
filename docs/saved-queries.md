# Saved queries для Langfuse UI (P24)

Три сохранённых вью для дежурного инженера. Создаются в Langfuse UI
(**Traces → Filters → Save**), работают на root-атрибутах, которые SDK
штампует на `agent.loop`-span через агрегацию per-trace (P20/P24):
`status`, `cost.usd_sum`, `cost.over_budget`, `steps.count`.

## Query 1 — «errors-24h»

- **Назначение:** трейсы со статусом error за последние 24 часа.
- **Фильтр Langfuse:**
  ```text
  trace.status = ERROR
  AND trace.timestamp >= now() - 24h
  ```
- **Открывать:** в начале инцидента, чтобы получить трейсы, требующие разбора.

## Query 2 — «cost-over-5c»

- **Назначение:** трейсы с суммарной стоимостью выше $0.05.
- **Фильтр Langfuse:**
  ```text
  observation.metadata.cost.usd_sum > 0.05
  AND trace.timestamp >= now() - 24h
  ```
- **Открывать:** для cost-аудита; совпадает с политикой tail-sampler
  `TAIL_SAMPLER_COST_THRESHOLD` — дорогие трейсы сохраняются всегда.

## Query 3 — «long-loops»

- **Назначение:** трейсы с числом дочерних span'ов > 10.
- **Фильтр Langfuse:**
  ```text
  observation.metadata.steps.count > 10
  AND trace.timestamp >= now() - 24h
  ```
- **Открывать:** для поиска «зациклившихся» агентов (agent.loop с большим
  числом llm.call/tool.call).

## Как SDK заполняет атрибуты root-span

| Атрибут | Источник | Где вычисляется |
|---|---|---|
| `status` | статус `agent.loop`; `error`, если любой дочерний span error | `ObservabilitySDK.agent_observed` + `_accumulate_trace_aggregates` |
| `cost.usd_sum` | сумма `cost.usd` всех `llm.call` трейса | `_accumulate_trace_aggregates` (`observability.py`) |
| `cost.over_budget` | `"true"`, если `cost.usd_sum > TAIL_SAMPLER_COST_THRESHOLD` | там же |
| `steps.count` | число span'ов трейса (llm.call + tool.call + root) | там же |
| `security.incident` | MVP-заглушка, всегда `"false"` | там же |

> Примечание: точное имя поля в Langfuse-фильтре зависит от маппинга
> атрибутов (см. `agent_obs/exporters/otlp_mapping.py`): в UI атрибуты
> видны как `observation.metadata.<key>`.