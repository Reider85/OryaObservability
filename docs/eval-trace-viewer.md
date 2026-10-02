# Eval-панель в trace-viewer (PC13)

Как eval-результаты видны в trace-viewer'е Langfuse/Phoenix: какие атрибуты
SDK выставляет на span'ах, как pending-состояние отображается до прихода
LLM-judge и как добраться до полных результатов через Query API.

**Screenshot: PENDING — live stand.** Снять Phoenix (`http://localhost:6006`)
или Langfuse (`http://localhost:3000`) после запуска стенда
(`./scripts/start-all.ps1`) и положить PNG рядом с этим файлом
(`docs/eval-trace-viewer.png`), затем вставить изображение в раздел
«Пример» ниже.

## Что видно в trace-viewer

| Бейдж / поле | Источник (span-атрибут) | Когда |
|---|---|---|
| `eval.results` (passed/failed/flags) | `eval.rule_based` | сразу при экспорте span'а |
| `PENDING` + таймстамп | `eval.pending=True` | пока LLM-judge job в очереди |
| `eval.llm_judge.status` | `eval.llm_judge.status` = `pending` / `skipped` | при отправке job'а / при сэмплинге |

Late annotation НЕ модифицирует закрытый span (он уже в ring buffer /
exporter'е, §7.3 ARCHITECT.md): результат LLM-judge живёт отдельно в
ClickHouse `eval_results_hot`, привязан к `trace_id`. Поэтому в UI
pending-бейдж остаётся на span'е, а итоговые scores приходят «сзади» и
доступны через Query API.

## Mapping атрибутов

| Внутренний атрибут | Label в Langfuse | OTLP-enrichment (`otlp_mapping.enrich_eval_attributes`) |
|---|---|---|
| `eval.rule_based` | `eval.results` | `eval.rule_based_passed`, `eval.rule_based_failed`, `eval.rule_based_flags` |
| `eval.pending` | `eval.pending` | `eval.status=pending`, `eval.pending_timestamp` |
| `eval.llm_judge.status` | `eval.llm_judge.status` | `eval.llm_judge_status` |

В Langfuse атрибуты видны как `observation.metadata.<key>`.

## Кто выставляет атрибуты (SDK)

- **`eval.rule_based`** — `ObservabilitySDK._apply_rule_based_eval` в `_enqueue`
  (после guardrail-маскировки, до ring buffer). Форма значения:
  `{"rules_passed": N, "rules_failed": M, "flags": [...]}`.
  Правила грузятся из `configs/eval_rules.yaml`; включается передачей
  `rule_based_evaluator=` в конструктор SDK или
  `AGENT_OBS_EVAL_RULES_ENABLED=true`.
- **`eval.pending` / `eval.llm_judge.status`** —
  `ObservabilitySDK._apply_llm_judge_pending` в `agent_observed` finally,
  когда сконфигурирован `llm_judge=` и job успешно положен в RQ-очередь.
  Отключается: `AGENT_OBS_LLM_JUDGE_ENABLED=false`.

Оба пути fail-open: ошибка evaluator'а логируется, span уходит в export
без eval-атрибутов — observability никогда не блокирует бизнес-логику.

## Пример (ASCII-mockup)

```text
┌ trace 01M3XWX8NDKSQEAT... ──────────────────────────────────────────┐
│ agent.loop:my-agent                    status=ok  cost=$0.0031      │
│ ├─ llm.call:gpt-4o-mini                tokens 1.2k/340             │
│ │    eval.results: passed=4 failed=0                                │
│ ├─ tool.call:search        status=ok                               │
│ └─ llm.call:gpt-4o-mini                eval: PENDING (12:01:05Z)   │
│                                          └ llm_judge queued         │
│                                                                     │
│ Eval panel (после late annotation, GET /traces/{id}/evals):         │
│   faithfulness      0.92                                            │
│   answer_relevancy  0.88                                            │
│   completeness      0.85                                            │
└─────────────────────────────────────────────────────────────────────┘
```

## Query API — полные результаты по trace_id

```bash
GET /traces/{trace_id}/evals
→ {"trace_id": "...", "eval_results": [{"eval_name": "llm_judge", "scores": {...}, ...}]}
```

Источник: ClickHouse `eval_results_hot` (primary), Redis fallback
(`eval_fallback:{trace_id}:*`, TTL 1h) — см. `agent_obs/eval/late_annotation.py`.
Пример ответа и код — `agent_obs/eval/api.py`, тесты — `tests/test_eval_api.py`.

## Ручная проверка (UI check, PC13 DoD)

1. `./scripts/start-all.ps1` + `AGENT_OBS_EVAL_RULES_ENABLED=true` на агенте.
2. Прогнать тестовый трейс пилотного агента.
3. Открыть trace-viewer → на `llm.call` видеть `eval.rule_based`
   (passed/failed/flags), на `agent.loop` — бейдж `PENDING` до готовности judge.
4. Дождаться job (5–30 c) → `GET /traces/{trace_id}/evals` вернёт scores.
5. Снять скриншот → `docs/eval-trace-viewer.png` (см. шапку файла).

## Связанные документы

- `docs/saved-queries.md` — какие атрибуты root-span штампит SDK.
- `docs/mvp-acceptance.md` — приёмка MVP, pending-критерии live-стенда.
- `analytics/CRITICAL-PROMPTS.md` PC09/PC10/PC12/PC13 — исходные тикеты.
