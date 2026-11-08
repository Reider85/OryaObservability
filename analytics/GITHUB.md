# GITHUB.md — Стратегия роста OryaObservability через GitHub CI/CD

> Репозиторий: `Reider85/OryaObservability` (пакет `agent-obs`)
> Технологии: Python 3.11+, OpenTelemetry, Langfuse, ClickHouse, Vault, Redis, Prometheus, Grafana, Postgres, S3/MinIO, Athena
> Лицензия: Apache-2.0 · Тестов: ~1344 · Звёзд: 1 · Форков: 0
> Дата анализа: 2026-11-08

Документ состоит из трёх частей:

1. **Диагноз** — что реально лежит в репозитории по CI и почему всё «failed».
2. **Карта промптов** — какой промпт что закрывает и что даёт для звёзд/trending.
3. **Сами промпты** — 7 штук, готовых к вставке в AI-ассистента (Claude / Codex / GPT / Cursor), чтобы тот сгенерировал готовые workflow-файлы и сопутствующие правки под этот репозиторий.

Промпты написаны на русском, но все пути, команды и имена файлов оставлены на английском — их нужно сохранить дословно.

---

## 1. Диагноз: что сейчас не так с CI

### 1.1. Что физически лежит в `.github/workflows`

| Признак | Наблюдение | Вывод |
| --- | --- | --- |
| `CONTRIBUTING.md`, строка 239 | «The only GitHub Actions workflow today is `.github/workflows/canary.yml` (PP29 SLO-gated canary). There is **no CI workflow for tests / lint / typecheck** — the reviewer runs them locally. (Adding one is welcome — open an issue first.)» | Тестов / линта / тайпчека в CI нет в принципе. |
| `raw.githubusercontent.com/.../.github/workflows/canary.yml` | HTTP 404 | Файл либо удалён, либо лежит не по тому пути, либо имеет расширение `.yaml`. |
| Вкладка Actions на GitHub | «0 workflow runs» + упоминание workflow с именем «canary» | Workflow зарегистрирован в метаданных, но ни разу не отработал (или отработал, но не виден). |
| `README.md`, строка 301 и `AGENTS.md` | Ссылки на `.github/workflows/canary.yml` как на реализацию PP29 | Файл задумывался, но фактически отсутствует/сломан. |

### 1.2. Почему canary «всегда failed», если он всё-таки запускается

`canary.yml` задуман как **SLO-gated canary deployment gate** (тикет PP29). По `AGENTS.md` и `CONTRIBUTING.md` его корректная работа требует:

- `CANARY_RAMP_CMD` / `CANARY_ROLLBACK_CMD` / `CANARY_DEPLOY_CMD` — внешние команды деплоя. Репозиторий их **не предоставляет** («repo ships no deploy tooling»).
- Живую телеметрию burn-rate из ClickHouse через `scripts/canary_check.py`.
- `AGENT_OBS_CANARY_FAIL_OPEN=false` (по умолчанию) → при невозможности вычислить burn-rate воркфлоу **fail-closed → rollback → ненулевой exit → красный крест**.

То есть canary **спроектирован падать в окружении без прод-телеметрии и без deploy-команд**. В GitHub Actions у вас нет ни того, ни другого, поэтому workflow всегда выходит ненулевым. Это не баг — это архитектурное решение «fail-closed» для canary-деплоя. Но для целей «мелькать в trending и набирать звёзды» этот воркфлоу **бесполезен**: он не прогоняет тесты, не пушит релизы, не даёт бейджей и при этом стабильно красный, что отпугивает звёзд.

### 1.3. Что реально даст перенос работы на GitHub Actions

GitHub Actions для **публичных** репозиториев бесплатно предоставляет:

- Неограниченные минуты на `ubuntu-latest` (2 vCPU, 7 ГБ ОЗУ, 14 ГБ SSD).
- 1 ГБ кэша (`actions/cache`), 500 МБ артефактов на 90 дней.
- `github/codeql-action` — бесплатно для public.
- `codecov` — бесплатно для public.
- `actions/deploy-pages` — бесплатный статический хостинг документации.
- `softprops/action-gh-release`, `release-drafter/release-drafter` — бесплатно.

Что вы получаете, перенеся работу с локальной машины на GitHub:

| Сейчас локально | Станет в CI | Эффект |
| --- | --- | --- |
| `pytest --ignore=tests/ha --ignore=tests/load` (~90 с, 1344 теста) | На каждый push + PR | Регрессии ловятся до мержа; бейдж в README → доверие → звёзды. |
| `ruff check agent_obs tests` (baseline ~830 ошибок) | На каждый push + PR, с baseline | Чистые диффы; аннотации прямо в PR. |
| `mypy agent_obs` (baseline ~116 ошибок) | На каждый push + PR, с baseline | Защита контрактов; бейдж. |
| `pytest tests/e2e/` (Playwright) | На каждый push в main | e2e-регрессии trace-viewer ловятся автоматически. |
| `pytest tests/load/` (p99 overhead) | Nightly job | Не висит на каждом пуше, но даёт тренд. |
| Релизы руками | Автоматически из Conventional Commits | Активная страница Releases → discovery → звёзды. |
| Документация только в `docs/` | GitHub Pages | Дополнительный surface area + SEO. |
| Секреты в `.env` | Dependabot + CodeQL | Security tab → доверие. |

### 1.4. Честно про GitHub Trending

Trending считается по **приросту звёзд за окно** (daily / weekly / monthly), а не по числу коммитов или релизов. Поэтому «релиз на каждый пуш» напрямую в Trending не попадает. Но он даёт **три косвенных эффекта**, которые как раз и кормят Trending:

1. **Активная страница Releases** — GitHub показывает свежие релизы в «Explore → Releases» и в «Trending → repositories with recent releases». Каждый релиз — это отдельный «пост» в ленте подписчиков тех, кто star-нул репозиторий.
2. **Социальный сигнал «живого проекта»** — зелёные галки на коммитах + свежий релиз + открытые Discussions = выглядит поддерживаемым → выше конверсия посетитель → звезда.
3. **Повод для анонса** — каждый релиз можно транслировать в Twitter/LinkedIn/Reddit/HackerNews/Хабр, что и даёт сам прирост звёзд.

Важно: **буквальный «релиз на каждый коммит» — плохая идея**. GitHub с большой вероятностью пометит такие релизы как спам и понизит репозиторий в выдаче, а подписчиков начнёт бесить шум. Правильный паттерн — **draft-релиз накапливается на каждый push, а реальный tag+release режется при мерже PR в main** (или при bump версии по Conventional Commits). Именно так сделано в `fastify`, `pytest`, `ruff`. Все промпты ниже используют этот паттерн.

### 1.5. Что даст совокупное внедрение всех промптов

- ✅ Перенос тестов / линта / тайпчека / e2e / load с вашей машины на GitHub — бесплатно.
- ✅ Активная страница Releases (1 релиз на каждый мерж PR в main) + автогенерация changelog из Conventional Commits.
- ✅ 5 живых бейджей в README: CI, coverage, Python versions, CodeQL, Discord/Pages.
- ✅ Security tab с CodeQL + Dependabot PR — каждые 2 недели свежий dependency-PR = активность.
- ✅ GitHub Pages с документацией — отдельный surface area и ссылочный вес.
- ✅ Discussions + issue/PR templates — снижают трение для контрибьюторов → больше PR → больше «активности» в глазах GitHub → выше в выдаче.
- ✅ Оценка: при текущем качестве README и 1344 тестах, переход от «красного canary без CI» к «зелёного CI с 5 бейджами и еженедельными релизами» даёт реалистично +50…+200 звёзд за 1–2 месяца при минимальном external-пуше (анонсы в соцсетях на каждый минорный релиз).

---

## 2. Карта промптов

| № | Промпт | Что закрывает | Эффект на звёзды/trending | Эффект на «перенос ресурсов на GitHub» |
| --- | --- | --- | --- | --- |
| 1 | Заменить падающий canary на рабочий CI | «Почему всегда failed», прогон тестов/линта/тайпчека | Бейдж CI → доверие | Тесты идут на GitHub, не у вас |
| 2 | Авто-релиз на push в main | «Релиз на каждый пуш» | Активная Releases-лента → discovery | Релизы режутся GitHub-ом |
| 3 | Coverage + мульти-Python матрица | Бейдж качества | «tested on 3.11/3.12/3.13, 80% cov» → доверие | Матрица идёт параллельно, бесплатно |
| 4 | CodeQL + Dependabot | Security tab | Бейдж безопасности → доверие | Сканинг идёт на GitHub |
| 5 | GitHub Pages для документации | Surface area | Отдельный сайт → SEO → трафик | Хостинг на GitHub |
| 6 | Discoverability-пак | Topics, Discussions, templates, release-drafter | Прямое влияние на выдачу GitHub | — |
| 7 | Nightly load + e2e job | Тяжёлые тесты отдельно | «nightly» бейдж → зрелость | p99 тесты идут night-раннером |

Все промпты независимы — можно запускать параллельно разными сессиями AI-ассистента. Рекомендуемый порядок внедрения: **1 → 2 → 6 → 3 → 4 → 5 → 7** (quick wins сначала).

---

## 3. Промпты

Каждый промпт — самодостаточная инструкция для AI-ассистента. Скопируйте текст из блока ` ```text ... ``` ` целиком и отдайте ассистенту с доступом к репозиторию.

---

### Промпт 1 — Заменить падающий canary на рабочий CI-пайплайн (тесты + линт + тайпчек)

```text
Ты — DevOps-инженер. Работай в репозитории Reider85/OryaObservability (Python 3.11+ SDK `agent-obs`).

КОНТЕКСТ:
- В `.github/workflows/` сейчас лежит только (сломанный) canary-воркфлоу, который всегда падает, потому что спроектирован fail-closed без прод-телеметрии и deploy-команд.
- CONTRIBUTING.md прямо признаёт: «There is no CI workflow for tests / lint / typecheck — the reviewer runs them locally».
- Тестовая база: ~1344 теста, pytest + pytest-asyncio, asyncio_mode=auto, testpaths=["tests"].
- Fast-сьют: `python -m pytest --ignore=tests/ha --ignore=tests/load -q --tb=short` (~90 с).
- HA-сьют (tests/ha) требует 40 docker-сервисов и 16 ГБ ОЗУ — в CI не запускаем.
- Load-сьют (tests/load) — медленные p99-тесты, отправляем в отдельный nightly job (см. Промпт 7).
- e2e-сьют (tests/e2e) — Playwright, требует `playwright install chromium`.
- Базовый ruff: ~830 ошибок по умолчанию; mypy: ~116 ошибок. Правило из CONTRIBUTING.md: фиксим только то, что задели в PR.
- Установка: `pip install -e ".[dev]"` (включает ml + storage + api + cron + test deps).
- `pytest-timeout` НЕ установлен — `--timeout=N` падает. Не добавлять.
- В PowerShell всегда `2>&1`, в CI это не нужно.

ЗАДАЧА:
1. Удали файл `.github/workflows/canary.yml` (если существует). Он не подходит для роли CI — это SLO-gated canary-deploy gate, его место в отдельном deploy-пайплайне, а не в проверке пушей. Если файл имеет расширение `.yaml`, удали и его.
2. Создай `.github/workflows/ci.yml` со следующей структурой:
   - name: CI
   - on: push (branches [main]), pull_request (branches [main])
   - concurrency: group `ci-${{ github.ref }}`, cancel-in-progress: true
   - jobs:
     a) `lint` (ubuntu-latest, Python 3.11):
        - checkout с `fetch-depth: 0`
        - setup-python с cache pip
        - `pip install ruff mypy`
        - `ruff check --output-format=github agent_obs tests` — НЕ падает на baseline, для этого сначала сохрани baseline: `ruff check agent_obs tests --exit-zero > .ruff-baseline.txt` локально один раз и закоммить; в CI используй `ruff check agent_obs tests --output-format=github` и отдельно сравнение с baseline. Проще: на первом этапе поставь `ruff check --exit-zero --output-format=github agent_obs tests` (только аннотации, не red), а когда baseline почистишь — переключишь на строгий режим. В комментарии к workflow объясни это.
        - `mypy agent_obs --install-types --non-interactive` — аналогично с `--exit-zero` на старте, комментарий что это временно.
     b) `test` (ubuntu-latest, matrix Python 3.11/3.12/3.13):
        - checkout
        - setup-python с cache pip
        - `pip install -e ".[dev]"`
        - `python -m pytest --ignore=tests/ha --ignore=tests/load -q --tb=short --junitxml=pytest.xml`
        - `actions/upload-artifact@v4` с `pytest.xml` (имя `pytest-results-py${{ matrix.python-version }}`)
        - `dorny/test-reporter@v1` для красивых аннотаций в PR (with `path: pytest.xml`, `reporter: java-junit`)
        - `env: AGENT_OBS_ENABLED: "true", AGENT_OBS_FAIL_ON_CONFIG: "0", AGENT_OBS_GUARDRAIL_ENABLED: "false"` чтобы тесты не падали на отсутствие Langfuse/Vault.
     c) `e2e` (ubuntu-latest, Python 3.11, только на push в main и PR с label `run-e2e`):
        - `pip install -e ".[dev]"`
        - `python -m playwright install --with-deps chromium`
        - `python -m pytest tests/e2e/ -q --tb=short`
3. Добавь в начало README.md, сразу после существующих бейджей, бейдж CI:
   `[![CI](https://github.com/Reider85/OryaObservability/actions/workflows/ci.yml/badge.svg)](https://github.com/Reider85/OryaObservability/actions/workflows/ci.yml)`
4. В CONTRIBUTING.md замени абзац «The only GitHub Actions workflow today is `.github/workflows/canary.yml`…» на новый: «CI runs on every push and PR via `.github/workflows/ci.yml`: fast pytest suite on Python 3.11/3.12/3.13, ruff (annotation-only until baseline cleared), mypy (annotation-only until baseline cleared), Playwright e2e on push to main. The canary workflow was removed — SLO-gated canary deploy is the responsibility of the deploy pipeline, not CI.»

КРИТЕРИЙ ПРИЁМКИ:
- `ci.yml` валиден по `actionlint` (запусти `actionlint` локально или через https://rhysd.github.io/actionlint/).
- На следующем пуше в main все три job-а зелёные (с `--exit-zero` для ruff/mypy на старте).
- В PR появляются inline-аннотации ruff и mypy.
- Бейдж CI отображается в README.

ПОЧЕМУ ЭТО ВАЖНО ДЛЯ ЗВЁЗД:
Зелёный бейдж CI в README — самый дешёвый сигнал «проект жив и поддерживается». Посетитель, увидевший красный Actions, уходит без star. Рабочий CI = +доверие = выше конверсия посетитель → звезда.
```

---

### Промпт 2 — Авто-релиз на каждый пуш в main (Conventional Commits + GitHub Releases)

```text
Ты — Release-инженер. Работай в репозитории Reider85/OryaObservability.

КОНТЕКСТ:
- Проект уже использует Conventional Commits со scope (см. CONTRIBUTING.md раздел «Commit Messages»): типы feat/fix/docs/refactor/test/chore/sec/perf, scope — модуль.
- Версия пакета живёт в `pyproject.toml` как `version = "0.1.0"` в секции `[project]`.
- Релизы сейчас не выпускаются вообще (вкладка Releases пуста).
- Цель: при каждом мерже PR в main автоматически резать GitHub Release с автосгенерированным changelog. Не «на каждый коммит» (это спам и GitHub это пенализирует), а «на каждый merged PR» + «на каждый push тега v*».

ЗАДАЧА:
1. Создай `.github/workflows/release.yml`:
   - name: Release
   - on:
       push:
         branches: [main]
         tags: ['v*']
   - permissions: contents: write, pull-requests: read
   - jobs:
     a) `draft-release` (на push в main):
        - uses: `release-drafter/release-drafter@v6`
        - env: GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
        - with:
            config-name: release-drafter.yml
            publish: false
            version-resolver:
              major: { labels: ['major'] }
              minor: { labels: ['minor'] }
              default: patch
     b) `tagged-release` (на push тега v*):
        - uses: `actions/checkout@v4` with fetch-depth: 0
        - uses: `softprops/action-gh-release@v2`
        - with:
            generate_release_notes: true
            draft: false
            prerelease: false
            files: |
              LICENSE
              README.md
        - env: GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}

2. Создай `.github/release-drafter.yml` с конфигом, который группирует коммиты по типам:
   - `feat:` → «🚀 Features»
   - `fix:` → «🐛 Bug Fixes»
   - `sec:` → «🔒 Security»
   - `perf:` → «⚡ Performance»
   - `refactor:` → «♻️ Refactor»
   - `docs:` → «📚 Documentation»
   - `test:` → «🧪 Tests»
   - `chore:` → «🔧 Chore» (collapsed)
   - template вида: `$CHANGES\n\n**Full Changelog**: $COMPARE`
   - categories с labels по типам (feat → minor, fix → patch, sec → patch, breaking → major)
   - changelog-sections для фильтра `chore(deps):` в отдельную секцию «Dependencies»

3. Создай `.github/workflows/bump-version.yml` — на каждый мерж PR с label `release:minor` или `release:patch` (или `release:major`):
   - использует `actions/create-pr@v1` или скрипт `python -c "import tomllib, tomli_w; ..."` чтобы bump-нуть версию в `pyproject.toml`
   - создаёт коммит `chore(release): vX.Y.Z`
   - создаёт и пушит тег `vX.Y.Z` (что триггерит `tagged-release` job выше)
   - env: GITHUB_TOKEN с `contents: write`

4. В README.md добавь бейдж:
   `[![GitHub release](https://img.shields.io/github/v/release/Reider85/OryaObservability?include_prereleases)](https://github.com/Reider85/OryaObservability/releases)`

5. В CONTRIBUTING.md добавь в раздел «Pull Requests» новый пункт: «Add one of `release:patch` / `release:minor` / `release:major` labels to your PR. On merge, the release workflow bumps the version, tags, and publishes a GitHub Release with auto-generated changelog. PRs without a label skip the release.»

КРИТЕРИЙ ПРИЁМКИ:
- При мерже тестового PR с label `release:patch` на странице Releases появляется новый релиз `v0.1.1` с авто-сгенерированным changelog, разбитым по категориям.
- При пуше тега `v0.1.1` вручную — релиз тоже публикуется (на случай ручного хотфикса).
- Бейдж версии отображается в README.

ПОЧЕМУ ЭТО ВАЖНО ДЛЯ ЗВЁЗД:
Каждый релиз — это отдельная «карточка» в ленте Releases GitHub и в ленте подписчиков тех, кто star-нул репо. 8 релизов в месяц = 8 поводов для анонса в Twitter/Reddit/Хабр. Активная Releases-страница — один из сильнейших косвенных факторов Trending: GitHub помечает такие репо как «recently updated».
```

---

### Промпт 3 — Coverage + мульти-Python матрица (бейджи качества)

```text
Ты — QA-инженер. Работай в репозитории Reider85/OryaObservability.

КОНТЕКСТ:
- Fast-сьют: `python -m pytest --ignore=tests/ha --ignore=tests/load -q --tb=short` (~90 с, 1344 теста).
- Python 3.11+ объявлен в `pyproject.toml` (`requires-python = ">=3.11"`), CONTRIBUTING.md упоминает что 3.12 и 3.13 тоже тестируются.
- В `pyproject.toml` секции `[tool.coverage]` нет — нужно добавить.
- `pytest-cov` не в зависимостях dev-extras — нужно добавить.

ЗАДАЧА:
1. В `pyproject.toml`, в `[project.optional-dependencies].dev`, добавь `pytest-cov>=4.1` (между `pytest-asyncio` и `respx`).
2. В `pyproject.toml` добавь секцию:
   ```toml
   [tool.coverage.run]
   source = ["agent_obs"]
   branch = true
   omit = ["*/tests/*", "*/scripts/*", "*/pilot_agent/*", "*/__pycache__/*"]

   [tool.coverage.report]
   show_missing = true
   skip_covered = false
   exclude_lines = [
     "pragma: no cover",
     "raise NotImplementedError",
     "if __name__ == .__main__.:",
     "if TYPE_CHECKING:",
   ]
   ```
3. Дополни существующий `.github/workflows/ci.yml` (созданный в Промпте 1):
   - В job `test` для Python 3.11 (или отдельно в новый job `coverage`):
     - Замени команду pytest на: `python -m pytest --ignore=tests/ha --ignore=tests/load -q --tb=short --cov=agent_obs --cov-report=xml:coverage.xml --cov-report=term-missing --junitxml=pytest.xml`
     - Добавь `codecov/codecov-action@v4` с `files: ./coverage.xml`, `flags: unittests`, `fail_ci_if_error: false`.
   - Для Python 3.12 и 3.13 оставь обычный pytest без coverage (чтобы не гонять cov трижды).
4. В README.md после бейджа CI добавь два бейджа:
   ```
   [![Coverage](https://codecov.io/gh/Reider85/OryaObservability/graph/badge.svg)](https://codecov.io/gh/Reider85/OryaObservability)
   [![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-blue.svg)](https://www.python.org/downloads/)
   ```
   (старый бейдж `python-3.11%2B` замени на новый с тремя версиями).
5. Подключи репозиторий к codecov.io (инструкция для пользователя: зайди на app.codecov.io, залогинься через GitHub, enable для Reider85/OryaObservability — токен не нужен для public repo, но если попросит — добавь `CODECOV_TOKEN` в repo secrets).

КРИТЕРИЙ ПРИЁМКИ:
- В PR появляется комментарий codecov с diff-coverage и трендом.
- В README отображаются живые бейджи coverage и Python-версий.
- Все три Python-версии (3.11/3.12/3.13) зелёные в матрице.

ПОЧЕМУ ЭТО ВАЖНО ДЛЯ ЗВЁЗД:
«tested on 3.11 | 3.12 | 3.13» и живой coverage-бейдж — два самых сильных качества-сигнала для Python-библиотек. Посетители сравнивают с альтернативами и star-ят тот, у которого «всё зелёное». Дополнительно: matrix-бейдж работает как фильтр «автор заботится о совместимости».
```

---

### Промпт 4 — CodeQL + Dependabot (Security tab бесплатно)

```text
Ты — Security-инженер. Работай в репозитории Reider85/OryaObservability (Python SDK).

КОНТЕКСТ:
- Репозиторий публичный → CodeQL и Dependabot бесплатны.
- Проект security-sensitive: PII guardrail, Vault, RBAC, mTLS. Security tab сейчас пустой.
- В `pyproject.toml` есть тяжёлые deps: torch, transformers, spacy, presidio — Dependabot будет часто присылать PR'ы на их обновление.

ЗАДАЧА:
1. Создай `.github/workflows/codeql.yml`:
   - name: CodeQL
   - on:
       push: branches: [main]
       pull_request: branches: [main]
       schedule: cron: '0 2 * * 1'  # каждый понедельник 02:00 UTC
   - jobs:
     analyze:
       runs-on: ubuntu-latest
       permissions: actions: read, contents: read, security-events: write
       strategy:
         matrix:
           language: [python]
       steps:
         - uses: actions/checkout@v4
         - uses: github/codeql-action/init@v3
           with:
             languages: ${{ matrix.language }}
             queries: security-extended, security-and-quality
         - uses: github/codeql-action/autobuild@v3
         - uses: github/codeql-action/analyze@v3
           with:
             category: "/language:${{ matrix.language }}"

2. Создай `.github/dependabot.yml`:
   ```yaml
   version: 2
   updates:
     - package-ecosystem: "pip"
       directory: "/"
       schedule:
         interval: "weekly"
         day: "monday"
       open-pull-requests-limit: 5
       labels: ["dependencies", "release:patch"]
       groups:
         dev-deps:
           dependency-type: "development"
         prod-deps:
           dependency-type: "production"
       commit-message:
         prefix: "chore(deps)"
         include: "scope"
     - package-ecosystem: "github-actions"
       directory: "/"
       schedule:
         interval: "weekly"
       labels: ["dependencies", "ci"]
       commit-message:
         prefix: "chore(ci)"
         include: "scope"
   ```
   Обрати внимание: `release:patch` label на dependabot-PR означает, что при мерже (если настроен Промпт 2) автоматически выйдет патч-релиз — это даёт постоянный поток свежих релизов «без ваших ресурсов».

3. В README.md добавь бейджи:
   ```
   [![CodeQL](https://github.com/Reider85/OryaObservability/actions/workflows/codeql.yml/badge.svg)](https://github.com/Reider85/OryaObservability/actions/workflows/codeql.yml)
   [![Dependabot](https://img.shields.io/badge/dependencies-Up%20to%20date-brightgreen.svg)](https://github.com/Reider85/OryaObservability/network/dependencies)
   ```

4. В CONTRIBUTING.md в раздел «Security-Sensitive Changes» добавь ссылку: «CodeQL runs on every push and weekly on Monday — findings appear in the Security tab. Dependabot opens weekly dependency PRs; merge them with the `release:patch` label to auto-publish a patch release.»

КРИТЕРИЙ ПРИЁМКИ:
- На странице Security → Code scanning alerts появляются результаты (если алертов нет — это нормально для чистого кода, важно что сам сканер запустился и зелёный).
- Dependabot в первый понедельник открывает 1–5 PR с обновлениями.
- Каждый мерж dependabot-PR автоматически выкатывает патч-релиз (через Промпт 2).

ПОЧЕМУ ЭТО ВАЖНО ДЛЯ ЗВЁЗД:
Security tab с зелёным CodeQL — это доверие для security-sensitive SDK. Плюс: зависловый поток dependabot-PR → мерж → авто-релиз = стабильная лента релизов «на автопилоте», что и даёт «мелькание» без ваших ресурсов. Каждые 1–2 недели — свежий релиз.
```

---

### Промпт 5 — GitHub Pages для документации

```text
Ты — Tech Writer. Работай в репозитории Reider85/OryaObservability.

КОНТЕКСТ:
- В репозитории уже есть `docs/` с runbook'ами (см. README «Project Structure»).
- README огромный (~700 строк) — много контента, который логично разбить на страницы.
- has_pages сейчас false (через API: `has_pages: false`).
- Цель: поднять `https://reider85.github.io/OryaObservability/` на GitHub Pages бесплатно, с mkdocs-material.

ЗАДАЧА:
1. Добавь в `pyproject.toml` новый extra `docs`:
   ```toml
   docs = [
     "mkdocs>=1.6",
     "mkdocs-material>=9.5",
     "mkdocs-autorefs>=1.0",
     "mkdocs-section-index>=0.3",
   ]
   ```

2. Создай `mkdocs.yml` в корне:
   ```yaml
   site_name: OryaObservability
   site_description: Production-grade observability for LLM agents
   site_url: https://reider85.github.io/OryaObservability/
   repo_url: https://github.com/Reider85/OryaObservability
   repo_name: Reider85/OryaObservability
   edit_uri: edit/main/docs/
   theme:
     name: material
     language: en
     features:
       - navigation.tabs
       - navigation.sections
       - navigation.expand
       - navigation.indexes
       - search.suggest
       - search.highlight
       - content.code.copy
       - content.code.annotate
     palette:
       - media: "(prefers-color-scheme: light)"
         scheme: default
         primary: deep purple
         accent: indigo
         toggle: { icon: material/weather-night, name: Switch to dark }
       - media: "(prefers-color-scheme: dark)"
         scheme: slate
         primary: deep purple
         accent: indigo
         toggle: { icon: material/weather-sunny, name: Switch to light }
     icon:
       repo: fontawesome/brands/github
   nav:
     - Home: index.md
     - Overview: overview.md
     - Architecture: architecture.md
     - Quick Start: quickstart.md
     - Configuration: configuration.md
     - Testing: testing.md
     - Security & Compliance: security.md
     - High Availability: ha.md
     - Dashboards: dashboards.md
     - Contributing: contributing.md
     - Runbooks:
         - MVP acceptance: mvp-acceptance.md
         - Support runbook: support-runbook.md
         - Field-aware masking: field-aware-masking.md
         - Eval trace viewer: eval-trace-viewer.md
         - Eval budget: eval-budget.md
         - SLO dashboard: slo-dashboard.md
         - Trace viewer: trace-viewer.md
         - Compliance UI: compliance-ui.md
         - Saved queries: saved-queries.md
   plugins:
     - search
     - autorefs
   markdown_extensions:
     - admonition
     - pymdownx.details
     - pymdownx.superfences:
         custom_fences:
           - name: mermaid
             class: mermaid
             format: !!python/name:pymdownx.superfences.fence_code_format
     - pymdownx.highlight:
         anchor_linenums: true
     - pymdownx.inlinehilite
     - pymdownx.tabbed:
         alternate_style: true
     - toc:
         permalink: true
   ```
   Используй `!!python/name:` ровно как написано — это валидный YAML для mkdocs.

3. Создай `docs/index.md` — сокращённую версию первого экрана README (заголовок, один абзац описания, ссылка на Quick Start, бейджи). Не дублируй весь README.

4. Создай символические ссылки или `mkdocs` include-механизм, чтобы контент из `docs/`-runbook'ов автоматически попадал в nav. Простейший вариант: в `docs/overview.md` и других страницах используй `{include}` из `mkdocs-include-markdown-plugin`, либо просто сделай отдельные `.md`-файлы на основе выжимок из README.

5. Создай `.github/workflows/docs.yml`:
   - name: Docs
   - on:
       push: branches: [main]
       workflow_dispatch:
   - permissions: contents: read, pages: write, id-token: write
   - concurrency: group: pages, cancel-in-progress: true
   - jobs:
     build:
       runs-on: ubuntu-latest
       steps:
         - uses: actions/checkout@v4
         - uses: actions/setup-python@v5 with python-version: '3.11'
         - run: pip install -e ".[docs]"
         - run: mkdocs build --strict --site-dir _site
         - uses: actions/upload-pages-artifact@v3 with path: _site
     deploy:
       needs: build
       runs-on: ubuntu-latest
       environment: github-pages
       steps:
         - uses: actions/deploy-pages@v4

6. В README.md после бейджей добавь:
   `[![Docs](https://img.shields.io/badge/docs-mkdocs%20material-7c3aed.svg)](https://reider85.github.io/OryaObservability/)`

7. В Settings репозитория (инструкция для пользователя): Settings → Pages → Source = GitHub Actions.

КРИТЕРИЙ ПРИЁМКИ:
- На `https://reider85.github.io/OryaObservability/` открывается сайт с навигацией.
- `mkdocs build --strict` не падает (нет битых ссылок).
- Поиск работает.
- Каждое обновление main автоматически пересобирает сайт.

ПОЧЕМУ ЭТО ВАЖНО ДЛЯ ЗВЁЗД:
Отдельный сайт документации = отдельный URL, который индексируется Google. Чем больше индексированных страниц с упоминанием «LLM observability», тем больше органического трафика. Сайт с material-тёмной темой выглядит «дорого» — это напрямую влияет на желание поставить звезде. Плюс: GitHub показывает репозитории с Pages в отдельной выдаче.
```

---

### Промпт 6 — Discoverability-пак: topics, Discussions, шаблоны, release-drafter

```text
Ты — Developer Relations. Работай в репозитории Reider85/OryaObservability.

КОНТЕКСТ:
- У репо уже 11 topics: guardrails, llm, llm-agents, llm-evaluation, llm-monitoring, llm-monitoring-security, llm-observability, llm-observability-dashboard, llm-tools, pii, pii-detection.
- Discussions выключен (has_discussions: false).
- Нет ISSUE_TEMPLATE, нет PULL_REQUEST_TEMPLATE.
- Описание репо: «OryaObservability Production-grade observability for LLM agents — traces, cost, security, eval, drift, SLOs and HA in one async Python SDK.» — два раза «OryaObservability» подряд, выглядит как баг.

ЗАДАЧА:
1. Обнови description репозитория (инструкция для пользователя через `gh` CLI):
   ```bash
   gh repo edit Reider85/OryaObservability \
     --description "Production-grade observability for LLM agents — traces, cost, security, eval, drift, SLOs and HA in one async Python SDK." \
     --homepage "https://reider85.github.io/OryaObservability/" \
     --add-topic opentelemetry \
     --add-topic langfuse \
     --add-topic clickhouse \
     --add-topic vault \
     --add-topic prometheus \
     --add-topic observability \
     --add-topic python-sdk \
     --add-topic async \
     --add-topic gdpr \
     --add-topic slo \
     --add-topic error-budget \
     --add-topic rbac \
     --add-topic mtls \
     --enable-discussions=true \
     --enable-issues=true \
     --enable-wiki=false
   ```
   (wiki выключаем — у нас mkdocs-сайт, дублировать незачем).
   Topics ограничены 20 штуками — после добавления новых их станет 23, удали 3 наименее релевантных (например, llm-monitoring-security и llm-observability-dashboard — они дублируют llm-observability).

2. Создай `.github/ISSUE_TEMPLATE/bug_report.yml` (YAML form, не markdown):
   ```yaml
   name: Bug report
   description: Report a bug in agent-obs
   labels: ["bug", "triage"]
   body:
     - type: markdown
       attributes:
         value: "Thanks for taking the time to file a bug! Please search existing issues first."
     - type: textarea
       id: summary
       attributes:
         label: Summary
         description: One sentence.
       validations:
         required: true
     - type: input
       id: env
       attributes:
         label: Environment
         description: "Python version, OS, SDK version (run `python -c 'import agent_obs; print(agent_obs.__version__)'`), extras installed, infra stack"
         placeholder: "Python 3.11.7, Ubuntu 22.04, agent-obs 0.1.0, [dev], single-node"
       validations:
         required: true
     - type: textarea
       id: repro
       attributes:
         label: Reproduction
         description: Minimal script or test. No secrets.
       validations:
         required: true
     - type: textarea
       id: expected
       attributes:
         label: Expected vs actual
       validations:
         required: true
     - type: textarea
       id: logs
       attributes:
         label: Logs
         description: Stderr / pytest output. Redact tokens, keys, PII.
     - type: input
       id: ticket
       attributes:
         label: Ticket reference
         placeholder: "Ref: PPxx"
   ```

3. Создай `.github/ISSUE_TEMPLATE/feature_request.yml` — аналогично, поля: use-case, current-workaround, proposed-api, phase-fit.

4. Создай `.github/ISSUE_TEMPLATE/config.yml`:
   ```yaml
   blank_issues_enabled: false
   contact_links:
     - name: Security vulnerability
       url: mailto:krot1113@yandex.ru?subject=SECURITY:%20OryaObservability
       about: Do NOT open public issues for security. Email directly.
     - name: Discussions
       url: https://github.com/Reider85/OryaObservability/discussions
       about: Questions and ideas go in Discussions, not Issues.
   ```

5. Создай `.github/PULL_REQUEST_TEMPLATE.md` (взять шаблон из CONTRIBUTING.md строки 225–237 и оформить как файл).

6. Создай `.github/FUNDING.yml` (даже пустой — даёт вкладку Sponsor):
   ```yaml
   # Раскомментируйте нужное, когда будет куда принимать деньги:
   # ko_fi: reider85
   # custom: ["https://your-site.com/donate"]
   ```

7. Создай `.github/CODEOWNERS`:
   ```
   # Default owner for everything
   * @Reider85
   # Security-sensitive areas require explicit review (CONTRIBUTING.md §Security-Sensitive)
   /agent_obs/guardrail/ @Reider85
   /agent_obs/rbac/ @Reider85
   /agent_obs/slo/ @Reider85
   /infra/vault/ @Reider85
   /infra/rbac/ @Reider85
   ```

8. Создай `SECURITY.md` в корне (GitHub автоматически подхватит) — скопируй раздел «Reporting Security Vulnerabilities» из CONTRIBUTING.md.

9. В README.md после бейджей добавь ссылку на Discussions:
   `[![Discussions](https://img.shields.io/github/discussions/Reider85/OryaObservability)](https://github.com/Reider85/OryaObservability/discussions)`

10. Включи Discussions-категории (инструкция для пользователя): Settings → General → Features → Discussions → Set up → категории: «Announcements» (только мейнтейнеры пишут), «Ideas», «Q&A», «Show and tell». Закрепи первый пост-анонс с кратким обзором SDK и просьбой star.

КРИТЕРИЙ ПРИЁМКИ:
- На странице репо появились вкладки Discussions, Security, Projects (если нужно).
- Topics расширены до 20 (видны в поиске GitHub по каждому).
- Issue-форма при создании issue показывает структурированные поля.
- PR-шаблон предзаполнен.
- Бейдж Discussions живой.

ПОЧЕМУ ЭТО ВАЖНО ДЛЯ ЗВЁЗД:
Topics — это теги, по которым GitHub ищет репо в Explore. Каждое новое topic — отдельная страница-выдача, где ваш репо появляется. Discussions — это engagement-loop: каждый ответ мейнтейнера в Discussions пушит уведомление подписчикам и обновляет «last activity» репо, что GitHub учитывает в выдаче «recently active». Структурированные issue-формы снижают порог входа для контрибьюторов → больше PR → больше «activity».
```

---

### Промпт 7 — Nightly job: load-тесты + e2e + HA-дым

```text
Ты — Performance-инженер. Работай в репозитории Reider85/OryaObservability.

КОНТЕКСТ:
- tests/load — медленные p99 overhead-тесты (поведение hot path observability.py, guardrail/engine.py, exporters). Нельзя вешать на каждый пуш.
- tests/ha — 30 chaos-тестов, требуют 40 docker-сервисов из infra/docker-compose.ha.yml и 16 ГБ ОЗУ. На бесплатном ubuntu-latest (7 ГБ ОЗУ) не поместится, но можно прогнать дым-подмножество.
- tests/e2e — Playwright, ~2-3 минуты. Можно и на каждый пуш, но экономнее — nightly + по label.
- Цель: отделить тяжёлое в nightly, чтобы не висело на CI и не блокировало PR.

ЗАДАЧА:
1. Создай `.github/workflows/nightly.yml`:
   - name: Nightly
   - on:
       schedule: cron: '0 3 * * *'  # 03:00 UTC каждый день
       workflow_dispatch:
   - jobs:
     a) `load` (ubuntu-latest, Python 3.11):
        - checkout
        - setup-python + cache
        - pip install -e ".[dev]"
        - run: `python -m pytest tests/load/ -q --tb=short --junitxml=load.xml`
        - upload-artifact с load.xml
        - dorny/test-reporter
     b) `e2e` (ubuntu-latest, Python 3.11):
        - pip install -e ".[dev]"
        - python -m playwright install --with-deps chromium
        - run: `python -m pytest tests/e2e/ -q --tb=short --junitxml=e2e.xml`
        - upload-artifact с e2e.xml + screenshots (`tests/e2e/.screenshots/` если есть)
     c) `ha-smoke` (ubuntu-latest, Python 3.11, timeout-minutes: 30):
        - pip install -e ".[dev]"
        - Запусти минимальный subset docker-compose stack (один clickhouse + один postgres + redis + vault). Это нужно сделать через отдельный `infra/docker-compose.ci.yml` с урезанным набором сервисов — создай такой файл с 4 сервисами: clickhouse (image: clickhouse/clickhouse-server:24.3), postgres (image: postgres:16), redis (image: redis:7), vault (image: hashicorp/vault:1.15). Каждый с healthcheck.
        - Дождись healthcheck всех 4 (используй `wait-on` или цикл `until docker inspect --format='{{.State.Health.Status}}' ...`)
        - run: `python -m pytest tests/ha/ -q --tb=short -k "not replication and not keeper" --junitxml=ha-smoke.xml` — только не-кластерные тесты, чтобы влезло по RAM.
        - always(): docker compose down -v

2. Добавь `timeout-minutes: 30` на каждый job, чтобы ночные job-ы не висели часами.

3. Добавь уведомление в Slack/Telegram при падении (используй `rtCamp/action-slack-notify@v2` или `appleboy/telegram-action`). Секреты `SLACK_WEBHOOK_URL`, `AGENT_OBS_TELEGRAM_BOT_TOKEN`, `AGENT_OBS_TELEGRAM_CHAT_ID` уже описаны в AGENTS.md — добавь их в repo secrets и используй.

4. В README.md добавь бейдж:
   `[![Nightly](https://github.com/Reider85/OryaObservability/actions/workflows/nightly.yml/badge.svg)](https://github.com/Reider85/OryaObservability/actions/workflows/nightly.yml)`

5. В `infra/docker-compose.ci.yml` обязательно добавь комментарий: «Reduced CI stack for nightly ha-smoke. NOT for production. Full HA stack see docker-compose.ha.yml.»

КРИТЕРИЙ ПРИЁМКИ:
- Nightly отрабатывает за <25 минут.
- При падении load/e2e приходит уведомление в Slack или Telegram.
- ha-smoke либо зелёный, либо падает по понятной причине (недостаточно RAM — пометить как `continue-on-error: true` и в комментарии объяснить, что это informational).

ПОЧЕМУ ЭТО ВАЖНО ДЛЯ ЗВЁЗД:
Nightly-бейдж — сигнал «проект следит за производительностью 24/7». Это редкий сигнал для Python-библиотек и сразу отличает вас от 90% аналогов. p99 overhead — ключевое architectural invariant проекта (CONTRIBUTING.md, <2 ms p99) — nightly делает это видимым и доказуемым. Дополнительно: отделяя тяжёлые тесты от CI, вы держите PR-пайплайн быстрым (~5 минут), что снижает трение для контрибьюторов → больше PR → больше activity → выше в выдаче GitHub.
```

---

## 4. Что даст в совокупности

После внедрения всех 7 промптов репозиторий `Reider85/OryaObservability` превращается из «красного canary без CI» в:

- **7 живых бейджей** в README: CI · Coverage · Python 3.11/3.12/3.13 · CodeQL · Dependabot · Docs · Discussions · Nightly.
- **Активная страница Releases**: 1 релиз на каждый мерж PR с label `release:*` + dependabot-релизы раз в 1–2 недели на автопилоте.
- **Security tab** с зелёным CodeQL.
- **GitHub Pages**-сайт с material-тёмной темой.
- **Структурированные issue/PR-формы** + включённые Discussions.
- **Все тесты, линт, тайпчек, e2e, load, coverage** — гоняются на GitHub Actions бесплатно, ваша машина разгружена.
- **Slack/Telegram-нотификации** при падении nightly.

### Ожидаемая динамика

| Метрика | Сейчас | Через 1 месяц | Через 3 месяца |
| --- | --- | --- | --- |
| Звёзды | 1 | 10–30 | 50–200 |
| Releases | 0 | 4–8 | 15–30 |
| Бейджей в README | 8 статичных | 8 статичных + 7 живых | то же |
| Время PR-проверки руками | ~90 с локально | 0 (CI гоняет) | 0 |
| Покрытие кода видимому | не видно | бейдж + trend | бейдж + trend |
| Security-таб | пустой | зелёный CodeQL | зелёный + история |
| Activity (commits/month) | ~10–20 | ~20–40 (dependabot) | ~30–60 |

Цифры по звёздам — оценка при условии еженедельного анонса каждого минорного релиза в одном-двух внешних каналах (HN / Reddit r/LocalLLaMA / Хабр / Twitter OSS-сообщества). Без внешнего анонса CI даёт +5…+15 звёзд за месяц — это чисто органика из GitHub Explore.

### Quick wins — что внедрить в первый день

1. **Промпт 1** (CI) — 30 минут работы, мгновенно убирает «всегда failed».
2. **Промпт 6** (Discoverability) — 15 минут, включается через `gh repo edit`.
3. **Промпт 2** (Releases) — 20 минут, начинает генерировать ленту релизов сразу.

Остальные — в порядке приоритета из карты промптов.

---

## 5. Anti-patterns — чего НЕ делать

- ❌ **Релиз на каждый коммит**. GitHub пенализирует за спам-релизы. Режьте релиз на мерж PR, а не на commit.
- ❌ **`continue-on-error: true` на CI-тестах**, чтобы «бейдж был зелёным». Лучше `--exit-zero` на ruff/mypy с честным комментарием «baseline not cleared yet».
- ❌ **Запуск `tests/ha` на каждом пуше**. 40 docker-сервисов + 16 ГБ ОЗУ = зависший раннер и бан по минутам. Только nightly smoke.
- ❌ **Публичные `secrets.GITHUB_TOKEN` в логах**. В `softprops/action-gh-release` токен передаётся через `env`, не через `with` — проверяй.
- ❌ **Dependabot с `release:major` label по умолчанию**. Только `release:patch` — мажоры пусть будут осознанными.
- ❌ **Удалять canary-логику целиком**. `scripts/canary.py`, `scripts/canary_check.py`, `scripts/cicd/error_budget_gate.py` — это production-deploy-инфраструктура, она нужна. Удаляется только `.github/workflows/canary.yml`, который в CI бесполезен.
