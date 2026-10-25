# Agent Observability Trace Viewer

PP10 реализация lightweight trace viewer с expandable events, поиском и keyboard navigation.

## Запуск

```bash
# Запуск trace viewer
python -m agent_obs.trace_viewer

# Или через uvicorn напрямую
uvicorn agent_obs.trace_viewer.ui:app --host 127.0.0.1 --port 8089
```

По умолчанию запускается на `http://127.0.0.1:8089`. Порты хост можно изменить через переменные окружения:
- `AGENT_OBS_VIEWER_PORT` (default: 8089)
- `AGENT_OBS_VIEWER_HOST` (default: 127.0.0.1)

## Функции

### 1. Expandable Events

Агрегированные span'ы показываются с кнопкой "show N events". По клику разворачивается таблица с микро-span'ами:

- **Auto-expand**: `?expand=events` в URL автоматически разворачивает все секции событий
- **Lazy load**: события загружаются только при раскрытии (через `/api/traces/{id}/spans/{span_id}/events`)
- **Кап событий**: максимум 200 событий на span (защита от cardinality)

### 2. Поиск в событиях

Input "search in events" фильтрует события по подстроке в name/attributes. Работает клиент-сайд после загрузки событий.

### 3. Выделение ошибок

События с `status="error"` выделяются красным фоном (класс `event-row--error`).

### 4. Keyboard Navigation

- **ArrowUp/ArrowDown**: навигация между строками
- **Enter/Space**: toggle expand/collapse для текущей строки
- **Escape**: collapse все события

### 5. Lazy Render

События рендерятся чанками (100 строк/чанк) при раскрытии для больших трейсов.

## API Endpoints

| Метод | Путь | Описание |
|---|---|---|
| GET | `/` | Главная страница с формой ввода trace_id |
| GET | `/trace/{trace_id}` | Просмотр трейса с expandable events |
| GET | `/trace/{trace_id}?expand=events` | Автоматическое раскрытие событий |
| GET | `/api/traces/{trace_id}/spans/{span_id}/events` | JSON событий для lazy load |
| GET | `/health` | Health check |

## Тестирование

### Unit tests (TestClient)

```bash
# Тесты для viewer UI
python -m pytest tests/test_trace_viewer.py -q

# Тесты для Langfuse event mapping
python -m pytest tests/test_langfuse_event_mapping.py -q
```

### E2E tests (Playwright)

```bash
# Установка браузеров (один раз)
playwright install chromium

# Запуск e2e тестов
python -m pytest tests/e2e/ -q

# Или с запуском сервера в фоне
python -m pytest tests/e2e/test_trace_viewer_e2e.py -v --tb=short
```

Тесты автоматически пропускаются если Playwright не установлен или браузеры недоступны.

## Интеграция с Langfuse

PP10 добавляет Langfuse mapping: каждое событие агрегированного span'а отправляется как дочерний OTLP span с:

- `langfuse.observation.type = "event"`
- `langfuse.observation.metadata.*` — атрибуты события
- `langfuse.observation.level = "ERROR"` для ошибок
- Детерминированный spanId (idempotent при retry)

Это позволяет видеть события как expandable EVENT observations в Langfuse UI.

## Пример использования

1. Откройте `http://127.0.0.1:8089`
2. Введите trace_id (например, из Grafana drill-down)
3. Нажмите "View Trace"
4. Кликните "Show N events" на агрегированном span'е
5. Используйте поиск для фильтрации событий
6. Нavigujte с клавиатурой

## Ручной чек-лист (DoD verification)

1. [ ] Агрегированные span'ы показываются с expandable секцией events
2. [ ] Поиск по events фильтрует корректно
3. [ ] Error-events выделены красным
4. [ ] Lazy render предотвращает тормоза на больших трейсах
5. [ ] E2E-тесты зелёные

## Зависимости

- FastAPI (уже в api extra)
- Playwright (только для e2e тестов, в dev extra)
- ClickHouse driver (через hot_store dependency injection)