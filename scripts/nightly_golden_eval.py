#!/usr/bin/env python3
"""PP13 (T3.3.2) — nightly-batch golden eval with regression alerting.

Every night at 03:00 UTC this job runs the full golden set through the pilot
agent and the LLM judge, compares per-metric scores against the expected
thresholds stored in ``eval/golden/*.yaml``, writes row-level results to the
ClickHouse ``eval_golden_results`` table, and publishes
``agent_obs_eval_golden_pass_rate``. The ``GoldenEvalRegression`` Prometheus
alert fires when the pass rate drops more than 0.10 below its 7-day moving
average.

The job runs from the long-lived cron scheduler (``scripts/cron/scheduler.py``)
so the pass-rate gauge survives between nightly runs and stays scrapeable.
It is also runnable standalone for development:

    python scripts/nightly_golden_eval.py --dry-run
    python scripts/nightly_golden_eval.py

See docs/eval-golden-runbook.md.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import yaml

# Repository root, so agent_obs / pilot_agent / scripts stay importable when
# this file is executed directly.
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

logging.basicConfig(
    level=os.environ.get("AGENT_OBS_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("agent_obs.eval.nightly_golden")

DEFAULT_GOLDEN_DIR = os.path.join(REPO_ROOT, "eval", "golden")
DEFAULT_JUDGE_MODEL = "gpt-4o-mini"
DEFAULT_AGENT_MODEL = "gpt-4o"
# Metric names must match agent_obs.eval.llm_judge.LLMJudgePrompts keys.
SUPPORTED_METRICS = ("faithfulness", "answer_relevancy", "completeness")


@dataclass
class GoldenCase:
    """One golden-set case loaded from eval/golden/*.yaml."""

    id: str
    question: str
    expected_answer: str = ""
    expected_scores: dict[str, float] = field(default_factory=dict)


@dataclass
class CaseResult:
    """Outcome of judging one golden case against its expected thresholds."""

    question_id: str
    expected_scores: dict[str, float]
    actual_scores: dict[str, float]
    passed: bool

    def storage_rows(self, nightly_ts: datetime) -> list[dict[str, Any]]:
        """Flatten to eval_golden_results rows (one per metric)."""
        return [
            {
                "nightly_ts": nightly_ts,
                "question_id": self.question_id,
                "metric_name": metric,
                "expected_score": self.expected_scores[metric],
                "actual_score": self.actual_scores.get(metric, 0.0),
                "pass": self.actual_scores.get(metric, 0.0)
                >= self.expected_scores[metric],
            }
            for metric in self.expected_scores
        ]


@dataclass
class NightlyResult:
    """Aggregated outcome of one nightly golden-eval run."""

    nightly_ts: datetime
    total: int = 0
    passed: int = 0
    case_results: list[CaseResult] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def pass_rate(self) -> float:
        """Fraction of cases that passed, 0.0 when nothing ran."""
        if self.total == 0:
            return 0.0
        return self.passed / self.total

    def storage_rows(self) -> list[dict[str, Any]]:
        """All eval_golden_results rows for this run."""
        rows: list[dict[str, Any]] = []
        for case_result in self.case_results:
            rows.extend(case_result.storage_rows(self.nightly_ts))
        return rows


def load_golden_set(golden_dir: str | Path = DEFAULT_GOLDEN_DIR) -> list[GoldenCase]:
    """Load every ``golden_cases`` entry from ``*.yaml`` under *golden_dir*.

    Raises ``ValueError`` on malformed cases so a broken golden set fails the
    run loudly instead of silently shrinking coverage.
    """
    golden_dir = Path(golden_dir)
    if not golden_dir.is_dir():
        raise ValueError(f"golden set directory not found: {golden_dir}")

    cases: list[GoldenCase] = []
    seen_ids: set[str] = set()
    for path in sorted(golden_dir.glob("*.yaml")):
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        raw_cases = document.get("golden_cases")
        if raw_cases is None:
            logger.warning("%s: no golden_cases key, skipping", path)
            continue
        if not isinstance(raw_cases, list):
            raise TypeError(f"{path}: golden_cases must be a list")
        for raw in raw_cases:
            case = _parse_case(raw, path)
            if case.id in seen_ids:
                raise ValueError(f"{path}: duplicate golden case id {case.id!r}")
            seen_ids.add(case.id)
            cases.append(case)

    if not cases:
        raise ValueError(f"no golden cases found under {golden_dir}")
    return cases


def _parse_case(raw: Any, source: Path) -> GoldenCase:
    """Validate one raw YAML mapping into a GoldenCase."""
    if not isinstance(raw, dict):
        raise TypeError(f"{source}: golden case must be a mapping, got {type(raw).__name__}")
    case_id = raw.get("id")
    question = raw.get("question")
    if not case_id or not isinstance(case_id, str):
        raise ValueError(f"{source}: golden case missing string 'id'")
    if not question or not isinstance(question, str):
        raise ValueError(f"{source}: case {case_id!r} missing string 'question'")

    expected_scores_raw = raw.get("expected_scores") or {}
    if not isinstance(expected_scores_raw, dict) or not expected_scores_raw:
        raise ValueError(f"{source}: case {case_id!r} missing non-empty 'expected_scores'")
    expected_scores: dict[str, float] = {}
    for metric, value in expected_scores_raw.items():
        if metric not in SUPPORTED_METRICS:
            raise ValueError(
                f"{source}: case {case_id!r} has unsupported metric {metric!r} "
                f"(supported: {', '.join(SUPPORTED_METRICS)})"
            )
        expected_scores[metric] = float(value)

    return GoldenCase(
        id=case_id,
        question=question,
        expected_answer=str(raw.get("expected_answer") or ""),
        expected_scores=expected_scores,
    )


def compare_scores(
    actual_scores: dict[str, float],
    expected_scores: dict[str, float],
) -> bool:
    """A case passes only when every expected metric meets its threshold."""
    if not expected_scores:
        return False
    return all(
        actual_scores.get(metric, 0.0) >= threshold
        for metric, threshold in expected_scores.items()
    )


async def _call_judge_llm(client: Any, model: str, prompt: str) -> dict[str, Any]:
    """Call the judge LLM and parse its JSON response.

    Mirrors ``agent_obs.eval.llm_judge_worker._call_llm`` but takes the
    OpenAI-compatible client explicitly so tests can inject a mock. The
    client is expected to carry its own api_key/base_url (built by
    ``build_judge_client`` or injected by the caller).
    """
    response = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": "You are an expert evaluator. Return JSON."},
            {"role": "user", "content": prompt},
        ],
        response_format={"type": "json_object"},
        temperature=0.0,
    )
    content = response.choices[0].message.content or "{}"
    return json.loads(content)


def build_judge_client() -> Any:
    """Build the OpenAI-compatible judge client from LLM_JUDGE_* / OPENAI_* env."""
    from openai import AsyncOpenAI

    api_key = os.environ.get("LLM_JUDGE_API_KEY") or os.environ.get("OPENAI_API_KEY", "")
    api_base = os.environ.get("LLM_JUDGE_API_BASE", "https://api.openai.com/v1")
    return AsyncOpenAI(api_key=api_key or "missing-key", base_url=api_base)


class _DryRunJudgeClient:
    """Offline stand-in for the judge client: always scores 0.95."""

    def __init__(self) -> None:
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self._create)
        )

    @staticmethod
    async def _create(**kwargs: Any) -> Any:
        message = SimpleNamespace(
            content=json.dumps({"score": 0.95, "reasoning": "dry-run", "flags": []})
        )
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    def close(self) -> None:
        return None


def _build_dry_run_judge_client() -> Any:
    return _DryRunJudgeClient()


async def judge_case(
    case: GoldenCase,
    actual_answer: str,
    client: Any,
    model: str = DEFAULT_JUDGE_MODEL,
) -> dict[str, float]:
    """Score *actual_answer* with the LLM judge for the case's expected metrics.

    Reuses the RAGAS-style prompt templates from ``LLMJudgePrompts``. The
    case's ``expected_answer`` doubles as the faithfulness context — the
    golden eval has no retrieval context of its own.
    """
    from agent_obs.eval.llm_judge import LLMJudgePrompts

    prompts = LLMJudgePrompts()
    templates = {
        "faithfulness": prompts.faithfulness,
        "answer_relevancy": prompts.answer_relevancy,
        "completeness": prompts.completeness,
    }
    scores: dict[str, float] = {}
    for metric in case.expected_scores:
        template = templates[metric]
        prompt = template.format(
            question=case.question,
            answer=actual_answer,
            context=case.expected_answer or case.question,
        )
        try:
            parsed = await _call_judge_llm(client, model, prompt)
            scores[metric] = float(parsed.get("score", 0.0))
        except Exception as exc:  # noqa: BLE001 - one metric failing must not kill the run
            logger.warning(
                "judge call failed: case=%s metric=%s error=%s", case.id, metric, exc
            )
            scores[metric] = 0.0
    return scores


def build_agent_runner() -> Callable[[str], Awaitable[str]]:
    """Build the async ``question -> answer`` runner used for each case.

    Imports are deferred so unit tests (and ``--dry-run``) never need a live
    OpenAI client or price book on disk.
    """
    from openai import AsyncOpenAI

    from agent_obs import ObservabilitySDK
    from pilot_agent.agent import PilotAgent, PilotAgentConfig

    api_key = os.environ.get("OPENAI_API_KEY", "")
    agent_model = os.environ.get("AGENT_OBS_GOLDEN_AGENT_MODEL", DEFAULT_AGENT_MODEL)
    price_book_path = os.environ.get(
        "AGENT_OBS_GOLDEN_PRICE_BOOK", os.path.join(REPO_ROOT, "price_book.yaml")
    )

    sdk = ObservabilitySDK(exporters=[], enabled=False)
    client = AsyncOpenAI(api_key=api_key) if api_key else AsyncOpenAI()
    agent = PilotAgent(
        sdk=sdk,
        client=client,
        config=PilotAgentConfig(model=agent_model, price_book_path=price_book_path),
    )

    async def _run(question: str) -> str:
        return await agent.run(question)

    return _run


async def run_nightly_golden_eval(
    golden_dir: str | Path | None = None,
    run_agent: Callable[[str], Awaitable[str]] | None = None,
    judge_client: Any | None = None,
    judge_model: str | None = None,
    hot_store: Any | None = None,
    dry_run: bool = False,
    nightly_ts: datetime | None = None,
) -> NightlyResult:
    """Run the full golden set and persist/export the outcome.

    ``run_agent`` and ``judge_client`` are injectable for tests; by default
    the pilot agent and an OpenAI-compatible judge client are constructed.
    Storage failures are recorded in ``result.errors`` and re-raised only
    after the pass-rate gauge is published, so a ClickHouse outage cannot
    blind the regression alert.
    """
    from agent_obs.metrics import eval_golden_pass_rate

    golden_dir = Path(golden_dir or DEFAULT_GOLDEN_DIR)
    judge_model = judge_model or os.environ.get(
        "AGENT_OBS_GOLDEN_JUDGE_MODEL", DEFAULT_JUDGE_MODEL
    )
    nightly_ts = nightly_ts or datetime.now(UTC)

    cases = load_golden_set(golden_dir)
    logger.info("nightly golden eval: %d cases from %s", len(cases), golden_dir)

    if run_agent is None:
        if dry_run:
            async def _stub_agent(question: str) -> str:
                return f"dry-run answer for: {question}"

            run_agent = _stub_agent
        else:
            run_agent = build_agent_runner()

    owns_judge_client = judge_client is None
    if judge_client is None:
        if dry_run:
            judge_client = _build_dry_run_judge_client()
        else:
            judge_client = build_judge_client()

    result = NightlyResult(nightly_ts=nightly_ts)
    try:
        for case in cases:
            try:
                actual_answer = await run_agent(case.question)
                actual_scores = await judge_case(
                    case, actual_answer, judge_client, model=judge_model
                )
            except Exception as exc:  # keep judging remaining cases
                logger.exception("case failed: %s", case.id)
                result.errors.append(f"{case.id}: {exc}")
                actual_scores = {metric: 0.0 for metric in case.expected_scores}

            case_result = CaseResult(
                question_id=case.id,
                expected_scores=case.expected_scores,
                actual_scores=actual_scores,
                passed=compare_scores(actual_scores, case.expected_scores),
            )
            result.case_results.append(case_result)
            result.total += 1
            if case_result.passed:
                result.passed += 1
            logger.info(
                "case %s: %s (scores=%s)",
                case.id,
                "pass" if case_result.passed else "fail",
                actual_scores,
            )
    finally:
        if owns_judge_client and hasattr(judge_client, "close"):
            close_result = judge_client.close()
            if asyncio.iscoroutine(close_result):
                await close_result

    eval_golden_pass_rate.set(result.pass_rate)
    logger.info(
        "nightly golden eval done: pass_rate=%.3f (%d/%d), errors=%d",
        result.pass_rate,
        result.passed,
        result.total,
        len(result.errors),
    )

    if dry_run:
        logger.info("dry-run: skipping eval_golden_results write")
        return result

    rows = result.storage_rows()
    if hot_store is None:
        from agent_obs.storage.hot import HotStore

        hot_store = HotStore()
    try:
        written = hot_store.write_eval_golden_results(rows)
        logger.info("eval_golden_results rows written: %d", written)
    except Exception:
        logger.exception("failed to persist eval_golden_results")
        result.errors.append("eval_golden_results write failed")
        if hasattr(hot_store, "close"):
            hot_store.close()
        raise
    if hasattr(hot_store, "close"):
        hot_store.close()
    return result


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the PP13 nightly golden eval batch."
    )
    parser.add_argument(
        "--golden-dir",
        default=os.environ.get("AGENT_OBS_GOLDEN_DIR", DEFAULT_GOLDEN_DIR),
        help="Directory containing golden-set *.yaml files",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Evaluate with stubbed agent answers; skip ClickHouse writes",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    start = time.time()
    result = asyncio.run(
        run_nightly_golden_eval(golden_dir=args.golden_dir, dry_run=args.dry_run)
    )
    logger.info("finished in %.1fs: pass_rate=%.3f", time.time() - start, result.pass_rate)
    return 1 if result.errors else 0


if __name__ == "__main__":
    sys.exit(main())
