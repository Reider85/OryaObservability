"""Async evaluation pipeline for agent observability.

Provides rule-based, LLM-judge, and embedding evaluators that assess
answer quality and attach results to traces via late annotation.

Submodules are loaded lazily (PEP 562): importing ``agent_obs.eval.base``
(e.g. from ``agent_obs.exporters.tail_sampler``) must not pull the FastAPI
eval API or the ML/LLM evaluator stack — those land in the ``[api]``/``[ml]``
extras (A-16 layering, same reasoning as scripts/sampler/requirements.txt).
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from agent_obs.eval.adaptive_sampler import AdaptiveEvalSampler, Trace
    from agent_obs.eval.api import app as eval_api_app
    from agent_obs.eval.base import BaseEvaluator, EvalResult
    from agent_obs.eval.embedding import EmbeddingEvaluator, GoldenStore
    from agent_obs.eval.llm_judge import LLMJudgeEvaluator
    from agent_obs.eval.late_annotation import (
        annotate,
        annotate_with_fallback,
        get_eval_results,
    )
    from agent_obs.eval.rule_based import (
        Rule,
        RuleBasedEvaluator,
        load_eval_rules,
        load_rule_based_evaluator,
    )

_LAZY_ATTRS = {
    "AdaptiveEvalSampler": ("agent_obs.eval.adaptive_sampler", "AdaptiveEvalSampler"),
    "Trace": ("agent_obs.eval.adaptive_sampler", "Trace"),
    "eval_api_app": ("agent_obs.eval.api", "app"),
    "BaseEvaluator": ("agent_obs.eval.base", "BaseEvaluator"),
    "EvalResult": ("agent_obs.eval.base", "EvalResult"),
    "EmbeddingEvaluator": ("agent_obs.eval.embedding", "EmbeddingEvaluator"),
    "GoldenStore": ("agent_obs.eval.embedding", "GoldenStore"),
    "LLMJudgeEvaluator": ("agent_obs.eval.llm_judge", "LLMJudgeEvaluator"),
    "annotate": ("agent_obs.eval.late_annotation", "annotate"),
    "annotate_with_fallback": ("agent_obs.eval.late_annotation", "annotate_with_fallback"),
    "get_eval_results": ("agent_obs.eval.late_annotation", "get_eval_results"),
    "Rule": ("agent_obs.eval.rule_based", "Rule"),
    "RuleBasedEvaluator": ("agent_obs.eval.rule_based", "RuleBasedEvaluator"),
    "load_eval_rules": ("agent_obs.eval.rule_based", "load_eval_rules"),
    "load_rule_based_evaluator": ("agent_obs.eval.rule_based", "load_rule_based_evaluator"),
}

__all__ = [
    "BaseEvaluator",
    "EvalResult",
    "Rule",
    "RuleBasedEvaluator",
    "LLMJudgeEvaluator",
    "EmbeddingEvaluator",
    "GoldenStore",
    "AdaptiveEvalSampler",
    "Trace",
    "annotate",
    "annotate_with_fallback",
    "get_eval_results",
    "load_eval_rules",
    "load_rule_based_evaluator",
    "eval_api_app",
]


def __getattr__(name: str):
    """Load eval submodule attributes lazily (PEP 562)."""
    target = _LAZY_ATTRS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attr = target
    return getattr(importlib.import_module(module_name), attr)


def __dir__() -> list[str]:  # pragma: no cover - introspection helper
    return sorted(set(__all__) | set(_LAZY_ATTRS))
