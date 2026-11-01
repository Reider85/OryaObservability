# Contributing to OryaObservability

First off — **thank you** for taking the time to contribute. OryaObservability is a security- and correctness-sensitive observability SDK for LLM agents, so this guide is stricter than for a typical open-source project. Please read it in full before opening a pull request.

This document is the canonical contributing guide. The working spec for AI agents and human contributors editing the codebase lives in [`AGENTS.md`](./AGENTS.md) — read that too, it documents commands, test conventions, architecture invariants, env vars and standing gotchas.

---

## Table of Contents

- [Code of Conduct](#code-of-conduct)
- [Project Philosophy](#project-philosophy)
- [Before You Start](#before-you-start)
- [Development Environment](#development-environment)
- [Making Changes](#making-changes)
- [Testing](#testing)
- [Code Style](#code-style)
- [Commit Messages](#commit-messages)
- [Pull Requests](#pull-requests)
- [Security-Sensitive Changes](#security-sensitive-changes)
- [Reporting Security Vulnerabilities](#reporting-security-vulnerabilities)
- [Issue Reporting](#issue-reporting)
- [Recognition](#recognition)

---

## Code of Conduct

Participation in this project is governed by the principle of **professional respect**. Be kind, assume good intent, focus on the technical merit of ideas, and accept that maintainers may decline contributions that conflict with the project's architecture invariants (see [Project Philosophy](#project-philosophy)).

Harassment, personal attacks, or discriminatory behaviour of any kind will not be tolerated. Report incidents by opening a private issue marked `conduct`.

---

## Project Philosophy

Three invariants govern every contribution. A PR that violates any of them will be rejected regardless of code quality.

### 1. Observability never blocks business logic

The SDK instruments agent loops that serve real users. The hot path — `_enqueue`, guardrail masking, span creation — must remain non-blocking and fast (< 5 ms p99 for the guardrail, < 2 ms p99 for span creation). If your change adds synchronous I/O to the hot path, it does not belong here.

### 2. Non-contradiction

No PR may cancel prior architectural decisions. The canonical SDK contracts are:

- `@agent_observed(agent_id, version)` decorator → `agent.loop` root span
- `async with sdk.llm_call(...)` / `sdk.tool_call(...)` context managers
- `Span` / `SpanContext` dataclasses (ULID-like 26-char Crockford-base32 ids)
- `BaseExporter` ABC (`export(batch)`, `flush()`)
- `GuardrailEngine.check_input()` → `GuardrailVerdict`
- `BaseEvaluator` ABC → `EvalResult`
- `BaseStorage` / `HotStore` / `WarmStore` / `ColdStore`
- The 6 span types: `agent.loop`, `llm.call`, `tool.call`, `agent.reasoning`, `agent.reasoning.compressed`, `llm.proxy.call`

If you need to change one of these, open an issue first and propose the change at the architecture level. Do not retrofit a contract change inside a feature PR.

### 3. Cost is a span attribute, not a separate billing stream

Cost (USD + token counts) lives as attributes on `llm.call` and `llm.proxy.call` spans. Do not introduce a parallel cost pipeline. The tenant-aware attribution tracker (`agent_obs/cost/attribution.py`) reads from the same span attributes.

---

## Before You Start

1. **Search existing issues and PRs** — your concern may already be tracked. Comment on an existing issue before opening a duplicate.
2. **Open an issue for non-trivial changes.** A bug fix with an obvious patch can go straight to a PR. A new feature, a dependency upgrade, or a refactor touching more than ~3 files should have an issue with maintainer sign-off first.
3. **Check the phase / ticket number.** The project is organised into MVP (`P01`–`P27`), CRITICAL (`PC01`–`PC38`) and PRODUCTION (`PP01`–`PP60`) tickets defined in `analytics/MVP-PROMPT.md`, `analytics/CRITICAL-PROMPTS.md` and `analytics/PRODUCTION-PROMPTS.md`. If your work maps to a ticket, reference it in the PR. If it does not map to any ticket, open an issue to discuss whether it belongs in scope.
4. **Read [`AGENTS.md`](./AGENTS.md).** It documents commands, test conventions, env vars, and standing gotchas that are not repeated here.

---

## Development Environment

### Prerequisites

- **Python 3.11+** (3.12 and 3.13 also tested)
- **Docker** + **Docker Compose** (v2 syntax) for the infra stack
- **Git** (the repo uses conventional commits — see [Commit Messages](#commit-messages))
- For the HA stack: 16 GB RAM, 4+ vCPU
- For ML extras (DeBERTa-v3, Presidio): ~3 GB model weights

### Setup

```bash
git clone https://github.com/reider85/OryaObservability.git
cd OryaObservability

# Editable install with dev extras (includes ML + storage + api + cron + test deps)
pip install -e ".[dev]"

# Verify the install — fast suite should pass in ~90s
python -m pytest --ignore=tests/ha --ignore=tests/load -q --tb=short
```

### Bringing up the infra stack

You do not need the infra stack for most code changes — the fast test suite mocks ClickHouse / Postgres / Vault / Redis. Bring up the stack only when you need to:

- Test an exporter or storage client against real services
- Run the HA or load test suites
- Manually verify a dashboard, alert rule, or trace-viewer change

**Linux / macOS:**

```bash
cd infra
cp .env.example .env            # fill in secrets
docker compose up -d
```

**Windows PowerShell (managed script with health checks):**

```powershell
./scripts/start-all.ps1
./scripts/stop-all.ps1          # graceful stop
./scripts/stop-all.ps1 -RemoveVolumes   # full teardown
```

### HA stack (PP01–PP38)

The HA stack (`infra/docker-compose.ha.yml`, ~40 services) is required for `tests/ha/`:

```bash
cd infra
docker compose -f docker-compose.ha.yml up -d

# Wait for health (each test fixture sleeps 60–120s anyway)
./infra/ha/healthchecks/check-all.sh

python -m pytest tests/ha/ -q
```

---

## Making Changes

1. **Create a branch** off `main`:

   ```bash
   git checkout -b feat/short-description
   ```

   Branch naming: `feat/...`, `fix/...`, `docs/...`, `refactor/...`, `test/...`, `chore/...`, `sec/...`.

2. **Make your changes.** Keep the diff focused — one logical change per PR. If you find yourself touching unrelated files, split the PR.

3. **Update or add tests.** Every non-trivial change needs test coverage. See [Testing](#testing).

4. **Update documentation** if your change affects:
   - Public SDK API → update `AGENTS.md` and the relevant `docs/` runbook
   - Env vars → update `AGENTS.md` env var reference and `README.md` Configuration section
   - Dashboards or alert rules → update the corresponding `docs/*-runbook.md`
   - DDL → update `infra/storage/` and any `infra/storage/migrations/` files

5. **Run the fast test suite** before committing:

   ```bash
   python -m pytest --ignore=tests/ha --ignore=tests/load -q --tb=short
   ```

6. **Run ruff and mypy on the files you touched** (see [Code Style](#code-style)).

---

## Testing

### Test tiers

| Tier | Command | When to run |
| --- | --- | --- |
| **Fast** | `python -m pytest --ignore=tests/ha --ignore=tests/load -q --tb=short` | **Always**, before every commit. ~90 s, ~1,344 tests. |
| **E2E** | `python -m pytest tests/e2e/ -q` | When you change `agent_obs/trace_viewer/`. Requires `playwright install chromium`. |
| **HA** | `python -m pytest tests/ha/ -q` | When you change `agent_obs/ha/`, `infra/docker-compose.ha.yml`, or any PP35–PP38 code. Requires the HA stack up. 30 tests, 60–120 s per fixture. |
| **Load** | `python -m pytest tests/load/ -q` | When you change the hot path (`observability.py`, `guardrail/engine.py`, exporters). Slow p99 overhead tests. |

### Critical rules

- **Do not run bare `pytest`.** `testpaths = ["tests"]` collects `tests/ha` (Docker chaos + minute-long sleeps) and `tests/load`. Always use `--ignore=tests/ha --ignore=tests/load` for the fast run.
- **`pytest-timeout` is not installed.** `--timeout=N` fails with "unrecognized arguments". Do not add it.
- **Mock heavy ML** (Presidio, DeBERTa, transformers, torch) via `MagicMock` in the fast suite. Real model weights are only loaded in `tests/eval/` integration tests marked `@pytest.mark.integration`.
- **Pin wall-clock** for day/night adaptive sampler tests. Use `freezegun` or direct monkeypatch of `time.time` / `datetime.now`.
- **Patch `scripts.cron.scheduler.<name>`** not the source modules — the scheduler imports jobs at module load and patches must target the registered name.
- **Reset circuit-breaker registry** — `tests/conftest.py` has an autouse fixture that resets the process-wide `CircuitBreaker` registry between tests (PP38). Do not disable it.
- **`tests/fixtures/ca.pem`** is the self-signed CA for mTLS tests. Do not regenerate it unless you understand the impact on `test_mtls.py`.

### Test conventions

- Framework: **pytest + pytest-asyncio**. `asyncio_mode = "auto"` in `pyproject.toml` — async tests run without `@pytest.mark.asyncio`. Many existing tests still have the decorator; either style is accepted.
- Use `@pytest.mark.integration` for tests that need real services (ClickHouse, Postgres, Vault, OpenAI). These are skipped by default in CI when the service is not reachable.
- Use `respx` for mocking `httpx` calls (exporters, proxy, OPA, Athena).
- Use `moto[s3]` for S3 / MinIO tests.
- Use `pytest-asyncio`'s `pytest.mark.asyncio` only when needed for clarity; the auto mode handles most cases.

### What to test

- **Every new public function or class** needs at least one happy-path test and one failure-mode test.
- **Every new guardrail rule** needs: (a) a test that PII is masked, (b) a test that non-PII passes through, (c) a test that the audit event is written.
- **Every new evaluator** needs: (a) a test with a passing input, (b) a test with a failing input, (c) a test that late annotation writes to the right table.
- **Every new storage method** needs: (a) a test with a real or mocked driver, (b) a test for the failover path (if applicable), (c) a test for the fail-closed path.
- **Every new Prometheus metric** needs a test asserting it appears in the registry with the expected labels.

---

## Code Style

### Python

- **Python 3.11+**. Use modern syntax: `match`/`case`, `from __future__ import annotations` is not needed (3.11+ has PEP 604 unions natively), `type` aliases (3.12+) are welcome where they aid clarity.
- **Async-first.** The SDK is async; new public APIs should be `async def` unless there is a strong reason otherwise. The guardrail masking path is sync by design (it must complete before the span leaves the process).
- **Type hints are mandatory** on all public functions. Internal helpers may omit hints only when the type is obvious.
- **Dataclasses over Pydantic** for SDK data objects (`Span`, `SpanContext`, `EvalResult`, `CostResult`, `GuardrailVerdict`). Pydantic is not a dependency; do not introduce it.
- **Lazy imports for heavy deps.** `torch`, `transformers`, `spacy`, `presidio-analyzer`, `fastapi`, `uvicorn`, `asyncpg`, `boto3`, `pyarrow`, `pandas` must be imported lazily inside functions or behind extras. The core SDK (`pip install -e .`) must import with only `httpx`, `prometheus-client`, `pyyaml`, `jsonschema`, `hvac`, `pyotp`, `openpyxl`.

### Ruff

Ruff runs with **defaults** (no config in `pyproject.toml`). The baseline is intentionally dirty — ~830 findings across the repo. **Fix only findings in files you touched.** Do not attempt a repo-wide ruff cleanup in a single PR; it will be rejected.

```bash
ruff check agent_obs/your_module.py tests/test_your_module.py
ruff format agent_obs/your_module.py tests/test_your_module.py
```

### Mypy

Mypy runs with **defaults**. The baseline is ~116 findings. Same rule: fix only findings in files you touched.

```bash
mypy agent_obs/your_module.py
```

If a third-party stub is missing, add a `# type: ignore[import-untyped]` with a comment explaining why — do not silence the whole file.

### YAML / SQL / HCL / Rego

- **YAML configs** (`configs/`, `price_book.yaml`): 2-space indent, no tabs, comments explaining any non-obvious value.
- **SQL DDL** (`infra/storage/`): idempotent — every `CREATE TABLE` must be `IF NOT EXISTS`, every `ALTER` must check `system.columns` / `system.tables` first. See `infra/storage/clickhouse_ddl.sql` for the pattern.
- **HCL** (Vault policies): no env interpolation (`${VAR}`) — HCL does not expand it; use `envsubst` in the init script instead.
- **Rego** (`infra/rbac/policies/`): fail-closed default (`allow = false` unless explicitly allowed).

### Imports

Import order (enforced informally; ruff's `I001` will flag violations):

1. Standard library
2. Third-party (core deps only)
3. Third-party (extras-gated, lazy)
4. Local (`agent_obs.*`)

Use absolute imports (`from agent_obs.observability import Span`), not relative.

---

## Commit Messages

The project uses **Conventional Commits** with scope. Scope is usually the module or infra component.

```
<type>(<scope>): <imperative summary>

<optional body — wrap at 72 chars>

<optional footer>
```

### Types

- `feat` — new feature
- `fix` — bug fix
- `docs` — documentation only
- `refactor` — code change that neither fixes a bug nor adds a feature
- `test` — adding or correcting tests
- `chore` — build, CI, tooling, deps
- `sec` — security fix (guardrail, vault, RBAC, mTLS)
- `perf` — performance improvement

### Scopes

Common scopes: `agent_obs`, `guardrail`, `eval`, `cost`, `storage`, `proxy`, `rbac`, `slo`, `ha`, `exporters`, `infra`, `cron`, `sampler`, `canary`, `docs`, `tests`.

### Examples

```
feat(guardrail): add passport MRZ regex to PIIDetector

Adds a two-line MRZ regex (TD1/TD3) to the passport pattern set in
pii_detector.py. Masks as [PASSPORT:hex4] consistent with existing
passport handling. Updates configs/pii_columns.yaml with 14 new
column synonyms (passport_number, passport_no, doc_mrz, ...).

Closes PC04 follow-up #42.
```

```
fix(storage): HotStore failover skips Distributed table writes on cooldown

The 30s cooldown was being checked after the write attempt, causing
a thundering-herd retry against a dead replica. Move the cooldown
check before the write and log the skip at INFO.

Reported-by: external user
```

```
sec(vault): enforce TOTP on every recover() call

PC19 was partially implemented — TOTP was optional when the vault
token had recovery permissions. Now mandatory regardless of token
scope. Adds test_vault_client.py::test_recover_without_totp_fails.
```

### Footer conventions

- `Closes #N` / `Fixes #N` — link to issue
- `Ref: PP42` / `Ref: PC19` — link to a ticket in the analytics prompts
- `Reported-by:`, `Co-authored-by:`, `Signed-off-by:` — standard trailers
- `BREAKING CHANGE:` — describe a breaking change (requires maintainer sign-off; see [Project Philosophy](#project-philosophy))

---

## Pull Requests

### Before opening

- [ ] Fast test suite passes: `python -m pytest --ignore=tests/ha --ignore=tests/load -q --tb=short`
- [ ] Ruff and mypy clean on touched files
- [ ] Tests added or updated for the change
- [ ] Documentation updated (`AGENTS.md`, `docs/`, `README.md` as applicable)
- [ ] No `BREAKING CHANGE` without prior issue sign-off
- [ ] No secrets, tokens, or PII in the diff
- [ ] Branch is rebased on `main`

### PR template

```markdown
## What

<1–3 sentences: what does this PR do?>

## Why

<Link to issue or ticket (Ref: PPxx / PCxx / Pxx). Why is this change needed?>

## How

<Bullet list of the approach. Call out any non-obvious design decisions.>

## Testing

<Which test tiers did you run? Paste the relevant pytest -q tail.>

## Checklist

- [ ] Fast suite passes
- [ ] Tests added / updated
- [ ] Docs updated
- [ ] No breaking changes (or sign-off linked)
- [ ] Touched files are ruff + mypy clean
```

### Review process

1. **Automated checks.** The only GitHub Actions workflow today is `.github/workflows/canary.yml` (PP29 SLO-gated canary). There is **no CI workflow for tests / lint / typecheck** — the reviewer runs them locally. (Adding one is welcome — open an issue first.)
2. **Maintainer review.** A maintainer will review within 7 days. Security-sensitive changes (see below) require two maintainer sign-offs.
3. **Acceptance harness.** If your change touches a CRITICAL or PRODUCTION exit criterion, the reviewer may ask you to run `scripts/acceptance/pc37_harness.py` and paste the results.
4. **Squash merge.** PRs are squash-merged. The commit message on the squash is editable by the maintainer; provide a clean one in your PR description.

---

## Security-Sensitive Changes

Some parts of the codebase have an elevated bar for acceptance. Changes to the following require **two maintainer sign-offs** and may require updates to the acceptance harness:

| Area | Files | Why |
| --- | --- | --- |
| **PII guardrail** | `agent_obs/guardrail/*` | A regression here can leak PII into ClickHouse / Langfuse / S3. |
| **Vault client** | `agent_obs/guardrail/vault_client.py`, `infra/vault/*` | Holds the keys to recover masked PII. |
| **Field masking policies** | `configs/field_masking.yaml`, `configs/pii_columns.yaml`, `agent_obs/guardrail/field_masker.py` | Wrong policy = PII in span attributes. |
| **Injection classifier** | `agent_obs/guardrail/injection_classifier.py` | Fail-open by design; must not silently flip to fail-closed. |
| **RBAC / OPA** | `agent_obs/rbac/*`, `infra/rbac/policies/*` | Cross-tenant leak risk. Rego must remain fail-closed default. |
| **Tenant isolation** | `infra/storage/postgres_warm.sql` (RLS), `infra/storage/clickhouse_ddl.sql` (partitioning) | RLS policy regression = cross-tenant data access. |
| **mTLS / certs** | `infra/ha/certs/*`, `scripts/certs/*`, `scripts/rotate-certs.sh` | Broken mTLS = unauthenticated internal traffic. |
| **Cost attribution / budget** | `agent_obs/cost/attribution.py` | Budget bypass = unbounded tenant spend. |
| **SLO / error budget gate** | `agent_obs/slo/*`, `scripts/cicd/error_budget_gate.py` | Gate bypass = deploys during SLO breach. |
| **Canary rollback** | `scripts/canary.py`, `scripts/canary_check.py`, `.github/workflows/canary.yml` | Failed rollback = stuck canary traffic. |

### What this means in practice

- **Add a test** for every new code path. For guardrail changes, add a regression test with a realistic PII payload (use the patterns already in `tests/test_pii_detector.py`).
- **Do not change fail-open / fail-closed defaults** without an issue. The defaults are documented in `AGENTS.md` and are part of the architecture contract.
- **Do not weaken audit trails.** Every `*AuditEvent` must remain mandatory and must retain its `reason` / `actor` / `timestamp` fields.
- **Update the acceptance harness** (`scripts/acceptance/`) if your change affects a CRITICAL (PC37) or PRODUCTION exit criterion.
- **Re-run the debuggability test** (`tests/test_debuggability.py`) for any guardrail change — the 90 % debuggability target (PC17) must hold.

---

## Reporting Security Vulnerabilities

**Do not open a public GitHub issue for security vulnerabilities.**

Email security concerns to **krot1113@yandex.ru** with the subject `SECURITY: OryaObservability`. Include:

1. Affected component and file(s)
2. A minimal reproduction (script, config, env)
3. The impact — what an attacker could do
4. Suggested fix if you have one

You will receive an acknowledgement within 72 hours. Coordinated disclosure is preferred; we will credit you in the release notes unless you ask to remain anonymous.

### In scope

- PII leakage through the guardrail pipeline
- Vault token / secret leakage
- RBAC / OPA bypass
- Tenant isolation bypass (ClickHouse partitioning or Postgres RLS)
- mTLS bypass
- Cost attribution / budget bypass
- SLO / error-budget gate bypass

### Out of scope

- Vulnerabilities in third-party dependencies (report upstream)
- Theoretical issues without a reproduction
- Social engineering of maintainers

---

## Issue Reporting

### Bug reports

Open a [GitHub issue](https://github.com/reider85/OryaObservability/issues) with:

- **Summary** — one sentence
- **Environment** — Python version, OS, SDK version (`python -c "import agent_obs; print(agent_obs.__version__)"`), which extras installed, which infra stack (single-node / HA / none)
- **Reproduction** — minimal script or test
- **Expected vs actual**
- **Logs** — relevant stderr / pytest output (no secrets — redact tokens, keys, PII)
- **Ticket reference** — `Ref: PPxx` if applicable

### Feature requests

Open an issue with:

- **Use case** — what are you trying to do that the SDK does not support?
- **Current workaround** — if any
- **Proposed API** — sketch the function signature / config shape / CLI
- **Phase fit** — does this map to a Level 4 (`E4.x`) roadmap item, or is it new scope?

---

## Recognition

Contributors are recognised in:

- The **release notes** (git tag messages) for each merged PR
- The **AUTHORS** section of `AGENTS.md` (maintainers only)
- A **Co-authored-by** trailer on the squash-merge commit (add yourself to your PR description)

Significant contributions (new module, major refactor, security fix) may be credited in the relevant `docs/` runbook.

Thank you for contributing to OryaObservability.
