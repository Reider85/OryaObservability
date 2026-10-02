"""Async evaluation pipeline for agent observability.

Provides rule-based, LLM-judge, and embedding evaluators that assess
answer quality and attach results to traces via late annotation.
"""

from agent_obs.eval.api import app as eval_api_app
from agent_obs.eval.base import BaseEvaluator, EvalResult
from agent_obs.eval.embedding import EmbeddingEvaluator, GoldenStore
from agent_obs.eval.llm_judge import LLMJudgeEvaluator
from agent_obs.eval.late_annotation import annotate, annotate_with_fallback, get_eval_results
from agent_obs.eval.rule_based import (
    Rule,
    RuleBasedEvaluator,
    load_eval_rules,
    load_rule_based_evaluator,
)

__all__ = [
    "BaseEvaluator",
    "EvalResult",
    "Rule",
    "RuleBasedEvaluator",
    "LLMJudgeEvaluator",
    "EmbeddingEvaluator",
    "GoldenStore",
    "annotate",
    "annotate_with_fallback",
    "get_eval_results",
    "load_eval_rules",
    "load_rule_based_evaluator",
    "eval_api_app",
]